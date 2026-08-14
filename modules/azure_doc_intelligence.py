from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from config.settings import settings
from config.constants import SECTION_HEADERS, SUBSECTION_HEADERS
from models.pdf_models import PdfAnalysis, PdfElement
from modules.coordinate_utils import polygon_to_bbox, normalize_bbox
from utils.text_utils import clean_text

# Optional model enums — present on recent SDKs, absent on older ones. We import
# defensively so the engine still works (in plain layout mode) when they're missing.
try:  # pragma: no cover - depends on installed SDK version
    from azure.ai.documentintelligence.models import (
        DocumentAnalysisFeature,
        AnalyzeOutputOption,
    )
except Exception:  # pragma: no cover
    DocumentAnalysisFeature = None
    AnalyzeOutputOption = None


def _safe(obj, *names, default=None):
    """Return the first present (non-None) attribute from *names* on *obj*."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


# Document Intelligence rejects/ignores very small crops (min ~50px) and reads
# short text (checkbox strips) more reliably when upscaled with a white margin.
_OCR_MIN_SIDE = 300      # upscale until the smaller side reaches this many px
_OCR_MAX_SCALE = 6       # never enlarge more than this factor
_OCR_BORDER = 20         # white padding (px) added on every side

# Hard timeouts (seconds) so a stalled DI request/poll fails fast instead of
# hanging the whole app forever. The full-PDF analysis gets a longer budget;
# small example-image OCRs get a short one so a bad image is skipped quickly.
_DI_ANALYZE_TIMEOUT = 180
_DI_OCR_TIMEOUT = 45


def _preprocess_image_for_ocr(image_bytes: bytes) -> bytes:
    """Upscale + pad small images so DI can OCR thin checkbox strips reliably.

    Returns the original bytes unchanged when Pillow is unavailable or on any
    error, so OCR still runs (just without the enhancement).
    """
    try:  # Pillow is optional; degrade gracefully if missing.
        import io
        from PIL import Image, ImageOps
    except Exception:
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if min(w, h) < _OCR_MIN_SIDE:
                scale = min(_OCR_MAX_SCALE, max(1, round(_OCR_MIN_SIDE / max(1, min(w, h)))))
                if scale > 1:
                    im = im.resize((w * scale, h * scale), Image.LANCZOS)
            im = ImageOps.expand(im, border=_OCR_BORDER, fill="white")
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return image_bytes


def _bounding_regions_to_bboxes(element, page_sizes, default_page=1):
    """
    Convert an element's ``bounding_regions`` into a list of
    ``(page_no, normalized_bbox)`` tuples. Falls back to a single zero bbox on
    the *default_page* when the element exposes no geometry.
    """
    regions = getattr(element, "bounding_regions", None) or []
    out = []
    for region in regions:
        page_no = int(getattr(region, "page_number", default_page) or default_page)
        width, height = page_sizes.get(page_no, (1.0, 1.0))
        bbox = normalize_bbox(polygon_to_bbox(getattr(region, "polygon", [])), width, height)
        out.append((page_no, bbox))
    if not out:
        out.append((int(default_page), [0.0, 0.0, 0.0, 0.0]))
    return out


def _spans_to_dicts(element):
    """Serialize an element's ``spans`` (offset/length) into plain dicts."""
    spans = getattr(element, "spans", None) or []
    return [
        {"offset": getattr(s, "offset", None), "length": getattr(s, "length", None)}
        for s in spans
    ]


def _spans_ranges(element):
    """Return an element's spans as a list of (start, end) integer ranges."""
    ranges = []
    for s in getattr(element, "spans", None) or []:
        offset = getattr(s, "offset", None)
        length = getattr(s, "length", None)
        if offset is not None and length is not None:
            ranges.append((int(offset), int(offset) + int(length)))
    return ranges


def _ranges_overlap(a_ranges, b_ranges):
    """True if any (start, end) range in a overlaps any range in b."""
    for a0, a1 in a_ranges:
        for b0, b1 in b_ranges:
            if a0 < b1 and b0 < a1:
                return True
    return False


def _build_style_index(result):
    """
    Pre-parse ``result.styles`` (from the STYLE_FONT add-on feature) into a list
    of ``(ranges, formatting_dict)`` so we can attach font/style attributes to
    any element by span overlap.
    """
    index = []
    for style in getattr(result, "styles", None) or []:
        ranges = _spans_ranges(style)
        if not ranges:
            continue
        fmt = {
            "font_family": _safe(style, "similar_font_family", "font_style", default=None),
            "font_style": getattr(style, "font_style", None),      # "normal" | "italic"
            "font_weight": getattr(style, "font_weight", None),    # "normal" | "bold"
            "is_handwritten": getattr(style, "is_handwritten", None),
            "color": getattr(style, "color", None),
            "background_color": getattr(style, "background_color", None),
            "style_confidence": getattr(style, "confidence", None),
        }
        index.append((ranges, fmt))
    return index


def _estimate_font_size_pt(bbox_norm, page_height_pt):
    """
    Document Intelligence does not return a font size directly. Estimate it from
    the value's normalized bounding-box height projected back to points
    (height_fraction * page_height_in_points). Returns None when unavailable.
    """
    if not bbox_norm or len(bbox_norm) != 4 or not page_height_pt:
        return None
    height_fraction = abs(float(bbox_norm[3]) - float(bbox_norm[1]))
    if height_fraction <= 0:
        return None
    # page_height_pt is in inches for DI PDFs (e.g. 14.0); convert inches→points.
    return round(height_fraction * float(page_height_pt) * 72.0, 1)


def _formatting_for_value(value_el, style_index, bbox_norm, page_height_pt):
    """
    Build a formatting dict for a KV value by overlapping its spans with the
    STYLE_FONT style index, plus a bbox-derived font-size estimate and
    convenience booleans (is_bold / is_italic).
    """
    fmt = {
        "font_family": None,
        "font_style": None,
        "font_weight": None,
        "is_bold": None,
        "is_italic": None,
        "is_handwritten": None,
        "color": None,
        "background_color": None,
        "font_size_pt_estimate": _estimate_font_size_pt(bbox_norm, page_height_pt),
        "bbox": list(bbox_norm) if bbox_norm else None,
        "style_confidence": None,
    }
    value_ranges = _spans_ranges(value_el) if value_el is not None else []
    if value_ranges and style_index:
        for ranges, style_fmt in style_index:
            if _ranges_overlap(value_ranges, ranges):
                for key in (
                    "font_family", "font_style", "font_weight",
                    "is_handwritten", "color", "background_color", "style_confidence",
                ):
                    if style_fmt.get(key) is not None:
                        fmt[key] = style_fmt[key]
    weight = (fmt.get("font_weight") or "").lower() if fmt.get("font_weight") else None
    style = (fmt.get("font_style") or "").lower() if fmt.get("font_style") else None
    fmt["is_bold"] = (weight == "bold") if weight else None
    fmt["is_italic"] = (style == "italic") if style else None
    return fmt


def _collect_lines(pages, page_dims):
    """Flatten every page line into ``{page, text, upper, bbox, cx, cy}``.

    Layout lines carry geometry we use to (a) locate section / sub-section
    headers and (b) find the printed label a checkbox belongs to. They are used
    transiently for enrichment only.
    """
    lines = []
    for page in pages:
        page_no = int(getattr(page, "page_number", 1) or 1)
        width, height = page_dims.get(page_no, (1.0, 1.0))
        for line in getattr(page, "lines", None) or []:
            content = clean_text(getattr(line, "content", "") or "")
            if not content:
                continue
            bbox = normalize_bbox(polygon_to_bbox(getattr(line, "polygon", [])), width, height)
            lines.append(
                {
                    "page": page_no,
                    "text": content,
                    "upper": content.upper(),
                    "bbox": bbox,
                    "cx": (bbox[0] + bbox[2]) / 2.0,
                    "cy": (bbox[1] + bbox[3]) / 2.0,
                }
            )
    return lines


def _build_header_anchors(lines, header_terms):
    """Return matched header anchors ``{page, term, y0, x0}`` sorted in reading
    order (page, then top edge).

    Matching is deliberately strict to avoid false positives:
      * exact (normalized) equality, or
      * the term is a prefix and the trailing remainder is either empty or a
        long descriptive tail (e.g. ``STATISTICAL INFORMATION [Wis. Stat...]``).
    A short trailing remainder (e.g. ``LICENSE`` vs ``LICENSE FEE``) is rejected
    so field labels are not mistaken for section bars.

    We anchor on the header's **top edge** (``bbox[1]``) rather than its centre
    because section labels on this form are rotated vertical text whose bounding
    box spans the entire block; the centre would sit halfway down the block and
    mis-assign every row in its upper half.
    """
    terms = sorted({t.upper() for t in header_terms}, key=len, reverse=True)
    anchors = []
    for ln in lines:
        up = ln["upper"]
        matched = None
        for term in terms:
            if up == term:
                matched = term
                break
            if up.startswith(term):
                tail = up[len(term):].strip(" :-\u2013\u2014")
                # empty tail = exact header; long tail = descriptive header.
                if not tail or len(tail) > 6:
                    matched = term
                    break
        if matched:
            anchors.append(
                {
                    "page": ln["page"],
                    "term": matched,
                    "y0": ln["bbox"][1],
                    "x0": ln["bbox"][0],
                }
            )
    anchors.sort(key=lambda a: (a["page"], a["y0"]))
    return anchors


def _section_for(anchors, page, cy):
    """Return (term) of the last section anchor whose top edge is at/above cy."""
    chosen = None
    chosen_y = -1.0
    for a in anchors:
        if a["page"] != page:
            continue
        if a["y0"] <= cy + 0.004:
            chosen, chosen_y = a["term"], a["y0"]
    return chosen, chosen_y


def _subsection_for(anchors, page, cy, section_y0):
    """Return the sub-section term for (page, cy), constrained to the current
    section band so a previous section's sub-section is never carried over."""
    chosen = None
    for a in anchors:
        if a["page"] != page:
            continue
        if a["y0"] <= cy + 0.004 and a["y0"] >= (section_y0 - 0.004):
            chosen = a["term"]
    return chosen


def _checkbox_label(lines, option_texts, header_terms, page, bbox):
    """Best-effort printed field label for a checkbox / selection-mark KV.

    The true label is the field header line above the option group (e.g.
    ``PREVIOUS MARRIAGE ENDED BY`` above Divorce/Death/Annulment). We therefore
    pick the closest line above the checkbox that is **neither** a section/
    sub-section bar **nor** one of the checkbox option words themselves — the
    latter exclusion prevents a neighbouring option (``Annulment``) from being
    returned as the label for its siblings.
    """
    cy = (bbox[1] + bbox[3]) / 2.0
    x0, x1 = bbox[0], bbox[2]
    best, best_dy = None, 1e9
    for ln in lines:
        if ln["page"] != page:
            continue
        up = ln["upper"]
        if up in header_terms or up in option_texts:
            continue
        dy = cy - ln["cy"]
        if dy <= 0 or dy > 0.09:  # must be above, within ~9% of page height
            continue
        # Header spans the group; require horizontal overlap with the checkbox.
        if ln["bbox"][2] < x0 - 0.02 or ln["bbox"][0] > x1 + 0.30:
            continue
        if dy < best_dy:
            best_dy, best = dy, ln["text"]
    return best


class AzureDocumentIntelligenceEngine:
    def __init__(self):
        if not settings.AZURE_DOC_INTELLIGENCE_ENDPOINT or not settings.AZURE_DOC_INTELLIGENCE_KEY:
            raise RuntimeError("Azure Document Intelligence environment variables are missing.")
        # ``connection_timeout`` / ``read_timeout`` bound the underlying HTTP
        # socket so a stalled connection to the DI endpoint fails fast instead of
        # hanging the app forever. ``retry_total`` keeps transient errors from
        # looping indefinitely.
        self.client = DocumentIntelligenceClient(
            endpoint=settings.AZURE_DOC_INTELLIGENCE_ENDPOINT,
            credential=AzureKeyCredential(settings.AZURE_DOC_INTELLIGENCE_KEY),
            connection_timeout=30,
            read_timeout=120,
            retry_total=3,
        )

    def _build_features(self):
        """Request key-value pairs plus font/style so each extracted value can
        carry formatting (bold, italic, font family, handwriting)."""
        if DocumentAnalysisFeature is None:
            return []
        features = []
        for name in ("KEY_VALUE_PAIRS", "STYLE_FONT"):
            feature = getattr(DocumentAnalysisFeature, name, None)
            if feature is not None:
                features.append(feature)
        return features

    def _run_analysis(self, data: bytes):
        """
        Call ``prebuilt-layout`` requesting every supported add-on feature, then
        gracefully degrade to a plain layout call if the service/SDK rejects the
        feature set (older SDKs, unsupported regions, etc.).

        When the degraded path is taken, ``self.features_degraded`` records the
        reason: without STYLE_FONT the ``formatting`` dict carries no
        font_weight/font_style, so downstream bold/italic checks become
        inconclusive rather than authoritative. ``_analyze_impl`` surfaces this
        on ``analysis.raw`` so the report can flag it instead of silently
        reporting every bold rule as unverifiable.
        """
        self.features_degraded = None
        features = self._build_features()
        kwargs = {"body": AnalyzeDocumentRequest(bytes_source=data)}
        if features:
            kwargs["features"] = features
        try:
            poller = self.client.begin_analyze_document("prebuilt-layout", **kwargs)
            return poller.result(timeout=_DI_ANALYZE_TIMEOUT)
        except Exception as exc:
            self.features_degraded = (
                f"Add-on features (KEY_VALUE_PAIRS/STYLE_FONT) were rejected "
                f"({type(exc).__name__}: {exc}); fell back to plain layout. "
                f"Bold/italic checks are unreliable for this run."
            )
            poller = self.client.begin_analyze_document(
                "prebuilt-layout",
                AnalyzeDocumentRequest(bytes_source=data),
            )
            return poller.result(timeout=_DI_ANALYZE_TIMEOUT)

    def ocr_image_bytes(self, image_bytes: bytes) -> str:
        """OCR a raw image (e.g. an example picture pulled from an Excel cell).

        Uses the same ``prebuilt-layout`` model as the main analyzer and returns
        the plain extracted text. Small/low-resolution crops (typical of thin
        checkbox strips) are upscaled and padded first because Document
        Intelligence has a minimum-dimension requirement and reads short text
        more reliably with margins. Best-effort: returns "" on any failure.
        """
        if not image_bytes:
            return ""
        image_bytes = _preprocess_image_for_ocr(image_bytes)
        try:
            poller = self.client.begin_analyze_document(
                "prebuilt-layout",
                AnalyzeDocumentRequest(bytes_source=image_bytes),
                # Short per-request socket timeouts + no retries so a stalled OCR
                # POST fails fast instead of retry-looping for minutes.
                connection_timeout=15,
                read_timeout=_DI_OCR_TIMEOUT,
                retry_total=0,
            )
            result = poller.result(timeout=_DI_OCR_TIMEOUT)
            return clean_text(getattr(result, "content", "") or "")
        except Exception:
            return ""

    def ocr_image_structured(self, image_bytes: bytes) -> str:
        """OCR an image and pair each checkbox with its label.

        Instead of a flat text dump, this returns a structured string like
        ``Divorce:unselected, Death:unselected, Annulment:unselected`` by
        matching every detected selection mark to the nearest word/label to its
        right (falling back to the nearest label on the same line). When the
        image has no selection marks, the plain OCR text is returned so callers
        always get *something* usable.
        """
        if not image_bytes:
            return ""
        image_bytes = _preprocess_image_for_ocr(image_bytes)
        try:
            poller = self.client.begin_analyze_document(
                "prebuilt-layout",
                AnalyzeDocumentRequest(bytes_source=image_bytes),
                # Same fast-fail budget as ocr_image_bytes: a stalled structured
                # OCR POST must not retry-loop for minutes and freeze parsing.
                connection_timeout=15,
                read_timeout=_DI_OCR_TIMEOUT,
                retry_total=0,
            )
            result = poller.result(timeout=_DI_OCR_TIMEOUT)
        except Exception:
            return ""

        pages = getattr(result, "pages", []) or []
        marks = []   # {state, cx, cy, h}
        words = []   # {text, x0, cy}
        for page in pages:
            # Normalize every coordinate by the page dimensions so the same-line
            # threshold below works regardless of the OCR unit (px vs inches).
            pw = float(getattr(page, "width", 0) or 0) or 1.0
            ph = float(getattr(page, "height", 0) or 0) or 1.0
            for sm in getattr(page, "selection_marks", None) or []:
                bbox = normalize_bbox(polygon_to_bbox(getattr(sm, "polygon", []) or []), pw, ph)
                if len(bbox) != 4:
                    continue
                marks.append({
                    "state": (getattr(sm, "state", "") or "").lower() or "unselected",
                    "cx": (bbox[0] + bbox[2]) / 2.0,
                    "cy": (bbox[1] + bbox[3]) / 2.0,
                    "h": abs(bbox[3] - bbox[1]),
                })
            for w in getattr(page, "words", None) or []:
                content = clean_text(getattr(w, "content", "") or "")
                if not content:
                    continue
                bbox = normalize_bbox(polygon_to_bbox(getattr(w, "polygon", []) or []), pw, ph)
                if len(bbox) != 4:
                    continue
                words.append({
                    "text": content,
                    "x0": bbox[0],
                    "cy": (bbox[1] + bbox[3]) / 2.0,
                })

        if not marks:
            return clean_text(getattr(result, "content", "") or "")

        # Multi-word labels: for each mark, collect every word on the same line
        # that lies to its right, up to (but not including) the next mark on that
        # line. Captures "No or Not Related" / "Yes, first cousins" fully. The
        # same-line tolerance adapts to the mark height so it works for both
        # single-line strips and taller labels.
        def _same_line(cy_a, cy_b, mark_h):
            tol = max(0.02, mark_h * 0.9)
            return abs(cy_a - cy_b) <= tol

        marks.sort(key=lambda m: (round(m["cy"], 2), m["cx"]))
        pairs = []
        for m in marks:
            # Right boundary = nearest mark to the right on the same line.
            next_cx = None
            for other in marks:
                if other is m:
                    continue
                if _same_line(other["cy"], m["cy"], m["h"]) and other["cx"] > m["cx"]:
                    next_cx = other["cx"] if next_cx is None else min(next_cx, other["cx"])
            label_words = [
                w for w in words
                if _same_line(w["cy"], m["cy"], m["h"])
                and w["x0"] >= m["cx"]
                and (next_cx is None or w["x0"] < next_cx)
            ]
            label_words.sort(key=lambda w: w["x0"])
            label = " ".join(w["text"] for w in label_words).strip() or "?"
            pairs.append(f"{label}:{m['state']}")
        return ", ".join(pairs)

    def analyze(self, pdf_path: str | Path) -> PdfAnalysis:
        """Public entry point with a hard wall-clock timeout.

        The Azure SDK's own timeouts occasionally fail to fire (stalled sockets,
        proxies). Running the real work in a worker thread and enforcing a
        wall-clock limit guarantees the app never hangs forever on Section 1.
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._analyze_impl, pdf_path)
            try:
                return future.result(timeout=_DI_ANALYZE_TIMEOUT + 30)
            except _FutureTimeout:
                raise TimeoutError(
                    f"PDF analysis exceeded {_DI_ANALYZE_TIMEOUT + 30}s and was aborted."
                )

    def _analyze_impl(self, pdf_path: str | Path) -> PdfAnalysis:
        with open(pdf_path, "rb") as f:
            data = f.read()
        result = self._run_analysis(data)

        analysis = PdfAnalysis(full_text=getattr(result, "content", "") or "", raw={})
        # Surface a degraded feature set (no STYLE_FONT -> no font_weight) so the
        # UI/report can warn that bold checks are inconclusive for this run,
        # rather than silently emitting unverifiable bold results.
        if getattr(self, "features_degraded", None):
            analysis.raw["features_degraded"] = self.features_degraded
        pages = getattr(result, "pages", []) or []
        analysis.pages = len(pages)

        # Pre-parse font/style spans (STYLE_FONT add-on) once, for value formatting.
        style_index = _build_style_index(result)

        # Width/height per page (points) — needed to normalize every bbox.
        page_dims: dict[int, tuple[float, float]] = {}
        for page in pages:
            page_no = int(getattr(page, "page_number", 1) or 1)
            width = float(getattr(page, "width", 1) or 1)
            height = float(getattr(page, "height", 1) or 1)
            page_dims[page_no] = (width, height)
            analysis.page_sizes[page_no] = [width, height]

        # Layout lines: stored on the analysis so the new DI post-processor
        # (modules/di_postprocessor.py) can detect left-margin/rotated section
        # bands, header bars, and checkbox group labels generically.
        lines = _collect_lines(pages, page_dims)
        analysis.lines = lines

        # =====================================================================
        # FALLBACK BLOCK A START  (legacy in-engine section/subsection/label
        # enrichment). Superseded by modules/di_postprocessor.py. To restore the
        # OLD behaviour: (1) un-comment FALLBACK BLOCK A here, (2) un-comment
        # FALLBACK BLOCK B (inside the KV loop), and (3) un-comment FALLBACK
        # BLOCK C (post-loop checkbox-label pass) near the end of analyze().
        # ---------------------------------------------------------------------
        # section_anchors = _build_header_anchors(lines, SECTION_HEADERS)
        # subsection_anchors = _build_header_anchors(lines, SUBSECTION_HEADERS)
        # header_terms = {a["term"] for a in section_anchors} | {a["term"] for a in subsection_anchors}
        # # Checkbox option words are collected during the KV loop below and used
        # # in a post-pass to resolve each checkbox's true field label.
        # option_texts: set[str] = set()
        # FALLBACK BLOCK A END
        # =====================================================================

        # ------------------------------------------------------------------
        # Key-value pairs only. Every field/value pair Azure detects becomes both
        # a flat PdfElement (with bbox) and a structured entry, and each carries a
        # ``formatting`` dict for the value (font family/size estimate, bold,
        # italic, handwriting, color, position) so downstream Excel-rule
        # validation can check formatting requirements.
        # ------------------------------------------------------------------
        for pair in getattr(result, "key_value_pairs", []) or []:
            key_el = getattr(pair, "key", None)
            value_el = getattr(pair, "value", None)
            key_content = getattr(key_el, "content", "") or "" if key_el else ""
            value_content = getattr(value_el, "content", "") or "" if value_el else ""
            kv_page, kv_bbox = (
                _bounding_regions_to_bboxes(key_el, page_dims)[0] if key_el else (1, [0.0, 0.0, 0.0, 0.0])
            )

            # Locate the value's own bbox/page (falls back to the key's) so the
            # font-size estimate and position reflect the printed value itself.
            if value_el is not None and getattr(value_el, "bounding_regions", None):
                value_page, value_bbox = _bounding_regions_to_bboxes(value_el, page_dims, default_page=kv_page)[0]
            else:
                value_page, value_bbox = kv_page, kv_bbox
            page_height_pt = page_dims.get(value_page, (1.0, 1.0))[1]
            formatting = _formatting_for_value(value_el, style_index, value_bbox, page_height_pt)
            formatting["page"] = value_page

            # =============================================================
            # FALLBACK BLOCK B START  (legacy per-KV section/subsection tag +
            # checkbox option collection). Superseded by di_postprocessor.py.
            # Re-enable together with FALLBACK BLOCKS A and C.
            # -------------------------------------------------------------
            # kv_cy = (kv_bbox[1] + kv_bbox[3]) / 2.0 if len(kv_bbox) == 4 else 0.0
            # section, section_y0 = _section_for(section_anchors, kv_page, kv_cy)
            # subsection = _subsection_for(subsection_anchors, kv_page, kv_cy, section_y0)
            # if is_checkbox:
            #     option_texts.add(clean_text(key_content).upper())
            # FALLBACK BLOCK B END
            # =============================================================
            is_checkbox = ":select" in (value_content or "").lower() or ":unselect" in (value_content or "").lower()
            # section/subsection/field_label are now computed by the post-processor.
            section = None
            subsection = None
            field_label = None


            analysis.elements.append(
                PdfElement(
                    kind="key_value_pair",
                    text=f"{key_content}: {value_content}".strip(": ").strip(),
                    page=kv_page,
                    bbox=kv_bbox,
                    confidence=getattr(pair, "confidence", None),
                    metadata={
                        "key": key_content,
                        "value": value_content,
                        "formatting": formatting,
                        "section": section,
                        "subsection": subsection,
                        "field_label": field_label,
                        "is_checkbox": is_checkbox,
                    },
                )
            )
            analysis.key_value_pairs.append(
                {
                    "page": kv_page,
                    "key": key_content,
                    "value": value_content,
                    "section": section,
                    "subsection": subsection,
                    "field_label": field_label,
                    "is_checkbox": is_checkbox,
                    "bbox": kv_bbox,
                    "value_bbox": value_bbox,
                    "confidence": getattr(pair, "confidence", None),
                    "formatting": formatting,
                }
            )

        # Tally every extracted element kind for quick UI / debugging summaries.
        analysis.element_counts = dict(Counter(el.kind for el in analysis.elements))

        # =====================================================================
        # FALLBACK BLOCK C START  (legacy post-loop checkbox field-label pass).
        # Superseded by modules/di_postprocessor.py. Re-enable together with
        # FALLBACK BLOCKS A and B.
        # ---------------------------------------------------------------------
        # for el, kv in zip(
        #     [e for e in analysis.elements if e.kind == "key_value_pair"],
        #     analysis.key_value_pairs,
        # ):
        #     if not kv.get("is_checkbox"):
        #         continue
        #     bbox = kv.get("bbox") or []
        #     if len(bbox) != 4:
        #         continue
        #     label = _checkbox_label(lines, option_texts, header_terms, kv.get("page", 1), bbox)
        #     kv["field_label"] = label
        #     if isinstance(el.metadata, dict):
        #         el.metadata["field_label"] = label
        # FALLBACK BLOCK C END
        # =====================================================================

        # New approach: deterministic Steps 1-9 reconstruction into overlay-ready
        # structured fields. Best-effort — never break raw extraction.
        try:
            from modules.di_postprocessor import structure_document

            analysis.structured_fields = structure_document(analysis)
        except Exception as exc:  # pragma: no cover - defensive
            analysis.structured_fields = []
            analysis.raw["postprocess_error"] = str(exc)

        # ===== Step 10a relabel =====
        # Splits over-merged composites (e.g. OFFICIANT NAME + MAILING ADDRESS,
        # WITNESS 1 + WITNESS 2) back into distinct text fields so each rule can
        # match its own field, and - when Azure OpenAI is configured - re-pairs
        # each checkbox group's key with the question stem actually printed above
        # it. Fail-safe: any error keeps the geometry output unchanged.
        if analysis.structured_fields:
            try:
                from modules.di_postprocessor import page_lines as _page_lines
                from modules.di_relabel import relabel_fields

                analysis.structured_fields = relabel_fields(
                    analysis.structured_fields, _page_lines(analysis)
                )
            except Exception as exc:  # pragma: no cover - defensive
                analysis.raw["relabel_error"] = str(exc)
        # END Step 10a

        # Step 10 (optional): LLM refinement of section/subsection/key only.
        # Gated by PRINTIQ_USE_STEP10 so the deterministic path needs no OpenAI
        # credentials. Bridges the app's API_ENDPOINT/API_KEY settings to the
        # AZURE_OPENAI_* env vars the reconstructor reads. Fail-safe: any error
        # keeps the geometry output unchanged.
        if settings.PRINTIQ_USE_STEP10 and analysis.structured_fields:
            try:
                import os
                from modules import di_postprocessor as pp
                from modules import di_llm_reconstructor as llm

                env_bridge = {
                    llm.AZURE_OPENAI_ENDPOINT: settings.AZURE_OPENAI_ENDPOINT,
                    llm.AZURE_OPENAI_API_KEY: settings.AZURE_OPENAI_KEY,
                    llm.AZURE_OPENAI_DEPLOYMENT: settings.AZURE_OPENAI_DEPLOYMENT,
                    llm.AZURE_OPENAI_API_VERSION: settings.AZURE_OPENAI_API_VERSION,
                }
                for var, value in env_bridge.items():
                    if value and not os.environ.get(var):
                        os.environ[var] = value

                bands = pp.detect_section_bands(analysis)
                page_lines = pp.page_lines(analysis)
                analysis.structured_fields = llm.reconstruct(
                    analysis.structured_fields, bands, lines_by_page=page_lines
                )
            except Exception as exc:  # pragma: no cover - defensive
                analysis.raw["step10_error"] = str(exc)

        return analysis


