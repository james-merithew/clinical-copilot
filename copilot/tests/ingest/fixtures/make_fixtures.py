"""Deterministic generator for Week-2 ingestion fixtures.

Produces synthetic, FICTIONAL clinical documents plus a machine-readable
ground_truth.json describing the exact expected extraction. No real patient
data (CLAUDE.md invariant). Output is byte-deterministic (reportlab
invariant mode) so the PDFs are commit-friendly.

Run:
    copilot/.venv/Scripts/python.exe copilot/tests/ingest/fixtures/make_fixtures.py

Generates, in this directory:
    lab_simple.pdf            text-layer Quest-style lab (TESTPATIENT ALPHA)
    intake_simple.pdf         one-page IM intake (TESTPATIENT ALPHA)
    lab_scanned.pdf           image-only render of the lab (NO text layer)
    demo_lab_delgado.pdf      demo lab for Marcus Delgado (pid 101)
    demo_intake_delgado.pdf   demo intake for Marcus Delgado (pid 101)
    medlist_simple.pdf        pharmacy-printout med list (TESTPATIENT ALPHA)
    demo_medlist_delgado.pdf  demo med list for Marcus Delgado (pid 101)
    ground_truth.json         expected extraction for every fixture
"""
from __future__ import annotations

import io
import json
import os

import pypdfium2 as pdfium
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_W, PAGE_H = letter

# ---------------------------------------------------------------------------
# Fixture data (single source of truth; also emitted to ground_truth.json)
# ---------------------------------------------------------------------------

LAB_SIMPLE = {
    "patient_name": "TESTPATIENT, ALPHA",
    "dob": "1970-01-01",
    "sex": "M",
    "patient_id": "TP-ALPHA-0001",
    "specimen_id": "742-118-4460-0",
    "account": "90000123",
    "ordering_provider": "CHEN, SARAH, MD",
    "date_collected": "2026-07-10",
    "date_received": "2026-07-10",
    "date_reported": "2026-07-11",
    "fasting": "Y",
    "panels": [
        {
            "panel": "Comprehensive Metabolic Panel (CMP)",
            "analytes": [
                # field_id, test_name, value, unit, reference_range, flag
                ("cmp_glucose", "Glucose", "142", "mg/dL", "70-99", "H"),
                ("cmp_bun", "Urea Nitrogen (BUN)", "15", "mg/dL", "7-20", ""),
                ("cmp_creatinine", "Creatinine", "0.9", "mg/dL", "0.60-1.30", ""),
                ("cmp_egfr", "eGFR", "95", "mL/min/1.73", ">=60", ""),
                ("cmp_sodium", "Sodium", "139", "mmol/L", "135-145", ""),
                ("cmp_potassium", "Potassium", "4.2", "mmol/L", "3.5-5.1", ""),
                ("cmp_chloride", "Chloride", "102", "mmol/L", "98-107", ""),
                ("cmp_co2", "Carbon Dioxide (CO2)", "26", "mmol/L", "22-29", ""),
                ("cmp_calcium", "Calcium", "9.4", "mg/dL", "8.6-10.2", ""),
            ],
        },
        {
            "panel": "Hemoglobin A1c",
            "analytes": [
                ("a1c", "Hemoglobin A1c", "8.4", "%", "4.0-5.6", "H"),
            ],
        },
    ],
}

LAB_DELGADO = {
    "patient_name": "DELGADO, MARCUS",
    "dob": "1961-04-12",
    "sex": "M",
    "patient_id": "SHOW-A",
    "specimen_id": "553-204-7788-0",
    "account": "90000123",
    "ordering_provider": "CHEN, SARAH, MD",
    "date_collected": "2026-07-12",
    "date_received": "2026-07-12",
    "date_reported": "2026-07-13",
    "fasting": "Y",
    "panels": [
        {
            "panel": "Comprehensive Metabolic Panel (CMP)",
            "analytes": [
                ("cmp_glucose", "Glucose", "168", "mg/dL", "70-99", "H"),
                ("cmp_bun", "Urea Nitrogen (BUN)", "22", "mg/dL", "7-20", "H"),
                ("cmp_creatinine", "Creatinine", "1.42", "mg/dL", "0.60-1.30", "H"),
                ("cmp_egfr", "eGFR", "58", "mL/min/1.73", ">=60", "L"),
                ("cmp_sodium", "Sodium", "140", "mmol/L", "135-145", ""),
                ("cmp_potassium", "Potassium", "4.6", "mmol/L", "3.5-5.1", ""),
                ("cmp_chloride", "Chloride", "104", "mmol/L", "98-107", ""),
                ("cmp_co2", "Carbon Dioxide (CO2)", "24", "mmol/L", "22-29", ""),
                ("cmp_calcium", "Calcium", "9.5", "mg/dL", "8.6-10.2", ""),
            ],
        },
        {
            "panel": "Hemoglobin A1c",
            "analytes": [
                ("a1c", "Hemoglobin A1c", "8.6", "%", "4.0-5.6", "H"),
            ],
        },
        {
            "panel": "Urine Albumin/Creatinine Ratio",
            "analytes": [
                ("urine_acr", "Albumin/Creatinine Ratio, Urine", "68", "mg/g", "<30", "H"),
            ],
        },
    ],
}

INTAKE_SIMPLE = {
    "patient_name": "TESTPATIENT, ALPHA",
    "dob": "1970-01-01",
    "sex": "M",
    "address": "100 Cedar Lane, Springfield, IL 62704",
    "phone": "(217) 555-0100",
    "emergency_contact": "BETA TESTPATIENT, spouse, (217) 555-0101",
    "chief_concern": "increased thirst and fatigue",
    "medications": [
        ("med_1", "Metformin", "1000 mg", "twice daily"),
        ("med_2", "Lisinopril", "10 mg", "once daily"),
        ("med_3", "Atorvastatin", "20 mg", "once daily at bedtime"),
    ],
    "allergies": [
        ("allergy_1", "Penicillin", "hives"),
        ("allergy_2", "Sulfa drugs", "rash"),
    ],
    "family_history": [
        ("fh_father", "Father", "Type 2 diabetes mellitus"),
        ("fh_mother", "Mother", "Hypertension"),
    ],
    "social_history": {
        "tobacco": "Former smoker, quit 2015",
        "alcohol": "2 drinks per week",
        "occupation": "Warehouse supervisor",
        "exercise": "Walks 20 minutes, 3 days per week",
    },
    "ros": "Positive: increased thirst, fatigue. Negative: chest pain, "
           "shortness of breath, fever, dysuria.",
}

INTAKE_DELGADO = {
    "patient_name": "DELGADO, MARCUS",
    "dob": "1961-04-12",
    "sex": "M",
    "address": "482 Maplewood Ave, Springfield, IL 62704",
    "phone": "(217) 555-0181",
    "emergency_contact": "ROSA DELGADO, spouse, (217) 555-0182",
    "chief_concern": "increased thirst and fatigue",
    "medications": [
        ("med_1", "Metformin", "1000 mg", "twice daily"),
        ("med_2", "Lisinopril", "10 mg", "once daily"),
        ("med_3", "Atorvastatin", "40 mg", "once daily at bedtime"),
    ],
    "allergies": [
        ("allergy_1", "Sulfa drugs", "rash"),
        ("allergy_2", "Shellfish", "hives"),
    ],
    "family_history": [
        ("fh_father", "Father", "Type 2 diabetes mellitus"),
        ("fh_mother", "Mother", "Hypertension"),
    ],
    "social_history": {
        "tobacco": "Never smoker",
        "alcohol": "Rare, social only",
        "occupation": "Retired postal worker",
        "exercise": "Walks the dog daily",
    },
    "ros": "Positive: increased thirst, fatigue, nocturia. Negative: chest "
           "pain, shortness of breath, fever, visual changes.",
}


# Medication lists (third doc type, T4). Row tuples:
# (field_id, name, strength, route, frequency, status, prescriber)
# field_id is the extractor's deterministic slug of the name (unique names here).
# frequency may be "" (a deliberate gap-case row); status "Discontinued"
# exercises activity=0 on persist.

MEDLIST_SIMPLE = {
    "patient_name": "TESTPATIENT, ALPHA",
    "dob": "1970-01-01",
    "list_date": "2026-07-10",
    "pharmacy": "SPRINGFIELD COMMUNITY PHARMACY",
    "medications": [
        ("metformin", "Metformin", "1000 mg", "oral", "BID", "Active", "Chen"),
        ("lisinopril", "Lisinopril", "10 mg", "oral", "once daily", "Active", "Chen"),
        ("atorvastatin", "Atorvastatin", "20 mg", "oral", "QHS", "Active", "Chen"),
        ("sertraline", "Sertraline", "100 mg", "oral", "once daily", "Active", "Chen"),
    ],
}

MEDLIST_DELGADO = {
    "patient_name": "DELGADO, MARCUS",
    "dob": "1961-04-12",
    "list_date": "2026-07-12",
    "pharmacy": "SPRINGFIELD COMMUNITY PHARMACY",
    # Consistent with INTAKE_DELGADO current meds, PLUS a Discontinued row
    # (activity=0 path) and a row with NO frequency printed (gap case).
    "medications": [
        ("metformin", "Metformin", "1000 mg", "oral", "twice daily", "Active", "Chen"),
        ("lisinopril", "Lisinopril", "10 mg", "oral", "once daily", "Active", "Chen"),
        ("atorvastatin", "Atorvastatin", "40 mg", "oral", "once daily at bedtime", "Active", "Chen"),
        ("glipizide", "Glipizide", "5 mg", "oral", "once daily", "Discontinued", "Chen"),
        ("aspirin", "Aspirin", "81 mg", "oral", "", "Active", "Chen"),
    ],
}


def medlist_row_line(med: tuple) -> str:
    """The EXACT single text-layer line for one med row: the space-joined
    non-empty columns. Drawn with ONE drawString so verbatim-quote
    reconstruction (tests/_helpers.medlist_row_quote) byte-matches."""
    _fid, name, strength, route, freq, status, prescriber = med
    return " ".join(p for p in (name, strength, route, freq, status, prescriber) if p)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _new_canvas(path_or_buf) -> canvas.Canvas:
    # invariant=True => deterministic file id + fixed timestamps (commit-friendly).
    c = canvas.Canvas(path_or_buf, pagesize=letter, invariant=True)
    c.setTitle("Synthetic Fixture")
    c.setAuthor("make_fixtures")
    c.setSubject("Week 2 ingestion fixture (synthetic)")
    return c


def draw_lab(c: canvas.Canvas, data: dict) -> None:
    left = 0.75 * inch
    right = PAGE_W - 0.75 * inch
    y = PAGE_H - 0.75 * inch

    c.setFont("Helvetica-Bold", 15)
    c.drawString(left, y, "SPRINGFIELD REFERENCE LABORATORY")
    y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(left, y, "1200 Analyte Way, Springfield, IL 62704  |  CLIA# 14D0000000  |  (800) 555-0000")
    y -= 10
    c.drawString(left, y, "*** SYNTHETIC DEMO REPORT - NOT A REAL PATIENT ***")
    y -= 14
    c.line(left, y, right, y)
    y -= 16

    # Header block (two columns)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, "Patient Report")
    c.setFont("Helvetica", 9)
    col2 = left + 3.4 * inch
    rows = [
        (f"Patient: {data['patient_name']}", f"Specimen ID: {data['specimen_id']}"),
        (f"DOB: {data['dob']}    Sex: {data['sex']}", f"Acct #: {data['account']}"),
        (f"Patient ID: {data['patient_id']}", f"Fasting: {data['fasting']}"),
        (f"Ordering Physician: {data['ordering_provider']}", ""),
        (f"Date Collected: {data['date_collected']}", f"Date Received: {data['date_received']}"),
        (f"Date Reported: {data['date_reported']}", ""),
    ]
    y -= 14
    for l_txt, r_txt in rows:
        c.drawString(left, y, l_txt)
        if r_txt:
            c.drawString(col2, y, r_txt)
        y -= 12
    y -= 4
    c.line(left, y, right, y)
    y -= 16

    # Column headers
    x_test = left
    x_result = left + 2.7 * inch
    x_flag = left + 3.5 * inch
    x_units = left + 4.1 * inch
    x_ref = left + 5.2 * inch
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x_test, y, "TEST")
    c.drawString(x_result, y, "RESULT")
    c.drawString(x_flag, y, "FLAG")
    c.drawString(x_units, y, "UNITS")
    c.drawString(x_ref, y, "REFERENCE INTERVAL")
    y -= 4
    c.line(left, y, right, y)
    y -= 14

    for panel in data["panels"]:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x_test, y, panel["panel"])
        y -= 13
        c.setFont("Helvetica", 9)
        for _fid, name, value, unit, ref, flag in panel["analytes"]:
            c.drawString(x_test, y, name)
            c.drawString(x_result, y, value)
            if flag:
                c.setFont("Helvetica-Bold", 9)
                c.drawString(x_flag, y, flag)
                c.setFont("Helvetica", 9)
            c.drawString(x_units, y, unit)
            c.drawString(x_ref, y, ref)
            y -= 12
        y -= 4

    y -= 6
    c.line(left, y, right, y)
    y -= 14
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(left, y, "Flag key: H = above reference interval, L = below reference interval.")
    y -= 10
    c.drawString(left, y, "End of report. Synthetic data generated for testing only.")
    c.showPage()


def _wrap(c: canvas.Canvas, text: str, x: float, y: float, width: float,
          font: str = "Helvetica", size: int = 9, leading: int = 12) -> float:
    c.setFont(font, size)
    words = text.split()
    line = ""
    for w in words:
        trial = (line + " " + w).strip()
        if c.stringWidth(trial, font, size) > width:
            c.drawString(x, y, line)
            y -= leading
            line = w
        else:
            line = trial
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def draw_intake(c: canvas.Canvas, data: dict) -> None:
    left = 0.75 * inch
    right = PAGE_W - 0.75 * inch
    width = right - left
    y = PAGE_H - 0.75 * inch

    c.setFont("Helvetica-Bold", 15)
    c.drawString(left, y, "SPRINGFIELD INTERNAL MEDICINE ASSOCIATES")
    y -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "New Patient Intake Form")
    c.setFont("Helvetica", 8)
    c.drawRightString(right, y, "*** SYNTHETIC DEMO - NOT A REAL PATIENT ***")
    y -= 12
    c.line(left, y, right, y)
    y -= 16

    def section(title: str, yy: float) -> float:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(left, yy, title)
        return yy - 14

    def field(label: str, value: str, yy: float) -> float:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(left, yy, label)
        c.setFont("Helvetica", 9)
        c.drawString(left + 1.7 * inch, yy, value)
        return yy - 13

    # Demographics
    y = section("Demographics", y)
    y = field("Name:", data["patient_name"], y)
    y = field("Date of Birth:", f"{data['dob']}    Sex: {data['sex']}", y)
    y = field("Address:", data["address"], y)
    y = field("Phone:", data["phone"], y)
    y = field("Emergency Contact:", data["emergency_contact"], y)
    y -= 6

    # Chief concern
    y = section("Reason for Visit / Chief Concern", y)
    c.setFont("Helvetica", 9)
    c.drawString(left, y, data["chief_concern"])
    y -= 18

    # Medications
    y = section("Current Medications", y)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "Medication")
    c.drawString(left + 2.3 * inch, y, "Dose")
    c.drawString(left + 3.5 * inch, y, "Frequency")
    y -= 12
    c.setFont("Helvetica", 9)
    for _fid, name, dose, freq in data["medications"]:
        c.drawString(left, y, name)
        c.drawString(left + 2.3 * inch, y, dose)
        c.drawString(left + 3.5 * inch, y, freq)
        y -= 12
    y -= 6

    # Allergies
    y = section("Allergies", y)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "Substance")
    c.drawString(left + 2.3 * inch, y, "Reaction")
    y -= 12
    c.setFont("Helvetica", 9)
    for _fid, sub, reaction in data["allergies"]:
        c.drawString(left, y, sub)
        c.drawString(left + 2.3 * inch, y, reaction)
        y -= 12
    y -= 6

    # Family history
    y = section("Family History", y)
    c.setFont("Helvetica", 9)
    for _fid, rel, cond in data["family_history"]:
        c.drawString(left, y, f"{rel}: {cond}")
        y -= 12
    y -= 6

    # Social history
    y = section("Social History", y)
    sh = data["social_history"]
    c.setFont("Helvetica", 9)
    c.drawString(left, y, f"Tobacco: {sh['tobacco']}")
    y -= 12
    c.drawString(left, y, f"Alcohol: {sh['alcohol']}")
    y -= 12
    c.drawString(left, y, f"Occupation: {sh['occupation']}")
    y -= 12
    c.drawString(left, y, f"Exercise: {sh['exercise']}")
    y -= 18

    # ROS
    y = section("Review of Systems", y)
    y = _wrap(c, data["ros"], left, y, width)
    y -= 10

    c.setFont("Helvetica-Oblique", 8)
    c.drawString(left, y, "Insurance section intentionally omitted for this synthetic fixture.")
    c.showPage()


def draw_medlist(c: canvas.Canvas, data: dict) -> None:
    """Pharmacy-printout-style medication list. Every med row is drawn with ONE
    single drawString (the space-joined columns) so the text layer contains the
    row line verbatim — verbatim-quote verification depends on that."""
    left = 0.75 * inch
    right = PAGE_W - 0.75 * inch
    y = PAGE_H - 0.75 * inch

    c.setFont("Helvetica-Bold", 15)
    c.drawString(left, y, data["pharmacy"])
    y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(left, y, "800 Dispensary Rd, Springfield, IL 62704  |  (800) 555-0042")
    y -= 10
    c.drawString(left, y, "*** SYNTHETIC DEMO - NOT A REAL PATIENT ***")
    y -= 14
    c.line(left, y, right, y)
    y -= 16

    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Current Medication List")
    y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(left, y, f"Patient: {data['patient_name']}")
    y -= 12
    c.drawString(left, y, f"DOB: {data['dob']}")
    y -= 12
    c.drawString(left, y, f"List Date: {data['list_date']}")
    y -= 8
    c.line(left, y, right, y)
    y -= 16

    # Column header (label row only — headers are never quoted by the extractor)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "MEDICATION | STRENGTH | ROUTE | FREQUENCY | STATUS | PRESCRIBER")
    y -= 4
    c.line(left, y, right, y)
    y -= 14

    c.setFont("Helvetica", 9)
    for med in data["medications"]:
        # ONE drawString per row — the whole row is a single text-layer line.
        c.drawString(left, y, medlist_row_line(med))
        y -= 13

    y -= 8
    c.line(left, y, right, y)
    y -= 14
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(left, y, "Patient-reported / pharmacy-reported list. Not a prescriber order record.")
    y -= 10
    c.drawString(left, y, "End of list. Synthetic data generated for testing only.")
    c.showPage()


# ---------------------------------------------------------------------------
# PDF builders
# ---------------------------------------------------------------------------

def build_medlist_pdf(path: str, data: dict) -> None:
    c = _new_canvas(path)
    draw_medlist(c, data)
    c.save()


def build_lab_pdf(path: str, data: dict) -> None:
    c = _new_canvas(path)
    draw_lab(c, data)
    c.save()


def build_intake_pdf(path: str, data: dict) -> None:
    c = _new_canvas(path)
    draw_intake(c, data)
    c.save()


def build_scanned_pdf(src_data: dict, out_path: str) -> None:
    """Render the lab report to a raster image, then wrap the image in a PDF
    with NO text layer (image-only, like a scan)."""
    # 1) build the text-layer lab in memory
    buf = io.BytesIO()
    c = _new_canvas(buf)
    draw_lab(c, src_data)
    c.save()
    buf.seek(0)

    # 2) rasterize page 0 to a PNG (deterministic: fixed scale)
    pdf = pdfium.PdfDocument(buf.getvalue())
    page = pdf[0]
    bitmap = page.render(scale=2.0)
    pil_image = bitmap.to_pil().convert("RGB")
    png_buf = io.BytesIO()
    pil_image.save(png_buf, format="PNG")
    png_buf.seek(0)
    page.close()
    pdf.close()

    # 3) wrap the image in a fresh PDF, drawing ONLY the image (no text ops)
    from reportlab.lib.utils import ImageReader
    c2 = _new_canvas(out_path)
    img = ImageReader(png_buf)
    c2.drawImage(img, 0, 0, width=PAGE_W, height=PAGE_H, preserveAspectRatio=True,
                 anchor="c")
    c2.showPage()
    c2.save()


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def _lab_ground_truth(data: dict) -> list[dict]:
    fields = []
    for panel in data["panels"]:
        for fid, name, value, unit, ref, flag in panel["analytes"]:
            fields.append({
                "field_id": fid,
                "test_name": name,
                "value": value,
                "unit": unit,
                "reference_range": ref,
                "abnormal_flag": flag,
                "page": 1,
            })
    return fields


def _intake_ground_truth(data: dict) -> dict:
    meds = [{"field_id": fid, "name": n, "dose": d, "frequency": f, "page": 1}
            for fid, n, d, f in data["medications"]]
    allergies = [{"field_id": fid, "substance": s, "reaction": r, "page": 1}
                 for fid, s, r in data["allergies"]]
    fh = [{"field_id": fid, "relation": rel, "condition": cond, "page": 1}
          for fid, rel, cond in data["family_history"]]
    return {
        "demographics": {
            "patient_name": data["patient_name"],
            "dob": data["dob"],
            "sex": data["sex"],
            "page": 1,
        },
        "chief_concern": {"value": data["chief_concern"], "page": 1},
        "medications": meds,
        "allergies": allergies,
        "family_history": fh,
    }


def _medlist_ground_truth(data: dict) -> dict:
    meds = [
        {
            "field_id": fid,
            "name": name,
            "strength": strength,
            "route": route,
            "frequency": freq,
            "status": status,
            "prescriber": prescriber,
            "page": 1,
        }
        for fid, name, strength, route, freq, status, prescriber in data["medications"]
    ]
    return {
        "patient_name": data["patient_name"],
        "dob": data["dob"],
        "list_date": data["list_date"],
        "medications": meds,
    }


def build_ground_truth() -> dict:
    return {
        "_note": "Synthetic fixtures for Week-2 ingestion golden extraction. "
                 "Fictional patients only. Values are the exact strings a "
                 "correct extractor must return.",
        "lab_simple.pdf": {
            "doc_type": "lab_report",
            "has_text_layer": True,
            "patient_name": LAB_SIMPLE["patient_name"],
            "dob": LAB_SIMPLE["dob"],
            "specimen_id": LAB_SIMPLE["specimen_id"],
            "date_collected": LAB_SIMPLE["date_collected"],
            "date_received": LAB_SIMPLE["date_received"],
            "date_reported": LAB_SIMPLE["date_reported"],
            "ordering_provider": LAB_SIMPLE["ordering_provider"],
            "abnormal_flags_expected": ["cmp_glucose", "a1c"],
            "analytes": _lab_ground_truth(LAB_SIMPLE),
        },
        "intake_simple.pdf": {
            "doc_type": "intake_form",
            "has_text_layer": True,
            **_intake_ground_truth(INTAKE_SIMPLE),
        },
        "lab_scanned.pdf": {
            "doc_type": "lab_report",
            "has_text_layer": False,
            "note": "Image-only render of the lab_simple content. Extractor "
                    "must return needs_review (no text-layer verification "
                    "possible); values below describe the depicted content.",
            "patient_name": LAB_SIMPLE["patient_name"],
            "date_collected": LAB_SIMPLE["date_collected"],
            "abnormal_flags_expected": ["cmp_glucose", "a1c"],
            "analytes": _lab_ground_truth(LAB_SIMPLE),
        },
        "demo_lab_delgado.pdf": {
            "doc_type": "lab_report",
            "has_text_layer": True,
            "pid": 101,
            "patient_name": LAB_DELGADO["patient_name"],
            "dob": LAB_DELGADO["dob"],
            "specimen_id": LAB_DELGADO["specimen_id"],
            "date_collected": LAB_DELGADO["date_collected"],
            "date_received": LAB_DELGADO["date_received"],
            "date_reported": LAB_DELGADO["date_reported"],
            "ordering_provider": LAB_DELGADO["ordering_provider"],
            "abnormal_flags_expected": [
                "cmp_glucose", "cmp_bun", "cmp_creatinine", "cmp_egfr",
                "a1c", "urine_acr",
            ],
            "analytes": _lab_ground_truth(LAB_DELGADO),
        },
        "demo_intake_delgado.pdf": {
            "doc_type": "intake_form",
            "has_text_layer": True,
            "pid": 101,
            **_intake_ground_truth(INTAKE_DELGADO),
        },
        "medlist_simple.pdf": {
            "doc_type": "medication_list",
            "has_text_layer": True,
            **_medlist_ground_truth(MEDLIST_SIMPLE),
        },
        "demo_medlist_delgado.pdf": {
            "doc_type": "medication_list",
            "has_text_layer": True,
            "pid": 101,
            **_medlist_ground_truth(MEDLIST_DELGADO),
        },
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def extract_text(path: str) -> str:
    pdf = pdfium.PdfDocument(path)
    parts = []
    for i in range(len(pdf)):
        page = pdf[i]
        tp = page.get_textpage()
        parts.append(tp.get_text_bounded())
        tp.close()
        page.close()
    pdf.close()
    return "\n".join(parts)


def verify() -> bool:
    ok = True
    print("\n=== VERIFICATION ===")

    # lab_simple: text layer must contain every analyte value
    text = extract_text(os.path.join(HERE, "lab_simple.pdf"))
    missing = []
    for panel in LAB_SIMPLE["panels"]:
        for _fid, name, value, _unit, _ref, _flag in panel["analytes"]:
            if value not in text:
                missing.append(f"{name}={value}")
    if missing:
        ok = False
        print(f"[FAIL] lab_simple.pdf missing values in text layer: {missing}")
    else:
        n = sum(len(p["analytes"]) for p in LAB_SIMPLE["panels"])
        print(f"[PASS] lab_simple.pdf text layer contains all {n} analyte values")

    # header strings present
    for token in (LAB_SIMPLE["patient_name"], LAB_SIMPLE["specimen_id"],
                  LAB_SIMPLE["date_collected"]):
        if token not in text:
            ok = False
            print(f"[FAIL] lab_simple.pdf text layer missing header token: {token}")

    # intake_simple: chief concern + meds present
    itext = extract_text(os.path.join(HERE, "intake_simple.pdf"))
    for token in [INTAKE_SIMPLE["chief_concern"]] + [m[1] for m in INTAKE_SIMPLE["medications"]]:
        if token not in itext:
            ok = False
            print(f"[FAIL] intake_simple.pdf missing token: {token}")
    if ok:
        print("[PASS] intake_simple.pdf contains chief concern and medications")

    # demo delgado lab: values present + is Delgado
    dtext = extract_text(os.path.join(HERE, "demo_lab_delgado.pdf"))
    d_missing = [f"{n}={v}" for p in LAB_DELGADO["panels"]
                 for _f, n, v, _u, _r, _fl in p["analytes"] if v not in dtext]
    if d_missing or "DELGADO, MARCUS" not in dtext:
        ok = False
        print(f"[FAIL] demo_lab_delgado.pdf issues: missing={d_missing}")
    else:
        print("[PASS] demo_lab_delgado.pdf text layer complete (Delgado, all analytes)")

    ditext = extract_text(os.path.join(HERE, "demo_intake_delgado.pdf"))
    if INTAKE_DELGADO["chief_concern"] not in ditext or "DELGADO, MARCUS" not in ditext:
        ok = False
        print("[FAIL] demo_intake_delgado.pdf missing chief concern or name")
    else:
        print("[PASS] demo_intake_delgado.pdf contains chief concern and name")

    # med lists: every row's SINGLE-drawString line must exist verbatim in the
    # text layer (the extractor's verbatim-quote contract), plus the header
    # tokens the scalar quotes reconstruct ("Patient: ...", "List Date: ...").
    for fname, data in (("medlist_simple.pdf", MEDLIST_SIMPLE),
                        ("demo_medlist_delgado.pdf", MEDLIST_DELGADO)):
        mtext = extract_text(os.path.join(HERE, fname))
        m_missing = [medlist_row_line(m) for m in data["medications"]
                     if medlist_row_line(m) not in mtext]
        for token in (f"Patient: {data['patient_name']}",
                      f"List Date: {data['list_date']}"):
            if token not in mtext:
                m_missing.append(token)
        if m_missing:
            ok = False
            print(f"[FAIL] {fname} missing verbatim lines: {m_missing}")
        else:
            print(f"[PASS] {fname} text layer contains all "
                  f"{len(data['medications'])} med rows verbatim + header tokens")

    # lab_scanned: text layer MUST be empty (image-only)
    stext = extract_text(os.path.join(HERE, "lab_scanned.pdf")).strip()
    if stext:
        ok = False
        print(f"[FAIL] lab_scanned.pdf has a text layer ({len(stext)} chars); expected empty")
    else:
        print("[PASS] lab_scanned.pdf has NO text layer (image-only, as intended)")

    # every PDF opens and reports page count
    print("\n--- PDF open check ---")
    for name in ("lab_simple.pdf", "intake_simple.pdf", "lab_scanned.pdf",
                 "demo_lab_delgado.pdf", "demo_intake_delgado.pdf",
                 "medlist_simple.pdf", "demo_medlist_delgado.pdf"):
        p = os.path.join(HERE, name)
        pdf = pdfium.PdfDocument(p)
        pages = len(pdf)
        pdf.close()
        size = os.path.getsize(p)
        print(f"  {name}: {pages} page(s), {size:,} bytes")

    print("\n=== {} ===".format("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return ok


def main() -> None:
    build_lab_pdf(os.path.join(HERE, "lab_simple.pdf"), LAB_SIMPLE)
    build_intake_pdf(os.path.join(HERE, "intake_simple.pdf"), INTAKE_SIMPLE)
    build_scanned_pdf(LAB_SIMPLE, os.path.join(HERE, "lab_scanned.pdf"))
    build_lab_pdf(os.path.join(HERE, "demo_lab_delgado.pdf"), LAB_DELGADO)
    build_intake_pdf(os.path.join(HERE, "demo_intake_delgado.pdf"), INTAKE_DELGADO)
    build_medlist_pdf(os.path.join(HERE, "medlist_simple.pdf"), MEDLIST_SIMPLE)
    build_medlist_pdf(os.path.join(HERE, "demo_medlist_delgado.pdf"), MEDLIST_DELGADO)

    with open(os.path.join(HERE, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(build_ground_truth(), f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("Generated fixtures in", HERE)
    ok = verify()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
