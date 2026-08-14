# PrintIQ

**Attachment E print rules parser / reviewer.**

PrintIQ takes two inputs — an Excel workbook of *print product rules* (the
specification) and a *generated printform PDF* (the actual output) — and
reports, rule by rule, whether the PDF appears to satisfy the rule.

The output is a set of statuses, an annotated PDF overlay, and downloadable
JSON/Excel reports.

---

## Scope and limits (read this first)

PrintIQ is **PDF-only**. It compares the *rule* against the *rendered PDF*.
It can verify:

- **presence** — is the field printed at all?
- **format / pattern** — does a date look like a date, does text fit `max_chars`?
- **formatting** — bold, layout
- **checkbox alignment** — are the printed options the ones the rule expects,
  and are they positioned where the label says they are?

It **cannot** verify value-correctness against an external source of truth. If
the rule says a name field must be printed and the PDF prints `Jane Smith`,
PrintIQ confirms a name was printed — not that `Jane Smith` is the right name.
This limitation is stated in the docstring of `models/comparison_models.py` and
is deliberate.

---

## The pipeline, step by step

The whole flow lives in `app.py` and runs top-to-bottom as a Streamlit script.
Every numbered `st.subheader` in that file corresponds to a stage below.

### Stage 0 — Session setup and upload

1. A per-run session directory is created (`utils/file_utils.create_session_dir`)
   and stored in `st.session_state.session_dir`. All intermediate artifacts
   (extracted images, reports) are written there.
2. The sidebar collects two uploads: the Excel rules workbook
   (`.xlsx/.xlsm/.xls`) and the PDF. Both are saved into the session dir via
   `save_upload`.
3. Nothing runs until **Submit** is clicked. `submit_clicked` sets
   `st.session_state.submitted` and *purges every cached pipeline output*
   (`analysis`, `rules`, `comparison_summary`, and their cache keys) so a
   resubmit is always a genuine full re-run.

**Why the Submit gate exists:** Streamlit re-executes the entire script on every
widget interaction. Without the gate, moving a filter checkbox would re-trigger
the Azure calls.

### Stage 0.5 — Content hashing

`_file_hash()` computes a short MD5 over each uploaded file's *contents*.

These hashes key all the caches. This is deliberate: keying on filename alone
would let a *different* file uploaded under the *same* name silently reuse a
stale cached result.

### Stage 1 — Analyze the PDF (Azure Document Intelligence)

- The DI client is built once via `@st.cache_resource` (`_get_di_engine`).
- `di_engine.analyze(pdf_path)` is called, cached under `analysis::{pdf_hash}`.
- The result (`analysis`) carries: `pages`, `elements` (+bboxes), `full_text`,
  `paragraphs`, `tables`, `key_value_pairs`, `selection_marks`, `barcodes`,
  `formulas`, `styles`, `languages`, page sizes and counts.

Two distinct things come out of DI and the difference matters throughout:

| Field | What it is |
|---|---|
| `key_value_pairs` | The **raw** DI pairs, as returned. |
| `structured_fields` | Post-processed fields with section/subsection/key/value assigned. This is what the comparison matches against. |

**Zero-field guard.** Immediately after analysis, `app.py` checks
`len(analysis.structured_fields)`. If it is `0`, a red error is shown. This
matters because with zero structured fields *every* rule falls through to
"field not located" and the entire report reads `MISSING_DATA` — which looks
like a comparison bug but is actually an extraction failure. The message
distinguishes the two cases:

- `key_value_pairs` present but `structured_fields` empty → the fault is in the
  step that builds structured fields from the raw pairs.
- both empty → DI itself extracted nothing; check the PDF and the analyze call.

### Stage 1.5 — Native PDF text fallback

`PdfNativeEngine` (PyMuPDF/fitz) extracts text directly from the PDF. If that
text is *longer* than DI's `full_text`, it replaces it. This mainly benefits
sheet matching in Stage 2.

If PyMuPDF is unavailable, a warning is shown — annotated-PDF generation
depends on it.

### Stage 2 — Select the Excel rule sheet

- `workbook_sheets()` lists every sheet in the workbook.
- `suggest_sheet(excel_path, analysis.full_text)` ranks sheets by how well they
  correspond to the PDF's text, so the right sheet is preselected.
- The reviewer can override the selection.

### Stage 3 — Parse, enrich, classify and repair rules

Cached under `rules::{excel_hash}::{pdf_hash}::{sheet}`. Four sub-steps:

1. **`parse_rules()`** — reads the sheet into `PrintRule` objects
   (section, subsection, item, rule type, expected options, `max_chars`,
   `if_missing`, `if_unknown`, …).
2. **`enrich_rules_with_images()`** — some rules carry an *example image* in the
   `example` column rather than text. This pulls those embedded images out
   (using openpyxl anchors for an exact cell→image mapping), OCRs each one
   through the *same* DI engine, and attaches the text as
   `example_ocr_text`. Best-effort: failures degrade to unchanged rules and a
   warning.
3. **`classify_rules()`** — assigns each rule a type: `STATIC_TEXT`,
   `TEXT_OR_LAYOUT`, `FIELD_TEXT`, `DATE_FORMAT`, `CHECKBOX`, `NO_PRINT_RULE`.
   The type determines which checks run later.
4. **`repair_rules()`** — routes rules containing typos or internal
   contradictions through an LLM for correction. Repaired rules are tagged
   `raw["_repaired"]`. No-op when Azure OpenAI is unconfigured.

Two review expanders are rendered here: a parsed-rules table (with internal
columns hidden and `expected_options` renamed for clarity), and an
**example-images OCR preview** showing each extracted image beside its OCR text
so the image→text step can be eyeballed.

### Stage 4 — Rule vs. PDF comparison (the core)

Cached under `cmp4::{excel_hash}::{pdf_hash}::{sheet}`.

```python
ComparisonEngine(
    selected_sheet, rules, analysis.structured_fields,
    key_value_pairs=analysis.key_value_pairs,
).run()
```

Note that **both** the structured fields and the raw key-value pairs are passed
in. The raw pairs are needed because checkbox groups are rebuilt *from geometry*
rather than trusting the post-processor's group key, which is mis-attributed on
this form (e.g. `PROOF OF STERILITY` carrying `Groom/Bride/Spouse` options).

For each rule the engine:

1. Finds the best-matching structured field, producing a `match_score`.
2. Runs the checks appropriate to the rule type — presence, date pattern,
   checkbox alignment/position, `max_chars`, bold, LLM instruction validation.
   Each yields a `CheckResult` (`name`, `status`, `expected`, `actual`,
   `message`).
3. Rolls the individual checks up into one status per rule.

The result is a `ComparisonSummary`. **This is the single source of truth** —
the overlay and every download are derived from it.

### Stage 5 — Raw DI output download

The complete `analysis` object is serialized to JSON and offered as a download.
This is the primary debugging artifact: when results look wrong, check here
first whether the problem is extraction or comparison.

### Stage 6 — Comparison details

Four metrics (rules compared / matched / unmatched rules / unmatched DI
fields), a status breakdown table, and the main side-by-side table pairing each
Excel rule with its matched DI field.

Also surfaced here:

- **LLM verification status** — whether Azure OpenAI is enabled, how many rows
  were assessed, how many were flagged `irregular`. When disabled, checkbox
  alignment notes are blank rather than silently absent.
- **Two-directional coverage** — `unmatched_rules` (rules with no DI field =
  possibly *missing* printed output) and `unmatched_fields` (DI fields no rule
  matched = possibly *extra* output).

Downloads: `rule_pdf_comparison.json` and `.xlsx`.

### Stage 7 — Visual overlay and final artifacts

- `comparison_to_results()` adapts the comparison into legacy
  `ValidationResult` objects, so the overlay and report writers keep working.
- Results are filtered by the sidebar status checkboxes, grouped by page
  (`group_results_by_page`, converting 1-based DI pages to 0-based indices),
  and one page is rendered at 150 DPI with colored boxes
  (`render_page_with_results`).
- `build_annotated_pdf_bytes()` produces a downloadable annotated PDF that
  matches the active filters exactly.
- `validation_results.json` and `validation_summary.xlsx` are written.

---

## Statuses

Defined in `config/constants.Status`. Roughly:

| Status | Meaning |
|---|---|
| `PASS` | Every check on the rule succeeded. |
| `FAIL` | A check failed. |
| `MISSING_DATA` | The field could not be located in the PDF; nothing was read. |
| `NOT_VALIDATED` | Deliberately not checked (e.g. `NO_PRINT_RULE`), or could not be checked. |
| `EXCEL_RULE_ISSUE` | The rule itself is malformed. |
| `INFO` / `WARNING` | Advisory. |

`NOT_VALIDATED`, `EXCEL_RULE_ISSUE`, `INFO` and `WARNING` are in `_HIDDEN_STATUSES`
— still valid in the enum and still written to reports, but not surfaced as
overlay/table filters.

> **Roll-up caveat.** `NOT_VALIDATED` ranks *below* `PASS` in the roll-up. A
> rule whose only finding is `NOT_VALIDATED` therefore still reports `PASS`
> overall. If you want a finding to actually stop a row passing, it must be
> raised at a status that outranks `PASS`.

---

## Caching model

| Cache | Key | Invalidated by |
|---|---|---|
| DI engine | `@st.cache_resource` | process restart |
| DI analysis | `analysis::{pdf_hash}` | new PDF |
| Rules | `rules::{excel_hash}::{pdf_hash}::{sheet}` | new Excel, new PDF, or sheet change |
| Comparison | `cmp4::{excel_hash}::{pdf_hash}::{sheet}` | as above |

The `cmp4::` prefix is a **manual version bump**. When comparison logic changes
in a way that should invalidate previously cached results, increment it
(`cmp4::` → `cmp5::`). Forgetting this is a common source of "my fix did
nothing" — the old summary is simply replayed from session state.

Clicking **Submit** bypasses all of the above by purging the keys outright.

---

## Configuration

- `config/settings.py` — application settings.
- Azure Document Intelligence credentials — required; without them Stage 1
  fails hard and the app stops.
- Azure OpenAI — *optional*. Gates `repair_rules()` and the checkbox LLM
  verification. Controlled by `PRINTIQ_USE_AOAI`. When off, those steps are
  no-ops and the UI says so explicitly.

---

## Running

```bash
streamlit run app.py
```

Then: upload both files in the sidebar → **Submit** → work down sections 1–7.

---

## Project layout

```
app.py                          Streamlit orchestrator; the whole pipeline
config/
  constants.py                  Status enum
  settings.py                   Settings
models/
  comparison_models.py          CheckResult, FieldComparison, ComparisonSummary
  validation_models.py          ValidationResult, BBox
modules/
  excel_parser.py               workbook_sheets, parse_rules, enrich_rules_with_images
  rule_classifier.py            classify_rules
  rule_repair.py                repair_rules (LLM)
  sheet_matcher.py              suggest_sheet
  azure_doc_intelligence.py     DI client + structured_fields post-processing
  pdf_native.py                 PyMuPDF text extraction
  comparison_engine.py          ComparisonEngine - matching and checks
  checkbox_geometry.py          Geometric checkbox regrouping
  checkbox_llm.py               LLM checkbox verification
  comparison_adapter.py         ComparisonSummary -> ValidationResult
  visual_overlay.py             Page rendering, annotated PDF
  report_writer.py              JSON/XLSX writers
utils/
  file_utils.py                 Session dirs, uploads
```

---

## Troubleshooting

**Everything reads `MISSING_DATA`.**
Check the red banner after Stage 1. If `structured_fields` is 0, this is an
*extraction* failure, not a comparison failure — no rule can match anything.
Download the DI JSON from Stage 5 and inspect whether `key_value_pairs` is also
empty. Both empty → DI/PDF problem. Pairs present, fields empty → the structured
field builder is at fault.

**A code change appears to have no effect.**
Bump the `cmp4::` cache prefix, or click Submit to force a full re-run.

**Checkbox options are attributed to the wrong field.**
Known issue on this form: DI's group key is unreliable, which is why groups are
rebuilt from geometry. Multi-row option grids (RACE, EDUCATION) span more rows
than a single-row geometric band can see, so geometric regrouping can itself
report false mis-attributions on those fields. Treat single-row fields and
multi-row grids differently when interpreting these findings.

**Annotated PDF fails to generate.**
PyMuPDF/fitz is probably missing; a warning appears in Stage 1.5.
