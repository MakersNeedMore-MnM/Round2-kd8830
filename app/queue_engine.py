# app/queue_engine.py
"""Wait-time prediction + learning loop.

The engine is deliberately explainable: an appointment's wait is the sum of
the *remaining* consult time of whoever is currently inside, plus the average
consult time of every patient still ahead in the queue, scaled by a per-doctor
efficiency factor that the system learns from observed reality.
"""
from datetime import datetime, timedelta, timezone

ACTIVE_STATUSES = ("booked", "checked_in", "in_consult")


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_utc().isoformat()


def to_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# core prediction
# --------------------------------------------------------------------------- #
def get_doctor(conn, name, department):
    return conn.execute(
        "SELECT * FROM doctors WHERE name=? AND department=?", (name, department)
    ).fetchone()


def queue_ahead(conn, appt):
    """Everyone with a lower id still in the pipeline for the same doctor."""
    return conn.execute(
        """SELECT * FROM appointments
           WHERE doctor=? AND department=?
             AND status IN ('booked','checked_in','in_consult')
             AND id < ?
           ORDER BY id""",
        (appt["doctor"], appt["department"], appt["id"]),
    ).fetchall()


def predict(conn, appt) -> dict:
    doc = get_doctor(conn, appt["doctor"], appt["department"])
    avg = float(doc["avg_consult_minutes"]) if doc else 12.0
    eff = float(doc["efficiency"]) if doc else 1.0

    ahead = queue_ahead(conn, appt)
    in_consult = [r for r in ahead if r["status"] == "in_consult"]
    waiting = [r for r in ahead if r["status"] in ("booked", "checked_in")]

    minutes = 0.0
    for row in in_consult:
        started = to_dt(row["actual_start"])
        elapsed = (now_utc() - started).total_seconds() / 60 if started else 0.0
        minutes += max(1.0, avg - elapsed)

    minutes += len(waiting) * avg * eff
    minutes = max(0, int(round(minutes)))

    return {
        "wait_minutes": minutes,
        "patients_ahead": len(ahead),
        "position": len(ahead) + 1,
        "predicted_start": (now_utc() + timedelta(minutes=minutes)).isoformat(),
        "doctor_avg": round(avg, 1),
        "efficiency": round(eff, 3),
    }


def recalculate_all(conn) -> None:
    rows = conn.execute(
        "SELECT * FROM appointments WHERE status IN ('booked','checked_in','in_consult') ORDER BY id"
    ).fetchall()
    for row in rows:
        p = predict(conn, row)
        conn.execute(
            "UPDATE appointments SET predicted_start=? WHERE id=?",
            (p["predicted_start"], row["id"]),
        )


# --------------------------------------------------------------------------- #
# learning loop
# --------------------------------------------------------------------------- #
def learn_from_consult(conn, doctor: str, department: str, observed_minutes: float) -> None:
    """Blend the observed consult duration into the doctor's rolling average."""
    doc = get_doctor(conn, doctor, department)
    if not doc:
        return
    old = float(doc["avg_consult_minutes"])
    blended = 0.8 * old + 0.2 * observed_minutes
    new_avg = max(3.0, min(60.0, blended))
    conn.execute(
        "UPDATE doctors SET avg_consult_minutes=? WHERE id=?",
        (round(new_avg, 1), doc["id"]),
    )


def learn_from_arrival(conn, doctor: str, department: str,
                       predicted_wait: float, actual_wait: float) -> None:
    """If we systematically under/over-predict, nudge the efficiency factor."""
    doc = get_doctor(conn, doctor, department)
    if not doc or predicted_wait <= 0:
        return
    ratio = max(0.6, min(1.8, actual_wait / predicted_wait))
    eff = float(doc["efficiency"])
    new_eff = max(0.5, min(2.0, eff * 0.85 + eff * ratio * 0.15))
    conn.execute("UPDATE doctors SET efficiency=? WHERE id=?", (round(new_eff, 3), doc["id"]))