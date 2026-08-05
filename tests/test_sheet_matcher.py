from modules.sheet_matcher import FORM_HINTS

def test_form_hints_present():
    assert "Marriage Application" in FORM_HINTS
    assert "Court Ordered Amendment" in FORM_HINTS
