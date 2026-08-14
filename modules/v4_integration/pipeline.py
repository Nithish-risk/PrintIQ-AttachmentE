from collections import Counter
from modules.comparison_engine import ComparisonEngine
from printiq_core.reading_order import assign_reading_order
from printiq_core.matcher import SequenceAwareMatcher
from .adapters import adapt_rules,adapt_fields
class V4Pipeline:
    def run(self,sheet,rules,analysis,native=None):
        source_fields=list(analysis.structured_fields or [])
        # Critical fail-safe: V4 enrichment must never remove DI fields before V3 validation.
        summary=ComparisonEngine(sheet,rules,source_fields,key_value_pairs=analysis.key_value_pairs).run()
        cr=adapt_rules(rules); lf=assign_reading_order(adapt_fields(source_fields))
        matches=SequenceAwareMatcher().match(cr,lf) if lf else []
        by_rule={m.rule_id:m for m in matches}
        for c in summary.comparisons:
            m=by_rule.get(c.rule_id)
            c.metadata.update({'v4_workflow_state':m.workflow_state.value if m else 'NOT_APPLICABLE','v4_match_score':m.score if m else 0,'v4_reasons':m.reasons if m else [],'v4_source_field_count':len(source_fields),'v4_adapted_field_count':len(lf)})
        summary.status_counts=dict(Counter(c.status.value for c in summary.comparisons))
        summary.llm_findings.append({'type':'v4_diagnostic','excel_rule_count':len(cr),'pdf_field_count':len(source_fields),'adapted_pdf_field_count':len(lf),'counts_match':len(cr)==len(source_fields),'review_required':sum(1 for m in matches if m.workflow_state.value=='REVIEW_REQUIRED')})
        return summary
