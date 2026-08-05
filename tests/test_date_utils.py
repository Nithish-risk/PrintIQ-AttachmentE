from utils.date_utils import infer_date_pattern, validate_date

def test_date_patterns():
    assert infer_date_pattern("FORMAT: MM/DD/YYYY", "") == "MM/DD/YYYY"
    assert validate_date("04/27/2026", "MM/DD/YYYY")
    assert validate_date("APRIL 27, 2026", "MONTH DD, YYYY")
    assert validate_date("APRIL 27 2026", "MONTH DD YYYY")
