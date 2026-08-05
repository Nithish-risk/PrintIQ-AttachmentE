from models.pdf_models import PdfAnalysis

def selection_marks(analysis: PdfAnalysis):
    return [e for e in analysis.elements if e.kind == "selection_mark"]

def checkbox_summary(analysis: PdfAnalysis) -> dict:
    marks = selection_marks(analysis)
    selected = [m for m in marks if "selected" in (m.text or "").lower()]
    return {"total_selection_marks": len(marks), "selected_marks": len(selected)}
