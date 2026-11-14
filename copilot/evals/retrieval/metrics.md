# Retrieval quality — aggregate recall@k + MRR

Keyless hybrid index (the no-Cohere-key shipping config), 17 labeled queries over the 9-doc guideline corpus.

| metric | value |
| --- | --- |
| recall@1 | 0.7941 |
| recall@3 | 0.9706 |
| recall@5 | 1.0000 |
| MRR | 0.9020 |

| query | relevant | first-rank | r@1 | r@3 | r@5 |
| --- | --- | --- | --- | --- | --- |
| metformin eGFR cutoff | metformin-egfr | 1 | 1.00 | 1.00 | 1.00 |
| can I keep metformin when kidney function is low | metformin-egfr | 1 | 1.00 | 1.00 | 1.00 |
| hypertension treatment target | acc-aha-htn-2017 | 1 | 1.00 | 1.00 | 1.00 |
| when do we start blood pressure medication | acc-aha-htn-2017 | 2 | 0.00 | 1.00 | 1.00 |
| A1c goal for most adults with diabetes | ada-soc-glycemic | 1 | 1.00 | 1.00 | 1.00 |
| how often to monitor glucose | ada-soc-glycemic | 1 | 1.00 | 1.00 | 1.00 |
| statin primary prevention groups | ascvd-statin-primary | 1 | 1.00 | 1.00 | 1.00 |
| who qualifies for a statin without established heart disease | ascvd-statin-primary | 2 | 0.00 | 1.00 | 1.00 |
| GDMT four pillars for HFrEF | acc-aha-hf-2022 | 1 | 1.00 | 1.00 | 1.00 |
| heart failure ejection fraction classes | acc-aha-hf-2022 | 1 | 1.00 | 1.00 | 1.00 |
| CKD staging CGA system | kdigo-ckd-2024 | 1 | 1.00 | 1.00 | 1.00 |
| when to refer chronic kidney disease to nephrology | ada-soc-ckd-dm, kdigo-ckd-2024 | 1 | 0.50 | 0.50 | 1.00 |
| screening for kidney disease in diabetes | ada-soc-ckd-dm | 1 | 1.00 | 1.00 | 1.00 |
| adult vaccination schedule | acip-adult-immunizations | 1 | 1.00 | 1.00 | 1.00 |
| which adult immunizations are due by age | acip-adult-immunizations | 3 | 0.00 | 1.00 | 1.00 |
| adult cancer screening recommendations | uspstf-screening-adults | 1 | 1.00 | 1.00 | 1.00 |
| preventive screening for bone health | uspstf-screening-adults | 1 | 1.00 | 1.00 | 1.00 |
