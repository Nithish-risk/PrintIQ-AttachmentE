"""Rebuild a rule's checkbox option group from raw DI key-value pairs.

Why this exists
---------------
``structured_fields`` assigns each checkbox group a key by nearest-preceding
line, and on real forms that is frequently wrong. Observed on the Wisconsin
marriage-licence sample:

    structured_fields key                     options actually carried
    ----------------------------------------  ------------------------------
    PROOF OF STERILITY                        Groom / Bride / Spouse
    COUNTRY                                   Groom / Bride / Spouse
    BIRTHPLACE - U.S. State/Territory...      Parent / Mother / Father
    DATE LAST MARRIAGE ENDED                  Yes / No / Not Required
    LICENSE FEE                               all 10 issuance-method options
    OFFICIANT MAILING ADDRESS                 No / Yes (active-duty military)

The boxes themselves are read correctly -- DI reports each one individually in
``key_value_pairs`` with its own bbox, value_bbox and ~0.997 confidence. Only
the *parent* attribution is wrong. So no amount of key matching helps; the group
has to be rebuilt from geometry.

Approach: ignore the DI group key entirely. Take the option labels the Excel
rule already declares (``rule.expected_options``) and, for each, find the raw
checkbox KVP that both (a) reads like that label and (b) sits nearest the rule's
matched label box. Proximity is scored anisotropically -- same-row is weighted
far more heavily than same-column -- because option strips run horizontally to
the right of their label.

Known limits (deliberately not papered over):
  * Reliable for single-row strips (Groom/Bride/Spouse, Parent/Mother/Father,
    Yes/No), which is the majority of the failures above.
  * The LICENSE ISSUANCE METHOD block wraps over three lines and the page-2 RACE
    block is a four-column grid. ``_ROW_TOLERANCE`` is widened by the number of
    expected options so those still resolve, but they are the weakest case and
    ``regroup`` reports its own confidence so callers can decline it.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from rapidfuzz import fuzz

from utils.text_utils import clean_text


# A checkbox on the same printed row as its label rarely differs in centre-y by
# more than ~1.5% of page height. Multi-row blocks get a wider band (see
# ``_row_tolerance``).
_BASE_ROW_TOLERANCE = 0.018
# Options are printed to the RIGHT of their label; a small negative allowance
# covers labels that sit slightly right of the first box.
_LEFT_ALLOWANCE = 0.06
# Beyond this horizontal span a "match" is almost certainly a same-named option
# belonging to a different field (every Yes/No pair on the form looks alike).
_MAX_DX = 0.75
# Label similarity below this is not the same option.
_MIN_LABEL_SCORE = 78.0


def _is_checkbox_kv(kv: dict) -> bool:
    if kv.get("is_checkbox"):
        return True
    value = str(kv.get("value") or "").lower()
    return ":select" in value or ":unselect" in value


def _state_of(kv: dict) -> str:
    value = str(kv.get("value") or "").lower()
    return "selected" if ("unselect" not in value and "select" in value) else "unselected"


def _centre(bbox: Sequence[float]) -> Optional[tuple[float, float]]:
    if not bbox or len(bbox) != 4:
        return None
    return (float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0


def _anchor(bbox: Sequence[float]) -> Optional[tuple[float, float]]:
    """Anchor point for the matched box: LEFT edge horizontally, centre vertically.

    Verified against the observed data rather than assumed. The bbox handed to
    ``regroup`` is NOT the label text box -- it is the box of the option strip
    itself. On PARTY B's PREVIOUS MARRIAGE ENDED BY it spans x 0.3990->0.6040
    while its options Divorce/Death/Annulment sit at cx 0.4206 / 0.4935 / 0.5723,
    i.e. all three fall *inside* it.

    Two anchors were tried and measured:
      * centre (0.5015) -> Divorce dx = -0.081, outside _LEFT_ALLOWANCE: 2/3.
      * right edge (0.6040) -> every option to the left of the anchor: 1/3.
    Left edge gives dx = +0.022 / +0.095 / +0.173, all inside the window, and is
    the only one consistent with the geometry actually present.
    """
    if not bbox or len(bbox) != 4:
        return None
    return float(bbox[0]), (float(bbox[1]) + float(bbox[3])) / 2.0


def _row_tolerance(option_count: int) -> float:
    """Vertical band to search, widened for blocks that wrap over several rows.

    A 3-option strip is one row; a 10-option issuance-method block is three; the
    17-option RACE grid is six. Growing the band with the option count lets those
    resolve without loosening the common single-row case.
    """
    extra_rows = max(0, (option_count - 1) // 3)
    return _BASE_ROW_TOLERANCE * (1 + extra_rows)


def _label_score(option: str, kv_key: str) -> float:
    a = clean_text(option or "").upper()
    b = clean_text(kv_key or "").upper()
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    # Substring both ways, but only when the shorter side is substantial --
    # "No" is a substring of "No or Not Related" and of "Not Required", so a
    # bare containment test would bind the wrong box on the sterility row.
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return 95.0
    return float(fuzz.token_sort_ratio(a, b))


def regroup(
    rule_options: Sequence[str],
    label_bbox: Sequence[float],
    page: int,
    checkbox_kvs: Sequence[dict],
) -> Optional[dict]:
    """Rebuild the option group for one rule from raw checkbox KVPs.

    ``label_bbox`` is the rule's matched label box (normalized, 4 values) and
    ``page`` its page number. Returns::

        {"options": [{"label": str, "state": str, "score": float}, ...],
         "matched": int, "expected": int, "confidence": float}

    or ``None`` when nothing usable was found -- callers should then fall back to
    whatever the DI group said rather than treating this as a defect.
    """
    options = [o for o in (rule_options or []) if clean_text(o)]
    anchor = _anchor(label_bbox or [])
    if not options or anchor is None or not checkbox_kvs:
        return None
    anchor_x, anchor_y = anchor
    tolerance = _row_tolerance(len(options))

    candidates = []
    for kv in checkbox_kvs:
        if kv.get("page") != page or not _is_checkbox_kv(kv):
            continue
        centre = _centre(kv.get("bbox") or [])
        if centre is None:
            continue
        cx, cy = centre
        dy = abs(cy - anchor_y)
        dx = cx - anchor_x
        if dy > tolerance or dx < -_LEFT_ALLOWANCE or dx > _MAX_DX:
            continue
        candidates.append({"kv": kv, "dx": abs(dx), "dy": dy})

    if not candidates:
        return None

    resolved: List[dict] = []
    taken: set[int] = set()
    for option in options:
        best = None
        best_rank = None
        for idx, cand in enumerate(candidates):
            if idx in taken:
                continue
            score = _label_score(option, cand["kv"].get("key") or "")
            if score < _MIN_LABEL_SCORE:
                continue
            # Label similarity dominates; distance only breaks ties between
            # equally-named boxes (which is exactly the Yes/No ambiguity).
            rank = (-score, cand["dy"], cand["dx"])
            if best_rank is None or rank < best_rank:
                best, best_rank, best_idx = cand, rank, idx
        if best is None:
            continue
        taken.add(best_idx)
        resolved.append(
            {
                "label": clean_text(best["kv"].get("key") or "") or option,
                "state": _state_of(best["kv"]),
                "score": round(-best_rank[0], 1),
                "excel_option": option,
            }
        )

    if not resolved:
        return None
    return {
        "options": resolved,
        "matched": len(resolved),
        "expected": len(options),
        "confidence": round(len(resolved) / float(len(options)), 3),
    }


def boxes_on_label_row(
    label_bbox: Sequence[float],
    page: int,
    checkbox_kvs: Sequence[dict],
    max_options: int = 24,
) -> Optional[List[dict]]:
    """Every checkbox physically sitting on the label's row, ignoring names.

    ``regroup`` needs the rule's declared option list to work. Many CHECKBOX
    rules have none -- the Excel example never parsed into options -- and for
    those ``_check_checkbox_alignment`` used to return ``None``, meaning the row
    got NO checkbox check at all and rolled up to PASS on "Field present." alone.
    That produced observed false passes: CMP-0021 and CMP-0040
    (BIRTHPLACE - U.S. State/Territory) reported PASS while carrying the
    Parent/Mother/Father boxes, which belong to a different field entirely.

    This function answers the weaker but always-available question: which boxes
    are actually printed on this label's row? Purely positional -- no label
    matching -- so it can contradict the DI group and reveal the mis-grouping.

    Returns ``[{"label", "state", "dx"}, ...]`` ordered left-to-right, or None.
    """
    anchor = _anchor(label_bbox or [])
    if anchor is None or not checkbox_kvs:
        return None
    anchor_x, anchor_y = anchor

    found: List[dict] = []
    for kv in checkbox_kvs:
        if kv.get("page") != page or not _is_checkbox_kv(kv):
            continue
        centre = _centre(kv.get("bbox") or [])
        if centre is None:
            continue
        cx, cy = centre
        dx = cx - anchor_x
        # Single-row band only. Widening here would re-import the neighbouring
        # field's boxes, which is the very error being detected.
        if abs(cy - anchor_y) > _BASE_ROW_TOLERANCE:
            continue
        if dx < -_LEFT_ALLOWANCE or dx > _MAX_DX:
            continue
        label = clean_text(kv.get("key") or "")
        if not label:
            continue
        found.append({"label": label, "state": _state_of(kv), "dx": dx})

    if not found or len(found) > max_options:
        return None
    found.sort(key=lambda o: o["dx"])
    return found
