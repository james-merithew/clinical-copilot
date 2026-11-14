# Week 2 Research Notes — Document Formats & Guideline Corpus

Researcher lane (Step R), 2026-07-13. Grounds the synthetic fixtures in
`copilot/tests/ingest/fixtures/` and the paraphrased corpus in
`copilot/corpus/guidelines/`. **All fixture content is synthetic** — fictional
patients only (per CLAUDE.md: demo data, never real patient data). No
copyrighted text is reproduced; formats and thresholds are paraphrased with
source attribution.

---

## 1. Outpatient lab report anatomy (Quest / LabCorp style)

Sources:
- LabCorp published sample patient reports (structure only, no content reused):
  https://www.labcorp.com/assets/1724 and
  https://files.labcorp.com/testmenu-d8/sample_reports/005013.pdf
- Quest sample report (structure): https://storage.googleapis.com/pmd-static/sample-reports/4090.pdf
- Flag conventions explainer: https://siphoxhealth.com/articles/how-can-i-understand-my-quest-diagnostics-results

### Header block (both vendors converge on the same fields)
| Field | Notes |
| --- | --- |
| Patient name (LAST, FIRST), DOB, Age, Sex | top-left block |
| Patient ID / MRN, Specimen ID | Specimen ID is the lab's accession key, e.g. LabCorp `083-992-9203-0` (3-3-4-1 digit groups); Quest uses "SPECIMEN:" + requisition number |
| Date Collected, Date Received, Date Reported | three distinct timestamps — collection precedes receipt precedes report |
| Ordering physician | "Ordering Physician:" (LabCorp) / "PHYSICIAN NAME" (Quest) |
| Account / client | ordering practice's account number and address |
| Fasting flag | Y/N where relevant (glucose, lipids) |

### Analyte table columns
`TESTS | RESULT | FLAG | UNITS | REFERENCE INTERVAL` (LabCorp wording;
Quest: "Test Name / In Range / Out of Range / Reference Range" — out-of-range
results are printed in a separate column so they pop). Some reports add a LAB
column (performing-site code) and previous result/date.

### Abnormal flagging conventions
- `H` = above reference interval, `L` = below.
- `HH` / `LL` (or `*`, or "critical") = beyond the lab's critical/alert limits, phoned to the ordering provider.
- `A` = abnormal (non-numeric, e.g. qualitative positives).
- Flag column empty when in range. Reference intervals are age/sex-specific and lab-specific.

### Panel groupings relevant to our persona (DM / HTN / CKD / CHF panel)
- **CMP (Comprehensive Metabolic Panel, 14 analytes):** glucose, BUN, creatinine, eGFR, sodium, potassium, chloride, CO2, calcium, total protein, albumin, total bilirubin, ALP, AST, ALT.
- **CBC:** WBC, RBC, hemoglobin, hematocrit, MCV, MCH, MCHC, RDW, platelets (+ differential).
- **HbA1c:** single analyte, %, reference ~4.0–5.6 % (≥6.5 % diagnostic of diabetes).
- **Lipid panel:** total cholesterol, triglycerides, HDL, calculated LDL, (VLDL, chol/HDL ratio).
- **Urine ACR (albumin/creatinine ratio):** mg/g creatinine, normal <30.
- **TSH:** mIU/L, reference ~0.40–4.50.

Fixture decision: `lab_simple.pdf` = CMP subset + HbA1c (~10 analytes) with
exactly one H flag; the Delgado demo lab adds eGFR + urine ACR so KDIGO/ADA
CKD protocols retrieve as relevant.

---

## 2. Internal-medicine new/returning patient intake form

Sources:
- USF Health General Internal Medicine new-patient questionnaire: https://health.usf.edu/care/patient-resources/~/media/D9AA725AB92C4A43A53B3D8FD5463045.ashx
- Tohickon Internal Medicine new-patient packet: https://tohickoninternalmedicine.com/uploads/3/5/2/1/35212242/new_patient_packet_for_website.pdf
- Field-inventory guides: https://www.commure.com/blog-scribe/new-patient-forms , https://www.tebra.com/theintake/patient-experience/optimize-operations/medical-intake-form

Standard sections, in the order they usually appear:
1. **Demographics** — legal name, DOB, sex, address, phone, email, emergency contact.
2. **Insurance** — carrier, member ID, group, policyholder. **Deliberately omitted from our synthetic form** — needless PHI-shaped surface with zero extraction value for the briefing use case.
3. **Chief concern / reason for visit** — one free-text line.
4. **Current medications** — table of name / dose / frequency (incl. OTC + supplements).
5. **Allergies** — substance + reaction (drug, food, environmental).
6. **Past medical / surgical history** — condition checklist + surgery list w/ years.
7. **Family history** — checklist per first-degree relative: diabetes, heart disease, HTN, stroke, cancer, mental illness.
8. **Social history** — tobacco (never/former/current, packs/day), alcohol (drinks/week), occupation, exercise.
9. **Review of systems** — per-organ-system symptom checklist (constitutional, cardiac, respiratory, GI, GU, neuro, endocrine…).
10. Signature/date + consents (omitted in fixture — not extraction targets).

Fixture decision: one page, sections 1 and 3–9, filled for the fictional
patient; ROS kept to a compact checked-box line list so the page stays single.

---

## 3. Guideline corpus — sources and the numbers each contributes

Nine corpus docs in `copilot/corpus/guidelines/`. Every numeric threshold
below is what the corpus file must state, verbatim-correct per source —
these get cited to physicians in the demo.

| doc_id | Source (canonical URL) | Load-bearing numbers |
| --- | --- | --- |
| `ada-soc-glycemic` | ADA Standards of Care in Diabetes—2026, Sec. 6 — https://diabetesjournals.org/care/article/49/Supplement_1/S132/163927/ | A1c **<7 %** for most nonpregnant adults; **<8 %** if limited life expectancy/hypoglycemia risk; A1c testing **2×/yr** at goal, **quarterly** if therapy changed or not at goal; diagnosis A1c **≥6.5 %**, FPG **≥126**, 2-h OGTT **≥200 mg/dL**; prediabetes A1c **5.7–6.4 %**; CGM time-in-range **>70 %** (70–180 mg/dL) |
| `ada-soc-ckd-dm` | ADA SoC 2026, Sec. 11 (CKD & Risk Management) — https://diabetesjournals.org/care/article/49/Supplement_1/S246/163914/ | Screen **annually**: spot urine ACR + eGFR in all T2DM from diagnosis; ACR ≥**30 mg/g** or eGFR <**60** ×2 over 3 mo = CKD; monitor **2×/yr** once CKD present; SGLT2i recommended if eGFR ≥**20** and ACR ≥**200 mg/g** (benefit also in guidance for CKD generally); finerenone for CKD w/ albuminuria on max RASi; ACEi/ARB titrated to max tolerated if albuminuria + HTN |
| `acc-aha-htn-2017` | 2017 ACC/AHA HTN guideline (Hypertension 71:e13–e115) — https://www.ahajournals.org/doi/10.1161/HYP.0000000000000065 | Normal <**120/80**; Elevated 120–129/<80; **Stage 1 = 130–139 / 80–89**; **Stage 2 ≥140/90**; treatment target **<130/80** for most treated adults (incl. DM, CKD); drug therapy for Stage 1 only if clinical ASCVD or 10-yr risk ≥**10 %**; confirm with out-of-office readings |
| `kdigo-ckd-2024` | KDIGO 2024 CKD guideline — https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf | CKD = abnormalities >**3 months**; GFR categories **G1 ≥90, G2 60–89, G3a 45–59, G3b 30–44, G4 15–29, G5 <15**; albuminuria **A1 <30, A2 30–300, A3 >300 mg/g**; CGA heat-map risk; RASi for diabetic CKD w/ A2–A3; SGLT2i for CKD w/ eGFR ≥20; statin for CKD age ≥50 |
| `acc-aha-hf-2022` | 2022 AHA/ACC/HFSA HF guideline (Circulation 145:e895) — https://www.ahajournals.org/doi/10.1161/CIR.0000000000001063 | Stages **A–D**; EF classes: HFrEF ≤**40 %**, HFmrEF **41–49 %**, HFpEF ≥**50 %**, HFimpEF; 4 GDMT pillars (ARNI/ACEi/ARB, beta-blocker, MRA, SGLT2i); BP control per <130/80 in Stage A; natriuretic-peptide screening in at-risk |
| `uspstf-screening-adults` | USPSTF A/B recs — https://www.uspreventiveservicestaskforce.org/uspstf/recommendation-topics/uspstf-a-and-b-recommendations | Colorectal: **45–75** (B for 45–49, A for 50–75, 2021); Breast: biennial mammography **40–74** (B, 2024); Prediabetes/T2DM: **35–70** w/ overweight/obesity (B, 2021); Lung: annual LDCT **50–80**, ≥**20 pack-yr**, quit <**15 yr** (B); AAA: one-time ultrasound men **65–75** ever-smoked (B); HTN screening all adults ≥18 (A); statin — see below |
| `ascvd-statin-primary` | 2018 AHA/ACC multisociety cholesterol guideline — https://www.ahajournals.org/doi/10.1161/CIR.0000000000000625 (cross-ref USPSTF 2022 statin rec) | High-intensity statin if LDL ≥**190**; moderate-intensity for **all DM aged 40–75** (no risk calc needed); 10-yr ASCVD risk bands: borderline **5–7.4 %**, intermediate **7.5–19.9 %** (statin if risk enhancers), high ≥**20 %** (high-intensity, aim LDL ↓≥50 %); CAC = 0 may defer (unless smoker/DM/FHx) |
| `metformin-egfr` | FDA Drug Safety Communication (2016, rev. 2017) — https://www.fda.gov/drugs/drug-safety-and-availability/fda-drug-safety-communication-fda-revises-warnings-regarding-use-diabetes-medicine-metformin-certain | **Contraindicated eGFR <30**; **do not initiate at eGFR 30–45**; if on it and eGFR falls to 30–45, reassess benefit/risk (common practice: cap 1,000 mg/day); eGFR at least **annually** on metformin; stop before iodinated contrast if eGFR 30–60 |
| `acip-adult-immunizations` | CDC/ACIP Adult Immunization Schedule — https://www.cdc.gov/vaccines/hcp/imz-schedules/adult-age.html | Influenza **annually ≥6 mo**; pneumococcal PCV for **all ≥50** (and 19–49 w/ risk conditions incl. DM, CKD, CHF); RZV (Shingrix) **2 doses ≥50**; Td/Tdap booster **q10 yr**; hep B vaccination for adults **19–59** (incl. all w/ diabetes in that band) |

Version note: the plan's doc_id keeps `acc-aha-htn-2017`; the corpus file
flags that a 2025 AHA/ACC update exists but the office protocol tracks the
2017 thresholds our decision support was validated against.

---

## 4. Fixture parameter cheat-sheet (what the generator encodes)

- **TESTPATIENT, ALPHA** — DOB 1970-01-01, fictional. `lab_simple.pdf`: CMP-subset
  + HbA1c, collected 2026-07-10, exactly one abnormal pair by design intent
  (glucose 142 H is the single flagged CMP analyte; A1c 8.4 H rides in its own
  panel) — ground truth records every flag explicitly.
- **Marcus Delgado (pid 101)** — DOB 1961-04-12 (matches `seed_showcase.sql`).
  Seeded A1c trend 6.9 → 7.5 → 8.2 (2026-05-01); seeded eGFR 64 → 61;
  LDL 158 → 165; BP up to 155/99. Demo lab (collected **2026-07-12**)
  continues the story: **A1c 8.6 H**, glucose 168 H, creatinine 1.42 H,
  **eGFR 58 L** (first dip below 60 → G3a), **urine ACR 68 H** (A2, 30–300)
  — makes `kdigo-ckd-2024`, `ada-soc-ckd-dm`, and `metformin-egfr`
  (58 is above the 45 caution line but trending toward it) all retrievable
  and clinically coherent.
- Delgado intake: chief concern "increased thirst and fatigue"; meds
  metformin 1000 mg BID + lisinopril 10 mg daily (matches seeded
  prescriptions) + atorvastatin 40 mg (new, supports statin corpus doc);
  allergy lisinopril-class alternative kept clean — allergies are
  sulfa (rash) and shellfish (hives), non-conflicting with seeded meds.
