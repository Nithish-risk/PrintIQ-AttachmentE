from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PrintRule(BaseModel):
    id: str
    sheet: str
    row_index: int
    section: Optional[str] = None
    subsection: Optional[str] = None
    item: Optional[str] = None
    if_missing: Optional[str] = None
    if_unknown: Optional[str] = None
    instruction: Optional[str] = None
    label_printed: Optional[str] = None
    example: Optional[str] = None
    # Populated when the ``example`` cell holds an embedded image instead of
    # text: the saved image path and the DI-OCR'd text extracted from it.
    example_image_path: Optional[str] = None
    example_ocr_text: Optional[str] = None
    # Structured interpretation of ``example`` (Phase A): what the printed value
    # is expected to look like. ``expected_kind`` is one of
    # "checkbox_group" | "text" | "date". ``expected_options`` holds the parsed
    # checkbox option labels (order preserved) when kind == "checkbox_group".
    expected_kind: Optional[str] = None
    expected_options: List[str] = Field(default_factory=list)
    max_chars: Optional[str] = None
    shrink_size: Optional[str] = None
    char_size: Optional[str] = None
    bold: Optional[str] = None
    font: Optional[str] = None
    rule_type: str = "UNKNOWN"
    raw: Dict[str, Any] = Field(default_factory=dict)
