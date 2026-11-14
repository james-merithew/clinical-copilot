---
doc_id: kdigo-ckd-2024
title: "Office Protocol: CKD Staging, Risk, and Management"
source_org: Kidney Disease Improving Global Outcomes (KDIGO)
source_url: https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf
version: "KDIGO 2024 Clinical Practice Guideline for CKD"
retrieved: "2026-07-13"
---

# CKD Staging and Management (Practice Protocol)

Our internal distillation of the KDIGO 2024 CKD guideline. Paraphrased for
office use; the source document carries the full evidence grading.

## Definition

CKD is an abnormality of kidney structure or function present for **more
than 3 months** with health implications — most commonly a sustained eGFR
below 60 mL/min/1.73 m², a urine ACR of 30 mg/g or higher, or both. A
single abnormal result is not CKD; repeat within about 3 months before
staging. Use a validated creatinine equation (CKD-EPI); add cystatin C
when creatinine-based estimates may mislead (extremes of muscle mass,
borderline staging decisions).

## Staging: the CGA system

Classify every CKD patient by **C**ause, **G**FR category, and
**A**lbuminuria category.

GFR categories (mL/min/1.73 m²):

| Category | eGFR |
| --- | --- |
| G1 | 90 or higher |
| G2 | 60–89 |
| G3a | 45–59 |
| G3b | 30–44 |
| G4 | 15–29 |
| G5 | below 15 |

Albuminuria categories (urine ACR):

| Category | ACR (mg/g) | Meaning |
| --- | --- | --- |
| A1 | below 30 | normal to mildly increased |
| A2 | 30–300 | moderately increased |
| A3 | above 300 | severely increased |

Risk rises stepwise along **both** axes (the KDIGO "heat map"): a G3a/A2
patient is at meaningfully higher risk than G3a/A1 — always report both
coordinates, never eGFR alone. Combined G/A category also sets monitoring
frequency: low-risk yearly, moderate yearly to twice yearly, high twice
yearly or more, very high three or more times per year.

## Management pillars

- **Blood pressure:** treat with a renin-angiotensin-system inhibitor
  (ACEi or ARB) at maximum tolerated dose for diabetic CKD with A2 or A3
  albuminuria, and for nondiabetic CKD with A3 (reasonable in A2).
- **SGLT2 inhibitor:** recommended for CKD when eGFR is 20 or higher —
  for type 2 diabetes with CKD, and for nondiabetic CKD with significant
  albuminuria; slows progression and reduces cardiovascular events.
- **Statin:** adults 50 and older with CKD not on dialysis should be on a
  statin (or statin/ezetimibe); see `ascvd-statin-primary` for intensity.
- **Glycemia:** individualized A1c per `ada-soc-glycemic`; drug renal
  dosing review at every stage change (see `metformin-egfr`).
- Avoid nephrotoxins where possible — NSAIDs in G3 and beyond deserve an
  explicit risk conversation; review contrast exposure plans.

## Referral triggers

Refer to nephrology for: eGFR below 30 (G4–G5); ACR above 300 mg/g (A3);
a sustained eGFR decline of more than 5 mL/min/1.73 m² per year;
persistent significant hematuria with albuminuria; refractory
hypertension; recurrent hyperkalemia; or uncertain cause.

## What the Co-Pilot should do with this

1. When both an eGFR and an ACR exist, state the full G/A stage (e.g.
   "eGFR 58 with ACR 68 mg/g = G3a/A2, moderately increased albuminuria")
   citing both lab rows plus this protocol.
2. When only one axis has data, report the one that exists and name the
   other as a gap — an eGFR without an ACR is an incomplete risk picture.
3. A first eGFR below 60 is provisional until a confirmatory value at
   least 3 months later; say "new, unconfirmed" rather than "CKD."
4. Cross-check the medication list on every stage change: RASi present?
   SGLT2i present? NSAID lurking? Metformin dose still appropriate?
