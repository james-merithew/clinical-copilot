"""Labs tool — `procedure_result` via the order → report → result chain."""

from __future__ import annotations

from ..contracts import LabResult, LabsResult, SourceRef
from ..db import query

_ABNORMAL = {"1", "a", "h", "l", "hh", "ll", "abnormal", "high", "low", "true", "yes"}


def get_labs(ctx, name: str | None = None) -> LabsResult:  # noqa: ANN001
    params: dict = {"pid": ctx.pid}
    name_clause = ""
    if name and name.strip():
        name_clause = "AND (pr.result_text LIKE :nm OR pr.result_code LIKE :nm)"
        params["nm"] = f"%{name.strip()}%"

    rows = query(
        f"""
        SELECT pr.procedure_result_id AS id, pr.result_text, pr.result_code,
               pr.result, pr.units, pr.range, pr.abnormal, pr.date
        FROM procedure_result pr
        JOIN procedure_report rep
          ON rep.procedure_report_id = pr.procedure_report_id
        JOIN procedure_order po
          ON po.procedure_order_id = rep.procedure_order_id
        WHERE po.patient_id = :pid
          AND pr.result_text IS NOT NULL AND pr.result_text <> ''
          {name_clause}
        ORDER BY pr.date DESC
        LIMIT 50
        """,
        params,
    )
    labs: list[LabResult] = []
    for r in rows:
        nm = r["result_text"] or r["result_code"] or "lab"
        date = str(r["date"])[:10] if r["date"] else None
        labs.append(
            LabResult(
                source=SourceRef(
                    table="procedure_result",
                    primary_key=int(r["id"]),
                    label=f"{nm} {date or ''}".strip(),
                ),
                name=nm,
                value=(str(r["result"]) if r["result"] is not None else None),
                units=(r["units"] or None),
                range=(r["range"] or None),
                abnormal=(str(r["abnormal"] or "").strip().lower() in _ABNORMAL),
                date=date,
            )
        )
    return LabsResult(labs=labs, truncated=len(rows) >= 50)
