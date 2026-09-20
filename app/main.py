

# app/main.py
"""FastAPI application: routes, websocket hub, and the background simulator loop."""
import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import notifier, simulator
from .config import settings
from .database import db, init_db
from .models import ApproveRequest, BookingRequest, RejectRequest
from .queue_engine import ACTIVE_STATUSES, now_iso, predict, recalculate_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


# --------------------------------------------------------------------------- #
# websocket hub
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()


# --------------------------------------------------------------------------- #
# FIX 4: run build_state off the event loop — it does blocking SQLite I/O
# --------------------------------------------------------------------------- #
async def build_state_async() -> dict:
    return await asyncio.to_thread(build_state)


async def push_state():
    await hub.broadcast({"type": "state", "data": await build_state_async()})


# --------------------------------------------------------------------------- #
# state snapshot  (synchronous — always call via asyncio.to_thread)
# --------------------------------------------------------------------------- #
def build_state() -> dict:
    with db() as conn:
        doctors = []
        for d in conn.execute("SELECT * FROM doctors ORDER BY department, name"):
            load = conn.execute(
                """SELECT COUNT(*) c FROM appointments
                   WHERE doctor=? AND department=? AND status IN ('booked','checked_in','in_consult')""",
                (d["name"], d["department"]),
            ).fetchone()["c"]
            doctors.append({**dict(d), "queue_length": load})

        appointments = []
        for r in conn.execute("SELECT * FROM appointments ORDER BY id DESC LIMIT 120"):
            row = dict(r)
            if row["status"] in ACTIVE_STATUSES:
                p = predict(conn, r)
                row["wait_minutes"] = p["wait_minutes"]
                row["position"] = p["position"]
                row["predicted_start"] = p["predicted_start"]
            appointments.append(row)

        pending = [dict(r) for r in conn.execute(
            """SELECT n.id, n.appointment_id, n.channel, n.draft, n.source, n.created_at,
                      a.token, a.patient_name, a.phone, a.department, a.doctor,
                      a.status AS appt_status
               FROM notifications n
               JOIN appointments a ON a.id = n.appointment_id
               WHERE n.status = 'pending'
               ORDER BY n.id DESC"""
        )]

        recent = [dict(r) for r in conn.execute(
            """SELECT n.id, n.channel, n.status, n.final_text, n.decided_at,
                      a.token, a.patient_name
               FROM notifications n
               JOIN appointments a ON a.id = n.appointment_id
               WHERE n.status != 'pending'
               ORDER BY n.id DESC LIMIT 8"""
        )]

        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT 80"
        )]

        counts = {
            s: conn.execute(
                "SELECT COUNT(*) c FROM appointments WHERE status=?", (s,)
            ).fetchone()["c"]
            for s in ("booked", "checked_in", "in_consult", "completed", "no_show")
        }
        total = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]

    return {
        "hospital": settings.HOSPITAL_NAME,
        "llm_enabled": bool(settings.LLM_API_KEY),
        "llm_model": settings.LLM_MODEL if settings.LLM_API_KEY else None,
        "lead_minutes": settings.NOTIFY_LEAD_MINUTES,
        "auto_simulate": settings.AUTO_SIMULATE,
        "doctors": doctors,
        "appointments": appointments,
        "pending": pending,
        "recent": recent,
        "logs": logs,
        "stats": {
            "total": total,
            **counts,
            "no_show_rate": round(counts["no_show"] / total * 100, 1) if total else 0.0,
        },
    }


# --------------------------------------------------------------------------- #
# background simulator
# --------------------------------------------------------------------------- #
async def _sim_loop():
    log.info("Simulator started (every %ss)", settings.SIMULATE_INTERVAL)
    while True:
        try:
            await asyncio.sleep(settings.SIMULATE_INTERVAL)
            # FIX 2: run the (synchronous, SQLite-heavy) tick in a worker thread
            result = await asyncio.to_thread(simulator.tick)
            if result["events"]:
                log.info("tick: %s event(s)", len(result["events"]))
            await push_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("simulator tick failed: %s", exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # FIX 5: keep blocking init work off the event loop
    await asyncio.to_thread(init_db)
    try:
        await asyncio.to_thread(simulator.seed)
    except Exception as exc:  # noqa: BLE001
        log.warning("seed failed at startup (continuing): %s", exc)

    task = None
    if settings.AUTO_SIMULATE:
        task = asyncio.create_task(_sim_loop())
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Smart Hospital Queue Bot", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    # FIX 4 (cont.): don't block the loop
    return await build_state_async()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        await ws.send_json({"type": "state", "data": await build_state_async()})
        while True:
            await ws.receive_text()  # keepalive pings from the client
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:  # noqa: BLE001
        hub.disconnect(ws)


# --------------------------------------------------------------------------- #
# bookings
# --------------------------------------------------------------------------- #
@app.post("/api/appointments")
async def create_appointment(req: BookingRequest):
    with db() as conn:
        doc = conn.execute(
            "SELECT * FROM doctors WHERE name=? AND department=?",
            (req.doctor, req.department),
        ).fetchone()
        if not doc:
            conn.execute(
                """INSERT INTO doctors (name, department, avg_consult_minutes, efficiency)
                   VALUES (?,?,?,1.0)""",
                (req.doctor, req.department, settings.DEFAULT_CONSULT_MINUTES),
            )

        seq = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"] + 1

        # FIX 6: strip punctuation/spaces before building the token prefix
        dept_code = "".join(ch for ch in req.department if ch.isalnum())[:3].upper() or "GEN"
        token = f"{dept_code}-{seq:03d}"

        cur = conn.execute(
            """INSERT INTO appointments
                   (token, patient_name, phone, department, doctor, status, booked_at)
               VALUES (?,?,?,?,?,'booked',?)""",
            (token, req.patient_name, req.phone, req.department, req.doctor, now_iso()),
        )
        appt_id = cur.lastrowid
        recalculate_all(conn)
        conn.execute(
            "INSERT INTO event_log (level, message, created_at) VALUES ('booking',?,?)",
            (f"{token} · {req.patient_name} booked with {req.doctor} ({req.department})",
             now_iso()),
        )

    # FIX 7: cap the draft call so a slow LLM can't stall the booking response.
    # notifier.create_draft already falls back to a template on any exception.
    try:
        notification_id = await asyncio.wait_for(
            notifier.create_draft(appt_id, channel="sms", kind="arrival"),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        log.warning("draft generation timed out for appt %s — booking still saved", appt_id)
        notification_id = None

    await push_state()
    return {"ok": True, "appointment_id": appt_id, "token": token,
            "notification_id": notification_id}


# --------------------------------------------------------------------------- #
# appointment lifecycle
# --------------------------------------------------------------------------- #
def _transition(appt_id: int, new_status: str, allowed: tuple[str, ...],
                extra_sql: str = "", extra_params: tuple = ()) -> dict:
    with db() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not appt:
            raise HTTPException(404, "appointment not found")
        if appt["status"] not in allowed:
            raise HTTPException(
                400,
                "cannot move from '{}' to '{}'".format(appt["status"], new_status),
            )

        if new_status == "in_consult" and appt["predicted_start"]:
            from .queue_engine import learn_from_arrival, to_dt
            booked = to_dt(appt["booked_at"])
            if booked:
                actual_wait = (to_dt(now_iso()) - booked).total_seconds() / 60
                pred_wait = (to_dt(appt["predicted_start"]) - booked).total_seconds() / 60
                learn_from_arrival(conn, appt["doctor"], appt["department"],
                                   pred_wait, actual_wait)

        conn.execute(
            f"UPDATE appointments SET status=? {extra_sql} WHERE id=?",
            (new_status, *extra_params, appt_id),
        )
        conn.execute(
            "INSERT INTO event_log (level, message, created_at) VALUES (?,?,?)",
            (new_status, f"{appt['token']} · {appt['patient_name']} → {new_status}",
             now_iso()),
        )
        recalculate_all(conn)

    return {"ok": True, "status": new_status}


@app.post("/api/appointments/{appt_id}/checkin")
async def checkin(appt_id: int):
    res = await asyncio.to_thread(_transition, appt_id, "checked_in", ("booked",))
    await push_state()
    return res


@app.post("/api/appointments/{appt_id}/start")
async def start_consult(appt_id: int):
    res = await asyncio.to_thread(
        _transition,
        appt_id, "in_consult", ("booked", "checked_in"),
        ", actual_start=?", (now_iso(),),
    )
    await push_state()
    return res


@app.post("/api/appointments/{appt_id}/complete")
async def complete(appt_id: int):
    res = await asyncio.to_thread(
        _transition,
        appt_id, "completed", ("in_consult", "checked_in"),
        ", completed_at=?", (now_iso(),),
    )
    await push_state()
    return res


@app.post("/api/appointments/{appt_id}/noshow")
async def mark_no_show(appt_id: int):
    res = await asyncio.to_thread(
        _transition, appt_id, "no_show", ("booked", "checked_in")
    )
    with db() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if appt and not appt["voice_called"]:
        try:
            await asyncio.wait_for(
                notifier.create_draft(appt_id, channel="voice", kind="voice"),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            log.warning("voice draft timed out for appt %s", appt_id)
    await push_state()
    return res


# --------------------------------------------------------------------------- #
# notification approvals  (the human-in-the-loop gate)
# --------------------------------------------------------------------------- #
@app.post("/api/notifications/{nid}/approve")
async def approve_notification(nid: int, req: ApproveRequest):
    # FIX 1 + FIX 3: run blocking decide() in a thread AND use `or` fallback
    result = await asyncio.to_thread(
        notifier.decide,
        nid, True, req.final_text, req.channel, None,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not approve")
    await push_state()
    return result


@app.post("/api/notifications/{nid}/reject")
async def reject_notification(nid: int, req: RejectRequest):
    result = await asyncio.to_thread(
        notifier.decide,
        nid, False, None, None, req.reason,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not reject")
    await push_state()
    return result


# --------------------------------------------------------------------------- #
# simulator controls
# --------------------------------------------------------------------------- #
@app.post("/api/tick")
async def manual_tick():
    result = await asyncio.to_thread(simulator.tick)
    await push_state()
    return {"ok": True, **result}


@app.post("/api/seed")
async def reseed():
    result = await asyncio.to_thread(simulator.seed, True)
    await push_state()
    return result