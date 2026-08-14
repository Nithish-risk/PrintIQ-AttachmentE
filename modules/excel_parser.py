import pandas as pd
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from rapidfuzz import fuzz
from models.rule_models import PrintRule
from utils.text_utils import clean_text, norm
from modules.excel_image_extractor import extract_cell_images

HEADER_ALIASES = {
    "section": ["section", "labels"],
    "subsection": ["sub-section", "sub section", "section heder", "section header"],
    "item": ["item name and number", "item", "field", "labels"],
    "if_missing": ["if = blank; note", "if missing (blank), print", "if missing (blank)", "if missing", "if blank"],
    "if_unknown": ["if = unknown; note", "if = unknown, print", "if = unknown", "if unknown", "if = unknown print", "if unknown print", "if unknown, print", "if=unknown", "unknown"],
    "instruction": ["printing instructions", "printing instructions--", "printing instructions--", "print instructions"],
    "label_printed": ["label printed", "label printed"],
    "example": ["example"],
    "max_chars": ["max. chars.", "max chars"],
    "shrink_size": ["if shrink to fit needed char size", "min char size", "shrink"],
    "char_size": ["char. size", "start char size", "char size"],
    "bold": ["bold"],
    "font": ["font"],
}

def workbook_sheets(path: str | Path) -> list[str]:
    return pd.ExcelFile(path, engine="openpyxl").sheet_names

def _find_header_row(df_raw: pd.DataFrame) -> int:
    best_idx, best_score = 0, -1
    for idx, row in df_raw.iterrows():
        vals = [clean_text(v).lower() for v in row.tolist()]
        joined = " | ".join(vals)
        score = sum(1 for key, aliases in HEADER_ALIASES.items() if any(a in joined for a in aliases))
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx

def _map_columns(columns) -> dict:
    """Map canonical rule fields to worksheet columns.

    Delegates to ``modules.schema_resolver``, which scores every (role, header)
    pair and assigns globally best-first. The previous implementation walked the
    canonical fields in ``HEADER_ALIASES`` order and let the first alias that hit
    an unused column win -- so an earlier role could steal a column belonging to
    a later one, shifting every subsequent value by one. That is what put
    ``6`` (the shrink size) into ``max_chars`` and ``10`` (the char size) into
    ``shrink_size``, which would have hard-FAILed name rows against a 6-character
    limit.

    Global assignment removes the ordering dependency entirely: "Char. Size" is
    claimed by the role that scores highest for it across all roles, not by
    whichever role happens to be examined first.

    Falls back to the legacy alias walk if the resolver cannot satisfy the
    required roles, so an unusual sheet still parses.
    """
    try:
        from modules.schema_resolver import resolve_schema

        schema = resolve_schema([str(c) for c in columns])
        if schema.usable:
            mapping = dict(schema.mapping)
            # ``shrink_char_size`` is this module's ``shrink_size``.
            if "shrink_char_size" in mapping:
                mapping["shrink_size"] = mapping.pop("shrink_char_size")
            # ``item_number`` is metadata, not a matchable label.
            mapping.pop("item_number", None)
            return mapping
    except Exception:
        pass
    return _map_columns_legacy(columns)


def _map_columns_legacy(columns) -> dict:
    """Original alias-priority mapping, kept as a fallback.

    Matching is **alias-priority**: for each canonical field the aliases are
    tried in the order listed, and the first alias that matches any not-yet-used
    column wins (exact match preferred over substring, per alias).
    """
    cleaned = [(col, clean_text(col).lower()) for col in columns]
    used: set = set()
    mapping: dict = {}

    for canonical, aliases in HEADER_ALIASES.items():
        matched_col = None
        for alias in aliases:
            # Exact (whole-header) match takes priority for this alias.
            for col, c in cleaned:
                if col in used:
                    continue
                if c == alias:
                    matched_col = col
                    break
            if matched_col is not None:
                break
            # Otherwise fall back to a substring match for this alias.
            for col, c in cleaned:
                if col in used:
                    continue
                if alias in c:
                    matched_col = col
                    break
            if matched_col is not None:
                break
        if matched_col is not None:
            mapping[canonical] = matched_col
            used.add(matched_col)
    return mapping

def read_sheet_text(path: str | Path, sheet: str) -> str:
    df = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str).fillna("")
    return "\n".join(" ".join(clean_text(v) for v in row if clean_text(v)) for row in df.values.tolist())


# ---------------------------------------------------------------------------
# Example interpretation (Phase A): turn the ``example`` cell / OCR text into a
# structured expectation so both LLM alignment and validation can reason about
# it. Detects checkbox option lists vs. plain text/date.
# ---------------------------------------------------------------------------
_DATE_HINT_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"mm/dd/yyyy|dd/mm/yyyy|yyyy|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\b",
    re.IGNORECASE,
)


def _parse_example(example: str | None, ocr_text: str | None,
                   from_image: bool = False) -> tuple[str | None, list[str]]:
    """Return ``(expected_kind, expected_options)`` for a rule's example.

    Recognizes three shapes:
      * checkbox group — either the OCR ``label:state`` form
        ("Divorce:unselected, Death:unselected") or a bracketed/slash/comma list
        ("[divorce, death, annulment]", "Yes / No"); returns the option labels.
      * date — the text contains an obvious date pattern/token.
      * text — anything else (default).

    ``from_image`` relaxes detection for examples that came from an OCR'd example
    *image* (as opposed to a plain text cell). Such images are almost always
    checkbox strips, so short **space-separated** option words like ``"No Yes"``
    or ``"Male Female"`` — which carry no bracket/slash separator — are still
    treated as a checkbox group. This leniency is NOT applied to text cells,
    where "First Name" etc. would be misread as options.
    """
    source = clean_text(ocr_text or "") or clean_text(example or "")
    if not source:
        return None, []

    # 1) OCR structured form: "Label:state, Label:state, ...".
    if ":" in source and re.search(r":(?:un)?selected", source, re.IGNORECASE):
        options = []
        for part in source.split(","):
            label = part.split(":", 1)[0].strip()
            if label and label != "?":
                options.append(label)
        if options:
            return "checkbox_group", options

    # 2) Bracketed or delimiter-separated option list, e.g.
    #    "[divorce, death, annulment]" or "Yes / No" or "Groom | Bride | Spouse".
    # Slash/pipe separators are strong option signals. A bare comma is NOT — it
    # frequently appears in names/addresses ("Smith, John" / "City, ST"), so we
    # only treat comma-separated text as options when it was bracketed in the
    # source (an explicit option list) to avoid false checkbox detection.
    raw = source.strip()
    was_bracketed = raw[:1] in "[({" and raw[-1:] in "])}"
    stripped = raw.strip("[](){}")
    has_strong_sep = any(sep in stripped for sep in ("/", "|"))
    if has_strong_sep or (
        was_bracketed and "," in stripped and len(stripped) <= 60
        and not _DATE_HINT_RE.search(stripped)
    ):
        parts = re.split(r"[\/|,]", stripped)
        options = [p.strip() for p in parts if p.strip()]
        # Heuristic: treat as options only when there are 2+ short tokens.
        if len(options) >= 2 and all(len(o) <= 30 for o in options):
            return "checkbox_group", options

    # 3) Date-looking example.
    if _DATE_HINT_RE.search(source):
        return "date", []

    # 3b) Image-only leniency: an OCR'd example image with no bracket/slash
    # separators but a few short word tokens (e.g. "No Yes", "Male Female",
    # "Divorce Death Annulment") is almost certainly a checkbox strip. Only
    # applied to image-sourced examples to avoid misreading text cells.
    if from_image:
        tokens = [t for t in re.split(r"\s+", stripped) if t]
        if (
            2 <= len(tokens) <= 6
            and all(t.isalpha() and len(t) <= 15 for t in tokens)
        ):
            return "checkbox_group", tokens

    # 4) Default: free text.
    return "text", []


# Matches option names in printing instructions such as:
#   "place an X in the Groom checkbox"
#   "place and X in the Mail to Party A checkbox"
# Captures the label sitting between "in the" and "checkbox".
_CHECKBOX_INSTRUCTION_RE = re.compile(
    r"in the\s+(.+?)\s+checkbox",
    re.IGNORECASE,
)


def _options_from_instruction(instruction: str | None) -> list[str]:
    """Extract checkbox option labels from an instruction's "place an X in the
    <label> checkbox" phrases. Returns a de-duplicated, order-preserving list
    (empty when the instruction describes no checkboxes)."""
    if not instruction:
        return []
    seen: list[str] = []
    for m in _CHECKBOX_INSTRUCTION_RE.finditer(instruction):
        label = clean_text(m.group(1))
        # Guard against runaway captures (a stray match spanning clauses).
        if label and len(label) <= 40 and label.lower() not in {s.lower() for s in seen}:
            seen.append(label)
    return seen


# Trailing part list in an item name, e.g.
#   "CURRENT NAME - First, Middle, Last, Suffix"
#   "ISSUING OFFICIAL - First, Middle, Last, Title"
#   "OFFICIANT MAILING ADDRESS - Street Address or PO Box, City, State, and Zip Code"
# Captures everything after the final dash-like separator.
_PART_TAIL_RE = re.compile(r"[-\u2013\u2014]\s*([^-\u2013\u2014]+)$")
# "<First> + <Middle> + <Last>" inside a FORMAT clause -- the authoritative
# ordering of the parts as they must be printed.
_FORMAT_TOKEN_RE = re.compile(r"<\s*([^>]+?)\s*>")
# "FORMAT:" / "Format:" introduces the printed layout. Present in ~60-70% of
# multi-word rules; when present, tokens BEFORE it are the data source and
# tokens after it are the printed parts.
_FORMAT_ANCHOR_RE = re.compile(r"\bformat\s*:", re.IGNORECASE)


def _split_parts(text: str) -> list[str]:
    parts = [clean_text(p) for p in re.split(r",|\band\b", text or "")]
    return [p for p in parts if p and len(p) <= 30]


def _item_head(item: str | None) -> str:
    """The item name with any trailing part list removed.

    "CURRENT NAME - First, Middle, Last, Suffix" -> "CURRENT NAME"
    "OFFICIANT MAILING ADDRESS - Street ..."     -> "OFFICIANT MAILING ADDRESS"
    """
    text = clean_text(item or "")
    m = _PART_TAIL_RE.search(text)
    head = text[: m.start()] if m else text
    # Drop a leading item number ("56. Certifier's Address").
    return clean_text(re.sub(r"^\s*\d+[\.\)]\s*", "", head))


def _is_source_token(token: str, item_head: str) -> bool:
    """True when a ``<...>`` token names the DATA SOURCE, not a printed part.

    Instructions commonly open by naming the value being printed and only then
    describe its layout::

        PRINT <Party A Current Name>
        FORMAT: <First> + <Middle> + <Last> + <Suffix>

    ``FORMAT:`` is NOT a reliable keyword across clients, so we cannot anchor on
    it. Instead we use a property that holds regardless of wording: the source
    token restates the field's own name, so it echoes the item head ("Party A
    Current Name" vs. "CURRENT NAME"), while genuine parts ("First", "Suffix")
    do not. Party/section qualifiers are stripped first so "Party A Current
    Name" and "CURRENT NAME" compare directly.

    Left in, such a token becomes a phantom part that matches no DI field and
    shifts the separator list out of step with the real parts.
    """
    if not item_head:
        return False
    t = norm(re.sub(r"\bparty\s+[ab]\b|\bdecedent\b|\bofficiant\b", " ", token, flags=re.I))
    h = norm(item_head)
    if not t or not h:
        return False
    if t == h or t in h or h in t:
        return True
    return fuzz.token_set_ratio(t, h) >= 88


def _part_labels(item: str | None, instruction: str | None) -> list[str]:
    """Component labels for a rule that spans several printed fields.

    Thin wrapper over ``_parse_format_clause`` kept for callers that only need
    the labels.
    """
    labels, _ = _parse_format_clause(item, instruction)
    return labels


def _parse_format_clause(item: str | None,
                         instruction: str | None) -> tuple[list[str], list[str]]:
    """Return ``(part_labels, part_separators)`` for a multi-part rule.

    The instruction states both the parts and how they are joined::

        FORMAT: <First> + <Middle> + <Last> + <Suffix>
        <Street Number> <Street Name> comma <City> comma <State> <Zip Code>

    Separators are read from the literal text BETWEEN consecutive ``<...>``
    tokens: the word "comma" means ", "; "+" or plain whitespace means " ".
    ``part_separators[i]`` is what precedes ``part_labels[i]``, so the first
    entry is always "".

    Tokens naming the data source rather than a printed part are dropped (see
    ``_is_source_token``).

    Anchoring: when a ``FORMAT:`` clause is present it is authoritative -- the
    tokens before it name the DATA SOURCE ("PRINT <Party A Current Name>") and
    only those after it describe the printed layout, so parsing starts there and
    no guessing is needed. This covers ~60-70% of rules. ``FORMAT:`` is absent in
    the rest, and those fall back to ``_is_source_token``'s echo heuristic.

    Falls back to the tail of the item name (space-joined) when the instruction
    has no token list. Returns ``([], [])`` for ordinary single-field rules.
    """
    text = clean_text(instruction or "")
    head = _item_head(item)

    # Prefer an explicit FORMAT: anchor; everything before it is the source.
    fmt_anchor = _FORMAT_ANCHOR_RE.search(text)
    scan_from = fmt_anchor.end() if fmt_anchor else 0

    kept: list[tuple[str, int, int]] = []  # (label, start, end)
    for match in _FORMAT_TOKEN_RE.finditer(text, scan_from):
        label = clean_text(match.group(1))
        if not label or len(label) > 30:
            continue
        # With a FORMAT: anchor every remaining token is a printed part, so the
        # source-echo heuristic (which can misfire) is skipped entirely.
        if not fmt_anchor and _is_source_token(label, head):
            continue
        kept.append((label, match.start(), match.end()))

    parts: list[str] = []
    separators: list[str] = []
    for idx, (label, start, _end) in enumerate(kept):
        if idx == 0:
            separators.append("")
        else:
            gap = text[kept[idx - 1][2]:start]
            separators.append(", " if re.search(r"\bcomma\b", gap, re.I) else " ")
        parts.append(label)

    if len(parts) >= 2:
        return parts, separators

    m = _PART_TAIL_RE.search(clean_text(item or ""))
    if m:
        fallback = _split_parts(m.group(1))
        if len(fallback) >= 2:
            return fallback, [""] + [" "] * (len(fallback) - 1)
    return [], []


def parse_rules(path: str | Path, sheet: str) -> list[PrintRule]:
    raw = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str).fillna("")
    header_idx = _find_header_row(raw)
    headers = [clean_text(v) or f"col_{i}" for i, v in enumerate(raw.iloc[header_idx].tolist())]
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = headers
    mapping = _map_columns(headers)

    rules = []
    current_section = None
    current_subsection = None
    for n, row in df.iterrows():
        data = {k: clean_text(row.get(col, "")) for k, col in mapping.items()}
        if data.get("section"):
            current_section = data.get("section")
        if data.get("subsection"):
            current_subsection = data.get("subsection")
        item = data.get("item") or data.get("label_printed") or data.get("instruction") or data.get("section")
        if not any(data.values()):
            continue
        expected_kind, expected_options = _parse_example(data.get("example"), None)
        part_labels, part_separators = _parse_format_clause(
            item, data.get("instruction")
        )
        # Fallback: when the example gave us no checkbox options, mine the
        # printing instruction for "place an X in the <label> checkbox" phrases.
        # This recovers options for rows whose example image OCR is empty.
        if not expected_options:
            instr_opts = _options_from_instruction(data.get("instruction"))
            if len(instr_opts) >= 2:
                expected_kind, expected_options = "checkbox_group", instr_opts
        rule = PrintRule(
            id=f"{sheet}-{len(rules)+1:04d}",
            sheet=sheet,
            row_index=int(n)+1,
            section=current_section,
            subsection=current_subsection,
            item=item,
            if_missing=data.get("if_missing"),
            if_unknown=data.get("if_unknown"),
            instruction=data.get("instruction"),
            label_printed=data.get("label_printed"),
            example=data.get("example"),
            expected_kind=expected_kind,
            expected_options=expected_options,
            part_labels=part_labels,
            part_separators=part_separators,
            max_chars=data.get("max_chars"),
            shrink_size=data.get("shrink_size"),
            char_size=data.get("char_size"),
            bold=data.get("bold"),
            font=data.get("font"),
            raw={str(k): clean_text(v) for k, v in row.to_dict().items()},
        )
        rules.append(rule)
    return rules


def _example_column_index(path: str | Path, sheet: str) -> int | None:
    """Return the 0-based worksheet column index of the ``example`` column.

    Mirrors ``parse_rules``' header detection so image anchors (which carry a
    0-based column) can be matched to the example column specifically.
    """
    raw = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl", dtype=str).fillna("")
    header_idx = _find_header_row(raw)
    headers = [clean_text(v) for v in raw.iloc[header_idx].tolist()]
    for col_idx, col in enumerate(headers):
        c = col.lower()
        if any(alias in c for alias in HEADER_ALIASES["example"]):
            return col_idx
    return None


def enrich_rules_with_images(
    rules: list[PrintRule],
    path: str | Path,
    sheet: str,
    out_dir: str | Path,
    ocr_engine=None,
    max_workers: int = 8,
) -> list[PrintRule]:
    """Attach embedded ``example`` images (and their OCR text) to *rules*.

    For each rule whose worksheet row has an image anchored in the example
    column, save the image path on the rule and — when an ``ocr_engine`` with
    ``ocr_image_*`` is supplied — OCR it into ``example_ocr_text``. OCR calls are
    network-bound Azure DI requests, so they are dispatched **in parallel** via a
    thread pool (big speedup vs. sequential). When the text ``example`` is empty,
    the OCR'd text is also copied into ``example`` so downstream classification
    can use it.
    """
    images_by_row = extract_cell_images(path, sheet, out_dir)
    if not images_by_row:
        return rules
    example_col = _example_column_index(path, sheet)

    # Pass 1: pick the image for each rule and record its path (no network).
    tasks: list[tuple[PrintRule, str]] = []
    for rule in rules:
        # ``row_index`` was stored as raw_index + 1; recover the 0-based row.
        row0 = (rule.row_index or 1) - 1
        entries = images_by_row.get(row0)
        if not entries:
            continue
        chosen = None
        if example_col is not None:
            chosen = next((e for e in entries if e["col"] == example_col), None)
        chosen = chosen or entries[0]
        rule.example_image_path = chosen["path"]
        if ocr_engine is not None:
            tasks.append((rule, chosen["path"]))

    if not tasks:
        return rules

    def _ocr_one(img_path: str) -> str:
        """OCR one example image (single call). Checkbox strips whose OCR is
        empty are recovered separately from the instruction text, so we do NOT
        make a second fallback OCR call here (that doubled network traffic and
        could stall the parse step)."""
        try:
            data = Path(img_path).read_bytes()
            if hasattr(ocr_engine, "ocr_image_structured"):
                return ocr_engine.ocr_image_structured(data) or ""
            if hasattr(ocr_engine, "ocr_image_bytes"):
                return ocr_engine.ocr_image_bytes(data) or ""
        except Exception:
            return ""
        return ""

    # Pass 2: OCR all chosen images concurrently, bounded by an OVERALL
    # wall-clock deadline. A stalled Azure DI request can hang inside the SDK's
    # initial POST (the SDK's own timeouts don't always fire), and a normal
    # ``with ThreadPoolExecutor`` block would wait for those hung threads at exit
    # — freezing the whole step. So we collect whatever finished before the
    # deadline and then shut the pool down WITHOUT waiting for stragglers.
    ocr_texts: list[str] = [""] * len(tasks)
    overall_deadline = 120  # seconds for the whole batch, then move on
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_idx = {pool.submit(_ocr_one, t[1]): i for i, t in enumerate(tasks)}
        try:
            for future in as_completed(future_to_idx, timeout=overall_deadline):
                idx = future_to_idx[future]
                try:
                    ocr_texts[idx] = future.result() or ""
                except Exception:
                    ocr_texts[idx] = ""
        except Exception:
            # Overall deadline hit: keep results gathered so far; the rest stay "".
            pass
    finally:
        # Do NOT block on hung OCR threads; abandon any that are still running.
        pool.shutdown(wait=False, cancel_futures=True)

    # Pass 3: apply results and re-derive the structured expectation.
    for (rule, _), text in zip(tasks, ocr_texts):
        if text:
            rule.example_ocr_text = text
            if not clean_text(rule.example or ""):
                rule.example = text
            new_kind, new_opts = _parse_example(
                rule.example, rule.example_ocr_text, from_image=True
            )
            # Don't let a weak OCR result wipe out checkbox options we already
            # recovered from the instruction: only replace when the image OCR
            # itself produced a usable checkbox group (or the rule had none).
            if new_opts or not rule.expected_options:
                rule.expected_kind, rule.expected_options = new_kind, new_opts
    return rules
