# app/database.py
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    department           TEXT NOT NULL,
    avg_consult_minutes  REAL NOT NULL DEFAULT 12,
    efficiency           REAL NOT NULL DEFAULT 1.0,
    status               TEXT NOT NULL DEFAULT 'available',
    UNIQUE(name, department)
);

CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL UNIQUE,
    patient_name    TEXT NOT NULL,
    phone           TEXT NOT NULL,
    department      TEXT NOT NULL,
    doctor          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'booked',
    booked_at       TEXT NOT NULL,
    predicted_start TEXT,
    actual_start    TEXT,
    completed_at    TEXT,
    notified_at     TEXT,
    voice_called    INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL,
    channel        TEXT NOT NULL DEFAULT 'sms',
    draft          TEXT NOT NULL,
    final_text     TEXT,
    source         TEXT NOT NULL DEFAULT 'template',
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    FOREIGN KEY(appointment_id) REFERENCES appointments(id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appt_doc   ON appointments(doctor, department, status);
CREATE INDEX IF NOT EXISTS idx_notif_stat ON notifications(status);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)