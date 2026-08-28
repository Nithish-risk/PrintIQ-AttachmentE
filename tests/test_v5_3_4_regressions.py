from types import SimpleNamespace
from modules.rule_classifier import classify_rule
from modules.reviewer_outcomes import _is_technical
from models.rule_models import PrintRule

def _rule(**kwargs):
    base = dict(id='r1', sheet='s', row_index=1, item='FIELD', instruction='PRINT <Value>')
    base.update(kwargs)
    return PrintRule(**base)

def test_text_field_is_not_checkbox_from_item_words_alone():
    rule = _rule(item='BIRTHPLACE - U.S. State/Territory or Canadian Province')
    assert classify_rule(rule).rule_type == 'FIELD_TEXT'

def test_address_is_not_classified_as_date():
    rule = _rule(item='OFFICIANT MAILING ADDRESS - Street Address, City, State, ZIP Code', instruction='PRINT <Address> FORMAT: <Street> comma <City> comma <State> <Zip Code>', example='123 MAIN STREET, MADISON, WI 53033')
    assert classify_rule(rule).rule_type == 'FIELD_TEXT'

def test_real_date_rule_remains_date():
    rule = _rule(item='DATE OF MARRIAGE', instruction='PRINT <Date> FORMAT: <month> space <dd> comma space <yyyy>', example='APRIL 27, 2026')
    assert classify_rule(rule).rule_type == 'DATE_FORMAT'

def test_no_print_rule_is_not_a_reviewer_work_item():
    assert _is_technical(SimpleNamespace(rule_type='NO_PRINT_RULE', rule_id='r1'))
