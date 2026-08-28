from __future__ import annotations
from collections import Counter
from config.constants import Status
from models.comparison_models import CheckResult
from utils.text_utils import norm

def enhance_summary(summary,rules,analysis,sheet):
    rule_by_id={r.id:r for r in rules}; full=norm(analysis.full_text or "")
    for c in summary.comparisons:
        rule=rule_by_id.get(c.rule_id)
        if not rule: continue
        # Fixed document content is recovered only by exact normalized presence.
        # A leaked workbook section does not turn a title/header into a data field.
        instruction=norm(getattr(rule,"instruction",None) or "")
        static_like=rule.rule_type in {"STATIC_TEXT","TEXT_OR_LAYOUT"} and not instruction
        if not c.matched and static_like:
            item=norm(rule.item or "")
            if item and item in full:
                c.matched=True; c.status=Status.PASS; c.di_kind="static_text"; c.di_key=rule.item; c.di_value=rule.item; c.match_score=100.0; c.page=1
                c.checks=[CheckResult(name="static_text_presence",status=Status.PASS,expected=rule.item,actual="located exactly in PDF text",message="Static text located exactly in the PDF text.")]
                c.message=c.checks[0].message; c.metadata["recovery_source"]="EXACT_FULL_TEXT"; c.metadata["static_text_exact"]=True
    summary.matched_count=sum(c.matched for c in summary.comparisons)
    summary.status_counts=dict(Counter(c.status.value for c in summary.comparisons))
    summary.unmatched_rules=[{"rule_id":c.rule_id,"section":c.section,"subsection":c.subsection,"item":c.item,"rule_type":c.rule_type} for c in summary.comparisons if not c.matched and c.rule_type!="NO_PRINT_RULE"]
    return summary
