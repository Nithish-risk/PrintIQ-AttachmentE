import re

SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

def mask_pii(text: str) -> str:
    if not text:
        return text
    return SSN_RE.sub("***-**-****", text)
