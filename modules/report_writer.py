import json
import pandas as pd
from pathlib import Path
from config.settings import settings
from modules.pii_utils import mask_pii

def results_to_records(results):
    records = []
    for r in results:
        d = r.model_dump(mode="json")
        if settings.PRINTIQ_MASK_PII:
            for k in ["expected", "actual", "message"]:
                if d.get(k):
                    d[k] = mask_pii(d[k])
        records.append(d)
    return records

def write_json(results, out_path: str | Path):
    Path(out_path).write_text(json.dumps(results_to_records(results), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(out_path)

def _excel_engine() -> str:
    """Prefer xlsxwriter, but fall back to openpyxl when it isn't installed."""
    try:
        import xlsxwriter  # noqa: F401
        return "xlsxwriter"
    except Exception:
        return "openpyxl"

def write_xlsx(results, out_path: str | Path):
    records = results_to_records(results)
    flat = []
    for d in records:
        row = {k:v for k,v in d.items() if k not in {"bbox","metadata"}}
        if d.get("bbox"):
            row.update({f"bbox_{k}":v for k,v in d["bbox"].items()})
        flat.append(row)
    df = pd.DataFrame(flat)
    summary = df.groupby("status").size().reset_index(name="count") if not df.empty else pd.DataFrame()
    with pd.ExcelWriter(out_path, engine=_excel_engine()) as writer:
        summary.to_excel(writer, index=False, sheet_name="Summary")
        df.to_excel(writer, index=False, sheet_name="Results")
    return str(out_path)


def _comparison_flat_records(summary) -> list[dict]:
    """Flatten a ComparisonSummary's comparisons into table rows."""
    rows = []
    for c in summary.comparisons:
        d = c.model_dump(mode="json")
        row = {
            "id": d.get("id"),
            "status": d.get("status"),
            "rule_id": d.get("rule_id"),
            "rule_type": d.get("rule_type"),
            "section": d.get("section"),
            "subsection": d.get("subsection"),
            "item": d.get("item"),
            "matched": d.get("matched"),
            "match_score": d.get("match_score"),
            "di_kind": d.get("di_kind"),
            "di_section": d.get("di_section"),
            "di_key": d.get("di_key"),
            "di_value": d.get("di_value"),
            "page": d.get("page"),
            "message": d.get("message"),
        }
        if settings.PRINTIQ_MASK_PII:
            for k in ("di_value", "message"):
                if row.get(k):
                    row[k] = mask_pii(str(row[k]))
        rows.append(row)
    return rows


def write_comparison_json(summary, out_path: str | Path):
    """Serialize a full ComparisonSummary (with coverage lists) to JSON."""
    data = summary.model_dump(mode="json")
    if settings.PRINTIQ_MASK_PII:
        for c in data.get("comparisons", []):
            for k in ("di_value", "message"):
                if c.get(k):
                    c[k] = mask_pii(str(c[k]))
    Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(out_path)


def write_comparison_xlsx(summary, out_path: str | Path):
    """Write comparison rows + coverage lists to a multi-sheet workbook."""
    comparisons_df = pd.DataFrame(_comparison_flat_records(summary))
    status_df = (
        pd.DataFrame(list(summary.status_counts.items()), columns=["status", "count"])
        if summary.status_counts else pd.DataFrame()
    )
    unmatched_rules_df = pd.DataFrame(summary.unmatched_rules)
    unmatched_fields_df = pd.DataFrame(summary.unmatched_fields)
    with pd.ExcelWriter(out_path, engine=_excel_engine()) as writer:
        status_df.to_excel(writer, index=False, sheet_name="Summary")
        comparisons_df.to_excel(writer, index=False, sheet_name="Comparisons")
        unmatched_rules_df.to_excel(writer, index=False, sheet_name="Unmatched Rules")
        unmatched_fields_df.to_excel(writer, index=False, sheet_name="Unmatched Fields")
    return str(out_path)

