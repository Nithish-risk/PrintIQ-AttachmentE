from config.constants import Status
from models.comparison_models import CheckResult, FieldComparison
from modules.field_grouping import collect_group, merge_group
from modules.reviewer_outcomes import reviewer_status


def _f(order,key,x):
    return {"order":order,"kind":"text","key":key,"value":"UNKNOWN","page":1,
            "bbox":[x,.7,x+.2,.71],"section":"MARRIAGE","subsection":"OFFICIANT",
            "raw":{"bbox":[x,.7,x+.2,.71]}}


def test_complete_sibling_is_not_consumed_as_child_part():
    w1=_f(1,"WITNESS 1 NAME - First, Middle, Last",.1)
    w2=_f(2,"WITNESS 2 NAME - First, Middle, Last",.55)
    group=collect_group(w1,["First","Middle","Last"],[w1,w2],set())
    assert [x["order"] for x in group] == [1]


def test_short_component_fields_still_group():
    a=_f(1,"CURRENT NAME- First",.1); m=_f(2,"Middle",.3); l=_f(3,"Last",.5)
    group=collect_group(a,["First","Middle","Last"],[a,m,l],set())
    assert [x["order"] for x in group] == [1,2,3]


def test_atomic_exact_composite_is_complete():
    f=_f(1,"OFFICIANT NAME - First, Middle, Last",.1)
    m=merge_group([f],["First","Middle","Last"],canonical_key="OFFICIANT NAME - First, Middle, Last")
    assert m["_composite_complete"] is True
    assert m["_candidate_reconstruction_source"] == "ATOMIC_COMPLETE_LABEL"


def _comparison(item,key,guard_score=100):
    return FieldComparison(id="c",status=Status.MISSING_DATA,rule_id="r",rule_type="FIELD_TEXT",
        item=item,matched=True,match_score=90,di_kind="text",di_key=key,di_value="",checks=[
            CheckResult(name="presence",status=Status.MISSING_DATA,message="blank")],metadata={
            "v4_workflow_state":"MATCHED","identity_guard":{"confident":True,"label_score":guard_score,"reasons":[]}})


def test_reviewer_uses_guard_for_punctuation_normalized_identity():
    final,meta=reviewer_status(_comparison("CITY, VILLAGE,TOWN","CITY, VILLAGE, TOWN"))
    assert final == Status.PASS
    assert meta["identity_source"] == "matcher_guard"


def test_guard_authority_handles_source_typo_without_lowering_safety():
    final,_=reviewer_status(_comparison("FATHER/PARENT BIRTH NAME - First, Middle, Lase, Suffix",
                                       "FATHER/PARENT BIRTH NAME - First, Middle, Last, Suffix",97.96))
    assert final == Status.PASS


def test_guard_rejection_still_blocks_pass():
    c=_comparison("COUNTRY","COUNTY",92.31)
    c.metadata["identity_guard"]={"confident":False,"label_score":92.31,"reasons":["generic-only labels differ"]}
    final,_=reviewer_status(c)
    assert final == Status.REVIEW_REQUIRED
