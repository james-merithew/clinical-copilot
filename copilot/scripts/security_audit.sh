#!/usr/bin/env sh
# security_audit.sh — dependency + static security audit, portable across CI and local.
#
# The Week-2 CI gate ran pip-audit + bandit only inside the GitLab `eval` job, so on a
# runner-less GitLab (runner availability on labs.gauntletai.com is not guaranteed) the
# security scan never ran at all. This script is the single source of truth for that
# audit: .gitlab-ci.yml calls it, and a developer (or pre-push, opt-in) can run the exact
# same check with no runner. It stays OUT of the default fast pre-push loop because
# pip-audit is network-y (it fetches the advisory DB) — opt in with COPILOT_SECURITY_AUDIT=1.
#
#   pip-audit : known-CVE scan over the pinned requirement set
#   bandit    : static security scan of the sidecar (medium severity + medium confidence
#               floors — the one Low-confidence B608 is the parameterized-by-design labs
#               query, triaged in review, not gated)
#
# Usage:  PYTHON=path/to/python sh copilot/scripts/security_audit.sh
set -e

PY="${PYTHON:-python}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

rc=0

# Triaged allowlist (accepted + tracked advisories) — a documented, self-justifying file,
# NOT a blanket disable: the audit still fails on any advisory not listed there.
ALLOWLIST="copilot/scripts/audit_allowlist.txt"
IGNORE=""
if [ -f "$ALLOWLIST" ]; then
    for id in $(sed 's/#.*//' "$ALLOWLIST" | awk 'NF {print $1}'); do
        IGNORE="$IGNORE --ignore-vuln $id"
    done
fi

echo "[security-audit] pip-audit — dependency CVE scan (network; allowlist: $ALLOWLIST)"
# shellcheck disable=SC2086 # intentional word-splitting of the --ignore-vuln args
"$PY" -m pip_audit -r copilot/requirements.txt $IGNORE || rc=1

echo "[security-audit] bandit — static security scan (offline)"
"$PY" -m bandit -q -r copilot/app redteam -ll -ii || rc=1

if [ "$rc" -ne 0 ]; then
    echo "[security-audit] FAIL — see findings above" >&2
    exit 1
fi
echo "[security-audit] PASS — no gated dependency or static-analysis findings"
