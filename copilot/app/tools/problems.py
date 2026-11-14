"""Problems + allergies tool — `lists` (type=medical_problem / allergy)."""

from __future__ import annotations

from ..contracts import Allergy, Problem, ProblemsResult, SourceRef
from ..db import query


def get_problems_allergies(ctx) -> ProblemsResult:  # noqa: ANN001
    probs = query(
        """
        SELECT id, title, begdate, activity
        FROM lists
        WHERE pid = :pid AND type = 'medical_problem'
          AND title IS NOT NULL AND title <> ''
        ORDER BY COALESCE(begdate, '1900-01-01') DESC
        """,
        {"pid": ctx.pid},
    )
    algs = query(
        """
        SELECT id, title, reaction, activity
        FROM lists
        WHERE pid = :pid AND type = 'allergy'
          AND title IS NOT NULL AND title <> ''
        """,
        {"pid": ctx.pid},
    )
    problems = [
        Problem(
            source=SourceRef(table="lists", primary_key=int(r["id"]), label=f"Problem: {r['title']}"),
            title=r["title"],
            status=("active" if str(r["activity"]) == "1" else "inactive"),
            onset=(str(r["begdate"])[:10] if r["begdate"] else None),
        )
        for r in probs
    ]
    allergies = [
        Allergy(
            source=SourceRef(table="lists", primary_key=int(r["id"]), label=f"Allergy: {r['title']}"),
            substance=r["title"],
            reaction=(r["reaction"] or None),
        )
        for r in algs
    ]
    return ProblemsResult(problems=problems, allergies=allergies, truncated=False)
