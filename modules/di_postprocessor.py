"""DI post-processor: turn raw Azure Document Intelligence key-value output into
clean, overlay-ready structured fields.

Implements Steps 1-9 of the reconstruction spec (LLM cleanup = Step 10 lives in
a separate module). Designed to generalize across many certificate types and
clients (Wisconsin, Missouri, Ohio, ...), so **nothing is hardcoded to a form**:
section/label text is whatever is physically printed on the page.

Key design points
------------------
* Works from an in-memory ``PdfAnalysis`` (primary) or a raw DI JSON dict.
* Section detection handles BOTH layouts we've seen:
    (a) rotated / left-margin vertical bands that span many rows
        (e.g. Missouri "FUNERAL DIRECTOR OR OTHER PERSON ACTING AS SUCH",
         "MEDICAL CERTIFIER"), and
    (b) full-width horizontal header bars (e.g. Wisconsin "ELIGIBILITY").
* Every structured field keeps geometry (``bbox``/``bboxes``/``page``) so the
  UI can draw a visual overlay. A flat table view is just a projection.
* Checkbox groups collapse into ONE field:
      key   = parent question text
      value = "Married-selected;Divorced-unselected;..."
  while retaining each option's bbox for overlay highlighting.
"""

from __future__ import annotations

from typing import Any, Optional

# ----------------------------- tunables ------------------------------------
# All thresholds are fractions of page height/width (bboxes are normalized).
V_GROUP_GAP = 0.016        # max vertical gap between checkboxes in one column-group
ROW_ALIGN_TOL = 0.010      # vertical tol for "same row"
ROW_H_GAP = 0.14           # max horizontal gap between same-row group options
COL_X_TOL = 0.06           # x-overlap tolerance for vertical (column) grouping
LABEL_MAX_ABOVE = 0.055    # how far above a group its parent label may sit
LEFT_MARGIN_X = 0.11       # x1 below this ⇒ candidate left-margin section band
BAND_MIN_HEIGHT = 0.04     # min vertical span of a rotated section band
TIER_X_TOL = 0.02          # x0 spread that still counts as the same tier
FULLWIDTH_MIN = 0.55       # line width fraction ⇒ candidate full-width header
HEADER_MAX_WORDS = 6       # a real header bar is short (not a sentence/paragraph)
COMPOSITE_ROW_TOL = 0.010  # same-row tol for composite (First/Middle/Last)


# ----------------------------- helpers -------------------------------------
def _cy(b):
    return (b[1] + b[3]) / 2.0 if b and len(b) == 4 else 0.0


def _cx(b):
    return (b[0] + b[2]) / 2.0 if b and len(b) == 4 else 0.0


def _is_selected(value: str) -> Optional[bool]:
    v = (value or "").lower()
    if ":selected:" in v:
        return True
    if ":unselected:" in v:
        return False
    return None


def _norm_space(s: str) -> str:
    return " ".join((s or "").split())


# --------------------------- Step 1: normalize -----------------------------
def _normalize(analysis) -> tuple[list[dict], list[dict], dict]:
    """Return (fields, lines, page_sizes) from a PdfAnalysis or DI JSON dict.

    ``fields`` are normalized text/checkbox records; ``lines`` are raw page
    text lines (for section/label detection).
    """
    if isinstance(analysis, dict):
        kv_pairs = analysis.get("key_value_pairs", []) or []
        lines = analysis.get("lines", []) or []
        page_sizes = analysis.get("page_sizes", {}) or {}
    else:
        kv_pairs = getattr(analysis, "key_value_pairs", []) or []
        lines = getattr(analysis, "lines", []) or []
        page_sizes = getattr(analysis, "page_sizes", {}) or {}

    fields = []
    for kv in kv_pairs:
        key = _norm_space(kv.get("key", ""))
        value = kv.get("value", "") or ""
        bbox = kv.get("bbox") or [0, 0, 0, 0]
        page = int(kv.get("page", 1) or 1)
        conf = kv.get("confidence")
        sel = _is_selected(value)
        if sel is not None:
            fields.append(
                {
                    "type": "checkbox",
                    "option": key,
                    "selected": sel,
                    "bbox": bbox,
                    "value_bbox": kv.get("value_bbox") or bbox,
                    "page": page,
                    "confidence": conf,
                }
            )
        else:
            fields.append(
                {
                    "type": "text",
                    "key": key,
                    "value": _norm_space(value),
                    "bbox": bbox,
                    "value_bbox": kv.get("value_bbox") or bbox,
                    "page": page,
                    "confidence": conf,
                }
            )
    return fields, lines, page_sizes


# --------------------- Step 7 helpers: section bands -----------------------
def _detect_left_margin_bands(lines: list[dict], page: int) -> list[dict]:
    """Detect rotated / left-margin vertical section & sub-section bands.

    On these certificates the section names (e.g. ``ELIGIBILITY``,
    ``LICENSE - PARTY A``, ``MEDICAL CERTIFIER``) are 90°-rotated text in the
    far-left margin whose bounding box spans many rows. Sub-sections (e.g.
    ``PARTY A``, ``GROOM/SPOUSE``, ``PARENTS``) are also rotated but sit in a
    second, slightly-indented column.

    We collect every left-margin line, then split it into two tiers by its
    horizontal centre: the outer tier = sections, the inner tier = sub-sections.
    Returns ``[{text, y0, y1, cx, tier}]`` (tier 0 = section, 1 = sub-section).
    """
    cand = []
    for ln in lines:
        if int(ln.get("page", 1)) != page:
            continue
        b = ln.get("bbox") or [0, 0, 0, 0]
        if len(b) != 4:
            continue
        cx = (b[0] + b[2]) / 2.0
        height = b[3] - b[1]
        # far-left margin AND spans several rows (rotated section text)
        if cx <= LEFT_MARGIN_X and height >= BAND_MIN_HEIGHT:
            text = _norm_space(ln.get("text", ""))
            if text:
                cand.append({"text": text, "y0": b[1], "y1": b[3], "cx": cx})
    if not cand:
        return []

    # Split into outer (section) vs inner (sub-section) tiers by x-centre.
    # Instead of "anything past min_x+TIER_X_TOL is tier 1" (which collapses to
    # all-tier-0 whenever the two rotated columns sit close together), cluster
    # the x-centres into two groups at their single largest gap: bands left of
    # the gap are sections (tier 0), bands right are sub-sections (tier 1). When
    # the columns are genuinely one (spread below TIER_X_TOL) everything stays
    # tier 0. This recovers the GROOM/SPOUSE / PARENTS / OTHER sub-sections.
    xs = sorted({round(c["cx"], 4) for c in cand})
    split_x = None
    if len(xs) >= 2 and (xs[-1] - xs[0]) > TIER_X_TOL:
        best_gap = 0.0
        for a, b in zip(xs, xs[1:]):
            if (b - a) > best_gap:
                best_gap, split_x = (b - a), (a + b) / 2.0
    for c in cand:
        c["tier"] = 1 if (split_x is not None and c["cx"] > split_x) else 0
    cand.sort(key=lambda d: (d["tier"], d["y0"]))
    return cand


def _detect_fullwidth_headers(lines: list[dict], page: int) -> list[dict]:
    """Detect full-width horizontal header bars on *page*.

    A header is a wide, single-row line with only a few words — this excludes
    instruction sentences / legal warnings that also span the page width.
    Returns ``[{text, y0}]`` sorted top-to-bottom.
    """
    headers = []
    for ln in lines:
        if int(ln.get("page", 1)) != page:
            continue
        b = ln.get("bbox") or [0, 0, 0, 0]
        if len(b) != 4:
            continue
        width = b[2] - b[0]
        height = b[3] - b[1]
        text = _norm_space(ln.get("text", ""))
        if not text:
            continue
        # short label, not a paragraph; reasonably wide OR clearly a heading.
        if len(text.split()) <= HEADER_MAX_WORDS and height <= 0.03 and width >= 0.10:
            headers.append({"text": text, "y0": b[1], "y1": b[3]})
    headers.sort(key=lambda d: d["y0"])
    return headers


def _section_for_field(field, left_bands, headers) -> tuple[Optional[str], Optional[str]]:
    """Return ``(section, subsection)`` for *field*.

    Section/sub-section come from the left-margin tiers whose vertical range
    contains the field — this is the only signal we trust. We deliberately do
    NOT fall back to "nearest line above": that fabricated wrong sections from
    ordinary field labels/values (e.g. ``PROOF OF RESIDENCE DOCUMENT``,
    ``UNNAMED UNNAMED UNNAMED``). A correct ``null`` is more useful downstream
    (the Step 10 LLM pass) than a confident wrong string.

    Selection among *containing* bands of a tier uses **nearest vertical
    centre**, not "latest-starting band". Adjacent party bands (e.g.
    ``LICENSE - PARTY A`` above ``LICENSE - PARTY B``) can have slightly
    overlapping rotated-text ranges; the old "largest y0 wins" rule then handed
    every overlapping Party A field to the lower-starting Party B band. Nearest
    centre picks the band the field truly sits inside.
    """
    cy = _cy(field.get("bbox"))
    section = None
    subsection = None
    sec_dist = 1e9
    sub_dist = 1e9
    for band in left_bands:
        if band["y0"] - 0.005 <= cy <= band["y1"] + 0.005:
            center = (band["y0"] + band["y1"]) / 2.0
            dist = abs(cy - center)
            if band["tier"] == 0 and dist < sec_dist:
                section, sec_dist = band["text"], dist
            elif band["tier"] == 1 and dist < sub_dist:
                subsection, sub_dist = band["text"], dist
    return section, subsection


# ---------------- Steps 3/5: parent-label detection ------------------------
def _looks_like_label(text: str) -> bool:
    """Reject non-label lines (stray checkbox glyphs, arrows, single symbols)."""
    if not text:
        return False
    stripped = text.strip()
    # must contain at least one alphanumeric character and more than 1 char
    if len(stripped) <= 1:
        return False
    if not any(ch.isalnum() for ch in stripped):
        return False
    # common OCR checkbox / bullet glyphs
    if stripped in {"✘", "✔", "☑", "☒", ">", "➢", "•", "·"}:
        return False
    return True


def _find_parent_label(group_bbox, page, lines, section_texts, option_upper) -> tuple[Optional[str], Optional[list]]:
    """Nearest printed label above *group_bbox* that best belongs to the group.

    Candidates must sit above the group (within LABEL_MAX_ABOVE) and genuinely
    overlap it horizontally — no wide slack — so a neighbouring column's header
    (e.g. ``DATE LAST MARRIAGE ENDED`` sitting to the right of the option strip)
    can't be stolen as the label. Among candidates we score by vertical
    closeness first, then by how well the label is horizontally centred over the
    group, and finally prefer the label that starts at/left of the group.

    Returns ``(label_text, label_bbox)`` so the overlay can highlight the label
    itself (not the checkbox strip). ``label_bbox`` is the tight OCR-line box.
    """
    gx0, gx1 = group_bbox[0], group_bbox[2]
    gcy_top = group_bbox[1]
    gcx = (gx0 + gx1) / 2.0
    gwidth = max(gx1 - gx0, 1e-6)

    best = None
    best_bbox = None
    best_score = 1e9
    for ln in lines:
        if int(ln.get("page", 1)) != page:
            continue
        b = ln.get("bbox") or [0, 0, 0, 0]
        if len(b) != 4:
            continue
        text = _norm_space(ln.get("text", ""))
        up = text.upper()
        if not _looks_like_label(text) or up in section_texts or up in option_upper:
            continue
        dy = gcy_top - _cy(b)
        if dy <= 0 or dy > LABEL_MAX_ABOVE:
            continue
        # genuine horizontal overlap between label span and group span
        overlap = min(b[2], gx1) - max(b[0], gx0)
        if overlap <= 0:
            continue
        lcx = (b[0] + b[2]) / 2.0
        # score: vertical distance dominates; add horizontal-centre misalignment
        score = dy + 0.5 * (abs(lcx - gcx) / gwidth) * LABEL_MAX_ABOVE
        if score < best_score:
            best_score, best, best_bbox = score, text, list(b)
    return best, best_bbox


def _find_label_bbox_for_key(key: str, page: int, lines: list[dict], field_bbox) -> Optional[list]:
    """Tight OCR-line bbox for a text field's printed *key* (label).

    The DI key/value bbox typically spans the whole row (label + value); for a
    clean overlay we want just the label. We locate the OCR line that best
    matches the key text and return its box. Preference order:
      * exact (normalized, upper) text match,
      * line fully contained in the key or key fully contained in the line
        (longest overlap wins),
    with a light vertical-proximity tie-break to the field's own row so we don't
    grab a same-worded label elsewhere on the page. Returns ``None`` when no
    confident line is found (caller then falls back to the field bbox).
    """
    ku = _norm_space(key).upper()
    if len(ku) < 3:
        return None
    fcy = _cy(field_bbox)
    best = None
    best_key = (-1.0, 1e9)  # (overlap_ratio, vertical_distance) — maximize ratio, min dist
    for ln in lines:
        if int(ln.get("page", 1)) != page:
            continue
        b = ln.get("bbox") or [0, 0, 0, 0]
        if len(b) != 4:
            continue
        t = _norm_space(ln.get("text", "")).upper()
        if not t:
            continue
        if t == ku:
            ratio = 1.0
        elif t in ku or ku in t:
            ratio = min(len(t), len(ku)) / max(len(t), len(ku))
        else:
            continue
        dy = abs(_cy(b) - fcy)
        cand_key = (ratio, -dy)
        if cand_key > (best_key[0], -best_key[1]):
            best_key = (ratio, dy)
            best = list(b)
    return best


# ------------- Step 4: checkbox grouping (per page) ------------------------
def _group_checkboxes(checkboxes: list[dict]) -> list[list[dict]]:
    """Cluster checkboxes into option groups by spatial proximity.

    Two checkboxes join the same group when they are either:
      * on the same row (aligned y) and horizontally near (a horizontal option
        strip, e.g. ``Divorce  Death  Annulment``), or
      * stacked in the same column (x-overlap) and vertically adjacent (a
        vertical option list, e.g. the RACE / EDUCATION columns).

    Requiring column-overlap for vertical merges stops one row's group from
    swallowing the next row's unrelated options.
    """
    n = len(checkboxes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    def x_overlap(a, b):
        return min(a[2], b[2]) - max(a[0], b[0])

    for i in range(n):
        bi = checkboxes[i]["bbox"]
        for j in range(i + 1, n):
            bj = checkboxes[j]["bbox"]
            dy = abs(_cy(bi) - _cy(bj))
            # same-row horizontal strip
            same_row = dy <= ROW_ALIGN_TOL and not (
                bi[0] > bj[2] + ROW_H_GAP or bj[0] > bi[2] + ROW_H_GAP
            )
            # same-column vertical list
            same_col = dy <= V_GROUP_GAP and x_overlap(bi, bj) > -COL_X_TOL and (
                min(bi[2], bj[2]) - max(bi[0], bj[0])
            ) >= -COL_X_TOL and abs(bi[0] - bj[0]) <= COL_X_TOL
            if same_row or same_col:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(checkboxes[i])
    return list(groups.values())


def _group_bbox(items: list[dict]) -> list[float]:
    xs0 = [it["bbox"][0] for it in items]
    ys0 = [it["bbox"][1] for it in items]
    xs1 = [it["bbox"][2] for it in items]
    ys1 = [it["bbox"][3] for it in items]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


# ------------------------- main entry point --------------------------------
def structure_document(analysis) -> list[dict]:
    """Run Steps 1-9 and return overlay-ready structured fields.

    Each returned field has:
        section, subsection, key, value, page, bbox, confidence, kind,
        and (for grouped/composite fields) ``children`` with per-option bboxes.
    """
    fields, lines, _page_sizes = _normalize(analysis)

    # Step 2: process each page independently.
    pages = sorted({f["page"] for f in fields})
    out: list[dict] = []

    for page in pages:
        page_fields = [f for f in fields if f["page"] == page]
        text_fields = [f for f in page_fields if f["type"] == "text"]
        checkboxes = [f for f in page_fields if f["type"] == "checkbox"]

        # Step 7: section bands for this page.
        left_bands = _detect_left_margin_bands(lines, page)
        headers = _detect_fullwidth_headers(lines, page)
        section_texts = {b["text"].upper() for b in left_bands} | {h["text"].upper() for h in headers}
        option_upper = {c["option"].upper() for c in checkboxes}

        # Step 4-6: checkbox groups → single composite fields.
        for group in _group_checkboxes(checkboxes):
            group.sort(key=lambda c: (_cy(c["bbox"]), _cx(c["bbox"])))
            gbbox = _group_bbox(group)
            parent, parent_bbox = _find_parent_label(gbbox, page, lines, section_texts, option_upper)
            value = ";".join(
                f"{c['option']}-{'selected' if c['selected'] else 'unselected'}" for c in group
            )
            confs = [c["confidence"] for c in group if c["confidence"] is not None]
            section, subsection = _section_for_field({"bbox": gbbox}, left_bands, headers)
            out.append(
                {
                    "kind": "checkbox_group",
                    "section": section,
                    "subsection": subsection,
                    "key": parent or (group[0]["option"] if group else ""),
                    "value": value,
                    "page": page,
                    "bbox": gbbox,
                    # Overlay target: the printed parent-label box (falls back to
                    # the group strip when no label was confidently located).
                    "label_bbox": parent_bbox,
                    "confidence": round(min(confs), 4) if confs else None,  # min: surface worst box
                    "children": [
                        {"option": c["option"], "selected": c["selected"], "bbox": c["bbox"]}
                        for c in group
                    ],
                }
            )

        # Step 8: composite reconstruction for text fields sharing a row under
        # one heading (First/Middle/Last/Suffix). Grouped by same-row proximity.
        used = set()
        text_fields.sort(key=lambda f: (_cy(f["bbox"]), _cx(f["bbox"])))
        for i, f in enumerate(text_fields):
            if i in used:
                continue
            row = [f]
            used.add(i)
            for j in range(i + 1, len(text_fields)):
                if j in used:
                    continue
                if abs(_cy(text_fields[j]["bbox"]) - _cy(f["bbox"])) <= COMPOSITE_ROW_TOL:
                    row.append(text_fields[j])
                    used.add(j)
            section, subsection = _section_for_field(f, left_bands, headers)
            if len(row) == 1:
                out.append(
                    {
                        "kind": "text",
                        "section": section,
                        "subsection": subsection,
                        "key": f["key"],
                        "value": f["value"],
                        "page": page,
                        "bbox": f["bbox"],
                        # Overlay target: the printed label (key) line only.
                        "label_bbox": _find_label_bbox_for_key(
                            f["key"], page, lines, f["bbox"]
                        ),
                        "confidence": f["confidence"],
                    }
                )
            else:
                row.sort(key=lambda r: _cx(r["bbox"]))
                confs = [r["confidence"] for r in row if r["confidence"] is not None]
                # For composites the individual part keys (First/Middle/Last)
                # are not a single printed label; try to find a heading line
                # sitting just above the row instead.
                comp_bbox = _group_bbox(row)
                _heading, heading_bbox = _find_parent_label(
                    comp_bbox, page, lines, section_texts, option_upper
                )
                out.append(
                    {
                        "kind": "composite",
                        "section": section,
                        "subsection": subsection,
                        "key": None,  # a heading may be attached later / by LLM
                        "value": {r["key"]: r["value"] for r in row},
                        "page": page,
                        "bbox": comp_bbox,
                        # Overlay target: the heading above the row (falls back
                        # to the row bbox when no heading was located).
                        "label_bbox": heading_bbox,
                        "confidence": round(min(confs), 4) if confs else None,
                        "children": [
                            {"key": r["key"], "value": r["value"], "bbox": r["bbox"]} for r in row
                        ],
                    }
                )

    # Stable reading order for display/overlay.
    out.sort(key=lambda f: (f["page"], f["bbox"][1], f["bbox"][0]))
    return out


def detect_section_bands(analysis) -> dict[int, list[dict]]:
    """Return ``{page: [band, ...]}`` of left-margin section/sub-section bands.

    Exposed for the Step 10 LLM reconstructor so it can see the authoritative
    section bands alongside the structured fields, without recomputing them.
    """
    _fields, lines, _page_sizes = _normalize(analysis)
    pages = sorted({int(ln.get("page", 1)) for ln in lines})
    return {page: _detect_left_margin_bands(lines, page) for page in pages}


def page_lines(analysis) -> dict[int, list[dict]]:
    """Return ``{page: [{text, bbox}, ...]}`` of all OCR lines.

    Exposed for Step 10 so the LLM can see the printed labels/question stems
    that live in the raw lines (e.g. ``RACE - Check all that apply``) which are
    otherwise absent from the key/value candidates.
    """
    _fields, lines, _page_sizes = _normalize(analysis)
    out: dict[int, list[dict]] = {}
    for ln in lines:
        page = int(ln.get("page", 1))
        out.setdefault(page, []).append(
            {"text": ln.get("text", ""), "bbox": ln.get("bbox")}
        )
    return out
