from rapidfuzz import fuzz
from .models import FieldMatch,WorkflowState
def n(v): return ' '.join(str(v or '').upper().split())
class SequenceAwareMatcher:
    def match(self,rules,fields):
        pairs=[]
        for r in rules:
            for f in fields:
                label=fuzz.token_set_ratio(n(r.item_name),n(f.label_text))/100
                sec=1 if not r.section or n(r.section)==n(f.section) else 0
                order=max(0,1-abs(r.document_order_index-(f.document_order_index or 0))/8)
                pairs.append((.65*label+.2*sec+.15*order,r,f))
        pairs.sort(key=lambda x:x[0],reverse=True); rr=set(); ff=set(); chosen={}
        for score,r,f in pairs:
            if r.id not in rr and f.id not in ff:
                chosen[r.id]=(score,f); rr.add(r.id); ff.add(f.id)
        return [FieldMatch(rule_id=r.id,field_id=chosen[r.id][1].id if r.id in chosen else None,score=round(chosen[r.id][0],4) if r.id in chosen else 0,workflow_state=WorkflowState.MATCHED if r.id in chosen and chosen[r.id][0]>=.55 else WorkflowState.REVIEW_REQUIRED,reasons=[] if r.id in chosen and chosen[r.id][0]>=.55 else ['low_match_confidence']) for r in rules]
