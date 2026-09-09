# KneeGrow — Biomechanical Telemetry & Rehabilitation Dashboard

A native Python desktop application for knee rehabilitation monitoring, joint kinematics analysis, and clinical tracking using an ESP32 brace device.

---

## 1. Setup

```bash
# 1. Activate the virtual environment
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows

# 2. Install dependencies (if not already installed)
pip install -r requirements.txt
```

---

## 2. Running the Application

```bash
# Run the application with default settings (COM4 @ 115200 baud)
python3 main.py

# Or specify a custom serial port (e.g. for macOS / Linux / specific COM port):
KNEEGROW_PORT=/dev/tty.usbserial-0001 python3 main.py

# On Windows PowerShell:
# $env:KNEEGROW_PORT="COM3"; python main.py
```

---

## 3. Demo Credentials

The database automatically seeds demo accounts on launch:

| Role | Email | Password | Assigned Clinician |
|---|---|---|---|
| **Patient** | `patient@kneegrow.local` | `patient123` | Dr. Sarah Jenkins |
| **Doctor** | `doctor@kneegrow.local` | `doctor123` | — |
| **Patient (Alternate)** | `alex@kneegrow.local` | `alex123` | Dr. Sarah Jenkins |

*(Quick demo buttons are also available directly on the login screen).*

---

## 4. Running Automated Tests

```bash
python3 -m pytest -v
```

---

## 5. Architecture & Telemetry Specification

- **Hardware Protocol:** `ANGLE=35.6,FSR=27,MOTOR=0,REP=4` (backward compatible, supports optional `SEQ` and `TIME`).
- **Telemetry Threading:** Background daemon thread reads serial frames and updates thread-safe `TelemetryState`. The UI thread never performs blocking serial I/O.
- **Session Lifecycle:** `IDLE → READY → ACTIVE ⇄ PAUSED → FINALIZING → SAVED`.
- **Session Finalization:** Computes canonical metrics and persists to SQLite in a worker thread.
- **Reports & Exports:** Printable PDF Session Reports via ReportLab; raw telemetry and repetition intervals CSV exports.
- **Security & Authorization:** Salted PBKDF2 password hashing; strict patient and doctor cohort data isolation.

> **Clinical Disclaimer:** All Range of Motion (ROM) metrics, repetition counts, velocity values, and movement phase classifications are telemetry-derived engineering measurements. They do not constitute an independent clinical diagnosis or validated recovery score.
