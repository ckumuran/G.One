"""
export.py — CSV export of raw telemetry and session records.

Provides clean stdlib-based CSV export functionality for:
- Raw telemetry data of a session (ts, angle, fsr, motor, rep, seq, device_ts)
- Repetition records of a session (rep_index, started_at, ended_at, duration_sec)

Enforces patient/doctor authorization filters (SR-03/SR-04).
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from typing import Optional

import db as _db


def export_telemetry_csv(
    conn: sqlite3.Connection,
    session_id: int,
    output_path: Optional[str] = None,
    patient_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
) -> str:
    """
    Export raw session telemetry as CSV formatted string.
    Optionally writes to output_path.
    Enforces authorization.
    """
    session = _db.get_session(conn, session_id, patient_id=patient_id, doctor_id=doctor_id)
    if session is None:
        raise PermissionError(f"Unauthorized access or session {session_id} not found")

    rows = _db.get_telemetry_for_session(conn, session_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["session_id", "ts", "angle", "fsr", "motor", "rep", "sequence", "device_ts"])

    for r in rows:
        writer.writerow([
            r["session_id"],
            r["ts"],
            r["angle"],
            r["fsr"],
            r["motor"],
            r["rep"],
            r["sequence"],
            r["device_ts"],
        ])

    csv_text = output.getvalue()
    output.close()

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            f.write(csv_text)

    return csv_text


def export_repetitions_csv(
    conn: sqlite3.Connection,
    session_id: int,
    output_path: Optional[str] = None,
    patient_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
) -> str:
    """
    Export repetition records for a session as CSV.
    Optionally writes to output_path.
    Enforces authorization.
    """
    session = _db.get_session(conn, session_id, patient_id=patient_id, doctor_id=doctor_id)
    if session is None:
        raise PermissionError(f"Unauthorized access or session {session_id} not found")

    rows = _db.get_repetitions_for_session(conn, session_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["session_id", "rep_index", "started_at", "ended_at", "duration_sec"])

    for r in rows:
        writer.writerow([
            r["session_id"],
            r["rep_index"],
            r["started_at"],
            r["ended_at"],
            r["duration_sec"],
        ])

    csv_text = output.getvalue()
    output.close()

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            f.write(csv_text)

    return csv_text
