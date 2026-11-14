"""Export the FastAPI-generated OpenAPI 3.x spec to a committed file.

The spec is NEVER hand-maintained: FastAPI derives it from the live route table
and the Pydantic request/response models, so `copilot/openapi.json` is a pure
projection of `app.main:app`. A drift guard (`copilot/tests/test_openapi_contract.py`)
regenerates the spec in-memory on every test run and fails if it diverges from
the committed file — implementation and spec cannot drift silently
(W2_ARCHITECTURE.md §9.5).

Usage (from the repo root, with the copilot venv):
    copilot/.venv/Scripts/python.exe copilot/scripts/export_openapi.py

Writes `copilot/openapi.json` (pretty-printed, stable key order, trailing
newline) so its diff is reviewable like code. The test renders the spec with
the same helper, so the committed artifact and the test always agree byte-for-byte.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `import app.*` work regardless of CWD (the package lives in copilot/).
_COPILOT_ROOT = Path(__file__).resolve().parent.parent
if str(_COPILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_COPILOT_ROOT))

OUTPUT = _COPILOT_ROOT / "openapi.json"


def build_spec() -> dict:
    """Return the OpenAPI document exactly as FastAPI serves it at /openapi.json.

    Importing `app.main` registers the Week 1 routes plus the Week 2 ingest
    router (`app.include_router`), so every shipped endpoint is present."""
    from app.main import app  # noqa: PLC0415 — deferred so sys.path is set first

    return app.openapi()


def render(spec: dict) -> str:
    """Deterministic serialization: sorted keys + trailing newline, so a spec
    change shows up as a minimal, reviewable diff."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    spec = build_spec()
    OUTPUT.write_text(render(spec), encoding="utf-8")
    paths = spec.get("paths", {})
    print(f"wrote {OUTPUT} — {len(paths)} paths, OpenAPI {spec.get('openapi')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
