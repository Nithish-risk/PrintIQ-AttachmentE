"""OCR every image anchored to a parsed Excel rule row, regardless of column.

The original workbook is never modified. OCR is merged into the in-memory
PrintRule property represented by the image's source column and retained in
rule.raw['_image_ocr_evidence'] for audit/UI display.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
import pandas as pd
from modules.excel_image_extractor import extract_cell_images
from modules import excel_parser as ep
from utils.text_utils import clean_text


def _append(existing: Any, addition: str) -> str:
    a, b = clean_text(existing or ""), clean_text(addition or "")
    if not b or b.lower() in a.lower(): return a
    return f"{a}\n{b}".strip() if a else b


def enrich_rules_with_all_images(rules, path, sheet, out_dir, ocr_engine=None, max_workers=4):
    images_by_row = extract_cell_images(path, sheet, out_dir)
    if not images_by_row: return rules
    raw = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str).fillna("")
    header_idx = ep._find_header_row(raw)
    headers = [clean_text(v) or f"col_{i}" for i, v in enumerate(raw.iloc[header_idx].tolist())]
    mapping = ep._map_columns(headers)
    canonical_by_col = {headers.index(col): canonical for canonical, col in mapping.items() if col in headers}
    rule_by_row = {(r.row_index or 1)-1: r for r in rules}
    tasks=[]
    for row0, entries in images_by_row.items():
        rule=rule_by_row.get(row0)
        if not rule: continue
        for entry in entries:
            tasks.append((rule,row0,int(entry.get("col",-1)),entry["path"]))
    def ocr_one(path_):
        if not ocr_engine: return ""
        try:
            data=Path(path_).read_bytes()
            if hasattr(ocr_engine,"ocr_image_structured"):
                text=ocr_engine.ocr_image_structured(data) or ""
                if text: return text
            return ocr_engine.ocr_image_bytes(data) if hasattr(ocr_engine,"ocr_image_bytes") else ""
        except Exception: return ""
    texts=[""]*len(tasks)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures={pool.submit(ocr_one,t[3]):i for i,t in enumerate(tasks)}
        for future in as_completed(futures):
            i=futures[future]
            try: texts[i]=future.result() or ""
            except Exception: texts[i]=""
    for (rule,row0,col,path_),text in zip(tasks,texts):
        canonical=canonical_by_col.get(col)
        column_name=headers[col] if 0<=col<len(headers) else f"col_{col}"
        rule.raw=dict(rule.raw or {})
        evidence={"row":row0+1,"column_index":col,"column_name":column_name,"canonical_field":canonical,"image_path":path_,"ocr_text":text}
        rule.raw.setdefault("_image_ocr_evidence",[]).append(evidence)
        if not text: continue
        if canonical and hasattr(rule,canonical):
            setattr(rule,canonical,_append(getattr(rule,canonical,None),text))
        else:
            rule.raw[f"{column_name}__image_ocr"]=_append(rule.raw.get(f"{column_name}__image_ocr"),text)
        # Preserve legacy preview and checkbox-option derivation for Example images.
        if canonical=="example":
            rule.example_image_path=rule.example_image_path or path_
            rule.example_ocr_text=_append(rule.example_ocr_text,text)
            kind,options=ep._parse_example(rule.example,text,from_image=True)
            if kind: rule.expected_kind=kind
            if options: rule.expected_options=options
    return rules
