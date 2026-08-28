"""All-column Excel image OCR and image-only row recovery.

This module never modifies the uploaded workbook. It stores all OCR evidence in
PrintRule.raw, merges OCR into the canonical property represented by the image's
anchor column, and associates image-only rows with the nearest rule/section so
large image specifications such as AFFIRMATION are not silently discarded.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
import pandas as pd
from modules.excel_image_extractor import extract_cell_images
from modules import excel_parser as ep
from utils.text_utils import clean_text


def _append(a: Any, b: Any) -> str:
    a, b = clean_text(a or ""), clean_text(b or "")
    if not b or b.lower() in a.lower(): return a
    return f"{a}\n{b}".strip() if a else b


def _plain_ocr(engine, data: bytes) -> str:
    if not engine: return ""
    try: return clean_text(engine.ocr_image_bytes(data) or "")
    except Exception: return ""


def _example_ocr(engine, data: bytes) -> tuple[str, str]:
    if not engine: return "", "none"
    try:
        structured = clean_text(engine.ocr_image_structured(data) or "")
        # Only keep structured output when DI actually found selection states.
        if structured and (":selected" in structured.lower() or ":unselected" in structured.lower()):
            return structured, "structured_checkbox"
    except Exception:
        pass
    return _plain_ocr(engine, data), "plain"


def _nearest_rule(rules, row0: int):
    if not rules: return None
    def distance(rule): return abs(((getattr(rule, "row_index", 1) or 1) - 1) - row0)
    return min(rules, key=distance)


def enrich_rules_with_all_images(rules, path, sheet, out_dir, ocr_engine=None, max_workers=4):
    images_by_row = extract_cell_images(path, sheet, out_dir)
    if not images_by_row: return rules
    raw = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str).fillna("")
    header_idx = ep._find_header_row(raw)
    headers = [clean_text(v) or f"col_{i}" for i, v in enumerate(raw.iloc[header_idx].tolist())]
    mapping = ep._map_columns(headers)
    canonical_by_col = {headers.index(actual): canonical for canonical, actual in mapping.items() if actual in headers}
    rule_by_row = {(getattr(r, "row_index", 1) or 1)-1: r for r in rules}
    jobs=[]
    for row0, entries in images_by_row.items():
        owner = rule_by_row.get(row0) or _nearest_rule(rules, row0)
        if not owner: continue
        exact_row = row0 in rule_by_row
        for entry in entries:
            col=int(entry.get("col",-1)); canonical=canonical_by_col.get(col)
            jobs.append((owner,row0,col,canonical,entry["path"],exact_row))
    def worker(job):
        _,_,_,canonical,image_path,_=job
        try: data=Path(image_path).read_bytes()
        except Exception: return "", "read_failed"
        return _example_ocr(ocr_engine,data) if canonical=="example" else (_plain_ocr(ocr_engine,data),"plain")
    outputs=[("","")]*len(jobs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures={pool.submit(worker,job):i for i,job in enumerate(jobs)}
        for future in as_completed(futures):
            i=futures[future]
            try: outputs[i]=future.result()
            except Exception: outputs[i]=("","error")
    for job,(text,mode) in zip(jobs,outputs):
        rule,row0,col,canonical,image_path,exact_row=job
        column_name=headers[col] if 0<=col<len(headers) else f"col_{col}"
        rule.raw=dict(getattr(rule,"raw",{}) or {})
        ev={"source_row":row0+1,"owner_rule_row":getattr(rule,"row_index",None),"exact_row_match":exact_row,"column_index":col,"column_name":column_name,"canonical_field":canonical,"image_path":image_path,"ocr_mode":mode,"ocr_text":text}
        rule.raw.setdefault("_image_ocr_evidence",[]).append(ev)
        if not exact_row: rule.raw.setdefault("_image_only_row_ocr",[]).append(ev)
        if not text: continue
        # Exact-row images merge into their canonical property. Image-only rows
        # merge into the nearest rule's instruction unless the anchor column is known.
        # Canonical properties may only be changed by an image anchored on the
        # exact worksheet row. Image-only rows remain audit evidence and must not
        # contaminate the nearest parsed rule.
        target = None
        if exact_row:
            target = canonical if canonical and hasattr(rule, canonical) else None
        if target:
            setattr(rule,target,_append(getattr(rule,target,None),text))
        else:
            rule.raw[f"{column_name}__image_ocr"]=_append(rule.raw.get(f"{column_name}__image_ocr"),text)
        if canonical=="example" and exact_row:
            if hasattr(rule,"example_image_path") and not getattr(rule,"example_image_path",None): rule.example_image_path=image_path
            if hasattr(rule,"example_ocr_text"): rule.example_ocr_text=_append(getattr(rule,"example_ocr_text",None),text)
            try:
                kind,options=ep._parse_example(getattr(rule,"example",None),text,from_image=True)
                if kind: rule.expected_kind=kind
                if options: rule.expected_options=options
            except Exception: pass
    return rules


def image_ocr_metrics(rules):
    evidence=[e for r in rules for e in ((getattr(r,"raw",{}) or {}).get("_image_ocr_evidence",[]))]
    return {
        "images_total":len(evidence),
        "images_with_text":sum(bool(e.get("ocr_text")) for e in evidence),
        "checkbox_example_images":sum(e.get("ocr_mode")=="structured_checkbox" for e in evidence),
        "image_only_rows":len({e.get("source_row") for e in evidence if not e.get("exact_row_match")}),
    }
