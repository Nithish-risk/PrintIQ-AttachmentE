import re
from config.constants import DATE_MONTHS

MONTH_DD_YYYY_COMMA = re.compile(rf"\b(?:{DATE_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", re.I)
MONTH_DD_YYYY_NO_COMMA = re.compile(rf"\b(?:{DATE_MONTHS})\s+\d{{1,2}}\s+\d{{4}}\b", re.I)
MM_DD_YYYY = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")

def infer_date_pattern(instruction: str = "", example: str = "", item: str = "") -> str | None:
    blob = f"{instruction or ''} {example or ''} {item or ''}".upper()
    if "MM/DD/YYYY" in blob or MM_DD_YYYY.search(blob):
        return "MM/DD/YYYY"
    if MONTH_DD_YYYY_COMMA.search(blob) or "COMMA" in blob:
        return "MONTH DD, YYYY"
    if MONTH_DD_YYYY_NO_COMMA.search(blob):
        return "MONTH DD YYYY"
    if "MONTH" in blob and "YYYY" in blob:
        return "MONTH DD, YYYY" if "COMMA" in blob else "MONTH DD YYYY"
    return None

def validate_date(value: str, pattern: str) -> bool:
    if not value:
        return False
    if pattern == "MM/DD/YYYY":
        return bool(MM_DD_YYYY.search(value))
    if pattern == "MONTH DD, YYYY":
        return bool(MONTH_DD_YYYY_COMMA.search(value))
    if pattern == "MONTH DD YYYY":
        return bool(MONTH_DD_YYYY_NO_COMMA.search(value))
    return False

def extract_dates(text: str):
    found = []
    for rx in [MONTH_DD_YYYY_COMMA, MONTH_DD_YYYY_NO_COMMA, MM_DD_YYYY]:
        found.extend(m.group(0) for m in rx.finditer(text or ""))
    return list(dict.fromkeys(found))
