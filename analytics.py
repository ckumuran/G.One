"""
analytics.py — Canonical session metric computation.

All metrics are computed from persisted telemetry rows, NOT from the live buffer.
This is the fix for the BASE bug where Average ROM was computed from the bounded
120-sample deque instead of the full session sample set.

Movement phase uses neutral engineering labels (increasing/decreasing/holding/unknown)
as required by SRS §2.4 and risk R2 — no clinical flexion/extension terminology
until angle direction is validated against real exercise motion.

Analytics results consumed by UI, history, and PDF reports (NFR-08 canonical analytics).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

import config


# ── Main entry point ─────────────────────────────────────────────────────────

def compute(
    rows: list[sqlite3.Row],
    rep_baseline: int = 0,
) -> dict[str, Any]:
    """
    Compute canonical session metrics from persisted telemetry rows.

    Returns a dict ready for db.save_session_metrics(), plus a '_reps' key
    containing the per-rep list for db.save_repetitions().

    Args:
        rows: telemetry rows ordered by ts (SELECT * FROM telemetry WHERE session_id=? ORDER BY ts)
        rep_baseline: cumulative REP value captured at session start
    """
    if not rows:
        return _empty_metrics()

    angles  = [float(r["angle"]) for r in rows]
    fsrs    = [int(r["fsr"] or 0) for r in rows]
    motors  = [int(r["motor"] or 0) for r in rows]
    reps    = [int(r["rep"] or 0) for r in rows]
    tss     = [float(r["ts"]) for r in rows]

    # ── ROM ──────────────────────────────────────────────────────────────────
    min_rom = min(angles)
    max_rom = max(angles)
    avg_rom = sum(angles) / len(angles)

    # ── Velocity series ──────────────────────────────────────────────────────
    velocities = _compute_velocities(angles, tss)   # deg/s per interval

    # ── Movement phase ───────────────────────────────────────────────────────
    phases = _compute_phases(velocities)

    # ── Rep segmentation ─────────────────────────────────────────────────────
    rep_records = _segment_reps(reps, tss, rep_baseline)
    total_reps = len(rep_records)

    # ── FSR peak ─────────────────────────────────────────────────────────────
    fsr_peak = max(fsrs) if fsrs else 0

    # ── Motor activity ───────────────────────────────────────────────────────
    motor_on_count = sum(1 for m in motors if m != 0)

    return {
        "min_rom":        round(min_rom, 2),
        "max_rom":        round(max_rom, 2),
        "avg_rom":        round(avg_rom, 2),
        "repetitions":    total_reps,
        "fsr_peak":       fsr_peak,
        "motor_on_count": motor_on_count,
        "config_version": "v1",
        # Internal — used by session.py to write repetitions table, not stored in session_metrics
        "_reps":          rep_records,
        "_velocities":    velocities,
        "_phases":        phases,
    }


# ── Velocity ─────────────────────────────────────────────────────────────────

def _compute_velocities(angles: list[float], tss: list[float]) -> list[float]:
    """
    Returns velocity (deg/s) for each interval between consecutive samples.
    Uses telemetry timestamps, never UI refresh timing (FR-16, SRS §10.1).
    Length is len(angles) - 1.
    """
    velocities: list[float] = []
    for i in range(1, len(angles)):
        dt = tss[i] - tss[i - 1]
        if dt <= 0:
            velocities.append(0.0)
        else:
            velocities.append((angles[i] - angles[i - 1]) / dt)
    return velocities


def compute_velocity_at_index(angles: list[float], tss: list[float], idx: int) -> float:
    """Velocity at a given sample index (for live display)."""
    if idx < 1 or idx >= len(angles):
        return 0.0
    dt = tss[idx] - tss[idx - 1]
    if dt <= 0:
        return 0.0
    return (angles[idx] - angles[idx - 1]) / dt


# ── Movement phase ────────────────────────────────────────────────────────────

PHASE_INCREASING = "increasing"
PHASE_DECREASING = "decreasing"
PHASE_HOLDING    = "holding"
PHASE_UNKNOWN    = "unknown"


def _compute_phases(velocities: list[float]) -> list[str]:
    """
    Assign a movement phase label to each velocity sample.
    Threshold: config.PHASE_VELOCITY_THRESHOLD (deg/s).
    Neutral engineering labels — no clinical flexion/extension terminology (SRS R2).
    """
    threshold = config.PHASE_VELOCITY_THRESHOLD
    phases: list[str] = []
    for v in velocities:
        if v > threshold:
            phases.append(PHASE_INCREASING)
        elif v < -threshold:
            phases.append(PHASE_DECREASING)
        else:
            phases.append(PHASE_HOLDING)
    return phases


def classify_phase(current_velocity: float) -> str:
    """Classify a single velocity value — used by live session display."""
    if current_velocity > config.PHASE_VELOCITY_THRESHOLD:
        return PHASE_INCREASING
    if current_velocity < -config.PHASE_VELOCITY_THRESHOLD:
        return PHASE_DECREASING
    return PHASE_HOLDING


# ── Rep segmentation ──────────────────────────────────────────────────────────

def _segment_reps(
    reps: list[int],
    tss: list[float],
    baseline: int,
) -> list[dict]:
    """
    Walk persisted telemetry REP column to produce per-rep timing records.

    Each firmware REP increment defines one rep:
      started_at = timestamp of previous REP value (or session start for rep 1)
      ended_at   = timestamp of this REP increment

    Returns list of dicts ready for db.save_repetitions().
    """
    records: list[dict] = []
    prev_rep = baseline
    prev_ts  = tss[0] if tss else 0.0
    rep_index = 0

    for i, (rep_val, ts) in enumerate(zip(reps, tss)):
        if rep_val > prev_rep:
            # One or more rep increments happened
            increments = rep_val - prev_rep
            # Distribute time evenly if multiple increments in one sample (rare)
            interval = (ts - prev_ts) / increments if increments > 1 else (ts - prev_ts)
            for j in range(increments):
                rep_index += 1
                started = prev_ts + j * interval
                ended   = prev_ts + (j + 1) * interval
                records.append({
                    "rep_index":    rep_index,
                    "started_at":   round(started, 4),
                    "ended_at":     round(ended, 4),
                    "duration_sec": round(ended - started, 4),
                })
            prev_ts = ts
        prev_rep = rep_val

    return records


# ── Live-session helpers (used by UI directly from TelemetryState) ────────────

def compute_live_stats(
    angle_history: list[float],
    fsr_history: list[float],
    session_angles: list[float],    # full angle list accumulated this session
) -> dict:
    """
    Quick stats for the live session display.
    Uses the live-plot buffer for current/peak; session_angles for average.
    """
    current = angle_history[-1] if angle_history else 0.0
    peak    = max(session_angles) if session_angles else 0.0
    avg     = (sum(session_angles) / len(session_angles)) if session_angles else 0.0
    low     = min(session_angles) if session_angles else 0.0
    fsr_cur = fsr_history[-1] if fsr_history else 0
    return {
        "current_rom": round(current, 1),
        "peak_rom":    round(peak, 1),
        "avg_rom":     round(avg, 1),
        "min_rom":     round(low, 1),
        "fsr":         fsr_cur,
    }


def _empty_metrics() -> dict:
    return {
        "min_rom":        None,
        "max_rom":        None,
        "avg_rom":        None,
        "repetitions":    0,
        "fsr_peak":       None,
        "motor_on_count": 0,
        "config_version": "v1",
        "_reps":          [],
        "_velocities":    [],
        "_phases":        [],
    }
