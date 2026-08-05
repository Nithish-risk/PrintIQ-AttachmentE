from rapidfuzz import fuzz
from modules.excel_parser import workbook_sheets, read_sheet_text
from utils.text_utils import norm

FORM_HINTS = {
    "Marriage Application": ["WISCONSIN MARRIAGE LICENSE APPLICATION", "F-05061", "STATISTICAL INFORMATION"],
    "CERTIFICATE OF MARRIAGE": ["CERTIFICATE OF MARRIAGE", "STATE FILE NUMBER", "STATE FILE DATE"],
    "Court Ordered Amendment": ["REPORT OF COURT ORDER TO AMEND", "F-05093", "PART I CURRENT MARRIAGE RECORD INFORMATION"],
    "Officiant Affidavitt": ["OFFICIANT AFFIDAVIT", "F-01481", "MARRIAGE RECORD AMENDMENT REQUEST"],
    "Marriage License": ["WISCONSIN MARRIAGE LICENSE", "ISSUED BY COUNTY CLERK"],
}

def suggest_sheet(excel_path: str, pdf_text: str) -> list[dict]:
    sheets = workbook_sheets(excel_path)
    pdf_norm = norm(pdf_text)
    results = []
    for sheet in sheets:
        try:
            stext = read_sheet_text(excel_path, sheet)
        except Exception:
            stext = sheet
        score = 0
        sheet_norm = norm(sheet)
        score += fuzz.token_set_ratio(pdf_norm[:6000], norm(stext)[:10000]) * 0.45
        score += fuzz.token_set_ratio(pdf_norm[:2000], sheet_norm) * 0.10
        for canonical, hints in FORM_HINTS.items():
            if fuzz.token_set_ratio(sheet_norm, norm(canonical)) > 80:
                for h in hints:
                    if norm(h) in pdf_norm:
                        score += 20
                    if norm(h) in norm(stext):
                        score += 5
        results.append({"sheet": sheet, "score": round(min(score, 100), 2)})
    return sorted(results, key=lambda x: x["score"], reverse=True)
