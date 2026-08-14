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
    # Component part labels named by a single rule that spans several printed
    # fields, e.g. "CURRENT NAME - First, Middle, Last, Suffix" -> ["First",
    # "Middle", "Last", "Suffix"]. Azure DI returns each part as its OWN field
    # ("CURRENT NAME- First", "Middle", "Last", "Suffix"), so a one-rule-to-one-
    # field binding would claim only the first part and leave the rest loose for
    # a later, unrelated rule to grab -- the observed cause of ISSUING OFFICIAL
    # binding to a stray "Last" in the Party A name block.
    #
    # Empty when the sheet gives each part its own row (e.g. "56. Certifier's
    # Address" with separate Number & Street / City / State / Zip rows); those
    # remain independent rules with their own instructions and char sizes.
    part_labels: List[str] = Field(default_factory=list)
    # Separator that must precede each entry in ``part_labels`` when the parts
    # are joined into the printed value. Same length as ``part_labels``; the
    # first entry is always "". Derived from the FORMAT clause, where "+" or
    # plain juxtaposition means a space and the literal word "comma" means ", ".
    #
    #   FORMAT: <Street Number> <Street Name> comma <City> comma <State> <Zip>
    #   -> ["", " ", ", ", ", ", " "]
    #   -> "123 NW LANCASTER AVENUE, LAKE NEBGAMMON, WI 53033"
    part_separators: List[str] = Field(default_factory=list)
    max_chars: Optional[str] = None
    shrink_size: Optional[str] = None
    char_size: Optional[str] = None
    bold: Optional[str] = None
    font: Optional[str] = None
    rule_type: str = "UNKNOWN"
    raw: Dict[str, Any] = Field(default_factory=dict)
