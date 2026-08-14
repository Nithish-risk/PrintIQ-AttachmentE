"""Group adjacent DI fields that belong to one Excel rule.

Azure DI splits a single ruled field into one KV pair per printed sub-label:

    "CURRENT NAME- First" = UNNAMED
    "Middle"              = UNNAMED
    "Last"                = UNNAMED
    "Suffix"              = ""

but the sheet describes all four with ONE row:

    CURRENT NAME - First, Middle, Last, Suffix
    FORMAT: <First> + <Middle> + <Last> + <Suffix>

Binding that rule to a single field would leave "Middle"/"Last"/"Suffix" loose
for an unrelated later rule to claim -- the observed cause of ISSUING OFFICIAL
(y=0.659) binding to a stray "Last" in the Party A name block (y=0.209).

The guard is geometric: the parts of one ruled field are printed on the same
imaginary horizontal line. We therefore only ever merge fields that share a page
and sit within a small vertical band, ordered left-to-right.

NOT every multi-part rule maps to multiple fields: DI returns OFFICIANT MAILING
ADDRESS as ONE field whose value already contains every part. So the parts are
an upper bound ("up to N fields"), never a requirement.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from rapidfuzz import fuzz

from utils.text_utils import clean_text, norm

# Maximum vertical gap (fraction of page height) between two fields still
# considered to be on the same printed line. DI label boxes for one row differ
# by <0.002 in the sample; 0.012 tolerates a wrapped label without reaching the
# next form row (~0.025 apart).
SAME_LINE_TOLERANCE = 0.012

# A part label must match a candidate field's key at least this well.
_PART_MATCH_FLOOR = 82.0


def _y_center(field: dict) -> Optional[float]:
    bbox = field.get("bbox") or []
    if len(bbox) != 4:
        return None
    return (float(bbox[1]) + float(bbox[3])) / 2.0


def _x_left(field: dict) -> float:
    bbox = field.get("bbox") or []
    return float(bbox[0]) if len(bbox) == 4 else 0.0


def same_line(a: dict, b: dict, tolerance: float = SAME_LINE_TOLERANCE) -> bool:
    """True when two DI fields sit on the same printed horizontal line."""
    if a.get("page") != b.get("page"):
        return False
    ya, yb = _y_center(a), _y_center(b)
    if ya is None or yb is None:
        return False
    return abs(ya - yb) <= tolerance


def _part_score(part: str, field: dict) -> float:
    """How well a part label ("Middle") matches a DI field's key."""
    key = clean_text(field.get("key") or "")
    if not key:
        return 0.0
    p_norm, k_norm = norm(part), norm(key)
    if p_norm == k_norm:
        return 100.0
    # "CURRENT NAME- First" contains the part "First".
    if p_norm and (p_norm in k_norm or k_norm in p_norm):
        return 96.0
    return float(fuzz.token_set_ratio(p_norm, k_norm))


def collect_group(
    anchor: dict,
    part_labels: List[str],
    candidates: List[dict],
    used: set,
) -> List[dict]:
    """Return the fields making up one multi-part rule, in printed order.

    *anchor* is the field the rule matched on its full item text (typically the
    first part, whose key carries the whole label). Remaining parts are matched
    against fields on the same line, left-to-right, skipping any already claimed.

    Returns ``[anchor]`` when no siblings are found -- correct for the case where
    DI kept the whole field intact.
    """
    if not part_labels:
        return [anchor]

    line_mates = [
        f for f in candidates
        if f["order"] not in used
        and f["order"] != anchor["order"]
        and same_line(anchor, f)
        and f.get("kind") != "checkbox_group"
    ]
    if not line_mates:
        return [anchor]

    group = [anchor]
    claimed = {anchor["order"]}
    for part in part_labels:
        best, best_score = None, _PART_MATCH_FLOOR
        for field in line_mates:
            if field["order"] in claimed:
                continue
            score = _part_score(part, field)
            if score > best_score:
                best, best_score = field, score
        if best is not None:
            group.append(best)
            claimed.add(best["order"])

    group.sort(key=_x_left)
    return group


def merge_group(group: List[dict], part_labels: List[str],
                part_separators: Optional[List[str]] = None) -> dict:
    """Collapse a field group into one record the existing checks can consume.

    Parts are joined in printed (left-to-right) order using the separators the
    rule's FORMAT clause specifies -- "+"/juxtaposition means a space, the
    literal word "comma" means ", ". Joining everything with a space would make
    an address print as "123 NW LANCASTER AVENUE LAKE NEBGAMMON WI 53033" and
    then fail its own format check against the comma-bearing example.

    A separator is emitted only BETWEEN two non-empty parts, so an absent middle
    component never leaves a doubled ", ," in the joined value.
    """
    if len(group) == 1:
        merged = dict(group[0])
        merged["_parts"] = [
            {"key": group[0].get("key", ""), "value": group[0].get("value", "")}
        ]
        merged["_group_orders"] = [group[0]["order"]]
        return merged

    parts = [{"key": f.get("key") or "", "value": clean_text(f.get("value") or "")}
             for f in group]
    seps = list(part_separators or [])

    joined = ""
    for idx, part in enumerate(parts):
        if not part["value"]:
            continue
        if joined:
            sep = seps[idx] if idx < len(seps) and seps[idx] else " "
            joined += sep
        joined += part["value"]

    merged = dict(group[0])
    merged["value"] = joined
    merged["_parts"] = parts
    merged["_group_orders"] = [f["order"] for f in group]
    merged["_merged_from"] = len(group)
    return merged


def find_part_gaps(merged: dict) -> List[str]:
    """Part labels that are blank but followed by a populated part.

    A trailing blank is normal (an absent Suffix). A blank BETWEEN populated
    parts means a value the print job should have supplied is missing -- a
    reportable gap.
    """
    parts = merged.get("_parts") or []
    if len(parts) < 2:
        return []
    last_filled = -1
    for i, part in enumerate(parts):
        if part["value"]:
            last_filled = i
    return [
        parts[i]["key"] or f"part {i + 1}"
        for i in range(last_filled)
        if not parts[i]["value"]
    ]
