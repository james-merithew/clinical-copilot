#!/bin/sh
# Install the repo git hooks (macOS/Linux/Git Bash). One-time, per clone.
# The pre-push hook is the AUTHORITATIVE eval gate — see .githooks/pre-push
# and copilot/evals/golden/README.md.
set -e
git config core.hooksPath .githooks
chmod +x .githooks/pre-push 2>/dev/null || true
echo "core.hooksPath -> .githooks"
echo "pre-push gate installed: ruff + pytest + golden eval runner + gate."
echo "Optional: set COPILOT_PYTHON to your venv python if it is not at copilot/.venv."
