"""Column-mapping inspector.

Quick diagnostic to see how the Excel headers of a given sheet map to the
canonical rule fields in ``HEADER_ALIASES``. Use this when onboarding a new
client/module whose column names differ: run it, spot any mis-maps or
"UNMAPPED" canonicals, then add the needed alias to
``modules.excel_parser.HEADER_ALIASES``.

Usage:
    python tools/inspect_column_mapping.py "path/to/rules.xlsx" [SheetName]

If the sheet name is omitted, every sheet in the workbook is inspected.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root or the tools/ folder.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from modules.excel_parser import (  # noqa: E402
    HEADER_ALIASES,
    _find_header_row,
    _map_columns,
    workbook_sheets,
)
from utils.text_utils import clean_text  # noqa: E402


def inspect_sheet(path: str, sheet: str) -> None:
    raw = pd.read_excel(
        path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str
    ).fillna("")
    header_idx = _find_header_row(raw)
    headers = [clean_text(v) or f"col_{i}" for i, v in enumerate(raw.iloc[header_idx].tolist())]
    mapping = _map_columns(headers)

    print(f"\n=== Sheet: {sheet} ===")
    print(f"Detected header row index: {header_idx}")
    print(f"Headers ({len(headers)}): {headers}")

    print("\nCanonical field -> matched header:")
    for canonical in HEADER_ALIASES:
        matched = mapping.get(canonical, "  <UNMAPPED>")
        print(f"  {canonical:<14} -> {matched}")

    mapped_cols = set(mapping.values())
    unmapped_headers = [h for h in headers if h not in mapped_cols]
    if unmapped_headers:
        print("\nHeaders not mapped to any canonical field:")
        for h in unmapped_headers:
            print(f"  - {h}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    sheets = [sys.argv[2]] if len(sys.argv) >= 3 else workbook_sheets(path)
    for sheet in sheets:
        try:
            inspect_sheet(path, sheet)
        except Exception as exc:  # pragma: no cover - diagnostic tool
            print(f"\n=== Sheet: {sheet} === (error: {type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()
