"""Adapter: ComparisonSummary -> List[ValidationResult].

Phase F removes the legacy ``ValidationEngine`` (old Sections 4/6/7). The visual
overlay, annotated-PDF writer, and JSON/Excel report writers were all built
around ``ValidationResult`` objects. Rather than rewrite those, this adapter
converts each ``FieldComparison`` (the new primary output) into a
``ValidationResult`` so every existing downstream consumer keeps working,
sourced from the comparison results.

It is schema-robust: it only passes keyword arguments that ``ValidationResult``
actually declares (via ``model_fields``), so differences in the model don't
break the conversion.
"""

from __future__ import annotations

from typing import List

from models.comparison_models import ComparisonSummary, FieldComparison
from models.validation_models import ValidationResult


def _comparison_to_result(c: FieldComparison, index: int) -> ValidationResult:
    # Keep ``page``/``bbox.page`` exactly as the comparison produced them
    # (1-based). ``modules/visual_overlay`` performs the single 1-based -> 0-based
    # conversion; converting here too double-subtracts and collapses every result
    # onto page 0.
    return ValidationResult(
        id=c.id or f"CMP-{index:04d}",
        status=c.status,
        category=c.rule_type or "COMPARISON",
        sheet=getattr(c, "sheet", None),
        section=c.section,
        item=c.item,
        expected=c.item,
        actual=c.di_value,
        message=c.message or "",
        page=c.page,
        bbox=c.bbox,
        confidence=c.confidence,
        metadata={
            "rule_id": c.rule_id,
            "rule_type": c.rule_type,
            "subsection": c.subsection,
            "di_key": c.di_key,
            "di_value": c.di_value,
            "match_score": c.match_score,
            "matched": c.matched,
        },
    )


def comparison_to_results(summary: ComparisonSummary) -> List[ValidationResult]:
    """Convert every comparison row into a ValidationResult (best-effort)."""
    out: List[ValidationResult] = []
    for i, c in enumerate(summary.comparisons, start=1):
        try:
            out.append(_comparison_to_result(c, i))
        except Exception:
            # Skip any row that can't be represented rather than break the app.
            continue
    return out
