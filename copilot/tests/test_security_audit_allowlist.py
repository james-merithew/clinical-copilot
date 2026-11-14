"""Policy guard for the pip-audit allowlist (copilot/scripts/audit_allowlist.txt).

The allowlist keeps the security gate green on ACCEPTED, TRACKED advisories — but only
if it stays a triaged list, not a silent blanket-ignore. These offline tests enforce that
discipline: every allowlisted advisory is a well-formed ID that carries a justification,
and the file parses to the same IDs the audit script feeds `pip-audit --ignore-vuln`.

Failure modes guarded:
- a bare advisory ID slipping in with no justification (accountability erodes);
- a malformed / duplicated ID silently ignored by pip-audit;
- the script's parse drifting from the file's real content.
"""

from __future__ import annotations

import re
from pathlib import Path

ALLOWLIST = Path(__file__).resolve().parents[1] / "scripts" / "audit_allowlist.txt"
ID_RE = re.compile(r"^(PYSEC|CVE|GHSA)-", re.IGNORECASE)


def _entries() -> list[tuple[str, str]]:
    """(advisory_id, justification) for each non-comment, non-blank line — parsed the
    SAME way security_audit.sh does (first token is the ID; text after '#' is the note)."""
    out: list[tuple[str, str]] = []
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        code = raw.split("#", 1)[0].strip()
        if not code:
            continue  # blank or full-line comment
        note = raw.split("#", 1)[1].strip() if "#" in raw else ""
        out.append((code.split()[0], note))
    return out


def test_every_allowlisted_advisory_is_wellformed_and_justified():
    entries = _entries()
    assert entries, "allowlist parsed to zero entries — parser or file drift"
    for advisory_id, note in entries:
        assert ID_RE.match(advisory_id), f"malformed advisory id: {advisory_id!r}"
        assert note, f"{advisory_id} has no justification — every accepted advisory must state why"


def test_no_duplicate_advisories():
    ids = [i for i, _ in _entries()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate advisory ids in allowlist: {sorted(dupes)}"
