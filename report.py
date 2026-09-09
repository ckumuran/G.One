"""
report.py — PDF Session Report Generation (P0).

Generates a clean, professional PDF session report using ReportLab.
All values are telemetry-derived from persisted SQLite records.
Includes explicit clinical disclaimer (SRS §10, R6).
"""

from __future__ import annotations

import io
import os
import sqlite3
import time
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    KeepTogether,
)

import db as _db


def generate_session_pdf(
    conn: sqlite3.Connection,
    session_id: int,
    output_path: Optional[str] = None,
    patient_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
) -> bytes:
    """
    Generate a PDF session report for the given session_id.
    Enforces authorization via patient_id or doctor_id if supplied.
    Returns the PDF bytes, and optionally writes to output_path if provided.
    """
    session = _db.get_session(conn, session_id, patient_id=patient_id, doctor_id=doctor_id)
    if session is None:
        raise PermissionError(f"Unauthorized access or session {session_id} not found")

    metrics = _db.get_session_metrics(conn, session_id)
    repetitions = _db.get_repetitions_for_session(conn, session_id)
    telemetry = _db.get_telemetry_for_session(conn, session_id)

    # Fetch patient and user info
    patient_row = conn.execute(
        """SELECT p.*, u.name as patient_name, u.email as patient_email,
                  d.name as doctor_name
           FROM patients p
           JOIN users u ON u.id = p.user_id
           LEFT JOIN users d ON d.id = p.doctor_id
           WHERE p.id = ?""",
        (session["patient_id"],),
    ).fetchone()

    patient_name = patient_row["patient_name"] if patient_row else f"Patient #{session['patient_id']}"
    doctor_name = patient_row["doctor_name"] if patient_row and patient_row["doctor_name"] else "Not Assigned"
    
    # Format dates and durations
    started_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session["started_at"])) if session["started_at"] else "N/A"
    duration_s = session["duration_sec"] or 0
    duration_str = f"{duration_s // 60}m {duration_s % 60}s"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0A0D16"),
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#5D6580"),
    )
    section_heading = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#151A28"),
        spaceBefore=12,
        spaceAfter=6,
    )
    cell_bold = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#151A28"),
    )
    cell_text = ParagraphStyle(
        "CellText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#262E42"),
    )
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Italic"],
        fontName="Helvetica-Oblique",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#707070"),
    )

    story = []

    # 1. Header Banner
    story.append(Paragraph("KneeGrow — Rehabilitation Session Report", title_style))
    story.append(Paragraph("Telemetry-Derived Biofeedback & Exercise Analytics", subtitle_style))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#4C7DFF"), spaceAfter=14))

    # 2. Session & Patient Metadata Table
    meta_data = [
        [
            Paragraph("<b>Patient Name:</b>", cell_bold),
            Paragraph(str(patient_name), cell_text),
            Paragraph("<b>Session ID:</b>", cell_bold),
            Paragraph(f"#{session['id']}", cell_text),
        ],
        [
            Paragraph("<b>Assigned Clinician:</b>", cell_bold),
            Paragraph(str(doctor_name), cell_text),
            Paragraph("<b>Date / Time:</b>", cell_bold),
            Paragraph(started_time_str, cell_text),
        ],
        [
            Paragraph("<b>Prescribed Exercise:</b>", cell_bold),
            Paragraph(str(session["exercise"]), cell_text),
            Paragraph("<b>Session Duration:</b>", cell_bold),
            Paragraph(duration_str, cell_text),
        ],
        [
            Paragraph("<b>Session Status:</b>", cell_bold),
            Paragraph(str(session["status"]).upper(), cell_text),
            Paragraph("<b>REP Baseline:</b>", cell_bold),
            Paragraph(str(session["rep_baseline"]), cell_text),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[1.5 * inch, 2.2 * inch, 1.3 * inch, 2.2 * inch])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F6FB")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D7E5")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E9F2")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # 3. Key Telemetry Metrics Summary
    story.append(Paragraph("Canonical Session Summary", section_heading))

    max_rom_val = f"{metrics['max_rom']:.1f}°" if metrics and metrics["max_rom"] is not None else "N/A"
    min_rom_val = f"{metrics['min_rom']:.1f}°" if metrics and metrics["min_rom"] is not None else "N/A"
    avg_rom_val = f"{metrics['avg_rom']:.1f}°" if metrics and metrics["avg_rom"] is not None else "N/A"
    reps_val = str(metrics["repetitions"]) if metrics and metrics["repetitions"] is not None else str(len(repetitions))
    fsr_val = str(metrics["fsr_peak"]) if metrics and metrics["fsr_peak"] is not None else "N/A"
    motor_val = str(metrics["motor_on_count"]) if metrics and metrics["motor_on_count"] is not None else "0"

    kpi_data = [
        [
            Paragraph("<b>Peak ROM</b>", cell_bold),
            Paragraph("<b>Min ROM</b>", cell_bold),
            Paragraph("<b>Average ROM</b>", cell_bold),
            Paragraph("<b>Completed Reps</b>", cell_bold),
            Paragraph("<b>Peak FSR</b>", cell_bold),
            Paragraph("<b>Motor Activations</b>", cell_bold),
        ],
        [
            Paragraph(f"<font size=14 color='#4C7DFF'><b>{max_rom_val}</b></font>", cell_text),
            Paragraph(f"<font size=14 color='#151A28'><b>{min_rom_val}</b></font>", cell_text),
            Paragraph(f"<font size=14 color='#151A28'><b>{avg_rom_val}</b></font>", cell_text),
            Paragraph(f"<font size=14 color='#34D399'><b>{reps_val}</b></font>", cell_text),
            Paragraph(f"<font size=14 color='#F5B335'><b>{fsr_val}</b></font>", cell_text),
            Paragraph(f"<font size=14 color='#151A28'><b>{motor_val}</b></font>", cell_text),
        ],
    ]
    kpi_table = Table(kpi_data, colWidths=[1.2 * inch] * 6)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEFFD")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#FFFFFF")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D7E5")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E9F2")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 14))

    # 4. Repetitions Breakdown Table (if available)
    if repetitions:
        story.append(Paragraph(f"Repetition Timing Breakdown ({len(repetitions)} detected)", section_heading))
        rep_rows = [
            [
                Paragraph("<b>Rep #</b>", cell_bold),
                Paragraph("<b>Start Offset (s)</b>", cell_bold),
                Paragraph("<b>End Offset (s)</b>", cell_bold),
                Paragraph("<b>Duration (s)</b>", cell_bold),
            ]
        ]
        first_ts = telemetry[0]["ts"] if telemetry else 0.0
        for r in repetitions:
            rel_start = max(0.0, r["started_at"] - first_ts) if first_ts else r["started_at"]
            rel_end = max(0.0, r["ended_at"] - first_ts) if first_ts else r["ended_at"]
            rep_rows.append([
                Paragraph(str(r["rep_index"]), cell_text),
                Paragraph(f"{rel_start:.2f}", cell_text),
                Paragraph(f"{rel_end:.2f}", cell_text),
                Paragraph(f"{r['duration_sec']:.2f}s", cell_text),
            ])

        rep_table = Table(rep_rows, colWidths=[1.2 * inch, 2.0 * inch, 2.0 * inch, 2.0 * inch])
        rep_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F3F8")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D7E5")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E9F2")),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(rep_table)
        story.append(Spacer(1, 14))

    # 5. Telemetry Sample Summary
    telemetry_count = len(telemetry)
    story.append(Paragraph(f"Raw Telemetry Summary: {telemetry_count} raw sensor frames captured at ~10Hz.", subtitle_style))
    story.append(Spacer(1, 14))

    # 6. Clinical & Technical Disclaimer (SRS §10, R6)
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D0D7E5"), spaceAfter=8))
    disclaimer_text = (
        "<b>CLINICAL DISCLAIMER & NOTICE:</b> All measurements, Range of Motion (ROM) metrics, "
        "repetition counts, velocities, and movement phase estimations presented in this report "
        "are telemetry-derived engineering metrics acquired from local sensor hardware. They do "
        "not constitute an independent medical diagnosis, clinical evaluation, or validated recovery "
        "score. Review and interpretation must be performed by a qualified healthcare professional."
    )
    story.append(Paragraph(disclaimer_text, disclaimer_style))

    # Build document
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

    return pdf_bytes
