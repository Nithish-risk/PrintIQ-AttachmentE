import re
from models.rule_models import PrintRule
from utils.text_utils import norm
from utils.date_utils import infer_date_pattern

TYPO_HINTS = ["PARENT", "LASE", "DESIGNATORR", "OFFCIANT", "EDUCATIONN", "ORIGN", "SPECIFIY"]

def classify_rule(rule: PrintRule) -> PrintRule:
    blob = norm(" ".join([rule.item or "", rule.instruction or "", rule.example or "", rule.if_missing or "", rule.if_unknown or ""]))
    if "NO PRINT RULE" in blob or "NO PRINT RULES" in blob or "LEAVE BLANK" in blob:
        rule.rule_type = "NO_PRINT_RULE"
    elif "CHECKBOX" in blob or "PLACE" in blob and "X" in blob or "CHECK ALL" in blob:
        rule.rule_type = "CHECKBOX"
    elif infer_date_pattern(rule.instruction, rule.example, rule.item):
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
