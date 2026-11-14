"""Deterministic critic — the graph's 4th node (Week 2 extension).

Runs inside `_finalize`, AFTER the `submit_briefing` coercion and BEFORE
`verify()`. The verifier remains the exit authority: the critic can only
NARROW the verifier's input (reject / strip), never rescue a claim. Fully
deterministic — no LLM call, no config flag (always on, like the verifier);
`CriticReport.model_review` is a defaulted seam for a future model-backed
second opinion and is never set here.

Two checks:

1. **Uncited-claim rejection.** A raw claim with empty/blank `handles` is
   rejected (`critic_rejected`) and merged into `quarantined_claims` by
   `_finalize` — wire-compatible with the verifier-only behavior (same
   quarantine counts). Claims WITH handles — even fabricated ones — pass
   through untouched: provenance is the verifier's job.

2. **Unsafe-action screen.** The agent is read-only (invariant 3) and must
   never *recommend placing or changing orders* either — a two-regex AND
   (action verb + recommendation frame, evaluated per sentence fragment) or a
   text that itself begins with an action verb (imperative chip labels:
   "Order a CBC"). Precision over recall, with two load-bearing exemptions:
   - **guideline-only claims** (all handles `gl:`) — corpus prose legitimately
     says "start a high-intensity statin"; the verifier's separation rule
     already keeps those out of the patient briefing;
   - **notes** — refusal text ("I can't place orders … read-only") never flows
     through the critic.
   A hit is STRIPPED (content problem, not an evidence problem — quarantine is
   for attribution failures), recorded as a `CriticFinding`, and `_finalize`
   appends one read-only notice to `notes`.

Pattern-precision notes (proven by the standing false-positive sweep in
`tests/test_critic.py` over every stub evidence text, every golden fixture
string, and every committed corpus chunk):
- the AND is scoped to a sentence fragment (`.!?;` or a newline), not the whole
  text — guideline chunks routinely contain "should" and "start" many lines
  apart;
- the imperative rule fires only when the WHOLE text starts with a BASE-FORM
  action verb (claim/chip-label shape). Corpus prose contains mid-paragraph
  imperatives ("Stop metformin.", "Prescribe glucagon …") that are reference
  knowledge, and past-tense chart facts ("Prescribed apixaban 5 mg") must
  reach the verifier so a fabricated handle is QUARANTINED, not stripped;
- the frame deliberately excludes the bare past participle "recommended"
  ("a recommended pneumococcal conjugate dose" is descriptive corpus prose,
  not the agent recommending an action).

PHI discipline: one `log_event` per turn, counts only — claim text and chip
labels never reach the log sink (findings ride the response, like
quarantined_claims).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .contracts import CriticFinding, CriticReport
from .correlation import log_event
from .verifier import RawClaim

log = logging.getLogger("copilot.critic")

_ACTION_VERBS = re.compile(
    r"\b(start(?:ing)?|initiat\w+|order(?:ing)?|prescrib\w+|increas\w+|decreas\w+"
    r"|titrat\w+|switch(?:ing)?\s+to|discontinu\w+|stop(?:ping)?|taper(?:ing)?"
    r"|renew(?:ing)?|refill(?:ing)?|adjust(?:ing)?|dose|dosing)\b",
    re.IGNORECASE,
)
_RECOMMEND_FRAME = re.compile(
    r"\b(should|recommend(?!ed\b)\w*|consider|suggest\w*|advis\w+|needs?\s+to|let'?s"
    r"|i\s+(?:will|would|can)|we\s+(?:will|should)|please)\b",
    re.IGNORECASE,
)
# Sentence-fragment boundaries for the AND scope (see module docstring).
_FRAGMENT_SPLIT = re.compile(r"[.!?\n;]")
# Imperative shape = the text begins with a BARE (base-form) action verb:
# "Order a CBC", "Refill oxycodone". Inflected forms are excluded on purpose —
# "Prescribed apixaban 5 mg" is a (possibly fabricated) past-tense chart fact
# whose fate belongs to the VERIFIER (quarantine), not the critic (ct-010).
_IMPERATIVE_START = re.compile(
    r"(start|initiate|order|prescribe|increase|decrease|titrate|switch"
    r"|discontinue|stop|taper|renew|refill|adjust)\b",
    re.IGNORECASE,
)


def is_unsafe_action(text: str) -> bool:
    """True when `text` reads as a suggested order / medication action."""
    t = (text or "").strip()
    if not t:
        return False
    if _IMPERATIVE_START.match(t):
        return True  # imperative shape: the text itself begins with an action verb
    for frag in _FRAGMENT_SPLIT.split(t):
        if _ACTION_VERBS.search(frag) and _RECOMMEND_FRAME.search(frag):
            return True
    return False


def _cited_handles(rc: RawClaim) -> list[str]:
    return [h for h in rc.handles if h and str(h).strip()]


@dataclass
class CritiqueResult:
    """What the critic did with one turn's raw claims + chip dicts."""

    surviving_claims: list[RawClaim] = field(default_factory=list)
    rejected_uncited: list[RawClaim] = field(default_factory=list)
    stripped_unsafe: list[RawClaim] = field(default_factory=list)
    surviving_chips: list[dict] = field(default_factory=list)
    stripped_chips: list[dict] = field(default_factory=list)
    report: CriticReport = field(default_factory=CriticReport)


def critique(raw_claims: list[RawClaim], chips: list[dict] | None = None) -> CritiqueResult:
    """Deterministic pre-verifier review. Never raises; never widens."""
    res = CritiqueResult()
    findings: list[CriticFinding] = []

    for rc in raw_claims:
        handles = _cited_handles(rc)
        if not handles:
            # Check 1: uncited → rejected; _finalize merges it into quarantine.
            res.rejected_uncited.append(rc)
            findings.append(CriticFinding(
                kind="uncited_claim", channel="briefing",
                text=rc.text, disposition="rejected_to_quarantine",
            ))
            continue
        guideline_only = all(str(h).startswith("gl:") for h in handles)
        if not guideline_only and is_unsafe_action(rc.text):
            # Check 2 (patient channel only): recommendation → stripped.
            res.stripped_unsafe.append(rc)
            findings.append(CriticFinding(
                kind="unsafe_action", channel="briefing",
                text=rc.text, disposition="stripped",
            ))
            continue
        res.surviving_claims.append(rc)

    for chip in chips or []:
        label = chip.get("label") if isinstance(chip, dict) else None
        if isinstance(label, str) and is_unsafe_action(label):
            res.stripped_chips.append(chip)
            findings.append(CriticFinding(
                kind="unsafe_action", channel="action_chip",
                text=label, disposition="stripped",
            ))
        else:
            res.surviving_chips.append(chip)

    res.report = CriticReport(
        reviewed=True,
        rejected_uncited=len(res.rejected_uncited),
        stripped_unsafe=len(res.stripped_unsafe) + len(res.stripped_chips),
        findings=findings,
    )
    # Counts only — never claim text (PHI discipline, enforced by the allowlist
    # AND by never offering the values here).
    log_event(
        log, "critic",
        rejected_uncited=res.report.rejected_uncited,
        stripped_unsafe=res.report.stripped_unsafe,
    )
    return res
