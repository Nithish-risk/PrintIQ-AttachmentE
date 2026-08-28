"""Single source of truth for reviewer-facing outcomes.

A PASS or FAIL is emitted only when field identity is reliable. Unresolved
matching, extraction, or rule-evaluation evidence is REVIEW_REQUIRED.
"""
from __future__ import annotations
from collections import Counter
from rapidfuzz import fuzz
from config.constants import Status

_IDENTITY_MIN_LABEL_SCORE = 88.0
_IDENTITY_MIN_MATCH_SCORE = 80.0


def _value(value):
    return getattr(value, "value", str(value or ""))


def _norm(value):
    return " ".join(str(value or "").upper().replace("–", "-").replace("—", "-").split())


def _is_technical(c):
    return c.rule_type in {"UNMATCHED_FIELD", "NO_PRINT_RULE"} or not c.rule_id


def _is_safe_static_recovery(c):
    meta = dict(c.metadata or {})
    if meta.get("recovery_source") not in {"FULL_TEXT", "EXACT_FULL_TEXT"}:
        return True
    # Full-text presence can validate only true document-level/static content.
    # Any sectioned/subsectioned rule represents a field and cannot pass merely
    # because its label appears in page text.
    if c.section or c.subsection:
        return False
    return c.rule_type == "STATIC_TEXT" or (
        c.rule_type == "TEXT_OR_LAYOUT"
        and _norm(c.item) == _norm(c.di_value)
        and _norm(c.item) == _norm(c.di_key)
    )


def _field_identity(c):
    """Return the authoritative guarded identity decision.

    The matcher guard normalizes punctuation, checks distinctive vocabulary and
    hierarchy, and operates before this reviewer layer. Re-running a different
    fuzzy metric here caused exact logical labels such as CITY, VILLAGE,TOWN and
    typo-repaired labels to be downgraded inconsistently.
    """
    meta = dict(c.metadata or {})
    reasons = []
    if not c.matched:
        reasons.append("No PDF field was confidently matched.")
    if meta.get("v4_workflow_state") == "REVIEW_REQUIRED":
        reasons.append("The sequence-aware matcher marked the field identity for review.")
    if not _is_safe_static_recovery(c):
        reasons.append("Full-text label recovery cannot validate a sectioned field rule.")

    guard = meta.get("identity_guard") if isinstance(meta.get("identity_guard"), dict) else None
    if guard is not None:
        label_score = float(guard.get("label_score") or 0.0)
        if c.matched and not guard.get("confident", False):
            reasons.extend(str(x) for x in (guard.get("reasons") or []) if x)
    elif c.di_kind == "static_text":
        label_score = 100.0 if _norm(c.item) == _norm(c.di_key) else 0.0
    else:
        label_score = float(fuzz.token_set_ratio(_norm(c.item), _norm(c.di_key))) if c.di_key else 0.0
        raw_match = float(c.match_score or 0.0)
        if c.matched and label_score < _IDENTITY_MIN_LABEL_SCORE:
            reasons.append(f"Excel/PDF field-label agreement is only {label_score:.1f}%.")
        if c.matched and raw_match < _IDENTITY_MIN_MATCH_SCORE:
            reasons.append(f"Operational match score is only {raw_match:.1f}.")

    raw_match = float(c.match_score or 0.0)
    confident = not reasons
    return confident, {
        "identity_confident": confident,
        "identity_label_score": round(label_score, 2),
        "identity_match_score": round(raw_match, 2),
        "identity_reasons": list(dict.fromkeys(reasons)),
        "identity_source": "matcher_guard" if guard is not None else "reviewer_fallback",
    }

def _review_reason(c, identity):
    reasons = list(identity.get("identity_reasons") or [])
    meta = dict(c.metadata or {})
    if meta.get("subsection_spec_conflict"):
        reasons.append(
            "Workbook/PDF subsection conflict: expected "
            f"{meta.get('expected_subsection')!r}, observed {meta.get('observed_subsection')!r}."
        )
    for check in c.checks or []:
        status = _value(check.status)
        if status != Status.PASS.value and check.message:
            reasons.append(str(check.message).strip())
    if not reasons and c.message:
        reasons.append(str(c.message).strip())
    if not reasons:
        reasons.append("The result could not be concluded safely.")
    return " ".join(dict.fromkeys(x for x in reasons if x))


def _placeholder_tokens(value):
    import re
    return [x for x in re.split(r"[\s,]+", str(value or "").upper()) if x]


def reviewer_status(c):
    """Return (status, identity metadata) for a FieldComparison.

    Final safety invariants are deliberately repeated here so no earlier-stage
    cache/import problem can turn an incomplete identity into PASS or FAIL.
    """
    identity_ok, identity = _field_identity(c)
    original = _value(c.status)
    check_values = {_value(check.status) for check in (c.checks or [])}
    meta = dict(c.metadata or {})

    expected_parts = list(meta.get("composite_expected_parts") or [])
    if len(expected_parts) > 1 and meta.get("composite_complete") is False:
        identity["identity_confident"] = False
        identity["identity_reasons"] = list(dict.fromkeys(
            list(identity.get("identity_reasons") or []) + ["Incomplete composite identity cannot produce PASS or FAIL."]
        ))
        return Status.REVIEW_REQUIRED, identity

    if c.rule_type == "CHECKBOX" and meta.get("checkbox_payload_valid") is False:
        identity["identity_confident"] = False
        identity["identity_reasons"] = list(dict.fromkeys(
            list(identity.get("identity_reasons") or []) + ["Malformed checkbox reconstruction payload."]
        ))
        return Status.REVIEW_REQUIRED, identity

    # An example demonstrates format unless an explicit option list exists. A
    # dropdown value differing lexically from the example is not a conclusive FAIL.
    if meta.get("instruction_validation_mode") == "FORMAT_EXAMPLE_ONLY" and Status.FAIL.value in check_values:
        identity["identity_reasons"] = list(dict.fromkeys(
            list(identity.get("identity_reasons") or []) + ["The example is a format exemplar, not an allowed-value whitelist."]
        ))
        return Status.REVIEW_REQUIRED, identity

    # Deterministic unknown rendering for multipart fields. If the workbook says
    # a single placeholder must be aligned under First, repeated placeholders are
    # a FAIL; lack of trustworthy geometry is REVIEW_REQUIRED.
    unknown_rule = _norm(meta.get("if_unknown_rule") or "")
    tokens = _placeholder_tokens(c.di_value)
    placeholders = {"UNKNOWN", "UNNAMED"}
    if len(expected_parts) > 1 and tokens and all(x in placeholders for x in tokens) and "SINGLE LINE" in unknown_rule:
        if len(tokens) > 1:
            return (Status.FAIL if identity_ok else Status.REVIEW_REQUIRED), identity
        if c.bbox is None or meta.get("geometry_trusted") is False:
            identity["identity_reasons"] = list(dict.fromkeys(
                list(identity.get("identity_reasons") or []) + ["Single unknown placeholder found, but required first-part alignment cannot be verified."]
            ))
            return Status.REVIEW_REQUIRED, identity
        return (Status.PASS if identity_ok else Status.REVIEW_REQUIRED), identity

    # A conclusive validation failure is only a reviewer FAIL when the field
    # being checked is itself reliable. Otherwise a valid failure applied to the
    # wrong field would be misleading.
    if original == Status.FAIL.value or Status.FAIL.value in check_values:
        return (Status.FAIL if identity_ok else Status.REVIEW_REQUIRED), identity

    # V4 review, unsafe full-text recovery, missing identity, or any unresolved
    # check blocks PASS.
    if not identity_ok:
        return Status.REVIEW_REQUIRED, identity

    pass_compatible = {Status.PASS.value, Status.UNKNOWN_DATA.value, Status.MISSING_DATA.value}
    if original in pass_compatible and c.matched and check_values.issubset(pass_compatible):
        return Status.PASS, identity

    return Status.REVIEW_REQUIRED, identity


def apply_reviewer_outcomes(summary):
    for c in summary.comparisons:
        if _is_technical(c):
            c.metadata = dict(c.metadata or {})
            c.metadata["technical_only"] = True
            continue
        original = _value(c.status)
        final, identity = reviewer_status(c)
        c.metadata = dict(c.metadata or {})
        c.metadata.update(identity)
        c.metadata["internal_status"] = original
        c.metadata["reviewer_status"] = final.value
        if final == Status.REVIEW_REQUIRED:
            reason = _review_reason(c, identity)
            c.metadata["review_reason"] = reason
            c.message = reason
        else:
            c.metadata.pop("review_reason", None)
        c.status = final
    summary.status_counts = dict(Counter(
        c.status.value for c in summary.comparisons
        if not (c.metadata or {}).get("technical_only")
    ))
    return summary
