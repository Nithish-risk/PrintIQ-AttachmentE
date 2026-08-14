"""Step 10a: post-geometry relabelling of DI structured fields.

Runs immediately after ``di_postprocessor.structure_document`` and before the
optional Step 10 LLM reconstruction. Two independent passes:

1. **Composite splitting (deterministic, always on).** The geometry layer
   stitches same-row text fields into a single ``composite`` field. That is
   correct for genuine name parts (``First``/``Middle``/``Last``/``Suffix``)
   but wrong when two unrelated fields happen to share a row
   (``OFFICIANT NAME`` + ``MAILING ADDRESS``, ``WITNESS 1`` + ``WITNESS 2``):
   the merged blob then binds to at most one rule and the other rule is
   reported as missing print output. We split those back into distinct ``text``
   fields, guarded by ``_key_is_part_label`` so real name composites stay merged.

2. **Checkbox key re-pairing (LLM, fail-safe).** A checkbox group's ``key`` is
   the nearest printed label above it, which is sometimes a neighbouring
   column's header rather than the real question stem (RACE <-> HISPANIC,
   LICENSE FEE). ``checkbox_llm.relabel_checkbox_key`` re-picks the stem from
   the printed lines around the group. Guardrailed: the model may only choose
   text that physically appears on the page. No-ops entirely when Azure OpenAI
   is unconfigured or ``PRINTIQ_USE_AOAI`` is off, so the deterministic path
   above still runs.

Every pass is best-effort: any error leaves the geometry output unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Printed part labels that indicate a *genuine* composite (one logical field
# split across several printed boxes). Anything else sharing a row is treated as
# two separate fields.
_PART_LABELS = {
    "FIRST", "FIRST NAME", "MIDDLE", "MIDDLE NAME", "LAST", "LAST NAME",
    "SUFFIX", "PREFIX", "TITLE", "MAIDEN", "MAIDEN NAME", "SURNAME",
    "GIVEN", "GIVEN NAME", "INITIAL", "MI",
    "CITY", "STATE", "ZIP", "ZIP CODE", "COUNTY", "COUNTRY",
    "STREET", "APT", "NUMBER", "MONTH", "DAY", "YEAR",
}

# Vertical/horizontal tolerance used when re-deriving a part's own bbox.
_ROW_TOL = 0.012


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _key_is_part_label(key: str) -> bool:
    """True when *key* names a part of one logical field (First/Middle/Last...).

    Used as the guard for composite splitting: a composite whose parts are all
    part labels is a real name/address composite and must stay merged.
    """
    k = _norm(key).upper().strip(":.-")
    if not k:
        return False
    if k in _PART_LABELS:
        return True
    # "FIRST NAME OF PARTY A" style: leading token is a part label.
    head = k.split(" OF ")[0].strip()
    return head in _PART_LABELS


def _child_bbox(field: dict, part_key: str) -> Optional[list]:
    """Best-effort bbox for one part of a composite.

    ``di_postprocessor`` keeps per-part geometry in ``children`` when available;
    otherwise we fall back to the composite's own bbox so the overlay still has
    something truthful to draw.
    """
    for child in field.get("children") or []:
        if not isinstance(child, dict):
            continue
        ckey = child.get("key") or child.get("option")
        if _norm(ckey).upper() == _norm(part_key).upper():
            bbox = child.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                return list(bbox)
    bbox = field.get("bbox")
    return list(bbox) if isinstance(bbox, list) and len(bbox) == 4 else None


def _split_composite(field: dict) -> list[dict]:
    """Return ``[field]`` when the composite is genuine, else one field per part.

    A composite is considered *over-merged* when at least one of its part keys
    is not a recognised part label (e.g. ``MAILING ADDRESS`` alongside
    ``OFFICIANT NAME``). Splitting lets each Excel rule bind to its own field.
    """
    value = field.get("value")
    if not isinstance(value, dict) or len(value) < 2:
        return [field]

    parts = [(k, v) for k, v in value.items() if _norm(k)]
    if not parts:
        return [field]
    # Genuine composite: every part is a printed part label -> keep merged.
    if all(_key_is_part_label(k) for k, _ in parts):
        return [field]

    out: list[dict] = []
    for part_key, part_value in parts:
        new = dict(field)
        new.pop("children", None)
        new["kind"] = "text"
        new["key"] = _norm(part_key)
        new["value"] = _norm(part_value)
        bbox = _child_bbox(field, part_key)
        if bbox:
            new["bbox"] = bbox
        new["label_bbox"] = field.get("label_bbox")
        new["split_from_composite"] = True
        out.append(new)
    return out or [field]


def _nearby_label_texts(field: dict, lines: list[dict], limit: int = 8) -> list[str]:
    """Printed line texts at/above *field* that horizontally overlap it.

    These are the only strings the LLM is allowed to choose a new key from, so
    a hallucinated question stem can never reach the output.
    """
    bbox = field.get("bbox") or [0, 0, 0, 0]
    if len(bbox) != 4:
        return []
    fx0, fy0, fx1, _fy1 = bbox
    scored: list[tuple[float, str]] = []
    for line in lines or []:
        text = _norm(line.get("text"))
        lb = line.get("bbox")
        if not text or not isinstance(lb, list) or len(lb) != 4:
            continue
        if lb[2] < fx0 - 0.02 or lb[0] > fx1 + 0.02:
            continue
        dy = fy0 - lb[1]
        if -0.01 <= dy <= 0.08:
            scored.append((abs(dy), text))
    scored.sort(key=lambda t: t[0])
    seen: list[str] = []
    for _dy, text in scored:
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def _relabel_checkboxes(fields: list[dict], lines_by_page: dict) -> list[dict]:
    """Re-pair each checkbox group's key with its real printed question stem.

    Entirely optional: returns *fields* unchanged when the LLM helper is
    unavailable, disabled, or returns nothing usable.
    """
    try:
        from config.settings import settings
        from modules.checkbox_llm import relabel_checkbox_key
    except Exception:
        return fields
    if not getattr(settings, "PRINTIQ_USE_AOAI", False):
        return fields

    for field in fields:
        if field.get("kind") != "checkbox_group":
            continue
        lines = (lines_by_page or {}).get(field.get("page", 1), [])
        candidates = _nearby_label_texts(field, lines)
        if not candidates:
            continue
        options = [
            _norm(c.get("option"))
            for c in (field.get("children") or [])
            if isinstance(c, dict) and _norm(c.get("option"))
        ]
        try:
            new_key = relabel_checkbox_key(
                current_key=_norm(field.get("key")),
                options=options,
                candidate_labels=candidates,
            )
        except Exception:
            new_key = None
        if new_key and _norm(new_key) != _norm(field.get("key")):
            field["key"] = _norm(new_key)
            field["key_relabeled"] = True
    return fields


def relabel_fields(
    structured_fields: list[dict],
    lines_by_page: Optional[dict] = None,
) -> list[dict]:
    """Run Step 10a over ``structure_document`` output.

    Parameters
    ----------
    structured_fields:
        Geometry-layer fields from ``di_postprocessor.structure_document``.
    lines_by_page:
        ``{page: [{text, bbox}, ...]}`` from ``di_postprocessor.page_lines``;
        required only for the optional checkbox re-pairing pass.

    Returns the relabelled list, or the input unchanged on any failure.
    """
    if not structured_fields:
        return structured_fields

    try:
        out: list[dict] = []
        for field in structured_fields:
            if field.get("kind") == "composite":
                out.extend(_split_composite(field))
            else:
                out.append(field)
    except Exception:
        return structured_fields

    try:
        out = _relabel_checkboxes(out, lines_by_page or {})
    except Exception:
        pass

    # Keep a stable reading order after splitting so the overlay/table order
    # still matches the printed page.
    try:
        out.sort(key=lambda f: (f.get("page", 1), (f.get("bbox") or [0, 0])[1],
                                (f.get("bbox") or [0, 0])[0]))
    except Exception:
        pass
    return out
