from config.constants import Status
from models.comparison_models import CheckResult, FieldComparison
from modules.checkbox_field_repair import _normalise_regrouped
from modules.matcher_guard import validate_record
from modules.reviewer_outcomes import reviewer_status


def _comparison(**kw):
    base=dict(id='c',status=Status.PASS,rule_id='r',rule_type='FIELD_TEXT',item='NAME - First, Middle, Last',matched=True,match_score=100,di_kind='text',di_key='NAME - First, Middle, Last',di_value='UNKNOWN',bbox=None,checks=[CheckResult(name='presence',status=Status.UNKNOWN_DATA,message='unknown')],metadata={'identity_guard':{'confident':True,'label_score':100,'reasons':[]},'composite_expected_parts':['First','Middle','Last'],'composite_complete':True,'if_unknown_rule':'If all fields are Unknown then print a single line of Unknown aligned under First'})
    base.update(kw); return FieldComparison(**base)


def test_reviewer_blocks_incomplete_composite_even_if_guard_claims_confident():
    c=_comparison(metadata={'identity_guard':{'confident':True,'label_score':100,'reasons':[]},'composite_expected_parts':['First','Middle','Last'],'composite_complete':False})
    final,_=reviewer_status(c)
    assert final == Status.REVIEW_REQUIRED


def test_guard_clears_incomplete_composite():
    row=_comparison(metadata={'composite_expected_parts':['First','Middle','Last'],'composite_detected_parts':['NAME - First'],'composite_complete':False}).model_dump(mode='json')
    out=validate_record(row)
    assert not out['matched'] and out['di_key'] is None and out['status']=='REVIEW_REQUIRED'


def test_checkbox_wrapper_unwraps_options_only():
    x=_normalise_regrouped({'options':[{'option':'Yes','selected':True},{'option':'No','selected':False}],'matched':2,'expected':2,'confidence':.9})
    assert [i['option'] for i in x] == ['Yes','No']


def test_checkbox_wrapper_without_options_is_rejected():
    assert _normalise_regrouped({'matched':5,'expected':7,'confidence':.8}) == []


def test_malformed_checkbox_metadata_labels_are_rejected():
    row={'id':'c','status':'PASS','rule_type':'CHECKBOX','item':'HISPANIC ORIGIN','matched':True,'match_score':100,'di_kind':'checkbox_group','di_key':'HISPANIC ORIGIN','di_value':'options-selected;matched-selected;expected-selected;confidence-selected','checks':[],'metadata':{}}
    assert validate_record(row)['status']=='REVIEW_REQUIRED'


def test_single_unknown_without_alignment_is_review():
    final,_=reviewer_status(_comparison())
    assert final == Status.REVIEW_REQUIRED


def test_repeated_unknown_is_deterministic_fail():
    final,_=reviewer_status(_comparison(di_value='UNKNOWN UNKNOWN'))
    assert final == Status.FAIL


def test_example_only_dropdown_failure_is_review_not_fail():
    c=_comparison(status=Status.FAIL,checks=[CheckResult(name='instruction',status=Status.FAIL,message='differs from example')])
    c.metadata['instruction_validation_mode']='FORMAT_EXAMPLE_ONLY'
    final,_=reviewer_status(c)
    assert final == Status.REVIEW_REQUIRED
