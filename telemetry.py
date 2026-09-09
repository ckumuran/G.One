"""
telemetry.py — Hardware/serial layer.

Extracted from BASE_kneegrow_dashboard.py with Tkinter coupling removed.
This module is the sole place that imports pyserial.
The UI never imports pyserial directly (NFR-11).

Thread model:
  SerialReader runs as a single daemon thread (same as BASE).
  TelemetryState is written only by SerialReader and read by everyone else.
  Reading is lock-free for simple scalar attributes (GIL provides atomicity for
  Python int/float assignment). History deques use a threading.Lock.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple

import config

logger = logging.getLogger(__name__)


# ── Telemetry packet ────────────────────────────────────────────────────────

@dataclass
class TelemetryPacket:
    """One parsed ESP32 telemetry line."""
    ts: float          # host receipt time (time.monotonic())
    angle: float
    fsr: int
    motor: int
    rep: int
    seq: Optional[int] = None          # future SEQ= field
    device_ts: Optional[float] = None  # future TIME= field


# ── Parser ──────────────────────────────────────────────────────────────────

# Pre-compile regexes (exactly matching BASE behaviour, case-insensitive)
_RE_ANGLE = re.compile(r"ANGLE\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_FSR   = re.compile(r"FSR\s*=\s*(-?\d+)",             re.IGNORECASE)
_RE_MOTOR = re.compile(r"MOTOR\s*=\s*(\d+)",             re.IGNORECASE)
_RE_REP   = re.compile(r"REP\s*=\s*(\d+)",               re.IGNORECASE)
_RE_SEQ   = re.compile(r"SEQ\s*=\s*(\d+)",               re.IGNORECASE)
_RE_TIME  = re.compile(r"TIME\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_line(
    line: str,
    prev_packet: Optional[TelemetryPacket] = None,
) -> Optional[TelemetryPacket]:
    """
    Parse one raw serial line into a TelemetryPacket.

    Returns None for lines that contain no recognised fields (malformed lines).
    Preserves previous values for missing optional fields exactly as BASE does.
    Never raises — caller may safely iterate without try/except per line.
    """
    if not line:
        return None

    try:
        m_angle = _RE_ANGLE.search(line)
        m_fsr   = _RE_FSR.search(line)
        m_motor = _RE_MOTOR.search(line)
        m_rep   = _RE_REP.search(line)
        m_seq   = _RE_SEQ.search(line)
        m_time  = _RE_TIME.search(line)

        if not (m_angle or m_fsr or m_motor or m_rep):
            return None  # no recognised field → malformed, skip

        prev = prev_packet
        return TelemetryPacket(
            ts=time.monotonic(),
            angle=float(m_angle.group(1)) if m_angle else (prev.angle if prev else 0.0),
            fsr=int(m_fsr.group(1))       if m_fsr   else (prev.fsr   if prev else 0),
            motor=int(m_motor.group(1))   if m_motor else (prev.motor  if prev else 0),
            rep=int(m_rep.group(1))       if m_rep   else (prev.rep    if prev else 0),
            seq=int(m_seq.group(1))       if m_seq   else None,
            device_ts=float(m_time.group(1)) if m_time else None,
        )
    except Exception:
        # Never let a parse error propagate — NFR-04
        return None


# ── TelemetryState ──────────────────────────────────────────────────────────

class TelemetryState:
    """
    Thread-safe shared state: latest telemetry values + bounded live-history deques.

    SerialReader is the only writer.
    UI/session poll this read-only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected: bool = False
        self.angle: float = 0.0
        self.fsr: int = 0
        self.motor: int = 0
        self.rep: int = 0
        self.seq: Optional[int] = None
        self.device_ts: Optional[float] = None
        self.last_packet: Optional[TelemetryPacket] = None

        # Bounded live-history deques (same size as BASE MAX_POINTS)
        self._angle_history: Deque[float] = deque(maxlen=config.MAX_POINTS)
        self._fsr_history:   Deque[int]   = deque(maxlen=config.MAX_POINTS)
        self._rep_history:   Deque[int]   = deque(maxlen=config.MAX_POINTS)

    # ── Writer (SerialReader thread only) ───────────────────────────────────

    def update(self, pkt: TelemetryPacket) -> None:
        with self._lock:
            self.connected = True
            self.angle = pkt.angle
            self.fsr   = pkt.fsr
            self.motor = pkt.motor
            self.rep   = pkt.rep
            self.seq   = pkt.seq
            self.device_ts = pkt.device_ts
            self.last_packet = pkt
            self._angle_history.append(pkt.angle)
            self._fsr_history.append(pkt.fsr)
            self._rep_history.append(pkt.rep)

    def mark_disconnected(self) -> None:
        with self._lock:
            self.connected = False

    def clear_histories(self) -> None:
        """Called at session start to reset live-plot buffers (matches BASE behaviour)."""
        with self._lock:
            self._angle_history.clear()
            self._fsr_history.clear()
            self._rep_history.clear()

    # ── Reader helpers (any thread) ─────────────────────────────────────────

    def get_histories(self) -> Tuple[list, list, list]:
        with self._lock:
            return (
                list(self._angle_history),
                list(self._fsr_history),
                list(self._rep_history),
            )

    def snapshot(self) -> dict:
        """Atomic snapshot of all current values."""
        with self._lock:
            return {
                "connected": self.connected,
                "angle":     self.angle,
                "fsr":       self.fsr,
                "motor":     self.motor,
                "rep":       self.rep,
                "seq":       self.seq,
                "device_ts": self.device_ts,
                "ts":        self.last_packet.ts if self.last_packet else None,
            }


# ── SerialReader ────────────────────────────────────────────────────────────

class SerialReader:
    """
    Daemon thread that reads serial lines from the ESP32 and updates TelemetryState.

    Behaviour preserved from BASE_kneegrow_dashboard.py:
    - COM4 / 115200 / timeout=0.15 (configurable via config.py)
    - 2-second post-open delay for ESP32 reset
    - daemon=True so process exits cleanly
    - SerialException → mark disconnected, sleep 1s, keep looping
    - Malformed line → skip, keep reading (NFR-04)
    - Generic exception → skip line, keep reading
    """

    def __init__(self, state: TelemetryState) -> None:
        self.state = state
        self._running = False
        self._ser = None
        self._thread: Optional[threading.Thread] = None

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect and start the background reader thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="SerialReader"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the reader to stop and close the serial port."""
        self._running = False
        self._close_port()
        logger.info("SerialReader stopped")

    # ── Internal ────────────────────────────────────────────────────────────

    def _connect(self) -> bool:
        """Attempt to open the serial port. Returns True on success."""
        try:
            import serial  # only import here — UI never reaches this
            self._ser = serial.Serial(
                config.SERIAL_PORT,
                config.SERIAL_BAUD,
                timeout=config.SERIAL_TIMEOUT,
            )
            time.sleep(2)  # give ESP32 time to reset after port open (BASE behaviour)
            logger.info(
                "Serial connected: %s @ %d baud",
                config.SERIAL_PORT,
                config.SERIAL_BAUD,
            )
            return True
        except Exception as exc:
            logger.warning("Serial connect failed: %s", exc)
            self._ser = None
            self.state.mark_disconnected()
            return False

    def _close_port(self) -> None:
        try:
            if self._ser is not None and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def _run(self) -> None:
        """Main reader loop — daemon thread body."""
        while self._running:
            if self._ser is None:
                self._connect()
                if self._ser is None:
                    time.sleep(1.0)
                    continue

            try:
                raw = self._ser.readline()
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                pkt = parse_line(line, self.state.last_packet)
                if pkt is None:
                    continue  # malformed — keep reading (NFR-04)

                self.state.update(pkt)

            except Exception as exc:
                import serial
                if isinstance(exc, (serial.SerialException, OSError)):
                    logger.warning("Serial error: %s — waiting to reconnect", exc)
                    self.state.mark_disconnected()
                    self._close_port()
                    time.sleep(1.0)
                # Any other exception: skip line, keep reading


# ── FakeSerial for tests (tests/ only, never imported by ui/) ───────────────

class FakeSerial:
    """
    Test double for pyserial.Serial.
    Yields lines from an iterable; marks StopIteration by returning b"".
    Never used in production — only importable from tests/.
    """

    def __init__(self, lines: list[str]) -> None:
        self._iter = iter(lines)
        self.is_open = True

    def readline(self) -> bytes:
        try:
            return (next(self._iter) + "\n").encode()
        except StopIteration:
            return b""

    def close(self) -> None:
        self.is_open = False
