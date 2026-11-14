"""Repo-root pytest bootstrap for the adversarial platform's tests.

Puts both the repo root (so ``import redteam`` resolves) and ``copilot/`` (so
``import app`` resolves, matching ``copilot/conftest.py``) on ``sys.path``. This
makes the ``redteam`` tests import identically whether pytest is invoked from the
repo root, from a CI runner, or from an isolated git worktree.

Deliberately does NOT make ``evals/`` an importable package: a top-level ``evals``
name would shadow ``copilot/evals``. All runnable logic lives in the ``redteam``
package; ``evals/adversarial/`` holds only declarative artifacts (cases, schema,
baseline) that the runner reads by path.
"""

import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent
_COPILOT = _ROOT / "copilot"

for _p in (str(_ROOT), str(_COPILOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
