import re
from models.rule_models import PrintRule
from utils.text_utils import norm
from utils.date_utils import infer_date_pattern

TYPO_HINTS = ["PARENT", "LASE", "DESIGNATORR", "OFFCIANT", "EDUCATIONN", "ORIGN", "SPECIFIY"]

_DATE_COMPONENT_RE = re.compile(r"\b(MONTH|MM)\b.*\b(DD|DAY)\b.*\b(YYYY|YEAR)\b", re.I | re.S)
_DATE_EXAMPLE_RE = re.compile(
    r"\b(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    re.I,
)

def _has_strong_date_evidence(rule: PrintRule) -> bool:
    """Require date semantics, not formatting punctuation such as 'comma'."""
    if rule.expected_kind == "date":
        return True
    if _DATE_EXAMPLE_RE.search(rule.example or ""):
        return True
    instruction = rule.instruction or ""
    return bool(_DATE_COMPONENT_RE.search(instruction))

def _part_schema_is_date(rule: PrintRule) -> bool:
    parts = {norm(x) for x in (rule.part_labels or []) if norm(x)}
    date_parts = {"MONTH", "MM", "DAY", "DD", "YEAR", "YYYY"}
    return bool(parts) and parts.issubset(date_parts) and bool(parts & {"YEAR", "YYYY"})


def classify_rule(rule: PrintRule) -> PrintRule:
    blob = norm(" ".join([rule.item or "", rule.instruction or "", rule.example or "", rule.if_missing or "", rule.if_unknown or ""]))
    instruction_blob = norm(" ".join([rule.instruction or "", rule.if_missing or "", rule.if_unknown or ""]))
    if "NO PRINT RULE" in blob or "NO PRINT RULES" in blob or "LEAVE BLANK" in blob:
        rule.rule_type = "NO_PRINT_RULE"
    elif (rule.expected_kind == "checkbox_group" and len(rule.expected_options or []) >= 2) or "CHECKBOX" in instruction_blob or ("PLACE" in instruction_blob and "X" in instruction_blob) or "CHECK ALL" in instruction_blob:
        rule.rule_type = "CHECKBOX"
    elif rule.part_labels and not _part_schema_is_date(rule):
        # A workbook-declared multi-part schema such as First/Middle/Last/Suffix
        # or Street/City/State/ZIP is structural evidence for composite text.
        # It takes precedence over accidental date-like noise from adjacent cells.
        rule.rule_type = "FIELD_TEXT"
    elif _has_strong_date_evidence(rule):
        rule.rule_type = "DATE_FORMAT"
    elif "PRINT <" in blob or "PRINT PARTY" in blob or "FORMAT:" in blob:
        rule.rule_type = "FIELD_TEXT"
    elif rule.item and not rule.instruction and not re.search(r"<[^>]+>", blob):
        rule.rule_type = "STATIC_TEXT"
    else:
        rule.rule_type = "TEXT_OR_LAYOUT"
    return rule

def classify_rules(rules: list[PrintRule]) -> list[PrintRule]:
    return [classify_rule(r) for r in rules]

def detect_excel_rule_issues(rules: list[PrintRule]) -> list[dict]:
    issues = []
    for r in rules:
        blob = norm(" ".join([r.section or "", r.subsection or "", r.item or "", r.instruction or "", r.example or ""]))
        if "PARTY B" in blob and "<PARTY A" in blob:
            issues.append({"rule_id": r.id, "issue": "Possible copy/paste issue: Party B rule references Party A field.", "row": r.row_index})
        if "OTHER/ALTERNATE" in blob and "EMAIL TO OFFICIANT CHECKBOX" in blob:
            issues.append({"rule_id": r.id, "issue": "Possible contradictory checkbox rule: Other/Alternate maps to Email to Officiant checkbox.", "row": r.row_index})
        for hint in TYPO_HINTS:
            if hint in blob and hint not in {"PARENT"}:  # parent is valid too; leave typo list extensible
                issues.append({"rule_id": r.id, "issue": f"Possible typo detected: {hint}.", "row": r.row_index})
    return issues
