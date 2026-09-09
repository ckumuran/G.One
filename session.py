"""
session.py — Session lifecycle state machine and telemetry recording.

States: IDLE → CONNECTING → READY → ACTIVE ⇄ PAUSED → FINALIZING → SAVED
                                              └──────────────────────────→ ABORTED

The FINALIZING → SAVED transition runs analytics + DB writes in a short-lived
worker thread to avoid blocking the Tkinter main thread (NFR-01, NFR-02).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Callable, Optional

import analytics
import config
import db as _db

logger = logging.getLogger(__name__)


# ── State constants ──────────────────────────────────────────────────────────

IDLE        = "IDLE"
CONNECTING  = "CONNECTING"
READY       = "READY"
ACTIVE      = "ACTIVE"
PAUSED      = "PAUSED"
FINALIZING  = "FINALIZING"
SAVED       = "SAVED"
ABORTED     = "ABORTED"


# ── Session ──────────────────────────────────────────────────────────────────

class Session:
    """
    Manages the lifecycle of one exercise session.

    thread_safe:
      .state, .rep_baseline, .session_reps(), .elapsed_sec() → safe to read from UI thread
      start/pause/resume/end call into the session from UI callbacks (Tkinter main thread)
      _flush_telemetry_buffer / finalize run on main thread timer / worker thread respectively
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        telemetry_state,       # TelemetryState, avoids circular import
        patient_id: int,
        exercise: str,
        on_finalized: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """
        conn:           main-thread SQLite connection (used for batch telemetry writes)
        telemetry_state: TelemetryState instance
        on_finalized:   called on the main thread after FINALIZING completes;
                        signature: (success: bool, message: str) -> None
        """
        self.conn = conn
        self._tstate = telemetry_state
        self.patient_id = patient_id
        self.exercise = exercise
        self.on_finalized = on_finalized

        self.state: str = IDLE
        self.session_id: Optional[int] = None
        self.rep_baseline: int = 0
        self._start_time: Optional[float] = None
        self._pause_elapsed: float = 0.0
        self._pause_start: Optional[float] = None

        # In-memory telemetry buffer — flushed to DB periodically
        self._buffer: list[dict] = []
        self._last_flush: float = 0.0
        self._lock = threading.Lock()

    # ── State transitions ────────────────────────────────────────────────────

    def start(self) -> None:
        """Transition IDLE/READY → ACTIVE."""
        if self.state not in (IDLE, READY):
            return

        snap = self._tstate.snapshot()
        self.rep_baseline = snap.get("rep", 0)
        self._start_time = time.monotonic()
        self._pause_elapsed = 0.0
        self._pause_start = None

        self._tstate.clear_histories()   # reset live-plot buffers (BASE behaviour)

        self.session_id = _db.create_session(
            self.conn,
            patient_id=self.patient_id,
            exercise=self.exercise,
            rep_baseline=self.rep_baseline,
            started_at=time.time(),
        )
        self.state = ACTIVE
        logger.info(
            "Session started: id=%d patient=%d exercise=%s rep_baseline=%d",
            self.session_id, self.patient_id, self.exercise, self.rep_baseline,
        )

    def pause(self) -> None:
        if self.state != ACTIVE:
            return
        self._pause_start = time.monotonic()
        self.state = PAUSED
        _db.update_session_status(self.conn, self.session_id, "paused")
        logger.info("Session paused: id=%d", self.session_id)

    def resume(self) -> None:
        if self.state != PAUSED:
            return
        if self._pause_start is not None:
            self._pause_elapsed += time.monotonic() - self._pause_start
            self._pause_start = None
        self.state = ACTIVE
        _db.update_session_status(self.conn, self.session_id, "active")
        logger.info("Session resumed: id=%d", self.session_id)

    def end(self, root_widget=None) -> None:
        """
        Transition ACTIVE/PAUSED → FINALIZING.
        Finalisation runs analytics + DB writes in a worker thread.
        root_widget: the Tkinter root, used for root.after(0, callback).
        """
        if self.state not in (ACTIVE, PAUSED):
            return
        if self.state == PAUSED and self._pause_start is not None:
            self._pause_elapsed += time.monotonic() - self._pause_start

        self.state = FINALIZING
        ended_wall = time.time()
        elapsed = int(self.elapsed_sec())

        _db.update_session_status(
            self.conn,
            self.session_id,
            "finalizing",
            ended_at=ended_wall,
            duration_sec=elapsed,
        )
        logger.info("Session finalizing: id=%d duration=%ds", self.session_id, elapsed)

        # Flush remaining buffer on main thread before handing off to worker
        self._flush_buffer()

        # ── Worker thread for analytics + DB summary writes ─────────────────
        def _worker():
            success = False
            message = ""
            worker_conn = None
            try:
                worker_conn = _db.init_db()          # own WAL connection
                rows = _db.get_telemetry_for_session(worker_conn, self.session_id)
                if rows:
                    metrics = analytics.compute(rows, self.rep_baseline)
                    _db.save_session_metrics(worker_conn, self.session_id, metrics)
                    _db.save_repetitions(worker_conn, self.session_id, metrics["_reps"])
                _db.update_session_status(worker_conn, self.session_id, "saved")
                success = True
                message = "Session saved successfully"
                logger.info("Session finalised: id=%d", self.session_id)
            except Exception as exc:
                logger.error("Session finalisation failed: %s", exc, exc_info=True)
                message = f"Save failed: {exc}"
                try:
                    if worker_conn:
                        _db.update_session_status(worker_conn, self.session_id, "aborted")
                except Exception:
                    pass
            finally:
                if worker_conn:
                    worker_conn.close()

            # Return to Tkinter thread
            def _done():
                self.state = SAVED if success else ABORTED
                if self.on_finalized:
                    self.on_finalized(success, message)

            if root_widget is not None:
                try:
                    root_widget.after(0, _done)
                except Exception:
                    _done()
            else:
                _done()

        t = threading.Thread(target=_worker, daemon=True, name="SessionFinalizer")
        t.start()

    def abort(self) -> None:
        """Cancel session without saving."""
        if self.state in (SAVED, ABORTED):
            return
        if self.session_id:
            try:
                _db.update_session_status(self.conn, self.session_id, "aborted")
            except Exception:
                pass
        self.state = ABORTED
        logger.info("Session aborted: id=%d", self.session_id)

    # ── Computed properties ──────────────────────────────────────────────────

    def session_reps(self) -> int:
        """Current REP minus baseline, clamped at 0 (FR-10, BASE behaviour)."""
        raw = self._tstate.rep - self.rep_baseline
        return max(0, raw)

    def elapsed_sec(self) -> float:
        """Wall-clock session time, excluding paused periods."""
        if self._start_time is None:
            return 0.0
        total = time.monotonic() - self._start_time - self._pause_elapsed
        if self._pause_start is not None:
            total -= (time.monotonic() - self._pause_start)
        return max(0.0, total)

    def elapsed_str(self) -> str:
        s = int(self.elapsed_sec())
        return f"{s // 60:02d}:{s % 60:02d}"

    # ── Telemetry recording ──────────────────────────────────────────────────

    def record_tick(self) -> None:
        """
        Called each UI after() tick while ACTIVE.
        Appends current telemetry to in-memory buffer.
        Flushes to DB roughly once per TELEMETRY_BATCH_SEC.
        """
        if self.state != ACTIVE:
            return
        snap = self._tstate.snapshot()
        if snap.get("ts") is None:
            return
        with self._lock:
            self._buffer.append({
                "ts":        snap["ts"],
                "angle":     snap["angle"],
                "fsr":       snap["fsr"],
                "motor":     snap["motor"],
                "rep":       snap["rep"],
                "sequence":  snap["seq"],
                "device_ts": snap["device_ts"],
            })

        now = time.monotonic()
        if now - self._last_flush >= config.TELEMETRY_BATCH_SEC:
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        if batch and self.session_id is not None:
            try:
                _db.save_telemetry_batch(self.conn, self.session_id, batch)
                self._last_flush = time.monotonic()
            except Exception as exc:
                logger.error("Telemetry batch write failed: %s", exc)
                # Re-queue failed batch so data is not lost
                with self._lock:
                    self._buffer = batch + self._buffer
