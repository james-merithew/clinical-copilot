"""Demographics tool — patient name/age/sex for the header + snapshot.

Internal only (NOT exposed to the LLM tool set — it doesn't need it). Read-only,
patient-scoped, source-attributed like every other tool.
"""

from __future__ import annotations

from datetime import date

from ..contracts import Demographics, SourceRef
from ..db import query


def _age(dob) -> int | None:  # noqa: ANN001
    if not dob:
        return None
    try:
        s = str(dob)[:10]
        y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
        today = date.today()
        return today.year - y - ((today.month, today.day) < (m, d))
    except Exception:  # noqa: BLE001
        return None


def get_demographics(ctx) -> Demographics:  # noqa: ANN001
    rows = query(
        "SELECT fname, lname, DOB, sex FROM patient_data WHERE pid = :pid LIMIT 1",
        {"pid": ctx.pid},
    )
    label = f"Patient {ctx.pid}"
    src = SourceRef(table="patient_data", primary_key=ctx.pid, label=label)
    if not rows:
        return Demographics(source=src, name=label)
    r = rows[0]
    name = " ".join(x for x in (r["fname"], r["lname"]) if x) or label
    dob = str(r["DOB"])[:10] if r["DOB"] else None
    return Demographics(
        source=SourceRef(table="patient_data", primary_key=ctx.pid, label=name),
        name=name,
        age=_age(r["DOB"]),
        sex=(r["sex"] or None),
        dob=dob,
    )
