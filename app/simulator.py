# app/simulator.py
"""Hospital live-data simulator."""
import logging
import random

from .database import db
from .notifier import _log, create_draft_sync
from .queue_engine import (learn_from_consult, now_iso, now_utc, recalculate_all,
                           to_dt)

log = logging.getLogger("simulator")

NO_SHOW_GRACE_MINUTES = 2
NO_SHOW_PROBABILITY = 0.20

DEMO_DOCTORS = [
    ("Dr. Meera Nair",   "Cardiology",        14),
    ("Dr. Arjun Rao",    "Cardiology",        10),
    ("Dr. Kavya Iyer",   "Orthopedics",       16),
    ("Dr. Rohan Gupta",  "General Medicine",   8),
    ("Dr. Sneha Patel",  "Pediatrics",        12),
]

DEMO_PATIENTS = [
    ("Aarav Sharma", "+919810000001"),
    ("Diya Menon", "+919810000002"),
    ("Vihaan Kapoor", "+919810000003"),
    ("Ananya Reddy", "+919810000004"),
    ("Ishaan Bose", "+919810000005"),
    ("Myra Joshi", "+919810000006"),
    ("Kabir Malhotra", "+919810000007"),
    ("Saanvi Desai", "+919810000008"),
]


def seed(force: bool = False) -> dict:
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM doctors").fetchone()["c"]
        if existing and not force:
            return {"ok": True, "seeded": False, "reason": "already has data"}

        for name, dept, avg in DEMO_DOCTORS:
            conn.execute(
                """INSERT OR IGNORE INTO doctors
                       (name, department, avg_consult_minutes, efficiency)
                   VALUES (?,?,?,1.0)""",
                (name, dept, avg),
            )

        created = 0
        for i, (pname, phone) in enumerate(DEMO_PATIENTS):
            dept, doctor, avg = random.choice(DEMO_DOCTORS)
            seq = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"] + 1
            token = f"{dept[:3].upper()}-{seq:03d}"
            status = "in_consult" if i == 0 else ("checked_in" if i < 3 else "booked")
            conn.execute(
                """INSERT INTO appointments
                       (token, patient_name, phone, department, doctor, status,
                        booked_at, actual_start)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (token, pname, phone, dept, doctor, status, now_iso(),
                 now_iso() if status == "in_consult" else None),
            )
            created += 1

        recalculate_all(conn)
        _log(conn, "seed",
             f"Demo data loaded: {len(DEMO_DOCTORS)} doctors, {created} appointments")

    return {"ok": True, "seeded": True, "appointments": created}


def tick() -> dict:
    events: list[dict] = []
    voice_drafts: list[int] = []

    with db() as conn:
        doctors = conn.execute("SELECT * FROM doctors ORDER BY id").fetchall()

        for doc in doctors:
            inside = conn.execute(
                """SELECT * FROM appointments
                   WHERE doctor=? AND department=? AND status='in_consult'
                   LIMIT 1""",
                (doc["name"], doc["department"]),
            ).fetchone()

            if inside and random.random() < 0.40:
                observed = max(3.0, random.gauss(float(doc["avg_consult_minutes"]), 3.0))
                conn.execute(
                    "UPDATE appointments SET status='completed', completed_at=? WHERE id=?",
                    (now_iso(), inside["id"]),
                )
                learn_from_consult(conn, doc["name"], doc["department"], observed)
                events.append({
                    "level": "done",
                    "message": f"{inside['token']} · {inside['patient_name']} consult finished "
                               f"({observed:.0f} min) — model updated",
                })
                _log(conn, "done", events[-1]["message"])
                inside = None

            if inside is None:
                nxt = conn.execute(
                    """SELECT * FROM appointments
                       WHERE doctor=? AND department=? AND status='checked_in'
                       ORDER BY id LIMIT 1""",
                    (doc["name"], doc["department"]),
                ).fetchone()

                if nxt is None:
                    candidate = conn.execute(
                        """SELECT * FROM appointments
                           WHERE doctor=? AND department=? AND status='booked'
                           ORDER BY id LIMIT 1""",
                        (doc["name"], doc["department"]),
                    ).fetchone()
                    if candidate and random.random() < 0.45:
                        conn.execute(
                            "UPDATE appointments SET status='checked_in' WHERE id=?",
                            (candidate["id"],),
                        )
                        events.append({
                            "level": "checkin",
                            "message": f"{candidate['token']} · {candidate['patient_name']} checked in",
                        })
                        _log(conn, "checkin", events[-1]["message"])
                        nxt = candidate

                if nxt:
                    conn.execute(
                        "UPDATE appointments SET status='in_consult', actual_start=? WHERE id=?",
                        (now_iso(), nxt["id"]),
                    )
                    events.append({
                        "level": "consult",
                        "message": f"{nxt['token']} · {nxt['patient_name']} now with {doc['name']}",
                    })
                    _log(conn, "consult", events[-1]["message"])

        notified = conn.execute(
            """SELECT * FROM appointments
               WHERE status IN ('booked','checked_in')
                 AND notified_at IS NOT NULL
                 AND voice_called = 0"""
        ).fetchall()

        for row in notified:
            sent_at = to_dt(row["notified_at"])
            if not sent_at:
                continue
            waited = (now_utc() - sent_at).total_seconds() / 60
            if waited >= NO_SHOW_GRACE_MINUTES and random.random() < NO_SHOW_PROBABILITY:
                conn.execute(
                    "UPDATE appointments SET status='no_show' WHERE id=?", (row["id"],)
                )
                events.append({
                    "level": "noshow",
                    "message": f"{row['token']} · {row['patient_name']} did not show — "
                               f"voice call draft created",
                })
                _log(conn, "noshow", events[-1]["message"])
                voice_drafts.append(row["id"])

        recalculate_all(conn)

    for appt_id in voice_drafts:
        create_draft_sync(appt_id, channel="voice", kind="voice")

    return {"events": events, "voice_drafts": voice_drafts}