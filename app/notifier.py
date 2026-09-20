# app/notifier.py
"""Draft creation, delivery, and the approve/reject decision path.

Nothing reaches a patient until a human operator approves it here.
"""
import logging

from . import llm_service
from .config import settings
from .database import db
from .queue_engine import now_iso, predict

log = logging.getLogger("notifier")


# --------------------------------------------------------------------------- #
# context builders
# --------------------------------------------------------------------------- #
def _build_ctx(appt, pred: dict, kind: str) -> dict:
    return {
        "hospital_name": settings.HOSPITAL_NAME,
        "patient_name": appt["patient_name"],
        "token": appt["token"],
        "department": appt["department"],
        "doctor": appt["doctor"],
        "wait_minutes": pred["wait_minutes"],
        "position": pred["position"],
        "patients_ahead": pred["patients_ahead"],
        "lead_minutes": settings.NOTIFY_LEAD_MINUTES,
        "kind": kind,
    }


# --------------------------------------------------------------------------- #
# draft creation
# --------------------------------------------------------------------------- #
async def create_draft(appointment_id: int, channel: str = "sms", kind: str = "arrival"):
    """Generate an AI draft and park it in the operator's approval queue."""
    with db() as conn:
        appt = conn.execute(
            "SELECT * FROM appointments WHERE id=?", (appointment_id,)
        ).fetchone()
        if not appt or appt["status"] in ("completed", "no_show", "cancelled"):
            return None
        pred = predict(conn, appt)
        ctx = _build_ctx(appt, pred, kind)

    result = await llm_service.generate_notification(ctx, kind=kind)

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO notifications
                   (appointment_id, channel, draft, source, status, created_at)
               VALUES (?,?,?,?,'pending',?)""",
            (appointment_id, channel, result["text"], result["source"], now_iso()),
        )
        nid = cur.lastrowid
        _log(conn, "draft",
             f"Draft #{nid} ({channel}) ready for {appt['token']} · {appt['patient_name']} "
             f"[{result['source']}]")
    return nid


def create_draft_sync(appointment_id: int, channel: str = "sms", kind: str = "arrival"):
    """Used by the simulator, which runs outside the event loop."""
    with db() as conn:
        appt = conn.execute(
            "SELECT * FROM appointments WHERE id=?", (appointment_id,)
        ).fetchone()
        if not appt:
            return None
        pred = predict(conn, appt)
        ctx = _build_ctx(appt, pred, kind)
        text = llm_service._voice_fallback(ctx) if kind == "voice" else llm_service._fallback(ctx)
        cur = conn.execute(
            """INSERT INTO notifications
                   (appointment_id, channel, draft, source, status, created_at)
               VALUES (?,?,?,'template','pending',?)""",
            (appointment_id, channel, text, now_iso()),
        )
        nid = cur.lastrowid
        _log(conn, "draft", f"Draft #{nid} ({channel}) ready for {appt['token']} [template]")
    return nid


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
def send_sms(phone: str, message: str):
    if settings.SMS_PROVIDER == "twilio" and settings.TWILIO_SID:
        try:
            from twilio.rest import Client  # type: ignore

            client = Client(settings.TWILIO_SID, settings.TWILIO_TOKEN)
            client.messages.create(to=phone, from_=settings.TWILIO_FROM, body=message)
            return True, "twilio"
        except Exception as exc:  # noqa: BLE001
            log.warning("Twilio SMS failed: %s", exc)
    log.info("[SMS -> %s] %s", phone, message)
    return True, "console"


def place_voice_call(phone: str, script: str):
    if settings.SMS_PROVIDER == "twilio" and settings.TWILIO_SID:
        try:
            from twilio.rest import Client  # type: ignore

            client = Client(settings.TWILIO_SID, settings.TWILIO_TOKEN)
            twiml = f"<Response><Say voice='alice'>{script}</Say></Response>"
            client.calls.create(to=phone, from_=settings.TWILIO_FROM, twiml=twiml)
            return True, "twilio"
        except Exception as exc:  # noqa: BLE001
            log.warning("Twilio voice call failed: %s", exc)
    log.info("[VOICE CALL -> %s] %s", phone, script)
    return True, "console"


# --------------------------------------------------------------------------- #
# operator decisions
# --------------------------------------------------------------------------- #
def decide(notification_id: int, approve: bool, final_text=None, channel=None, reason=None):
    """Apply the operator's decision. Returns a result dict for the API layer."""
    with db() as conn:
        notif = conn.execute(
            "SELECT * FROM notifications WHERE id=?", (notification_id,)
        ).fetchone()
        if not notif:
            return {"ok": False, "error": "notification not found"}
        if notif["status"] != "pending":
            return {"ok": False, "error": f"already {notif['status']}"}

        appt = conn.execute(
            "SELECT * FROM appointments WHERE id=?", (notif["appointment_id"],)
        ).fetchone()
        if not appt:
            return {"ok": False, "error": "appointment not found"}

        ch = (channel or notif["channel"] or "sms").lower()
        text = (final_text or notif["draft"]).strip()
        edited = bool(final_text and final_text.strip() != notif["draft"].strip())

        if not approve:
            conn.execute(
                "UPDATE notifications SET status='rejected', decided_at=? WHERE id=?",
                (now_iso(), notification_id),
            )
            _log(conn, "reject",
                 f"Draft #{notification_id} rejected for {appt['token']}"
                 + (f" ({reason})" if reason else ""))
            return {"ok": True, "status": "rejected"}

        conn.execute(
            """UPDATE notifications
               SET status='approved', final_text=?, channel=?, decided_at=?
               WHERE id=?""",
            (text, ch, now_iso(), notification_id),
        )

    # ---- delivery happens outside the transaction ----
    if ch == "voice":
        ok, provider = place_voice_call(appt["phone"], text)
    else:
        ok, provider = send_sms(appt["phone"], text)

    with db() as conn:
        if ch == "voice":
            conn.execute(
                "UPDATE appointments SET voice_called=1 WHERE id=?", (appt["id"],)
            )
        else:
            conn.execute(
                "UPDATE appointments SET notified_at=? WHERE id=?",
                (now_iso(), appt["id"]),
            )
        _log(conn, "sent",
             f"{ch.upper()} {'(edited) ' if edited else ''}sent to {appt['patient_name']} "
             f"({appt['phone']}) via {provider}")

    return {"ok": True, "status": "approved", "channel": ch, "provider": provider}


# --------------------------------------------------------------------------- #
def _log(conn, level: str, message: str) -> None:
    conn.execute(
        "INSERT INTO event_log (level, message, created_at) VALUES (?,?,?)",
        (level, message, now_iso()),
    )