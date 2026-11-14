"""Pytest bootstrap for the Clinical Co-Pilot sidecar test suite.

Inserts the copilot directory (this file's dir) onto ``sys.path`` so that
``import app...`` resolves no matter what CWD pytest is invoked from. Keeping
this at the copilot root means both ``tests/`` (hermetic units) and ``evals/``
(DB integration) share one import root.
"""

from __future__ import annotations

import os
import sys

_COPILOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _COPILOT_DIR not in sys.path:
    sys.path.insert(0, _COPILOT_DIR)
