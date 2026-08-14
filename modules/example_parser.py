"""DEPRECATED -- superseded by ``modules.excel_parser._parse_example``.

This module duplicated checkbox-option extraction that ``excel_parser`` already
performs (and which populates ``PrintRule.expected_kind`` /
``expected_options``). ``excel_parser``'s version is strictly better informed:
it also handles the OCR ``label:state`` form from embedded example images and
the "place an X in the <label> checkbox" instruction fallback.

Kept only as a thin delegation so any existing import keeps working. New code
should call ``excel_parser`` directly.
"""

from __future__ import annotations

from typing import List

from utils.text_utils import clean_text, norm


def extract_options(example: str) -> List[str]:
    """Checkbox option labels for *example*. Delegates to ``excel_parser``."""
    from modules.excel_parser import _parse_example

    kind, options = _parse_example(example, None)
    return options if kind == "checkbox_group" else []


def looks_like_checkbox(example: str, instruction: str = "") -> bool:
    """True when the row describes a checkbox group rather than a text field."""
    if extract_options(example):
        return True
    instr = norm(instruction or "")
    return any(
        kw in instr
        for kw in ("place x", "check box", "checkbox", "check all", "mark x", "place an x")
    )


def format_exemplar(example: str) -> str:
    """Sample printed value used for format comparison on text rows."""
    return clean_text(example or "")
