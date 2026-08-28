"""PrintIQ field-identity guard.

Drop-in post-match validation for rule/PDF comparison records. It addresses:
1. scores above 100;
2. token-subset false positives such as DATE LAST MARRIAGE ENDED -> DATE OF MARRIAGE;
3. section/subsection leakage;
4. checkbox groups attributed to unrelated rows.

This module deliberately changes unsafe matches to REVIEW_REQUIRED. It does not
invent a replacement field when the extraction does not contain a trustworthy one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

STOP = {
    "a","an","and","apply","at","by","check","choose","completed","enter","field",
    "for","from","in","is","label","of","on","one","option","or","the","this","time",
    "to","with"
}
GENERIC = {"name","date","number","country","county","city","state","parent","party","field","value"}


def _norm(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.upper().replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(value: Any) -> List[str]:
    return [x.lower() for x in _norm(value).split() if x.lower() not in STOP]


def _content(value: Any) -> Set[str]:
    return {x for x in _tokens(value) if len(x) > 1 and x not in GENERIC}


def _ratio(a: Any, b: Any) -> float:
    return 100.0 * SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _coverage(a: Any, b: Any) -> Tuple[float, float]:
    aa, bb = set(_tokens(a)), set(_tokens(b))
    if not aa or not bb:
        return 0.0, 0.0
    common = aa & bb
    return 100.0 * len(common) / len(aa), 100.0 * len(common) / len(bb)


def _section_compatible(expected: Any, actual: Any) -> bool:
    e, a = _norm(expected), _norm(actual)
    if not e or not a:
        return True
    return e == a or e in a or a in e


def identity_evidence(rule: Mapping[str, Any], field: Mapping[str, Any]) -> Dict[str, Any]:
    item = rule.get("item", "")
    key = field.get("di_key", field.get("key", ""))
    seq = _ratio(item, key)
    cov_rule, cov_pdf = _coverage(item, key)
    expected_distinct = _content(item)
    actual_distinct = _content(key)
    shared_distinct = expected_distinct & actual_distinct
    missing_distinct = expected_distinct - actual_distinct

    section_ok = _section_compatible(rule.get("section"), field.get("di_section", field.get("section")))
    subsection_ok = _section_compatible(rule.get("subsection"), field.get("di_subsection", field.get("subsection")))

    # Require lexical resemblance plus the distinctive words that identify the row.
    # Exact/near-exact labels pass. Short generic labels need section support.
    exact = _norm(item) == _norm(key) and bool(_norm(item))
    # When both labels contain only generic vocabulary, an empty distinctive-token
    # set is absence of semantic evidence, not agreement. Such labels must match
    # exactly. This general rule blocks typo-neighbour collisions such as
    # COUNTRY -> COUNTY without maintaining client- or form-specific pairs.
    generic_only = bool(_tokens(item)) and bool(_tokens(key)) and not expected_distinct and not actual_distinct
    generic_collision = generic_only and not exact

    distinctive_ok = exact or (not generic_collision and (not expected_distinct or bool(shared_distinct)))
    if len(expected_distinct) >= 2:
        distinctive_ok = exact or len(shared_distinct) >= min(2, len(expected_distinct))

    lexical_ok = (not generic_collision) and (exact or seq >= 72.0 or (cov_rule >= 75.0 and cov_pdf >= 60.0))
    hierarchy_ok = section_ok and subsection_ok
    confident = bool(lexical_ok and distinctive_ok and hierarchy_ok)

    reasons: List[str] = []
    if generic_collision:
        reasons.append(f"generic-only labels differ: {_norm(item)} vs {_norm(key)}")
    elif not lexical_ok:
        reasons.append(f"weak label similarity ({seq:.1f}%)")
    if not distinctive_ok:
        reasons.append("distinctive label words do not agree")
    if missing_distinct and not exact:
        reasons.append("missing expected words: " + ", ".join(sorted(missing_distinct)))
    if not section_ok:
        reasons.append("section mismatch")
    if not subsection_ok:
        reasons.append("subsection mismatch")

    return {
        "confident": confident,
        "label_score": round(min(100.0, seq), 2),
        "rule_coverage": round(cov_rule, 2),
        "pdf_coverage": round(cov_pdf, 2),
        "shared_distinctive_tokens": sorted(shared_distinct),
        "reasons": reasons,
    }


def _is_checkbox(record: Mapping[str, Any]) -> bool:
    return str(record.get("rule_type", "")).upper() == "CHECKBOX" or str(record.get("di_kind", "")).lower() == "checkbox_group"


def _checkbox_options(value: Any) -> Set[str]:
    result: Set[str] = set()
    for part in str(value or "").split(";"):
        label = re.sub(r"-(?:un)?selected\s*$", "", part.strip(), flags=re.I)
        result.update(_content(label))
    return result


def validate_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    meta = dict(out.get("metadata") or {})
    checks = [dict(x) for x in (out.get("checks") or [])]

    # Normalize every public score to the documented 0..100 range.
    for key in ("match_score",):
        try:
            out[key] = round(max(0.0, min(100.0, float(out.get(key, 0.0)))), 2)
        except (TypeError, ValueError):
            out[key] = 0.0

    if not out.get("matched"):
        out["metadata"] = meta
        return out

    evidence = identity_evidence(out, out)
    meta["identity_guard"] = evidence

    # A workbook-declared multipart rule can never conclude PASS/FAIL from a
    # partial logical candidate.
    expected_parts = list(meta.get("composite_expected_parts") or [])
    composite_incomplete = len(expected_parts) > 1 and meta.get("composite_complete") is False

    # Checkbox option text must have some relation to the expected row unless the
    # row label itself is a confident match and geometry has already been validated.
    checkbox_mismatch = False
    if _is_checkbox(out):
        option_tokens = _checkbox_options(out.get("di_value"))
        expected_tokens = _content(out.get("item"))
        has_position_warning = any(
            c.get("name") == "checkbox_position" and c.get("status") != "PASS" for c in checks
        )
        if has_position_warning or (option_tokens and expected_tokens and not (option_tokens & expected_tokens) and not evidence["confident"]):
            checkbox_mismatch = True

    # Serialized checkbox labels must be actual options, never keys from a
    # reconstruction response wrapper.
    malformed_checkbox = False
    if _is_checkbox(out):
        forbidden = {"options", "matched", "expected", "confidence", "score", "reason", "result"}
        labels = {_norm(x.rsplit("-", 1)[0]).lower() for x in str(out.get("di_value") or "").split(";") if x.strip()}
        malformed_checkbox = bool(labels & forbidden)
        if meta.get("checkbox_payload_valid") is False:
            malformed_checkbox = True

    if (not evidence["confident"]) or checkbox_mismatch or composite_incomplete or malformed_checkbox:
        reasons = list(evidence["reasons"])
        if composite_incomplete:
            detected = list(meta.get("composite_detected_parts") or [])
            reasons.append("incomplete composite identity: expected " + ", ".join(map(str, expected_parts)))
            if detected:
                reasons.append("detected only " + ", ".join(map(str, detected)))
        if malformed_checkbox:
            reasons.append("checkbox payload contains reconstruction metadata instead of printed option labels")
        if checkbox_mismatch:
            reasons.append("checkbox group is not trustworthy for this row")
        reason = "; ".join(dict.fromkeys(reasons)) or "field identity is not trustworthy"
        out.update({
            "status": "REVIEW_REQUIRED",
            "matched": False,
            "match_score": 0.0,
            "di_kind": None,
            "di_section": None,
            "di_subsection": None,
            "di_key": None,
            "di_value": None,
            "page": None,
            "bbox": None,
            "confidence": None,
            "checks": [{
                "name": "identity_guard",
                "status": "MISSING_DATA",
                "expected": out.get("item", ""),
                "actual": "candidate rejected",
                "message": reason,
            }],
            "message": "Candidate PDF field rejected by identity guard: " + reason,
        })
        meta.update({
            "identity_confident": False,
            "internal_status": "MISSING_DATA",
            "reviewer_status": "REVIEW_REQUIRED",
            "review_reason": out["message"],
        })
    else:
        meta["identity_confident"] = True
        meta["identity_label_score"] = evidence["label_score"]

    out["metadata"] = meta
    return out



def _bbox_signature(row: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    bbox = row.get("bbox")
    if not isinstance(bbox, Mapping):
        return None
    vals = [bbox.get(k) for k in ("x0", "y0", "x1", "y1")]
    try:
        return (row.get("page"),) + tuple(round(float(v), 6) for v in vals)
    except (TypeError, ValueError):
        return None


def _audit_shared_geometry(rows: List[Dict[str, Any]]) -> None:
    """Mark non-unique geometry as untrusted and suppress misleading overlays.

    This is schema-driven and form-agnostic. If the exact same box is assigned to
    different logical PDF keys, the box cannot independently locate all of them.
    Label identity remains available, but the shared fallback box is removed from
    overlays until extraction supplies field-specific geometry.
    """
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for row in rows:
        if not row.get("matched"):
            continue
        sig = _bbox_signature(row)
        if sig is not None:
            groups.setdefault(sig, []).append(row)
    for grouped in groups.values():
        keys = {_norm(r.get("di_key")) for r in grouped if _norm(r.get("di_key"))}
        if len(keys) <= 1:
            continue
        for row in grouped:
            meta = dict(row.get("metadata") or {})
            meta.update({
                "geometry_trusted": False,
                "geometry_reason": "The same bounding box was assigned to multiple logical PDF fields.",
                "shared_geometry_keys": sorted(keys),
            })
            row["metadata"] = meta
            row["bbox"] = None

def validate_document(document: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(document)
    comparisons = [validate_record(x) for x in document.get("comparisons", [])]
    _audit_shared_geometry(comparisons)
    result["comparisons"] = comparisons
    counts: Dict[str, int] = {}
    for row in comparisons:
        s = str(row.get("status", "REVIEW_REQUIRED"))
        counts[s] = counts.get(s, 0) + 1
    result["status_counts"] = counts
    result["matched_count"] = sum(bool(x.get("matched")) for x in comparisons)
    result["identity_guard_version"] = "1.4.0"
    return result


def apply_identity_guard(summary):
    """Apply the dictionary guard to a ComparisonSummary and return the same model type.

    The conversion keeps the identity guard in the single source of truth before
    reviewer outcome calculation, overlays, JSON exports, and Excel exports.
    """
    guarded = validate_document(summary.model_dump(mode="json"))
    return type(summary).model_validate(guarded)
