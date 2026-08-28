from modules.matcher_guard import identity_evidence, validate_document
from modules.rule_classifier import classify_rule
from models.rule_models import PrintRule


def _rule(**kw):
    base=dict(id='r',sheet='s',row_index=1,item='FIELD',instruction='PRINT <Value>')
    base.update(kw)
    return PrintRule(**base)


def test_generic_typo_neighbor_country_county_is_rejected():
    e=identity_evidence({'item':'COUNTRY'}, {'di_key':'County'})
    assert not e['confident']
    assert any('generic-only labels differ' in x for x in e['reasons'])


def test_generic_exact_label_is_accepted():
    assert identity_evidence({'item':'COUNTRY'}, {'di_key':'Country'})['confident']


def test_address_with_comma_words_is_not_date():
    r=_rule(item='OFFICIANT MAILING ADDRESS - Street Address or PO Box, City, State, and ZIP Code', instruction='PRINT <Address> FORMAT: <Street> comma <City> comma <State> <Zip Code>', example='123 MAIN STREET, MADISON, WI 53033')
    assert classify_rule(r).rule_type == 'FIELD_TEXT'


def test_real_date_remains_date():
    r=_rule(item='DATE OF EVENT', instruction='PRINT <Date> FORMAT: <month> space <dd> comma space <yyyy>', example='APRIL 27, 2026')
    assert classify_rule(r).rule_type == 'DATE_FORMAT'


def test_shared_box_for_different_keys_is_not_used_for_overlay():
    bbox={'page':1,'x0':.1,'y0':.2,'x1':.3,'y1':.4,'normalized':True}
    doc={'comparisons':[
        {'id':'1','status':'PASS','matched':True,'match_score':100,'rule_type':'FIELD_TEXT','item':'EMAIL','di_key':'EMAIL','page':1,'bbox':bbox},
        {'id':'2','status':'PASS','matched':True,'match_score':100,'rule_type':'FIELD_TEXT','item':'PHONE','di_key':'PHONE','page':1,'bbox':bbox},
    ]}
    out=validate_document(doc)
    assert all(r['bbox'] is None for r in out['comparisons'])
    assert all(r['metadata']['geometry_trusted'] is False for r in out['comparisons'])
