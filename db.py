"""
db.py — SQLite persistence layer.

Six tables matching the SRS §9.1 data model.
Plain functions; no ORM; no repository abstraction.
All patient/session queries require an explicit patient_id or doctor_id filter —
there is no "fetch by session_id alone" function, preventing authorization bypasses (SR-03/SR-04).
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
import time
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


# ── Connection ──────────────────────────────────────────────────────────────

def _get_connection(db_path: str = config.DB_PATH) -> sqlite3.Connection:
    """
    Open a SQLite connection in WAL mode.
    Each call returns a new connection — callers own their connection lifetime.
    (Prevents sharing connections across threads, which sqlite3 forbids by default.)
    """
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema creation ─────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('patient','doctor')),
    created_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL UNIQUE REFERENCES users(id),
    doctor_id  INTEGER          REFERENCES users(id),
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    exercise     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','paused','finalizing','saved','aborted')),
    rep_baseline INTEGER NOT NULL DEFAULT 0,
    started_at   REAL    NOT NULL,
    ended_at     REAL,
    duration_sec INTEGER
);

CREATE TABLE IF NOT EXISTS telemetry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id),
    ts             REAL    NOT NULL,
    angle          REAL    NOT NULL,
    fsr            INTEGER,
    motor          INTEGER,
    rep            INTEGER,
    sequence       INTEGER,          -- future SEQ= field
    device_ts      REAL              -- future TIME= field
);
CREATE INDEX IF NOT EXISTS idx_telemetry_session_ts ON telemetry(session_id, ts);

CREATE TABLE IF NOT EXISTS repetitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES sessions(id),
    rep_index         INTEGER NOT NULL,
    started_at        REAL    NOT NULL,
    ended_at          REAL    NOT NULL,
    duration_sec      REAL    NOT NULL,
    peak_rom          REAL,           -- P1
    peak_velocity     REAL,           -- P1
    fsr_peak          INTEGER,        -- P1
    completion_status TEXT            -- P1: 'completed' | 'incomplete'
);
CREATE INDEX IF NOT EXISTS idx_repetitions_session ON repetitions(session_id, rep_index);

CREATE TABLE IF NOT EXISTS session_metrics (
    session_id    INTEGER PRIMARY KEY REFERENCES sessions(id),
    min_rom       REAL,
    max_rom       REAL,
    avg_rom       REAL,
    repetitions   INTEGER,
    fsr_peak      INTEGER,
    motor_on_count INTEGER,
    consistency   REAL,              -- P1 (ROM SD across reps)
    smoothness    REAL,              -- P2 (jerk-based, unvalidated)
    config_version TEXT NOT NULL DEFAULT 'v1'
);
"""


def init_db(db_path: str = config.DB_PATH) -> sqlite3.Connection:
    """Create tables if they don't exist. Returns open connection."""
    conn = _get_connection(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    logger.info("Database initialised: %s", db_path)
    return conn


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(
    conn: sqlite3.Connection,
    name: str,
    email: str,
    password_hash: str,
    role: str,
) -> int:
    """Insert a user. Returns new user id."""
    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
        (name, email, password_hash, role, time.time()),
    )
    conn.commit()
    logger.info("User created: email=%s role=%s", email, role)
    return cur.lastrowid


def get_user_by_email(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)
    ).fetchone()


# ── Patients ─────────────────────────────────────────────────────────────────

def create_patient(
    conn: sqlite3.Connection,
    user_id: int,
    doctor_id: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO patients (user_id, doctor_id, created_at) VALUES (?,?,?)",
        (user_id, doctor_id, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def get_patient_by_user_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM patients WHERE user_id=?", (user_id,)
    ).fetchone()


def get_patients_for_doctor(conn: sqlite3.Connection, doctor_id: int) -> list[sqlite3.Row]:
    """Return all patients assigned to this doctor (SR-04)."""
    return conn.execute(
        """SELECT p.*, u.name, u.email
           FROM patients p
           JOIN users u ON u.id = p.user_id
           WHERE p.doctor_id=?
           ORDER BY u.name""",
        (doctor_id,),
    ).fetchall()


# ── Sessions ─────────────────────────────────────────────────────────────────

def create_session(
    conn: sqlite3.Connection,
    patient_id: int,
    exercise: str,
    rep_baseline: int,
    started_at: float,
) -> int:
    cur = conn.execute(
        """INSERT INTO sessions (patient_id, exercise, status, rep_baseline, started_at)
           VALUES (?,?,'active',?,?)""",
        (patient_id, exercise, rep_baseline, started_at),
    )
    conn.commit()
    logger.info("Session created: patient_id=%d exercise=%s", patient_id, exercise)
    return cur.lastrowid


def update_session_status(
    conn: sqlite3.Connection,
    session_id: int,
    status: str,
    ended_at: Optional[float] = None,
    duration_sec: Optional[int] = None,
) -> None:
    conn.execute(
        "UPDATE sessions SET status=?, ended_at=?, duration_sec=? WHERE id=?",
        (status, ended_at, duration_sec, session_id),
    )
    conn.commit()


def get_sessions_for_patient(
    conn: sqlite3.Connection,
    patient_id: int,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Session history for a patient (SR-03 — only their own data)."""
    return conn.execute(
        """SELECT s.*, sm.max_rom, sm.avg_rom, sm.repetitions AS total_reps
           FROM sessions s
           LEFT JOIN session_metrics sm ON sm.session_id = s.id
           WHERE s.patient_id=? AND s.status='saved'
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (patient_id, limit),
    ).fetchall()


def get_session(
    conn: sqlite3.Connection,
    session_id: int,
    patient_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """
    Fetch one session, enforcing authorization (SR-03/SR-04).
    Caller must supply either patient_id (own data) or doctor_id (assigned patient).
    """
    if patient_id is not None:
        return conn.execute(
            "SELECT * FROM sessions WHERE id=? AND patient_id=?",
            (session_id, patient_id),
        ).fetchone()
    if doctor_id is not None:
        return conn.execute(
            """SELECT s.* FROM sessions s
               JOIN patients p ON p.id = s.patient_id
               WHERE s.id=? AND p.doctor_id=?""",
            (session_id, doctor_id),
        ).fetchone()
    return None  # no auth context supplied → deny


def get_sessions_for_doctor_patient(
    conn: sqlite3.Connection,
    patient_id: int,
    doctor_id: int,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Sessions of a specific patient, only if that patient is assigned to this doctor."""
    return conn.execute(
        """SELECT s.*, sm.max_rom, sm.avg_rom, sm.repetitions AS total_reps
           FROM sessions s
           JOIN patients p ON p.id = s.patient_id
           LEFT JOIN session_metrics sm ON sm.session_id = s.id
           WHERE s.patient_id=? AND p.doctor_id=? AND s.status='saved'
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (patient_id, doctor_id, limit),
    ).fetchall()


# ── Telemetry ─────────────────────────────────────────────────────────────────

def save_telemetry_batch(
    conn: sqlite3.Connection,
    session_id: int,
    rows: list[dict],
) -> None:
    """Batch-insert telemetry rows. rows: list of {ts, angle, fsr, motor, rep, seq, device_ts}."""
    if not rows:
        return
    conn.executemany(
        """INSERT INTO telemetry
           (session_id, ts, angle, fsr, motor, rep, sequence, device_ts)
           VALUES (:session_id, :ts, :angle, :fsr, :motor, :rep, :sequence, :device_ts)""",
        [{"session_id": session_id, **r} for r in rows],
    )
    conn.commit()


def get_telemetry_for_session(
    conn: sqlite3.Connection,
    session_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM telemetry WHERE session_id=? ORDER BY ts",
        (session_id,),
    ).fetchall()


# ── Repetitions ───────────────────────────────────────────────────────────────

def save_repetitions(
    conn: sqlite3.Connection,
    session_id: int,
    reps: list[dict],
) -> None:
    """reps: list of {rep_index, started_at, ended_at, duration_sec}."""
    if not reps:
        return
    conn.executemany(
        """INSERT INTO repetitions
           (session_id, rep_index, started_at, ended_at, duration_sec)
           VALUES (:session_id, :rep_index, :started_at, :ended_at, :duration_sec)""",
        [{"session_id": session_id, **r} for r in reps],
    )
    conn.commit()


def get_repetitions_for_session(
    conn: sqlite3.Connection,
    session_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM repetitions WHERE session_id=? ORDER BY rep_index",
        (session_id,),
    ).fetchall()


# ── Session metrics ───────────────────────────────────────────────────────────

def save_session_metrics(
    conn: sqlite3.Connection,
    session_id: int,
    metrics: dict,
) -> None:
    """
    Upsert canonical session_metrics row.
    metrics keys: min_rom, max_rom, avg_rom, repetitions, fsr_peak, motor_on_count, config_version
    """
    conn.execute(
        """INSERT INTO session_metrics
           (session_id, min_rom, max_rom, avg_rom, repetitions,
            fsr_peak, motor_on_count, config_version)
           VALUES (:session_id, :min_rom, :max_rom, :avg_rom, :repetitions,
                   :fsr_peak, :motor_on_count, :config_version)
           ON CONFLICT(session_id) DO UPDATE SET
               min_rom=excluded.min_rom,
               max_rom=excluded.max_rom,
               avg_rom=excluded.avg_rom,
               repetitions=excluded.repetitions,
               fsr_peak=excluded.fsr_peak,
               motor_on_count=excluded.motor_on_count,
               config_version=excluded.config_version""",
        {"session_id": session_id, **metrics},
    )
    conn.commit()
    logger.info("Session metrics saved: session_id=%d", session_id)


def get_session_metrics(
    conn: sqlite3.Connection,
    session_id: int,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM session_metrics WHERE session_id=?", (session_id,)
    ).fetchone()


# ── ROM trend ─────────────────────────────────────────────────────────────────

def get_rom_trend(
    conn: sqlite3.Connection,
    patient_id: int,
    limit: int = 30,
) -> list[sqlite3.Row]:
    """Chronological max_rom per saved session for this patient."""
    return conn.execute(
        """SELECT s.started_at, s.exercise, sm.max_rom, sm.avg_rom, sm.repetitions
           FROM sessions s
           JOIN session_metrics sm ON sm.session_id = s.id
           WHERE s.patient_id=? AND s.status='saved' AND sm.max_rom IS NOT NULL
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (patient_id, limit),
    ).fetchall()


def get_rom_trend_for_doctor_patient(
    conn: sqlite3.Connection,
    patient_id: int,
    doctor_id: int,
    limit: int = 30,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.started_at, s.exercise, sm.max_rom, sm.avg_rom, sm.repetitions
           FROM sessions s
           JOIN patients p ON p.id = s.patient_id
           JOIN session_metrics sm ON sm.session_id = s.id
           WHERE s.patient_id=? AND p.doctor_id=? AND s.status='saved'
             AND sm.max_rom IS NOT NULL
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (patient_id, doctor_id, limit),
    ).fetchall()


# ── Doctor dashboard aggregates ───────────────────────────────────────────────

def get_doctor_stats(conn: sqlite3.Connection, doctor_id: int) -> dict:
    """Top-level counts for the Doctor Dashboard."""
    active = conn.execute(
        "SELECT COUNT(*) FROM patients WHERE doctor_id=?", (doctor_id,)
    ).fetchone()[0]

    import time as _t
    today_start = _t.time() - (_t.time() % 86400)  # approximate day boundary
    sessions_today = conn.execute(
        """SELECT COUNT(*) FROM sessions s
           JOIN patients p ON p.id = s.patient_id
           WHERE p.doctor_id=? AND s.started_at >= ?""",
        (doctor_id, today_start),
    ).fetchone()[0]
    sessions_done = conn.execute(
        """SELECT COUNT(*) FROM sessions s
           JOIN patients p ON p.id = s.patient_id
           WHERE p.doctor_id=? AND s.started_at >= ? AND s.status='saved'""",
        (doctor_id, today_start),
    ).fetchone()[0]

    return {
        "active_patients": active,
        "sessions_today": sessions_today,
        "sessions_completed": sessions_done,
    }


def get_attention_patients(
    conn: sqlite3.Connection,
    doctor_id: int,
    days_inactive: int = 3,
    decline_sessions: int = 2,
) -> list[dict]:
    """
    Return patients flagged for attention by documented rules (SRS §13, §15):
    Rule A: No session in the last `days_inactive` days.
    Rule B: Max ROM declining for `decline_sessions` consecutive sessions.

    This is an application-defined review flag, NOT a medical diagnosis.
    """
    import time as _t
    cutoff = _t.time() - days_inactive * 86400
    patients = get_patients_for_doctor(conn, doctor_id)

    flagged = []
    for p in patients:
        pid = p["id"]
        reasons = []

        # Rule A: inactive
        last_session = conn.execute(
            "SELECT MAX(started_at) FROM sessions WHERE patient_id=? AND status='saved'",
            (pid,),
        ).fetchone()[0]
        if last_session is None or last_session < cutoff:
            days = int((_t.time() - (last_session or 0)) / 86400)
            reasons.append(f"No session in {days}+ days" if last_session else "No sessions recorded")

        # Rule B: ROM declining
        recent_roms = conn.execute(
            """SELECT sm.max_rom FROM sessions s
               JOIN session_metrics sm ON sm.session_id = s.id
               WHERE s.patient_id=? AND s.status='saved' AND sm.max_rom IS NOT NULL
               ORDER BY s.started_at DESC LIMIT ?""",
            (pid, decline_sessions + 1),
        ).fetchall()
        rom_values = [r[0] for r in recent_roms]
        if len(rom_values) >= decline_sessions:
            if all(
                rom_values[i] < rom_values[i + 1]
                for i in range(len(rom_values) - 1)
            ):
                reasons.append("ROM declining across recent sessions")

        if reasons:
            flagged.append({
                "patient_id":   pid,
                "name":         p["name"],
                "email":        p["email"],
                "reasons":      reasons,
                "last_session": last_session,
            })

    return flagged
