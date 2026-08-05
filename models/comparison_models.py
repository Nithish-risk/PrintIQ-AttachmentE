# filepath: c:\Users\kumarn40\OneDrive - Reed Elsevier Group ICO Reed Elsevier Inc\Desktop\Gen AI\VITALIQ_printiq_data\V 2.1\models\comparison_models.py
"""Models for comparing parsed Excel print rules against DI structured fields.

The comparison layer pairs each ``PrintRule`` (the *expected* specification)
with the best-matching DI ``structured_field`` (the *actual* extracted output),
then records per-check outcomes. Everything is PDF-only: we can verify
presence, format, pattern and formatting — never value-correctness against an
external source of truth.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from config.constants import Status
from models.validation_models import BBox


class CheckResult(BaseModel):
    """A single named check (e.g. presence, date_pattern, max_chars, bold)."""

    name: str
    status: Status
    expected: Optional[str] = None
    actual: Optional[str] = None
    message: str = ""


class FieldComparison(BaseModel):
    """One aligned rule ↔ DI-field comparison row."""

    id: str
    status: Status  # roll-up status across all checks
    category: str = "Comparison"
    sheet: Optional[str] = None

    # --- Excel rule (expected) side ---
    rule_id: Optional[str] = None
    rule_type: Optional[str] = None
    section: Optional[str] = None
    subsection: Optional[str] = None
    item: Optional[str] = None

    # --- DI structured_field (actual) side ---
    matched: bool = False
    match_score: float = 0.0
    di_kind: Optional[str] = None
    di_section: Optional[str] = None
    di_subsection: Optional[str] = None
    di_key: Optional[str] = None
    di_value: Optional[str] = None

    # --- geometry for overlay ---
    page: Optional[int] = None
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None

    checks: List[CheckResult] = Field(default_factory=list)
    message: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ComparisonSummary(BaseModel):
    """Aggregate view: matched comparisons plus two-directional coverage."""

    sheet: Optional[str] = None
    total_rules: int = 0
    total_fields: int = 0
    matched_count: int = 0
    comparisons: List[FieldComparison] = Field(default_factory=list)
    # Rules with no matched DI field (potentially missing printed output).
    unmatched_rules: List[Dict[str, Any]] = Field(default_factory=list)
    # DI fields matched by no rule (potentially extra/unexpected output).
    unmatched_fields: List[Dict[str, Any]] = Field(default_factory=list)
    status_counts: Dict[str, int] = Field(default_factory=dict)
    # Phase C.2: optional LLM verification findings, list of {rule_id, note}.
    llm_findings: List[Dict[str, Any]] = Field(default_factory=list)