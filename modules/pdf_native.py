from pathlib import Path
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

class PdfNativeEngine:
    def __init__(self, pdf_path: str | Path):
        self.pdf_path = str(pdf_path)
        self.available = fitz is not None
        self.doc = fitz.open(self.pdf_path) if self.available else None

    def text(self) -> str:
        if not self.available:
            return ""
        return "\n".join(page.get_text() for page in self.doc)

    def page_count(self) -> int:
        return len(self.doc) if self.available else 0

    def page_size(self, page_no: int):
        page = self.doc[page_no-1]
        r = page.rect
        return float(r.width), float(r.height)

    def save(self, out_path: str | Path):
        if self.available:
            self.doc.save(str(out_path), garbage=4, deflate=True)
