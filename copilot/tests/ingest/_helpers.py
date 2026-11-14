"""Shared helpers for the hermetic ingestion tests (no API, no MariaDB).

- SQLite mini-DDL mirroring the columns the writer touches, with an ATTACHed
  `copilot` schema so production-qualified table names run verbatim;
- `StubVLM`: a scripted Anthropic stand-in returning canned forced-tool
  payloads;
- `lab_payload()` / `intake_payload()`: payloads whose verbatim quotes are
  REAL lines from the fixture PDFs' text layers, so deterministic verification
  is exercised against the true fixtures, not a mock of itself.

Fixtures (`ingest_db`, `ingest_settings`) live in conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).parent / "fixtures"

GROUND_TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))

LAB_SIMPLE_PDF = (FIXTURES / "lab_simple.pdf").read_bytes()
INTAKE_SIMPLE_PDF = (FIXTURES / "intake_simple.pdf").read_bytes()
LAB_SCANNED_PDF = (FIXTURES / "lab_scanned.pdf").read_bytes()
MEDLIST_SIMPLE_PDF = (FIXTURES / "medlist_simple.pdf").read_bytes()

DDL = [
    # --- minimal OpenEMR shapes (columns the writer touches) -------------------
    """CREATE TABLE documents (
        id INTEGER PRIMARY KEY, type TEXT, size INTEGER, date TEXT, url TEXT,
        mimetype TEXT, docdate TEXT, hash TEXT, name TEXT, foreign_id INTEGER,
        storagemethod INTEGER DEFAULT 0, revision TEXT, document_data TEXT,
        deleted INTEGER DEFAULT 0)""",
    "CREATE TABLE sequences (id INTEGER NOT NULL)",
    "INSERT INTO sequences VALUES (1000)",
    """CREATE TABLE procedure_order (
        procedure_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER DEFAULT 0, patient_id INTEGER,
        encounter_id INTEGER DEFAULT 0, date_collected TEXT, date_ordered TEXT,
        order_status TEXT, activity INTEGER DEFAULT 1, procedure_order_type TEXT)""",
    """CREATE TABLE procedure_order_code (
        procedure_order_id INTEGER, procedure_order_seq INTEGER,
        procedure_code TEXT, procedure_name TEXT)""",
    """CREATE TABLE procedure_report (
        procedure_report_id INTEGER PRIMARY KEY AUTOINCREMENT,
        procedure_order_id INTEGER, procedure_order_seq INTEGER DEFAULT 1,
        date_collected TEXT, date_report TEXT, report_status TEXT,
        review_status TEXT DEFAULT 'received', report_notes TEXT)""",
    """CREATE TABLE procedure_result (
        procedure_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        procedure_report_id INTEGER, result_data_type TEXT DEFAULT 'S',
        result_code TEXT DEFAULT '', result_text TEXT, date TEXT,
        units TEXT DEFAULT '', result TEXT DEFAULT '', `range` TEXT DEFAULT '',
        abnormal TEXT DEFAULT '', comments TEXT, document_id INTEGER DEFAULT 0,
        result_status TEXT DEFAULT '')""",
    """CREATE TABLE lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, type TEXT, title TEXT,
        begdate TEXT, activity INTEGER, comments TEXT, pid INTEGER, user TEXT,
        reaction TEXT DEFAULT '')""",
    """CREATE TABLE patient_data (
        pid INTEGER PRIMARY KEY, fname TEXT, lname TEXT, DOB TEXT,
        phone_home TEXT, providerID INTEGER)""",
    # chart identity for pid 101 (Marcus Delgado, the showcase patient)
    """INSERT INTO patient_data (pid, fname, lname, DOB, phone_home, providerID)
       VALUES (101, 'Marcus', 'Delgado', '1961-04-12', '(217) 555-0181', 1)""",
    # --- sidecar-owned copilot schema (mirrors the seeder DDL) ------------------
    """CREATE TABLE copilot.ingestion_run (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT,
        pid INTEGER, doc_type TEXT, content_hash TEXT, document_id INTEGER,
        status TEXT, prompt_version TEXT, fields_total INTEGER,
        fields_extracted INTEGER, fields_not_found INTEGER,
        fields_failed_verification INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (content_hash, pid))""",
    """CREATE TABLE copilot.provenance (
        provenance_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
        document_id INTEGER, field_id TEXT, target_table TEXT,
        target_pk INTEGER, page INTEGER, bbox_x0 REAL, bbox_y0 REAL,
        bbox_x1 REAL, bbox_y1 REAL, bbox_origin TEXT, confidence REAL,
        quote TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (document_id, field_id))""",
    """CREATE TABLE copilot.intake_fact (
        fact_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
        document_id INTEGER, pid INTEGER, kind TEXT, field_id TEXT,
        value TEXT, chart_value TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
]


def count(engine, table: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) AS n FROM {table}")).scalar() or 0)


# ---- scripted VLM -------------------------------------------------------------


class StubVLM:
    """Anthropic stand-in: `client.messages.create` returns scripted responses
    in order (last one repeats). Counts calls for retry assertions.

    A payload dict → a forced-tool `submit_extraction` reply with the FLAT
    schema shape the extractor now expects. A `None` entry → a reply with NO
    tool_use block (a text-only response), which drives the extractor's
    retry-on-invalid path (`no tool_use block in response`)."""

    def __init__(self, payloads: list[dict | None]) -> None:
        self._payloads = list(payloads)
        self.calls = 0
        self.requests: list[dict] = []  # kwargs of every create() call (prompt asserts)
        self.messages = self  # so `client.messages.create(...)` resolves here

    def create(self, **_kw):
        self.requests.append(_kw)
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        if payload is None:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="I cannot fill that.")],
                stop_reason="end_turn",
            )
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id=f"tu_{self.calls}",
                    name="submit_extraction", input=payload,
                )
            ],
            stop_reason="tool_use",
        )


# ---- canned FLAT payload builders (quotes = REAL fixture text-layer lines) ------


def _sf(value, quote, page=1):
    """A flat scalar field {value, verbatim_quote, page}."""
    return {"value": value, "verbatim_quote": quote, "page": page}


def lab_row_quote(a: dict) -> str:
    """Reconstruct the text-layer row line for one ground-truth analyte:
    'Glucose 142 H mg/dL 70-99' / 'Sodium 139 mmol/L 135-145'."""
    parts = [a["test_name"], a["value"]]
    if a["abnormal_flag"]:
        parts.append(a["abnormal_flag"])
    parts += [a["unit"], a["reference_range"]]
    return " ".join(parts)


def lab_payload(fixture: str = "lab_simple.pdf") -> dict:
    """A fully correct FLAT forced-tool payload for a lab fixture (the
    well-behaved VLM). Every quote exists in the real text layer."""
    gt = GROUND_TRUTH[fixture]
    values = []
    for a in gt["analytes"]:
        quote = lab_row_quote(a)
        values.append(
            {
                "test_name": a["test_name"],
                "value": a["value"],
                "unit": a["unit"],
                "reference_range": a["reference_range"],
                "abnormal_flag": a["abnormal_flag"] or "",
                "verbatim_quote": quote,
                "page": 1,
            }
        )
    return {
        "patient_name": _sf(gt["patient_name"], f"Patient: {gt['patient_name']}"),
        "collection_date": _sf(
            gt["date_collected"], f"Date Collected: {gt['date_collected']}"
        ),
        "lab_values": values,
    }


def medlist_row_quote(m: dict) -> str:
    """Reconstruct the text-layer row line for one ground-truth med: the
    space-joined non-empty columns, byte-matching the fixture's single
    drawString per row (make_fixtures.medlist_row_line), e.g.
    'Metformin 1000 mg oral BID Active Chen'."""
    parts = [m["name"], m["strength"], m["route"], m["frequency"], m["status"],
             m["prescriber"]]
    return " ".join(p for p in parts if p)


def medlist_payload(fixture: str = "medlist_simple.pdf") -> dict:
    """A fully correct FLAT forced-tool payload for a med-list fixture (the
    well-behaved VLM). Every quote exists verbatim in the real text layer."""
    gt = GROUND_TRUTH[fixture]
    meds = []
    for m in gt["medications"]:
        quote = medlist_row_quote(m)
        meds.append(
            {
                "name": m["name"],
                "strength": m["strength"] or "",
                "route": m["route"] or "",
                "frequency": m["frequency"] or "",
                "status": m["status"] or "",
                "prescriber": m["prescriber"] or "",
                "verbatim_quote": quote,
                "page": 1,
            }
        )
    return {
        "patient_name": _sf(gt["patient_name"], f"Patient: {gt['patient_name']}"),
        "list_date": _sf(gt["list_date"], f"List Date: {gt['list_date']}"),
        "medications": meds,
    }


def intake_payload(fixture: str = "intake_simple.pdf") -> dict:
    gt = GROUND_TRUTH[fixture]
    demo = gt["demographics"]
    meds = []
    for m in gt["medications"]:
        quote = f"{m['name']} {m['dose']} {m['frequency']}"
        meds.append({"name": m["name"], "detail": f"{m['dose']} {m['frequency']}",
                     "verbatim_quote": quote, "page": 1})
    allergies = []
    for a in gt["allergies"]:
        quote = f"{a['substance']} {a['reaction']}"
        allergies.append({"name": a["substance"], "detail": a["reaction"],
                          "verbatim_quote": quote, "page": 1})
    # family history: the first line exactly as printed on the form (value must
    # sit inside its own quote to verify)
    fh_line = f"{gt['family_history'][0]['relation']}: {gt['family_history'][0]['condition']}"
    return {
        "patient_name": _sf(demo["patient_name"], f"Name: {demo['patient_name']}"),
        "date_of_birth": _sf(demo["dob"], f"Date of Birth: {demo['dob']}"),
        "chief_concern": _sf(gt["chief_concern"]["value"], gt["chief_concern"]["value"]),
        "family_history": _sf(fh_line, fh_line),
        "medications": meds,
        "allergies": allergies,
    }
