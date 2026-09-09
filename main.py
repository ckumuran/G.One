"""
main.py — KneeGrow Desktop Application Entry Point.

Starts the application:
1. Configures CustomTkinter theme and logging
2. Initialises database and seeds initial demo accounts
3. Starts the hardware SerialReader daemon thread
4. Manages role-based screen routing (Patient vs Doctor)
5. Handles clean application shutdown
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import tkinter as tk
from typing import Optional

import customtkinter as ctk

import config
import db as _db
from seed import seed_database
from session import Session
from telemetry import SerialReader, TelemetryState
from ui import theme
from ui.doctor_dashboard import DoctorDashboardFrame
from ui.history import HistoryFrame
from ui.live_session import LiveSessionFrame
from ui.login import LoginFrame
from ui.patient_dashboard import PatientDashboardFrame
from ui.patient_detail import PatientDetailFrame
from ui.session_analysis import SessionAnalysisFrame
from ui.session_summary import SessionSummaryFrame
from ui.widgets import TopNav

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kneegrow")


class KneeGrowApp(ctk.CTk):
    """Main application controller and window."""

    def __init__(self):
        super().__init__()

        # Window settings
        self.title("KneeGrow — Biomechanical Telemetry & Rehabilitation Dashboard")
        self.geometry("1200x800")
        self.minsize(960, 640)

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=theme.BG_DARK)

        # 1. Database initialisation
        self.conn = _db.init_db()
        seed_database()

        # 2. Hardware / Telemetry layer
        self.telemetry_state = TelemetryState()
        self.serial_reader = SerialReader(self.telemetry_state)
        self.serial_reader.start()

        # 3. Application session state
        self.current_user: Optional[sqlite3.Row] = None
        self.patient_row: Optional[sqlite3.Row] = None
        self.active_session: Optional[Session] = None

        # Container for main screen
        self.nav_bar: Optional[TopNav] = None
        self.content_frame = ctk.CTkFrame(self, fg_color=theme.BG_DARK, corner_radius=0)
        self.content_frame.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Show initial screen
        self.show_login()

    # ── Navigation / Screen Routing ──────────────────────────────────────────

    def _clear_content(self):
        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def show_login(self):
        self.current_user = None
        self.patient_row = None
        self.active_session = None

        if self.nav_bar is not None:
            self.nav_bar.destroy()
            self.nav_bar = None

        self._clear_content()
        login_view = LoginFrame(
            self.content_frame,
            conn=self.conn,
            on_login_success=self._on_login_success,
        )
        login_view.pack(fill="both", expand=True)

    def _on_login_success(self, user: sqlite3.Row):
        self.current_user = user
        role = user["role"]
        logger.info("Authenticated as %s (%s)", user["name"], role)

        if role == "patient":
            # Lookup patient profile
            self.patient_row = _db.get_patient_by_user_id(self.conn, user["id"])
            if not self.patient_row:
                # Create patient record if missing
                pid = _db.create_patient(self.conn, user_id=user["id"])
                self.patient_row = _db.get_patient_by_user_id(self.conn, user["id"])
            self.show_patient_dashboard()
        elif role == "doctor":
            self.show_doctor_dashboard()
        else:
            logger.error("Unknown user role: %s", role)
            self.show_login()

    def _setup_nav(self, active_nav: str):
        if self.nav_bar is not None:
            self.nav_bar.destroy()
            self.nav_bar = None

        if not self.current_user:
            return

        role = self.current_user["role"]
        nav_items = []
        if role == "patient":
            nav_items = [
                ("Dashboard", self.show_patient_dashboard),
                ("History", self.show_patient_history),
            ]
        elif role == "doctor":
            nav_items = [
                ("Patients", self.show_doctor_dashboard),
            ]

        self.nav_bar = TopNav(
            self,
            user_name=self.current_user["name"],
            user_role=role,
            on_logout=self.show_login,
            nav_items=nav_items,
            active_nav=active_nav,
        )
        self.nav_bar.pack(side="top", fill="x", before=self.content_frame)

    # ── Patient Screens ──────────────────────────────────────────────────────

    def show_patient_dashboard(self):
        self._setup_nav(active_nav="Dashboard")
        self._clear_content()

        view = PatientDashboardFrame(
            self.content_frame,
            conn=self.conn,
            patient_row=self.patient_row,
            telemetry_state=self.telemetry_state,
            on_start_session=self.start_live_session,
            on_view_session=lambda sid: self.show_session_summary(sid, is_doctor=False),
        )
        view.pack(fill="both", expand=True)

    def start_live_session(self, exercise: str):
        # Create new Session lifecycle instance
        self.active_session = Session(
            conn=self.conn,
            telemetry_state=self.telemetry_state,
            patient_id=self.patient_row["id"],
            exercise=exercise,
        )

        self._setup_nav(active_nav="Live Session")
        self._clear_content()

        view = LiveSessionFrame(
            self.content_frame,
            conn=self.conn,
            session_instance=self.active_session,
            telemetry_state=self.telemetry_state,
            on_session_saved=lambda sid: self.show_session_summary(sid, is_doctor=False),
            on_back_to_dashboard=self.show_patient_dashboard,
        )
        view.pack(fill="both", expand=True)

    def show_session_summary(self, session_id: int, is_doctor: bool = False, doctor_patient_id: Optional[int] = None):
        self._setup_nav(active_nav="Summary")
        self._clear_content()

        patient_id = self.patient_row["id"] if self.patient_row else None
        doctor_id = self.current_user["id"] if is_doctor else None

        back_cb = (lambda: self.show_patient_detail(doctor_patient_id)) if (is_doctor and doctor_patient_id) else (
            self.show_doctor_dashboard if is_doctor else self.show_patient_dashboard
        )

        view = SessionSummaryFrame(
            self.content_frame,
            conn=self.conn,
            session_id=session_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            on_back=back_cb,
        )
        view.pack(fill="both", expand=True)

    def show_patient_history(self):
        self._setup_nav(active_nav="History")
        self._clear_content()

        view = HistoryFrame(
            self.content_frame,
            conn=self.conn,
            patient_id=self.patient_row["id"],
            on_view_session=lambda sid: self.show_session_summary(sid, is_doctor=False),
            on_back=self.show_patient_dashboard,
        )
        view.pack(fill="both", expand=True)

    # ── Doctor Screens ───────────────────────────────────────────────────────

    def show_doctor_dashboard(self):
        self._setup_nav(active_nav="Patients")
        self._clear_content()

        view = DoctorDashboardFrame(
            self.content_frame,
            conn=self.conn,
            doctor_user=self.current_user,
            on_select_patient=self.show_patient_detail,
        )
        view.pack(fill="both", expand=True)

    def show_patient_detail(self, patient_id: int):
        self._setup_nav(active_nav="Patients")
        self._clear_content()

        view = PatientDetailFrame(
            self.content_frame,
            conn=self.conn,
            doctor_id=self.current_user["id"],
            patient_id=patient_id,
            on_view_session=lambda sid: self.show_doctor_session_analysis(patient_id, sid),
            on_back=self.show_doctor_dashboard,
        )
        view.pack(fill="both", expand=True)

    def show_doctor_session_analysis(self, patient_id: int, session_id: int):
        self._setup_nav(active_nav="Patients")
        self._clear_content()

        view = SessionAnalysisFrame(
            self.content_frame,
            conn=self.conn,
            doctor_id=self.current_user["id"],
            session_id=session_id,
            on_back=lambda: self.show_patient_detail(patient_id),
        )
        view.pack(fill="both", expand=True)

    # ── Application Shutdown ─────────────────────────────────────────────────

    def _on_closing(self):
        logger.info("Shutting down KneeGrow application...")
        try:
            self.serial_reader.stop()
        except Exception:
            pass

        try:
            self.conn.close()
        except Exception:
            pass

        self.destroy()
        sys.exit(0)


def main():
    app = KneeGrowApp()
    app.mainloop()


if __name__ == "__main__":
    main()
