"""Document ingestion (Week 2) — deterministic pipeline, never a model tool.

The LLM/VLM proposes extractions; deterministic code validates, verifies
against the PDF text layer, and persists (invariant: the model never writes).
Contracts live in `schemas.py` and are the source of truth for every module
in this package (pdf_text, extractor, verify_extraction, writer, ...).
"""
