"""Deterministic Verifier — the safety gate that runs before any response.

Two guarantees (W1_ARCHITECTURE.md §9), both deterministic-first:

1. **Hard source attribution + field grounding.** A claim is VERIFIED only if:
   - every cited handle was really issued this turn (provenance), AND
   - every value-like number in the claim (doses, lab values) appears in the
     cited evidence (defeats "cited the lisinopril row for a metformin dose"),
     AND
   - the claim shares at least one content keyword with the cited evidence
     (defeats wholesale mis-citation).
   Otherwise it is QUARANTINED — shown as "unverified", never as fact.

2. **Domain-constraint enforcement** over structured evidence (not the model's
   prose), so it cannot be talked out of firing: allergy↔active-med, high-risk
   drug–drug pairs, and abnormal labs. Flags are ALWAYS surfaced.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .contracts import (
    Allergy,
    Claim,
    ConstraintFlag,
    ConstraintSeverity,
    LabResult,
    Medication,
    VerificationResult,
    Vital,
)
from .correlation import log_event
from .evidence import EvidenceSet, keywords, numbers, value_numbers

log = logging.getLogger("copilot.verifier")


@dataclass
class RawClaim:
    """A claim as the model submits it: free text + cited evidence handles."""

    text: str
    handles: list[str] = field(default_factory=list)


# --- Curated domain knowledge (MVP; not a full clinical rules engine) ---------

DRUG_CLASSES: dict[str, set[str]] = {
    "penicillin": {
        "penicillin", "amoxicillin", "amoxicillin-clavulanate", "augmentin",
        "ampicillin", "dicloxacillin", "piperacillin", "nafcillin",
    },
    "cephalosporin": {"cephalexin", "cefazolin", "ceftriaxone", "cefuroxime", "cefdinir"},
    "sulfa": {"sulfamethoxazole", "trimethoprim-sulfamethoxazole", "bactrim", "sulfasalazine"},
    "nsaid": {"ibuprofen", "naproxen", "ketorolac", "diclofenac", "aspirin", "celecoxib", "meloxicam", "indomethacin"},
    "ace_inhibitor": {"lisinopril", "enalapril", "ramipril", "benazepril", "captopril"},
    "arb": {"losartan", "valsartan", "olmesartan", "candesartan", "irbesartan"},
    "statin": {"simvastatin", "atorvastatin", "rosuvastatin", "pravastatin", "lovastatin"},
    "macrolide": {"clarithromycin", "erythromycin", "azithromycin"},
    "potassium_sparing": {"spironolactone", "triamterene", "amiloride", "eplerenone"},
    "ssri": {"sertraline", "fluoxetine", "paroxetine", "citalopram", "escitalopram"},
    "benzodiazepine": {"alprazolam", "lorazepam", "diazepam", "clonazepam", "temazepam"},
    "opioid": {"oxycodone", "hydrocodone", "morphine", "fentanyl", "tramadol", "codeine", "hydromorphone"},
    "sulfonylurea": {"glipizide", "glyburide", "glimepiride"},
}

# High-risk drug–drug pairs, expressed as (tokens_a, tokens_b, message). A token
# is either a class name (above) or a bare ingredient string.
DDI_PAIRS: list[tuple[set[str], set[str], str]] = [
    ({"warfarin"}, {"nsaid"},
     "Anticoagulant + NSAID/antiplatelet — elevated bleeding risk"),
    ({"warfarin"}, {"amiodarone"}, "Amiodarone potentiates warfarin — bleeding risk"),
    ({"warfarin"}, {"ssri", "sulfa"}, "Increased bleeding/INR risk with warfarin"),
    ({"ace_inhibitor", "arb"}, {"potassium_sparing", "potassium"},
     "RAAS agent + potassium-sparing agent — hyperkalemia risk"),
    ({"ace_inhibitor"}, {"arb"}, "ACE inhibitor + ARB — hyperkalemia / renal risk (dual RAAS blockade)"),
    ({"statin"}, {"macrolide", "gemfibrozil", "itraconazole"},
     "Statin + CYP3A4 inhibitor/fibrate — rhabdomyolysis risk"),
    ({"opioid"}, {"benzodiazepine"},
     "Opioid + benzodiazepine — additive respiratory depression / sedation"),
    ({"ssri"}, {"nsaid"}, "SSRI + NSAID — increased GI bleeding risk"),
]

# Lab tests where a RISING value is clinically worse (for the trend flag).
HIGHER_IS_WORSE = ("a1c", "glucose", "creatinine", "ldl", "potassium", "inr", "bnp", "psa")

_RANGE_LOHI = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*$")
_RANGE_LT = re.compile(r"^\s*[<≤]\s*(-?\d+(?:\.\d+)?)")
_RANGE_GT = re.compile(r"^\s*[>≥]\s*(-?\d+(?:\.\d+)?)")


def _num(s) -> float | None:  # noqa: ANN001
    try:
        return float(str(s).strip().split()[0])
    except Exception:  # noqa: BLE001
        return None


def _out_of_range(value, range_str) -> bool:  # noqa: ANN001
    """Deterministic reference-range check (covers the brief's 'thresholds')."""
    v = _num(value)
    if v is None or not range_str:
        return False
    m = _RANGE_LOHI.match(range_str)
    if m:
        return v < float(m.group(1)) or v > float(m.group(2))
    m = _RANGE_LT.match(range_str)
    if m:
        return v >= float(m.group(1))
    m = _RANGE_GT.match(range_str)
    if m:
        return v <= float(m.group(1))
    return False


def _tokens_for(name: str | None) -> tuple[set[str], str]:
    """Return the drug's class tokens + its normalized name."""
    n = (name or "").lower()
    classes = {cls for cls, members in DRUG_CLASSES.items() if any(m in n or n in m for m in members)}
    return classes, n


def _token_hit(token: str, name: str, classes: set[str]) -> bool:
    return token in classes or token in name


def _check_constraints(
    meds: list[Medication],
    allergies: list[Allergy],
    labs: list[LabResult],
    vitals: list[Vital] | None = None,
) -> list[ConstraintFlag]:
    flags: list[ConstraintFlag] = []
    active_meds = [m for m in meds if (m.status or "active") != "inactive"]

    # 1. allergy ↔ active medication (class-aware: penicillin allergy ⇒ amoxicillin)
    for al in allergies:
        al_classes, al_name = _tokens_for(al.substance)
        al_classes |= {al_name} if al_name else set()
        for m in active_meds:
            m_classes, m_name = _tokens_for(m.name)
            hit = bool(al_classes & m_classes) or (al_name and (al_name in m_name or m_name in al_name))
            if hit:
                flags.append(
                    ConstraintFlag(
                        kind="allergy_interaction",
                        severity=ConstraintSeverity.critical,
                        message=f"Active medication '{m.name}' conflicts with documented allergy to '{al.substance}'.",
                        source_refs=[al.source, m.source],
                    )
                )

    # 2. high-risk drug–drug pairs
    for a_tokens, b_tokens, msg in DDI_PAIRS:
        a_hit = b_hit = None
        for m in active_meds:
            m_classes, m_name = _tokens_for(m.name)
            if any(_token_hit(t, m_name, m_classes) for t in a_tokens):
                a_hit = m
            if any(_token_hit(t, m_name, m_classes) for t in b_tokens):
                b_hit = m
        if a_hit and b_hit and a_hit.source.primary_key != b_hit.source.primary_key:
            flags.append(
                ConstraintFlag(
                    kind="drug_drug",
                    severity=ConstraintSeverity.warning,
                    message=f"{msg} ({a_hit.name} + {b_hit.name}).",
                    source_refs=[a_hit.source, b_hit.source],
                )
            )

    # 3. Abnormal labs — flag the MOST RECENT result per test (source `abnormal`
    #    flag OR computed out-of-reference-range), not every historical value.
    latest_by_test: dict[str, LabResult] = {}
    for lab in labs:
        key = lab.name.lower()
        cur = latest_by_test.get(key)
        if cur is None or (lab.date or "") > (cur.date or ""):
            latest_by_test[key] = lab
    for lab in latest_by_test.values():
        if lab.abnormal or _out_of_range(lab.value, lab.range):
            flags.append(
                ConstraintFlag(
                    kind="threshold",
                    severity=ConstraintSeverity.warning,
                    message=f"Abnormal lab: {lab.name} = {lab.value or '?'} {lab.units or ''} (ref {lab.range or 'n/a'}).",
                    source_refs=[lab.source],
                )
            )

    # 4. Worsening trend — ≥3 strictly-rising values for a higher-is-worse test.
    by_name: dict[str, list[LabResult]] = {}
    for lab in labs:
        by_name.setdefault(lab.name.lower(), []).append(lab)
    for name, series in by_name.items():
        if not any(k in name for k in HIGHER_IS_WORSE):
            continue
        pts = [(lab.date or "", _num(lab.value), lab) for lab in series if _num(lab.value) is not None and lab.date]
        pts.sort(key=lambda p: p[0])  # ascending by date
        vals = [p[1] for p in pts]
        if len(vals) >= 3 and all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
            first, last = pts[0], pts[-1]
            flags.append(
                ConstraintFlag(
                    kind="threshold",
                    severity=ConstraintSeverity.warning,
                    message=f"Rising trend in {series[0].name}: {first[1]:g} → {last[1]:g} ({first[0]} → {last[0]}).",
                    source_refs=[p[2].source for p in pts],
                )
            )

    # 5. Abnormal vitals (latest reading) — deterministic thresholds.
    if vitals:
        v = vitals[0]
        if v.bps and (v.bps >= 140 or (v.bpd and v.bpd >= 90)):
            flags.append(
                ConstraintFlag(
                    kind="threshold",
                    severity=ConstraintSeverity.warning,
                    message=f"Elevated blood pressure: {int(v.bps)}/{int(v.bpd or 0)} mmHg.",
                    source_refs=[v.source],
                )
            )
        if v.pulse and (v.pulse > 100 or v.pulse < 60):
            flags.append(
                ConstraintFlag(
                    kind="threshold",
                    severity=ConstraintSeverity.warning,
                    message=f"Abnormal heart rate: {int(v.pulse)} bpm.",
                    source_refs=[v.source],
                )
            )
    return flags


def _judge(rc: RawClaim, evidence: EvidenceSet) -> tuple[bool, Claim]:
    handles = [h for h in rc.handles if h]
    refs = [evidence.get(h).source_ref for h in handles if evidence.get(h)]  # type: ignore[union-attr]

    # (a) provenance: at least one handle, all real
    if not handles or any(evidence.get(h) is None for h in handles):
        return (False, Claim(text=rc.text, source_refs=refs))

    cited = evidence.cited_text(handles)
    # (b) value grounding: every dose/lab value in the claim must be in evidence.
    #     A claim asserting a PATIENT value must ground it against PATIENT
    #     evidence only — a number that appears solely in a cited GUIDELINE chunk
    #     (e.g. an ADA target of 7 %) cannot license "Her A1c is 7.0" as a
    #     patient fact. Guideline-only claims still ground on guideline text.
    patient_handles = [
        h for h in handles
        if getattr(evidence.get(h).source_ref, "source_type", "") != "guideline"  # type: ignore[union-attr]
    ]
    grounding_text = evidence.cited_text(patient_handles) if patient_handles else cited
    if not value_numbers(rc.text) <= numbers(grounding_text):
        return (False, Claim(text=rc.text, source_refs=refs))
    # (c) content overlap: claim must share a keyword with its cited evidence
    kw = keywords(rc.text)
    if kw and not (kw & keywords(cited)):
        return (False, Claim(text=rc.text, source_refs=refs))

    return (True, Claim(text=rc.text, source_refs=refs))


def verify(
    raw_claims: list[RawClaim],
    evidence: EvidenceSet,
    meds: list[Medication],
    allergies: list[Allergy],
    labs: list[LabResult],
    vitals: list[Vital] | None = None,
) -> VerificationResult:
    verified: list[Claim] = []
    quarantined: list[Claim] = []
    for rc in raw_claims:
        ok, claim = _judge(rc, evidence)
        (verified if ok else quarantined).append(claim)

    # Separation rule (W2 §6.3, deterministic): partition VERIFIED claims by
    # the kind of sources they cite. A claim citing only guideline chunks is
    # reference knowledge → `guideline_evidence` (separate UI header); a claim
    # with >=1 patient-record handle stays in the briefing (mixed allowed —
    # its values were already grounded against the union of cited evidence in
    # _judge). A claim with ZERO patient-record handles can therefore never
    # appear in the patient briefing — a partition function, not a prompt rule.
    # Patient-only turns are unaffected: their partition is trivially all-briefing.
    briefing_claims: list[Claim] = []
    guideline_only: list[Claim] = []
    for claim in verified:
        refs = claim.source_refs
        if refs and all(getattr(r, "source_type", "") == "guideline" for r in refs):
            guideline_only.append(claim)
        else:
            briefing_claims.append(claim)

    flags = _check_constraints(meds, allergies, labs, vitals)
    log_event(
        log,
        "verify",
        verified=len(briefing_claims),
        quarantined=len(quarantined),
        constraint_flags=len(flags),
        retrieval_cited=len(guideline_only),
    )
    return VerificationResult(
        verified_claims=briefing_claims,
        quarantined_claims=quarantined,
        constraint_flags=flags,
        guideline_evidence=guideline_only,
    )
