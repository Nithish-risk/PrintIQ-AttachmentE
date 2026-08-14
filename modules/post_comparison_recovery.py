"""Evidence-based comparison recovery after the standard ComparisonEngine run.

Recovers static text from page text, duplicate/unmatched rules from unused
structured fields, and checkbox rules whose current group has incompatible
options. It never changes Azure DI extraction and records every recovery in
metadata.
"""
from __future__ import annotations
from copy import deepcopy
from rapidfuzz import fuzz
from config.constants import Status
from models.comparison_models import CheckResult
from modules.comparison_engine import ComparisonEngine
from utils.text_utils import clean_text, norm

def _value(field):
    v=field.get("value")
    if isinstance(v,dict): return " ".join(str(x) for x in v.values() if x)
    return clean_text(v or "")
def _key(field): return clean_text(field.get("key") or "")
def _options(value):
    out=set()
    for chunk in str(value or "").split(";"):
        label=chunk.rsplit("-",1)[0].strip()
        if label: out.add(norm(label))
    return out
def _expected(rule): return {norm(x) for x in (rule.expected_options or []) if norm(x)}
def _candidate_score(rule,field,rule_pos,total_rules,field_pos,total_fields):
    label=fuzz.token_set_ratio(norm(rule.item or ""),norm(_key(field)))/100
    section=.15 if not rule.section or norm(rule.section)==norm(field.get("section") or "") else 0
    subsection=.10 if not rule.subsection or norm(rule.subsection)==norm(field.get("subsection") or "") else 0
    expected=_expected(rule); actual=_options(_value(field))
    if expected:
        option=len(expected & actual)/len(expected) if actual else 0
        if actual and not expected.intersection(actual): return 0
    else: option=.5
    rp=rule_pos/max(1,total_rules-1); fp=field_pos/max(1,total_fields-1)
    order=max(0,1-abs(rp-fp)*3)
    return .52*label+section+subsection+.15*option+.08*order

def enhance_summary(summary,rules,analysis,sheet):
    fields=list(analysis.structured_fields or [])
    rule_by_id={r.id:r for r in rules}
    used_keys={(c.di_kind,c.di_key,c.page,clean_text(c.di_value or "")) for c in summary.comparisons if c.matched}
    full_norm=norm(analysis.full_text or "")
    total_rules=len(rules); total_fields=len(fields)
    for pos,c in enumerate(summary.comparisons):
        rule=rule_by_id.get(c.rule_id)
        if not rule or rule.rule_type=="NO_PRINT_RULE": continue
        # Static/document text is validated against page text, not KV fields.
        if not c.matched and rule.rule_type in {"STATIC_TEXT","TEXT_OR_LAYOUT"}:
            item_norm=norm(rule.item or "")
            score=fuzz.partial_ratio(item_norm,full_norm) if item_norm else 0
            if item_norm and (item_norm in full_norm or score>=92):
                c.matched=True; c.status=Status.PASS; c.di_kind="static_text"; c.di_key=rule.item; c.di_value=rule.item; c.match_score=float(score); c.page=1
                c.checks=[CheckResult(name="static_text_presence",status=Status.PASS,expected=rule.item,actual="located in page text",message="Static text located in the PDF page text.")]
                c.message=c.checks[0].message; c.metadata["recovery_source"]="FULL_TEXT"; continue
        expected=_expected(rule)
        current_actual=_options(c.di_value) if c.matched else set()
        incompatible=bool(expected and current_actual and not expected.intersection(current_actual))
        if c.matched and not incompatible: continue
        best=None; best_score=0
        for fi,field in enumerate(fields):
            identity=(field.get("kind"),_key(field),field.get("page",1),_value(field))
            # Allow an already-used field only when repairing a demonstrably incompatible checkbox.
            if identity in used_keys and not incompatible: continue
            score=_candidate_score(rule,field,pos,total_rules,fi,total_fields)
            if score>best_score: best_score=score; best=field
        threshold=.78 if expected else .84
        if not best or best_score<threshold: continue
        candidate=deepcopy(best)
        # Exact/strong label evidence permits filling missing hierarchy for the isolated re-check.
        if fuzz.token_set_ratio(norm(rule.item or ""),norm(_key(candidate)))>=92:
            candidate["section"]=candidate.get("section") or rule.section
            candidate["subsection"]=candidate.get("subsection") or rule.subsection
        rerun=ComparisonEngine(sheet,[rule],[candidate],key_value_pairs=analysis.key_value_pairs).run()
        if not rerun.comparisons or not rerun.comparisons[0].matched: continue
        recovered=rerun.comparisons[0]
        old_meta=dict(c.metadata or {})
        old_meta.update({"recovery_source":"UNUSED_STRUCTURED_FIELD","recovery_score":round(best_score,4),"previous_di_key":c.di_key,"previous_di_value":c.di_value})
        for name in c.model_fields:
            if name in {"id","rule_id"}: continue
            setattr(c,name,getattr(recovered,name))
        c.metadata.update(old_meta)
        used_keys.add((c.di_kind,c.di_key,c.page,clean_text(c.di_value or "")))
    summary.matched_count=sum(1 for c in summary.comparisons if c.matched)
    from collections import Counter
    summary.status_counts=dict(Counter(c.status.value for c in summary.comparisons))
    summary.unmatched_rules=[{"rule_id":c.rule_id,"section":c.section,"subsection":c.subsection,"item":c.item,"rule_type":c.rule_type} for c in summary.comparisons if not c.matched and c.rule_type!="NO_PRINT_RULE"]
    return summary
