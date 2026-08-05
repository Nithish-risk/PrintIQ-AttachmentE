from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class PdfElement(BaseModel):
    kind: str
    text: str = ""
    page: int
    bbox: List[float] = Field(default_factory=list)  # normalized x0,y0,x1,y1
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class PdfAnalysis(BaseModel):
    full_text: str = ""
    pages: int = 0
    elements: List[PdfElement] = Field(default_factory=list)
    page_sizes: Dict[int, List[float]] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Rich structured layout captured from Azure Document Intelligence so
    # downstream consumers can access *everything* the model returns, not
    # just flattened word/line/selection-mark elements.
    # ------------------------------------------------------------------
    paragraphs: List[Dict[str, Any]] = Field(default_factory=list)
    tables: List[Dict[str, Any]] = Field(default_factory=list)
    key_value_pairs: List[Dict[str, Any]] = Field(default_factory=list)
    selection_marks: List[Dict[str, Any]] = Field(default_factory=list)
    barcodes: List[Dict[str, Any]] = Field(default_factory=list)
    formulas: List[Dict[str, Any]] = Field(default_factory=list)
    styles: List[Dict[str, Any]] = Field(default_factory=list)
    languages: List[Dict[str, Any]] = Field(default_factory=list)

    # Counts of every element kind extracted (words, lines, selection_marks,
    # barcodes, formulas, ...) for quick UI/debug summaries.
    element_counts: Dict[str, int] = Field(default_factory=dict)

    raw: Dict[str, Any] = Field(default_factory=dict)

    # Raw page text lines (page, text, bbox, cx, cy) captured for the new
    # DI post-processor (section-band / label detection). Populated by the
    # engine; safe to ignore for the legacy fallback path.
    lines: List[Dict[str, Any]] = Field(default_factory=list)

    # Structured output produced by modules/di_postprocessor.py (Steps 1-9).
    # Overlay-ready: each field keeps bbox(es)/page/confidence.
    structured_fields: List[Dict[str, Any]] = Field(default_factory=list)

