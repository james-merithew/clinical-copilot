# Install the repo git hooks (Windows). One-time, per clone.
# The pre-push hook is the AUTHORITATIVE eval gate — see .githooks/pre-push
# and copilot/evals/golden/README.md.
$ErrorActionPreference = "Stop"
git config core.hooksPath .githooks
Write-Host "core.hooksPath -> .githooks"
Write-Host "pre-push gate installed: ruff + pytest + golden eval runner + gate."
Write-Host "Optional: set COPILOT_PYTHON to your venv python if it is not at copilot\.venv."
