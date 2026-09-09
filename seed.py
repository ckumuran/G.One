"""
seed.py — Database Seeding Script for Demo / Development.

Creates standard demonstration accounts:
- Doctor: doctor@kneegrow.local (password: doctor123)
- Patient 1: patient@kneegrow.local (password: patient123, assigned to Doctor)
- Patient 2: alex@kneegrow.local (password: alex123, assigned to Doctor)
- Patient 3: unassigned@kneegrow.local (password: pass123, unassigned patient for isolation testing)
"""

from __future__ import annotations

import sqlite3
import time

import auth
import config
import db as _db


def seed_database(db_path: str = config.DB_PATH) -> None:
    """Seed initial users and patients if not already present."""
    conn = _db.init_db(db_path)

    # 1. Doctor
    doc_user = _db.get_user_by_email(conn, "doctor@kneegrow.local")
    if not doc_user:
        doc_id = _db.create_user(
            conn,
            name="Dr. Nick Ger",
            email="doctor@kneegrow.local",
            password_hash=auth.hash_password("doctor123"),
            role="doctor",
        )
    else:
        doc_id = doc_user["id"]

    # 2. Patient 1 (Assigned to Doctor)
    p1_user = _db.get_user_by_email(conn, "patient@kneegrow.local")
    if not p1_user:
        p1_uid = _db.create_user(
            conn,
            name="Kumaran1",
            email="patient@kneegrow.local",
            password_hash=auth.hash_password("patient123"),
            role="patient",
        )
        p1_id = _db.create_patient(conn, user_id=p1_uid, doctor_id=doc_id)
        
        # Seed a sample historical session for Jane Doe
        _seed_sample_session(conn, p1_id, exercise="Knee Flexion Extension", max_rom=85.5, reps=12, days_ago=1)
        _seed_sample_session(conn, p1_id, exercise="Knee Extension", max_rom=82.0, reps=10, days_ago=2)
    
    # 3. Patient 2 (Assigned to Doctor - flagged for attention / inactive)
    p2_user = _db.get_user_by_email(conn, "alex@kneegrow.local")
    if not p2_user:
        p2_uid = _db.create_user(
            conn,
            name="Kumaran2",
            email="alex@kneegrow.local",
            password_hash=auth.hash_password("alex123"),
            role="patient",
        )
        p2_id = _db.create_patient(conn, user_id=p2_uid, doctor_id=doc_id)
        # Inactive session (5 days ago)
        _seed_sample_session(conn, p2_id, exercise="Passive ROM", max_rom=65.0, reps=8, days_ago=5)

    # 4. Patient 3 (Unassigned patient - for isolation test)
    p3_user = _db.get_user_by_email(conn, "unassigned@kneegrow.local")
    if not p3_user:
        p3_uid = _db.create_user(
            conn,
            name="Kumaran3",
            email="unassigned@kneegrow.local",
            password_hash=auth.hash_password("pass123"),
            role="patient",
        )
        _db.create_patient(conn, user_id=p3_uid, doctor_id=None)

    conn.close()


def _seed_sample_session(
    conn: sqlite3.Connection,
    patient_id: int,
    exercise: str,
    max_rom: float,
    reps: int,
    days_ago: int,
) -> int:
    """Create a realistic historical session with telemetry and metrics."""
    ts_start = time.time() - (days_ago * 86400)
    duration = 180  # 3 minutes

    session_id = _db.create_session(
        conn,
        patient_id=patient_id,
        exercise=exercise,
        rep_baseline=0,
        started_at=ts_start,
    )
    _db.update_session_status(
        conn,
        session_id=session_id,
        status="saved",
        ended_at=ts_start + duration,
        duration_sec=duration,
    )

    # Telemetry batch
    telemetry_rows = []
    import math
    for i in range(100):
        sample_ts = ts_start + (i * 1.8)
        # Synthetic sine wave for seeded history
        angle = 20.0 + (max_rom - 20.0) * (0.5 * (1.0 + math.sin(i * 0.2)))
        fsr = int(15 + 10 * math.sin(i * 0.2))
        motor = 1 if angle > 60 else 0
        rep = int(i / (100 / reps))
        telemetry_rows.append({
            "ts": sample_ts,
            "angle": round(angle, 2),
            "fsr": max(0, fsr),
            "motor": motor,
            "rep": rep,
            "sequence": None,
            "device_ts": None,
        })
    _db.save_telemetry_batch(conn, session_id, telemetry_rows)

    # Session metrics
    _db.save_session_metrics(
        conn,
        session_id,
        {
            "min_rom": 20.0,
            "max_rom": round(max_rom, 1),
            "avg_rom": round((max_rom + 20.0) / 2, 1),
            "repetitions": reps,
            "fsr_peak": 25,
            "motor_on_count": 30,
            "config_version": "v1",
        },
    )

    # Repetitions
    rep_records = []
    interval = duration / reps
    for r in range(1, reps + 1):
        r_start = ts_start + (r - 1) * interval
        r_end = ts_start + r * interval
        rep_records.append({
            "rep_index": r,
            "started_at": round(r_start, 2),
            "ended_at": round(r_end, 2),
            "duration_sec": round(interval, 2),
        })
    _db.save_repetitions(conn, session_id, rep_records)

    return session_id


if __name__ == "__main__":
    seed_database()
    print("Database seeded successfully with demo accounts.")
