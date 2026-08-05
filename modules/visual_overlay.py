from __future__ import annotations

# =============================================================================
# PAGE-NUMBER CONVENTION (IMPORTANT)
# -----------------------------------------------------------------------------
# The rest of the pipeline (DI structured_fields, ComparisonEngine,
# ValidationResult.page and BBox.page) is **1-based** (page 1, page 2, ...).
# PyMuPDF (``fitz``) is **0-based** (``doc[0]`` is the first page).
#
# THIS MODULE IS THE SINGLE CONVERSION BOUNDARY. Every function here converts
# 1-based -> 0-based exactly once, at the point it indexes into the PDF:
#   * ``group_results_by_page``  -> returns 0-based page-index keys
#   * ``render_page_to_pil`` / ``render_page_with_results`` -> expect 0-based
#   * ``build_annotated_pdf_bytes`` -> indexes ``doc[page_index]`` (0-based)
#
# Do NOT convert page numbers anywhere upstream (e.g. the comparison adapter);
# a second subtraction collapses every page onto page 0.
# =============================================================================

from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Iterable, Dict, List, Optional, Tuple
import math
import tempfile

import matplotlib
matplotlib.use("Agg")  # headless, thread-safe backend for Streamlit rendering

from PIL import Image

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

from config.constants import Status

# Hex colors for Streamlit visual overlay and downloaded annotated PDF.
# These mirror the pass/fail/warning status model used by printiq.
STATUS_COLORS_HEX: Dict[str, str] = {
    Status.PASS.value: "#16A34A",              # green
    Status.FAIL.value: "#DC2626",              # red
    Status.WARNING.value: "#F59E0B",           # amber
    Status.UNKNOWN_DATA.value: "#8B5CF6",      # purple
    Status.MISSING_DATA.value: "#2563EB",      # blue
    Status.EXCEL_RULE_ISSUE.value: "#EAB308",  # yellow
    Status.NOT_VALIDATED.value: "#6B7280",     # gray
    Status.INFO.value: "#0EA5E9",              # sky blue
}


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = (hex_color or "#2563EB").lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_to_rgb01(hex_color: str) -> Tuple[float, float, float]:
    r, g, b = _hex_to_rgb(hex_color)
    return r / 255.0, g / 255.0, b / 255.0


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _safe_page_index(result, page_count: int) -> Optional[int]:
    page = getattr(result, "page", None)
    bbox = getattr(result, "bbox", None)
    if page is None and bbox is not None:
        page = getattr(bbox, "page", None)
    if page is None:
        return 0 if page_count else None
    try:
        return max(0, min(int(page) - 1, page_count - 1))
    except Exception:
        return 0 if page_count else None


def group_results_by_page(results: Iterable) -> Dict[int, List]:
    """Group ValidationResult objects by zero-based PDF page index.

    Prefer the bbox's own page when present so a result is always grouped onto
    the page its geometry actually belongs to (prevents cross-page bleed when a
    result's ``page`` and ``bbox.page`` disagree).
    """
    grouped: Dict[int, List] = defaultdict(list)
    for result in results:
        bbox = getattr(result, "bbox", None)
        page = getattr(bbox, "page", None) if bbox is not None else None
        if page is None:
            page = getattr(result, "page", None)
        try:
            page_index = max(0, int(page) - 1) if page is not None else 0
        except Exception:
            page_index = 0
        grouped[page_index].append(result)
    return grouped


def _result_bbox_to_pixels(result, page_width_px: int, page_height_px: int) -> Optional[Tuple[float, float, float, float]]:
    bbox = getattr(result, "bbox", None)
    if bbox is None:
        return None
    raw = [getattr(bbox, "x0", None), getattr(bbox, "y0", None), getattr(bbox, "x1", None), getattr(bbox, "y1", None)]
    if not all(_finite_number(v) for v in raw):
        return None

    x0, y0, x1, y1 = [float(v) for v in raw]

    # printiq stores Document Intelligence boxes as normalized coordinates.
    # If values are <= 1.5, treat them as normalized; otherwise assume already points/pixels.
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) <= 1.5:
        x0, x1 = x0 * page_width_px, x1 * page_width_px
        y0, y1 = y0 * page_height_px, y1 * page_height_px

    x0 = max(0.0, min(x0, page_width_px))
    x1 = max(0.0, min(x1, page_width_px))
    y0 = max(0.0, min(y0, page_height_px))
    y1 = max(0.0, min(y1, page_height_px))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    # Ensure visible marker even for tiny boxes.
    if (x1 - x0) < 8:
        x1 = min(page_width_px, x0 + 18)
    if (y1 - y0) < 8:
        y1 = min(page_height_px, y0 + 18)

    if (x1 - x0) <= 0 or (y1 - y0) <= 0:
        return None
    return x0, y0, x1, y1


def render_page_to_pil(pdf_path: str | Path, page_index: int, dpi: int = 150) -> Image.Image:
    """Render one PDF page as a PIL image for Streamlit display."""
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for visual PDF rendering. Install pymupdf.")
    doc = fitz.open(str(pdf_path), filetype="pdf")
    try:
        page_index = max(0, min(int(page_index), len(doc) - 1))
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        doc.close()


def render_page_with_results(
    pdf_path: str | Path,
    page_index: int,
    results: List,
    dpi: int = 150,
    color_map: Optional[Dict[str, str]] = None,
    legend_statuses: Optional[Iterable[str]] = None,
) -> Image.Image:
    """
    Render one PDF page and draw printiq validation overlays on top, matching
    the reference project's matplotlib style exactly: every result *with a
    bbox* gets a filled translucent rectangle (alpha 0.18) topped by a crisp
    border (alpha 0.9). Results without a bbox are skipped — same as reference.

    A color-coded legend is drawn in the top-right corner so the meaning of each
    box color is clear directly from the overlay image.
    """
    import matplotlib.patches as patches
    import matplotlib.pyplot as plt

    color_map = color_map or STATUS_COLORS_HEX
    page_image = render_page_to_pil(pdf_path, page_index=page_index, dpi=dpi).convert("RGB")
    img_width, img_height = page_image.size

    fig, ax = plt.subplots(figsize=(12, 16))
    ax.imshow(page_image)

    # Track which statuses actually appear on this page so the legend only lists
    # the colors the user can see.
    present_statuses: List[str] = []

    for result in results:
        bbox = _result_bbox_to_pixels(result, img_width, img_height)
        if bbox is None:
            # Reference skips defects without a bbox in the visual overlay.
            continue
        x0, y0, x1, y1 = bbox
        status = getattr(getattr(result, "status", None), "value", str(getattr(result, "status", "INFO")))
        if status not in present_statuses:
            present_statuses.append(status)
        color_hex = color_map.get(status, "#2563EB")
        color = _hex_to_rgb01(color_hex)

        # Filled translucent highlight
        rect = patches.Rectangle(
            (x0, y0),
            x1 - x0,
            y1 - y0,
            linewidth=2.5,
            edgecolor=color,
            facecolor=color,
            alpha=0.18,
        )
        ax.add_patch(rect)
        # Border-only overlay on top for a crisp edge
        border = patches.Rectangle(
            (x0, y0),
            x1 - x0,
            y1 - y0,
            linewidth=2.5,
            edgecolor=color,
            facecolor="none",
            alpha=0.9,
        )
        ax.add_patch(border)

    ax.set_title(f"Page {page_index + 1} — Validation Results Highlighted", fontsize=14, weight="bold")
    ax.axis("off")

    # ------------------------------------------------------------------
    # Color-coded legend so the overlay is self-explanatory. Prefer the caller-
    # supplied status list (the sidebar filters); otherwise fall back to the
    # statuses actually drawn on this page.
    # ------------------------------------------------------------------
    if legend_statuses is not None:
        legend_order = [s for s in legend_statuses if s in color_map]
    else:
        legend_order = present_statuses
    if legend_order:
        legend_handles = [
            patches.Patch(
                facecolor=_hex_to_rgb01(color_map.get(status, "#2563EB")),
                edgecolor="black",
                alpha=0.85,
                label=status,
            )
            for status in legend_order
        ]
        ax.legend(
            handles=legend_handles,
            title="Status legend",
            loc="upper right",
            fontsize=9,
            title_fontsize=10,
            framealpha=0.9,
            borderpad=0.8,
        )

    plt.tight_layout()

    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    fig.savefig(tmp_file.name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    with Image.open(tmp_file.name) as image:
        return image.copy()


def _normalized_bbox_to_pdf_rect(result, page) -> Optional["fitz.Rect"]:  # type: ignore[name-defined]
    bbox = getattr(result, "bbox", None)
    if bbox is None:
        return None
    raw = [getattr(bbox, "x0", None), getattr(bbox, "y0", None), getattr(bbox, "x1", None), getattr(bbox, "y1", None)]
    if not all(_finite_number(v) for v in raw):
        return None
    x0, y0, x1, y1 = [float(v) for v in raw]
    width, height = float(page.rect.width), float(page.rect.height)
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) <= 1.5:
        x0, x1 = x0 * width, x1 * width
        y0, y1 = y0 * height, y1 * height
    x0 = max(0.0, min(x0, width))
    x1 = max(0.0, min(x1, width))
    y0 = max(0.0, min(y0, height))
    y1 = max(0.0, min(y1, height))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) < 8:
        x1 = min(width, x0 + 18)
    if (y1 - y0) < 8:
        y1 = min(height, y0 + 18)
    if (x1 - x0) <= 0 or (y1 - y0) <= 0:
        return None
    return fitz.Rect(x0, y0, x1, y1)


def build_annotated_pdf_bytes(
    pdf_path: str | Path,
    results: List,
    color_map: Optional[Dict[str, str]] = None,
    enabled_statuses: Optional[Iterable[str]] = None,
) -> bytes:
    """
    Return annotated PDF bytes for Streamlit download, matching the reference
    project's vector style: each result with a bbox is drawn as a filled
    translucent rectangle (fill_opacity 0.18) + a crisp border + a small status
    label above the box. Results without a bbox are skipped — same as reference.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for annotated PDF generation. Install pymupdf.")

    color_map = color_map or STATUS_COLORS_HEX
    enabled = set(enabled_statuses or color_map.keys())

    doc = fitz.open(str(pdf_path), filetype="pdf")
    try:
        grouped = group_results_by_page(results)
        for page_index, page_results in grouped.items():
            if page_index < 0 or page_index >= len(doc):
                continue
            page = doc[page_index]
            for result in page_results:
                # Defensive: skip any result whose bbox page (0-based) doesn't
                # match the page we're drawing on, preventing cross-page bleed.
                bbox = getattr(result, "bbox", None)
                bpage = getattr(bbox, "page", None) if bbox is not None else None
                if bpage is not None:
                    try:
                        if max(0, int(bpage) - 1) != page_index:
                            continue
                    except Exception:
                        pass
                status = getattr(getattr(result, "status", None), "value", str(getattr(result, "status", "INFO")))
                if status not in enabled:
                    continue
                rect = _normalized_bbox_to_pdf_rect(result, page)
                if rect is None:
                    # Reference skips defects without a bbox in the annotated PDF.
                    continue
                color_hex = color_map.get(status, "#2563EB")
                color = _hex_to_rgb01(color_hex)
                try:
                    # Filled translucent highlight
                    page.draw_rect(rect, color=color, fill=color, fill_opacity=0.18, width=1.2, overlay=True)
                    # Crisp border
                    page.draw_rect(rect, color=color, width=1.5, overlay=True)
                    # Small status label above the box
                    if status:
                        page.insert_text(
                            (rect.x0, max(8.0, rect.y0 - 2.0)),
                            status,
                            fontsize=6,
                            color=color,
                            overlay=True,
                        )
                except Exception:
                    continue

        output = BytesIO()
        doc.save(output, garbage=4, deflate=True, clean=True, incremental=False)
        return output.getvalue()
    finally:
        doc.close()
