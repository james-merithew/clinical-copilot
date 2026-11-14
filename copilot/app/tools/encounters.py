"""Care-transitions tool — recent encounters (office / ED / inpatient)."""

from __future__ import annotations

from ..config import get_settings
from ..contracts import CareTransitionsResult, Encounter, SourceRef
from ..db import query


def _kind(pos_code, class_code, etype, reason) -> str:
    blob = " ".join(str(x or "").lower() for x in (pos_code, class_code, etype, reason))
    # POS 23 = ER, 21 = inpatient hospital; plus text heuristics for CCDA data.
    if "23" in str(pos_code or "") or any(k in blob for k in ("emerg", "er ", " ed")):
        return "ed"
    if "21" in str(pos_code or "") or any(k in blob for k in ("inpatient", "admission", "hospital")):
        return "inpatient"
    return "office"


def get_care_transitions(ctx) -> CareTransitionsResult:  # noqa: ANN001
    s = get_settings()
    cap = s.phase1_encounter_cap * 3
    rows = query(
        """
        SELECT fe.id, fe.date, fe.reason, fe.pos_code, fe.class_code,
               fe.encounter_type_description AS etype,
               NULLIF(TRIM(CONCAT_WS(' ', u.fname, u.lname)), '') AS provider
        FROM form_encounter fe
        LEFT JOIN users u ON u.id = fe.provider_id
        WHERE fe.pid = :pid
        ORDER BY fe.date DESC
        LIMIT :cap
        """,
        {"pid": ctx.pid, "cap": cap},
    )
    encs: list[Encounter] = []
    for r in rows:
        date = str(r["date"])[:10] if r["date"] else ""
        kind = _kind(r["pos_code"], r["class_code"], r["etype"], r["reason"])
        encs.append(
            Encounter(
                source=SourceRef(
                    table="form_encounter",
                    primary_key=int(r["id"]),
                    label=f"{kind} visit {date}".strip(),
                ),
                date=date,
                kind=kind,  # type: ignore[arg-type]
                reason=(r["reason"] or None),
                provider=r["provider"],
            )
        )
    return CareTransitionsResult(encounters=encs, truncated=len(rows) >= cap)
