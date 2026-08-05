import streamlit as st
from pathlib import Path
import hashlib
import pandas as pd

from utils.file_utils import create_session_dir, save_upload
from modules.excel_parser import workbook_sheets, parse_rules, enrich_rules_with_images
from modules.rule_classifier import classify_rules
from modules.rule_repair import repair_rules
from modules.sheet_matcher import suggest_sheet
from modules.azure_doc_intelligence import AzureDocumentIntelligenceEngine
from modules.pdf_native import PdfNativeEngine
from modules.comparison_engine import ComparisonEngine
from modules.comparison_adapter import comparison_to_results
from modules.report_writer import (
    write_json,
    write_xlsx,
    write_comparison_json,
    write_comparison_xlsx,
)
from config.constants import Status
from config.settings import settings

from modules.visual_overlay import (
    STATUS_COLORS_HEX,
    group_results_by_page,
    render_page_with_results,
    build_annotated_pdf_bytes,
)

st.set_page_config(page_title="printiq", layout="wide", page_icon="📑")

st.title("PrintIQ")
st.caption("Attachment E print rules Parser/Reviewer")

if "session_dir" not in st.session_state:
    st.session_state.session_dir = create_session_dir()

with st.sidebar:
    st.header("Upload")
    excel_file = st.file_uploader("Excel Print Product Rules", type=["xlsx", "xlsm", "xls"])
    pdf_file = st.file_uploader("Generated Printform PDF", type=["pdf"])
    submit_clicked = st.button(
        "Submit",
        type="primary",
        use_container_width=True,
        disabled=not (excel_file and pdf_file),
    )
    st.divider()
    st.header("Overlay filters")
    st.caption("Colors below match the boxes drawn on the PDF overlay.")
    # Statuses hidden from the UI filters (kept valid in the enum, just not
    # surfaced to the reviewer as overlay/table filters).
    _HIDDEN_STATUSES = {
        Status.NOT_VALIDATED,
        Status.EXCEL_RULE_ISSUE,
        Status.INFO,
        Status.WARNING,
    }
    visible_statuses = [s for s in Status if s not in _HIDDEN_STATUSES]
    default_statuses = [s.value for s in visible_statuses]
    enabled_statuses = []
    for s in visible_statuses:
        swatch_col, check_col = st.columns([1, 8])
        with swatch_col:
            color = STATUS_COLORS_HEX.get(s.value, "#2563EB")
            st.markdown(
                f"<div style='width:16px;height:16px;border-radius:3px;"
                f"background:{color};border:1px solid #333;margin-top:6px;'></div>",
                unsafe_allow_html=True,
            )
        with check_col:
            if st.checkbox(s.value, value=True):
                enabled_statuses.append(s.value)

if not excel_file or not pdf_file:
    st.session_state.pop("submitted", None)
    st.info("Upload both the Excel rules workbook and the generated PDF to start validation.")
    st.stop()

# Start (or restart) processing only when the user explicitly clicks Submit.
# The flag is persisted in session state so the app keeps showing results across
# the reruns triggered by other widgets (filters, page selector, etc.).
if submit_clicked:
    st.session_state.submitted = True
    st.session_state.pop("results", None)
    # Force a fresh full run: drop all cached pipeline outputs.
    for _k in (
        "analysis", "analysis_key", "rules", "rules_key",
        "example_image_count", "repaired_count",
        "comparison_summary", "cmp_key",
    ):
        st.session_state.pop(_k, None)

if not st.session_state.get("submitted"):
    st.info("Click **Submit** in the sidebar to start validation.")
    st.stop()

session_dir = Path(st.session_state.session_dir)
excel_path = save_upload(excel_file, session_dir)
pdf_path = save_upload(pdf_file, session_dir)


def _file_hash(path) -> str:
    """Short content hash so caches key on file *content*, not just name.

    Prevents a different file uploaded under the same name from reusing a stale
    cached result.
    """
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


excel_hash = _file_hash(excel_path)
pdf_hash = _file_hash(pdf_path)

st.subheader("1. Analyze PDF")


@st.cache_resource(show_spinner=False)
def _get_di_engine():
    """Build the Azure DI client once and reuse it across reruns."""
    return AzureDocumentIntelligenceEngine()


di_engine = _get_di_engine()
# Cache the (slow, network-heavy) DI analysis by PDF name so Streamlit re-runs
# triggered by filters/selectors don't re-analyze the PDF.
_analysis_key = f"analysis::{pdf_hash}"
if st.session_state.get("analysis_key") == _analysis_key and "analysis" in st.session_state:
    analysis = st.session_state["analysis"]
    st.success(f"PDF analyzed (cached): {analysis.pages} page(s), {len(analysis.elements)} layout element(s).")
else:
    with st.spinner("Analyzing PDF with Azure Document Intelligence..."):
        try:
            analysis = di_engine.analyze(pdf_path)
            st.session_state["analysis"] = analysis
            st.session_state["analysis_key"] = _analysis_key
            st.success(f"PDF analyzed: {analysis.pages} page(s), {len(analysis.elements)} layout element(s).")
        except Exception as e:
            st.error(f"Azure Document Intelligence analysis failed: {e}")
            st.stop()


native = PdfNativeEngine(pdf_path)
if native.available:
    native_text = native.text()
    if len(native_text) > len(analysis.full_text or ""):
        analysis.full_text = native_text
else:
    st.warning("PyMuPDF/fitz is unavailable. Annotated PDF generation may fail unless fallback is implemented.")

st.subheader("2. Select Excel rule sheet")
sheets = workbook_sheets(excel_path)
suggestions = suggest_sheet(excel_path, analysis.full_text)

col1, col2 = st.columns([1, 2])
with col1:
    top_sheet = suggestions[0]["sheet"] if suggestions else sheets[0]
    selected_sheet = st.selectbox("Suggested / selected sheet", sheets, index=sheets.index(top_sheet) if top_sheet in sheets else 0)
with col2:
    st.write("Sheet suggestions")
    st.dataframe(pd.DataFrame(suggestions), use_container_width=True, hide_index=True)

st.subheader("3. Parse and classify rules")
# Cache the whole rule pipeline (parse + example-image OCR + classify + LLM
# repair) so it runs once per (Excel, PDF, sheet) instead of on every Streamlit
# re-run triggered by filters/selectors.
_rules_key = f"rules::{excel_hash}::{pdf_hash}::{selected_sheet}"
if st.session_state.get("rules_key") == _rules_key and "rules" in st.session_state:
    rules = st.session_state["rules"]
    example_image_count = st.session_state.get("example_image_count", 0)
    repaired_count = st.session_state.get("repaired_count", 0)
else:
    parsed_rules = parse_rules(excel_path, selected_sheet)
    # Pull any images embedded in the ``example`` column, OCR them via the same
    # DI model, and attach the extracted text (exact cell mapping via openpyxl
    # anchors). Best-effort: no images / no OCR -> unchanged rules.
    with st.spinner("Extracting example images from the Excel rules..."):
        try:
            parsed_rules = enrich_rules_with_images(
                parsed_rules, excel_path, selected_sheet,
                out_dir=session_dir / "example_images", ocr_engine=di_engine,
                max_workers=4,
            )
        except Exception as exc:
            st.warning(f"Example image extraction skipped: {type(exc).__name__}: {exc}")
    example_image_count = sum(1 for r in parsed_rules if r.example_image_path)
    rules = classify_rules(parsed_rules)
    # Replace typo/contradiction rules with LLM-corrected versions (best-effort;
    # no-op when Azure OpenAI is unavailable).
    with st.spinner("Repairing typo/contradiction rules with the LLM..."):
        repaired_rules = repair_rules(rules)
    repaired_count = sum(1 for r in repaired_rules if (r.raw or {}).get("_repaired"))
    rules = repaired_rules
    st.session_state["rules"] = rules
    st.session_state["rules_key"] = _rules_key
    st.session_state["example_image_count"] = example_image_count
    st.session_state["repaired_count"] = repaired_count

st.write(f"Parsed **{len(rules)}** rule rows from `{selected_sheet}`.")
if example_image_count:
    st.info(f"{example_image_count} rule(s) had example images; OCR text was extracted and attached.")
if repaired_count:
    st.info(f"{repaired_count} rule(s) with typos/contradictions were auto-corrected by the LLM.")
with st.expander("Preview parsed rules", expanded=False):
    # Show the parsed rule set, hiding internal/bulky columns and renaming the
    # options column for clarity.
    _preview_df = pd.DataFrame([r.model_dump() for r in rules]).head(500)
    _hide_cols = [
        "label_printed", "example_image_path", "example_ocr_text",
        "expected_kind", "raw",
    ]
    _preview_df = _preview_df.drop(columns=[c for c in _hide_cols if c in _preview_df.columns])
    _preview_df = _preview_df.rename(
        columns={"expected_options": "expected options in case of checkbox"}
    )
    # Reorder: move if_missing / if_unknown to sit between the checkbox-options
    # column and max_chars for a more logical left-to-right reading order.
    _opts_col = "expected options in case of checkbox"
    _move_cols = [c for c in ("if_missing", "if_unknown") if c in _preview_df.columns]
    if _move_cols and _opts_col in _preview_df.columns:
        ordered = []
        for col in _preview_df.columns:
            if col in _move_cols:
                continue  # will be re-inserted after the options column
            ordered.append(col)
            if col == _opts_col:
                ordered.extend(_move_cols)
        _preview_df = _preview_df[ordered]
    st.dataframe(_preview_df, use_container_width=True)

# Option 2 preview: show each extracted example image next to its OCR'd text so
# you can eyeball the image-to-text extraction. Purely additive/for review.
rules_with_images = [r for r in rules if r.example_image_path]
if rules_with_images:
    with st.expander(f"Example images (OCR preview) — {len(rules_with_images)}", expanded=False):
        for r in rules_with_images:
            img_col, txt_col = st.columns([1, 2])
            with img_col:
                try:
                    st.image(r.example_image_path, use_container_width=True)
                except Exception:
                    st.caption("(image could not be rendered)")
            with txt_col:
                st.markdown(f"**{r.id}** — {r.item or ''}")
                st.text(r.example_ocr_text or "(no OCR text extracted)")
            st.divider()

st.subheader("4. Rule vs. PDF comparison")

# Align each parsed Excel rule with the best-matching DI structured_field and
# run checks (presence, date, checkbox alignment, max chars, bold, LLM
# instruction validation). Cached per (Excel, PDF, sheet) so filter/page
# interactions don't re-run it. This comparison is the single source of truth;
# the overlay and downloads are derived from it via the adapter.
_cmp_key = f"cmp::{excel_hash}::{pdf_hash}::{selected_sheet}"
if st.session_state.get("cmp_key") == _cmp_key and "comparison_summary" in st.session_state:
    comparison_summary = st.session_state["comparison_summary"]
else:
    with st.spinner("Comparing rules against the PDF..."):
        comparison_summary = ComparisonEngine(
            selected_sheet, rules, analysis.structured_fields
        ).run()
    st.session_state["comparison_summary"] = comparison_summary
    st.session_state["cmp_key"] = _cmp_key

# Derive legacy-style ValidationResults from the comparison so the overlay and
# report/annotation writers (built around ValidationResult) keep working.
results = comparison_to_results(comparison_summary)

st.session_state.results = results
st.session_state.selected_sheet = selected_sheet

# Status values hidden from every UI table.
HIDDEN_STATUS_VALUES = {s.value for s in _HIDDEN_STATUSES}

st.subheader("5. Azure Document Intelligence output")
# Serialize the complete analysis (full text, every element + bbox, paragraphs,
# tables, key-value pairs, selection marks, barcodes, formulas, styles,
# languages, page sizes, counts) so the entire Document Intelligence output can
# be downloaded as a single JSON file.
azure_di_json_bytes = analysis.model_dump_json(indent=2).encode("utf-8")
st.download_button(
    "⬇️ Download full Document Intelligence output (JSON)",
    data=azure_di_json_bytes,
    file_name="azure_document_intelligence_output.json",
    mime="application/json",
    use_container_width=True,
    key="download_azure_di_json",
)

st.subheader("6. Rule vs. PDF comparison details")

cmp_cols = st.columns(4)
cmp_cols[0].metric("Rules compared", len(comparison_summary.comparisons))
cmp_cols[1].metric("Matched", comparison_summary.matched_count)
cmp_cols[2].metric("Unmatched rules", len(comparison_summary.unmatched_rules))
cmp_cols[3].metric("Unmatched DI fields", len(comparison_summary.unmatched_fields))

st.write("**Status breakdown**")
st.dataframe(
    pd.DataFrame(
        [(k, v) for k, v in comparison_summary.status_counts.items() if k not in HIDDEN_STATUS_VALUES],
        columns=["status", "count"],
    ),
    use_container_width=True,
    hide_index=True,
)

st.write("**Side-by-side: Excel rule vs. matched DI field**")
# Phase C status: show whether the LLM verification ran and how many findings.
try:
    from modules.checkbox_llm import _get_helper as _cb_helper
    _llm_on = bool(_cb_helper().enabled)
except Exception:
    _llm_on = False
_n_findings = len(comparison_summary.llm_findings or [])
_n_irregular = sum(
    1 for f in (comparison_summary.llm_findings or [])
    if isinstance(f, dict) and str(f.get("verdict")) == "irregular"
)
if _llm_on:
    st.caption(f"🧠 LLM verification: **enabled** — assessed {_n_findings} row(s), "
               f"{_n_irregular} flagged as irregular.")
else:
    st.caption("🧠 LLM verification: **disabled** (Azure OpenAI not configured / `PRINTIQ_USE_AOAI` off). "
               "Checkbox alignment and notes will be blank.")
# Phase C.2: map each row's verdict/note back to its rule for the table column.
def _fmt_verdict(f: dict) -> str:
    verdict = str(f.get("verdict") or "")
    note = str(f.get("note") or "")
    if verdict == "irregular":
        return f"⚠️ {note or 'irregular pairing'}"
    return f"✓ {note}" if note else "✓ consistent"


_llm_note_by_rule = {
    f.get("rule_id"): _fmt_verdict(f)
    for f in (comparison_summary.llm_findings or [])
    if isinstance(f, dict)
}
comparison_rows = [
    {
        "status": c.status.value,
        "rule_id": c.rule_id,
        "rule_type": c.rule_type,
        "section": c.section,
        "item": c.item,
        "matched": c.matched,
        "score": c.match_score,
        "di_key": c.di_key,
        "di_value": c.di_value,
        "page": c.page,
        "message": c.message,
        "llm_note": _llm_note_by_rule.get(c.rule_id, ""),
    }
    for c in comparison_summary.comparisons
    if c.status.value not in HIDDEN_STATUS_VALUES
]
st.dataframe(pd.DataFrame(comparison_rows), use_container_width=True, hide_index=True)

# Phase C.2: separate LLM verification findings expander (alternative view).
if comparison_summary.llm_findings:
    with st.expander(f"LLM verification findings — {len(comparison_summary.llm_findings)}", expanded=False):
        st.dataframe(
            pd.DataFrame(comparison_summary.llm_findings),
            use_container_width=True,
            hide_index=True,
        )

with st.expander("Rules with no matching DI field (possible missing output)"):
    st.dataframe(pd.DataFrame(comparison_summary.unmatched_rules), use_container_width=True, hide_index=True)
with st.expander("DI fields matched by no rule (possible extra output)"):
    st.dataframe(pd.DataFrame(comparison_summary.unmatched_fields), use_container_width=True, hide_index=True)

out_cmp_json = session_dir / "rule_pdf_comparison.json"
out_cmp_xlsx = session_dir / "rule_pdf_comparison.xlsx"
write_comparison_json(comparison_summary, out_cmp_json)
write_comparison_xlsx(comparison_summary, out_cmp_xlsx)
cc1, cc2 = st.columns(2)
with cc1:
    st.download_button(
        "Download comparison JSON",
        out_cmp_json.read_bytes(),
        file_name="rule_pdf_comparison.json",
        mime="application/json",
        use_container_width=True,
        key="download_comparison_json",
    )
with cc2:
    st.download_button(
        "Download comparison Excel",
        out_cmp_xlsx.read_bytes(),
        file_name="rule_pdf_comparison.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key="download_comparison_xlsx",
    )

st.subheader("7. Visual PDF overlay")

# Results already filtered by sidebar statuses for the table. Use the same filter for the overlay.
filtered_results = [r for r in results if r.status.value in enabled_statuses]
# Group by 0-based page index (visual_overlay converts 1-based page -> 0-based).
results_by_page = group_results_by_page(filtered_results)
page_options = sorted(results_by_page.keys()) or [0]

selected_page = st.selectbox(
    "Select page to inspect",
    options=page_options,
    format_func=lambda page_idx: f"Page {page_idx + 1}",
    index=0,
)

overlay_image = render_page_with_results(
    pdf_path,
    page_index=selected_page,
    # ``results_by_page`` is already keyed by the correct 0-based page, so pass
    # exactly that page's results (no cross-page bleed).
    results=results_by_page.get(selected_page, []),
    dpi=150,
    color_map=STATUS_COLORS_HEX,
    legend_statuses=enabled_statuses,
)
st.image(
    overlay_image,
    caption=f"Page {selected_page + 1} overlay",
    use_container_width=True,
)

# Generate downloadable artifacts. The annotated PDF is generated as bytes so the
# download exactly matches the overlay filters selected in the sidebar.
out_json = session_dir / "validation_results.json"
out_xlsx = session_dir / "validation_summary.xlsx"
write_json(results, out_json)
write_xlsx(results, out_xlsx)

try:
    annotated_pdf_bytes = build_annotated_pdf_bytes(
        pdf_path,
        filtered_results,
        color_map=STATUS_COLORS_HEX,
        enabled_statuses=enabled_statuses,
    )
except Exception as exc:
    annotated_pdf_bytes = b""
    st.warning(f"Annotated PDF could not be generated: {type(exc).__name__}: {exc}")

c1, c2, c3 = st.columns(3)
with c1:
    st.download_button(
        "📄 Download annotated PDF",
        data=annotated_pdf_bytes,
        file_name="printiq_annotated.pdf",
        mime="application/pdf",
        use_container_width=True,
        disabled=not annotated_pdf_bytes,
        key="download_annotated_pdf_overlay",
    )
with c2:
    st.download_button(
        "Download JSON",
        out_json.read_bytes(),
        file_name="validation_results.json",
        mime="application/json",
        use_container_width=True,
    )
with c3:
    st.download_button(
        "Download Excel summary",
        out_xlsx.read_bytes(),
        file_name="validation_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
