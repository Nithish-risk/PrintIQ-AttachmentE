from models.rule_models import PrintRule
from modules.rule_classifier import classify_rule

def test_checkbox_rule():
    r = PrintRule(id="1", sheet="s", row_index=1, item="Waiver", instruction="THEN place X in Yes checkbox")
    assert classify_rule(r).rule_type == "CHECKBOX"
