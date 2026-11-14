"""redteam — the adversarial evaluation platform's agent layer.

A closed loop of four agents wrapping the read-only Clinical Co-Pilot target:
Orchestrator (deterministic ranking + budget) -> Red Team (generate + mutate) ->
Judge (independent verdict, deterministic safety veto) -> Documentation
(confirmed exploit -> structured report). Agents communicate only through the
versioned message contracts in ``redteam.contracts`` (mirrors of ``contracts/*.json``),
carried over the Co-Pilot's own typed HandoffLog. See ``ARCHITECTURE.md``.
"""

__all__ = ["contracts"]

# Path bootstrap: make `import app` (the co-pilot package under copilot/) and
# `import redteam` resolve whether we run as a pytest suite, a `python -m redteam.*`
# CLI, or a pre-push gate script. Mirrors copilot/conftest.py + the repo-root conftest.
import pathlib as _pathlib
import sys as _sys

_ROOT = _pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "copilot")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
