"""Medications tool — reconciles `prescriptions` and `lists` (type=medication).

OpenEMR records meds in two places that can diverge (AUDIT.md). We surface both,
pick the authoritative row for attribution (the `prescriptions` row when present,
since it carries structured dose), and flag any divergence explicitly rather than
silently smoothing it.
"""

from __future__ import annotations

from ..contracts import Medication, MedicationsResult, SourceRef
from ..db import query


def _norm(name: str | None) -> str:
    return (name or "").strip().lower()


def get_medications(ctx) -> MedicationsResult:  # noqa: ANN001
    rx = query(
        """
        SELECT id, drug, dosage, unit, active, start_date
        FROM prescriptions
        WHERE patient_id = :pid AND drug IS NOT NULL AND drug <> ''
        ORDER BY COALESCE(start_date, '1900-01-01') DESC
        """,
        {"pid": ctx.pid},
    )
    ls = query(
        """
        SELECT id, title, activity, begdate
        FROM lists
        WHERE pid = :pid AND type = 'medication' AND title IS NOT NULL AND title <> ''
        """,
        {"pid": ctx.pid},
    )

    lists_by_name: dict[str, list[dict]] = {}
    for r in ls:
        lists_by_name.setdefault(_norm(r["title"]), []).append(r)

    meds: list[Medication] = []
    seen: set[str] = set()
    for r in rx:
        name = r["drug"]
        key = _norm(name)
        seen.add(key)
        dose = " ".join(str(x) for x in (r["dosage"], r["unit"]) if x) or None
        rx_active = str(r["active"]) == "1"
        diverges = False
        note = None
        if key in lists_by_name:
            lr = lists_by_name[key][0]
            list_active = str(lr["activity"]) == "1"
            if list_active != rx_active:
                diverges = True
                note = (
                    f"prescriptions active={rx_active} vs problem-list "
                    f"active={list_active} (lists.id={lr['id']})"
                )
        meds.append(
            Medication(
                source=SourceRef(
                    table="prescriptions", primary_key=int(r["id"]), label=f"Rx {name}"
                ),
                name=name,
                dose=dose,
                status=("active" if rx_active else "inactive"),
                started=(str(r["start_date"])[:10] if r["start_date"] else None),
                diverges=diverges,
                divergence_note=note,
            )
        )

    # Meds recorded only in the problem list (not in prescriptions).
    for key, rows in lists_by_name.items():
        if key in seen:
            continue
        lr = rows[0]
        meds.append(
            Medication(
                source=SourceRef(
                    table="lists", primary_key=int(lr["id"]), label=f"Problem-list med {lr['title']}"
                ),
                name=lr["title"],
                dose=None,
                status=("active" if str(lr["activity"]) == "1" else "inactive"),
            )
        )

    return MedicationsResult(medications=meds, truncated=False)
