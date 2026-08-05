# filepath: c:\Users\kumarn40\OneDrive - Reed Elsevier Group ICO Reed Elsevier Inc\Desktop\Gen AI\VITALIQ_printiq_data\V 2.1\modules\rule_repair.py
"""LLM repair of Excel print rules flagged with typos/contradictions.

``detect_excel_rule_issues`` (rule_classifier) surfaces rows that look wrong
(misspellings, Party A/B copy-paste contradictions). Instead of emitting a
separate ``EXCEL_RULE_ISSUE`` status, we send those rows to Azure OpenAI, get a
meaning-preserving corrected version, and **replace the original rule** so the
downstream comparison validates against the cleaned rule.

Fail-safe: when the LLM is unavailable or returns something unusable, the
original rule is kept unchanged.
"""

from __future__ import annotations

from typing import List

from models.rule_models import PrintRule
from modules.rule_classifier import detect_excel_rule_issues, classify_rule
from modules.azure_openai_helper import AzureOpenAIHelper

# Text fields the model is allowed to correct.
_REPAIRABLE_FIELDS = (
    "section", "subsection", "item", "if_missing", "if_unknown",
    "instruction", "label_printed", "example",
)


def repair_rules(rules: List[PrintRule], helper: AzureOpenAIHelper | None = None) -> List[PrintRule]:
    """Return a new rule list with flagged rows LLM-corrected in place.

    Each corrected rule is re-classified so its ``rule_type`` reflects the fixed
    text. Rows with no detected issue pass through untouched.
    """
    issues = detect_excel_rule_issues(rules)
    if not issues:
        return rules

    helper = helper or AzureOpenAIHelper()
    if not helper.enabled:
        return rules

    # Map rule_id -> combined issue description (a rule may have several).
    issues_by_id: dict[str, list[str]] = {}
    for issue in issues:
        issues_by_id.setdefault(issue.get("rule_id"), []).append(issue.get("issue", ""))

    repaired: List[PrintRule] = []
    for rule in rules:
        rule_issues = issues_by_id.get(rule.id)
        if not rule_issues:
            repaired.append(rule)
            continue

        payload = {k: getattr(rule, k) for k in _REPAIRABLE_FIELDS}
        corrected = helper.repair_rule(payload, "; ".join(i for i in rule_issues if i))

        # Build a corrected PrintRule, keeping non-text fields intact and
        # recording the repair in ``raw`` for auditability.
        data = rule.model_dump()
        for k in _REPAIRABLE_FIELDS:
            if k in corrected:
                data[k] = corrected[k]
        data.setdefault("raw", {})
        data["raw"] = {**(data.get("raw") or {}),
                       "_repaired": True,
                       "_repair_issues": rule_issues,
                       "_original": payload}
        new_rule = classify_rule(PrintRule(**data))
        repaired.append(new_rule)

    return repaired
