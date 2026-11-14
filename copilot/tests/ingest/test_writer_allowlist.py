"""writer: the in-code allowlist — the second, independent enforcement of the
least-privilege boundary (DB grants being the first).

Failure mode guarded: a future code path (or a bug) tries to write outside the
sanctioned surface — UPDATE a clinical row, DELETE anything, touch
patient_data — and the DB grant alone is the only thing stopping it. The
in-code chokepoint must reject these regardless of what the DB would allow
(in these tests SQLite would happily allow everything — which is exactly why
the code-level rejection is what's being proven).
"""

from __future__ import annotations

import pytest

from app.ingest import writer
from app.ingest.writer import WriterViolation


def test_insert_into_disallowed_tables_rejected(ingest_db, ingest_settings):
    with ingest_db.connect() as conn:
        with pytest.raises(WriterViolation):
            writer.insert_row(conn, "patient_data", {"pid": 999})
        with pytest.raises(WriterViolation):
            writer.insert_row(conn, "users", {"id": 1})
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "INSERT INTO prescriptions (id) VALUES (1)")


def test_update_and_delete_on_clinical_tables_rejected(ingest_db, ingest_settings):
    with ingest_db.connect() as conn:
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "UPDATE lists SET title = 'x' WHERE id = 1")
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "UPDATE procedure_result SET result = '1'")
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "DELETE FROM lists WHERE id = 1")
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "DELETE FROM documents")
        with pytest.raises(WriterViolation):
            writer.execute_write(conn, "DROP TABLE lists")


def test_multiple_statements_rejected(ingest_db, ingest_settings):
    with ingest_db.connect() as conn:
        with pytest.raises(WriterViolation):
            writer.execute_write(
                conn, "INSERT INTO lists (pid) VALUES (1); DELETE FROM lists"
            )


def test_read_rows_rejects_non_select(ingest_db, ingest_settings):
    with ingest_db.connect() as conn:
        with pytest.raises(WriterViolation):
            writer.read_rows(conn, "DELETE FROM lists")


def test_documents_fallback_requires_flag(ingest_db, ingest_settings, monkeypatch):
    monkeypatch.setattr(ingest_settings, "ingest_db_doc_fallback", False)
    with ingest_db.begin() as conn:
        with pytest.raises(WriterViolation):
            writer.store_document_fallback(
                conn, 101, b"%PDF-1.4", "application/pdf", "ab" * 64, "lab_report"
            )


def test_invalid_column_identifier_rejected(ingest_db, ingest_settings):
    with ingest_db.connect() as conn:
        with pytest.raises(WriterViolation):
            writer.insert_row(conn, "lists", {"pid); DROP TABLE lists;--": 1})
