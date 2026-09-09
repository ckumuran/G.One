"""
config.py — Runtime configuration for KneeGrow.

All values have safe defaults matching BASE_kneegrow_dashboard.py.
Override via environment variables (SR-10: no hard-coded secrets in source).
"""

import os
import pathlib

# ── Serial / hardware defaults (match BASE exactly) ────────────────────────
SERIAL_PORT: str = os.environ.get("KNEEGROW_PORT", "COM4")
SERIAL_BAUD: int = int(os.environ.get("KNEEGROW_BAUD", "115200"))
SERIAL_TIMEOUT: float = float(os.environ.get("KNEEGROW_TIMEOUT", "0.15"))

# ── Database ────────────────────────────────────────────────────────────────
_default_db = str(pathlib.Path(__file__).parent / "data" / "kneegrow.db")
DB_PATH: str = os.environ.get("KNEEGROW_DB", _default_db)

# ── Live-plot buffer (matches BASE MAX_POINTS = 120) ───────────────────────
MAX_POINTS: int = 120

# ── UI refresh cadence ──────────────────────────────────────────────────────
UI_REFRESH_MS: int = 100          # Tkinter after() interval

# ── Telemetry batch-write interval during ACTIVE session ───────────────────
TELEMETRY_BATCH_SEC: float = 1.0  # flush to DB roughly once per second

# ── Movement-phase velocity threshold (deg/s) ──────────────────────────────
# Angle increasing faster than this → "increasing"; decreasing faster → "decreasing"
PHASE_VELOCITY_THRESHOLD: float = 5.0

# ── "Requiring attention" doctor flag: days since last session ──────────────
ATTENTION_DAYS_INACTIVE: int = 3
# and/or: ROM declining in last N sessions (consecutive decreases)
ATTENTION_ROM_DECLINE_SESSIONS: int = 2

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.environ.get("KNEEGROW_LOG", "INFO")
