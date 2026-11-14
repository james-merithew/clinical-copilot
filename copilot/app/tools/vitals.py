"""Vitals tool — recent readings from `form_vitals` (read-only, source-attributed).

Internal only (not a model-facing tool). Feeds the metric cards + the abnormal-
vital notifications.
"""

from __future__ import annotations

from ..contracts import SourceRef, Vital, VitalsResult
from ..db import query


def _f(v) -> float | None:  # noqa: ANN001
    try:
        f = float(v)
        return f if f != 0 else None  # OpenEMR stores 0 for "not recorded"
    except Exception:  # noqa: BLE001
        return None


def get_vitals(ctx) -> VitalsResult:  # noqa: ANN001
    rows = query(
        """
        SELECT id, date, bps, bpd, pulse, weight, height, temperature,
               BMI AS bmi, oxygen_saturation
        FROM form_vitals
        WHERE pid = :pid AND activity = 1
        ORDER BY date DESC
        LIMIT 30
        """,
        {"pid": ctx.pid},
    )
    vitals: list[Vital] = []
    for r in rows:
        date = str(r["date"])[:10] if r["date"] else None
        vitals.append(
            Vital(
                source=SourceRef(
                    table="form_vitals", primary_key=int(r["id"]), label=f"Vitals {date or ''}".strip()
                ),
                date=date,
                bps=_f(r["bps"]),
                bpd=_f(r["bpd"]),
                pulse=_f(r["pulse"]),
                weight=_f(r["weight"]),
                height=_f(r["height"]),
                bmi=_f(r["bmi"]),
                temperature=_f(r["temperature"]),
                spo2=_f(r["oxygen_saturation"]),
            )
        )
    return VitalsResult(vitals=vitals, truncated=len(rows) >= 30)
