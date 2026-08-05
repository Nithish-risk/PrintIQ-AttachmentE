import re
from rapidfuzz import fuzz

def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def norm(value) -> str:
    return clean_text(value).upper()

def fuzzy_score(a: str, b: str) -> float:
    return fuzz.token_set_ratio(norm(a), norm(b))

def contains_fuzzy(haystack: str, needle: str, threshold: int = 85) -> bool:
    if not needle:
        return False
    if norm(needle) in norm(haystack):
        return True
    return fuzzy_score(haystack, needle) >= threshold

def likely_static_label(text: str) -> bool:
    t = norm(text)
    return len(t) > 3 and not re.search(r"<[^>]+>", t) and any(c.isalpha() for c in t)
