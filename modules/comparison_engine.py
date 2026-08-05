"""Compare parsed Excel print rules against DI ``structured_fields``.

This is the primary validation layer. It aligns each ``PrintRule`` (expected)
with the best-matching DI ``structured_field`` (actual) using
section/subsection-scoped fuzzy scoring, then runs per-``rule_type`` checks.
Everything is PDF-only and rule-relative: we only check that the printed value
*follows the Excel rules/format* (presence, format vs. the example, pattern,
max-chars, bold, checkbox alignment). We never judge whether a value is the
*correct* value for its label. Fields located but empty become MISSING_DATA;
fields whose value is a placeholder (e.g. "Unknown") become UNKNOWN_DATA.
Results are adapted to ``ValidationResult`` for the overlay and report writers
via ``modules/comparison_adapter.py``.
""" 

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from config.constants import Status
from models.rule_models import PrintRule
from models.validation_models import BBox
from models.comparison_models import CheckResult, FieldComparison, ComparisonSummary
from modules.kv_matcher import detect_party, is_sentinel
from utils.text_utils import clean_text, norm
from utils.date_utils import infer_date_pattern, validate_date


# ---------------------------------------------------------------------------
# Field flattening: structured_fields carry heterogeneous ``value`` shapes
# (str for text/checkbox_group, dict for composite). Normalize to a common
# record the matcher/checks can reason about.
# ---------------------------------------------------------------------------
def _field_value_str(field: dict) -> str:
    value = field.get("value")
    if isinstance(value, dict):
        return " ".join(f"{k}={v}" for k, v in value.items() if v)
    return clean_text(value or "")


def _field_bbox(field: dict) -> Optional[BBox]:
    # Prefer the tight printed-label box (key line / parent label / heading) so
    # the overlay highlights the label only, never the value or checkbox glyphs.
    # Fall back to the field's own bbox when no label box was located.
    bbox = field.get("label_bbox") or field.get("bbox") or []
    if len(bbox) != 4:
        return None
    return BBox(page=field.get("page", 1), x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3])


def _format_signature(text: str) -> str:
    """Reduce a string to a coarse format signature so two values can be
    compared for *shape* (not content).

    Letters -> ``A``, digits -> ``9``, runs collapsed, punctuation/spacing kept.
    So ``"NOVEMBER 30, 1999"`` and ``"SEPTEMBER 30, 2025"`` both become
    ``"A 9, 9"`` — the same format. Used to check a printed value against the
    rule's ``example`` without judging the actual value.
    """
    s = clean_text(text or "")
    if not s:
        return ""
    out: List[str] = []
    prev = ""
    for ch in s:
        if ch.isalpha():
            token = "A"
        elif ch.isdigit():
            token = "9"
        elif ch.isspace():
            token = " "
        else:
            token = ch
        # Collapse consecutive identical class tokens (A/9/space).
        if token in ("A", "9", " ") and token == prev:
            continue
        out.append(token)
        prev = token
    return "".join(out).strip()


def _format_matches_example(value: str, example: str) -> Optional[bool]:
    """True/False when both *value* and *example* have a comparable format;
    None when we can't compare (either is blank)."""
    v_sig = _format_signature(value)
    e_sig = _format_signature(example)
    if not v_sig or not e_sig:
        return None
    return v_sig == e_sig


# ---------------------------------------------------------------------------
# Matcher tunables (all additive to the fuzzy key score unless noted).
# ---------------------------------------------------------------------------
# Minimum post-bonus score a candidate must reach to be accepted as a match.
# Below this the rule is left unmatched (an honest FAIL) rather than bound to a
# wrong DI field. Every legitimate composite/text/checkbox match observed scores
# >= 77; the wrong cross-section composite blobs (empty section, matched only on
# value-fallback) land at ~63-64. A 70 floor drops those two false binds
# (officiant->witness, party-B-address->officiant) without losing any real match.
MIN_ACCEPT_SCORE = 70.0
# Applied when the DI field's kind is incompatible with the rule's expected kind
# (checkbox rule ↔ non-checkbox field, or vice-versa). Large enough to demote a
# wrong-kind field below any right-kind candidate, but not a hard skip so a rule
# with no compatible field can still surface its closest (and then likely fail).
KIND_MISMATCH_PENALTY = -60.0
# Section / subsection agreement bonuses and (stronger) mismatch penalties.
# Party A/B share identical item text, so subsection must dominate ties.
SECTION_MATCH_BONUS = 12.0
SECTION_MISMATCH_PENALTY = -15.0
SUBSECTION_MATCH_BONUS = 10.0
SUBSECTION_MISMATCH_PENALTY = -15.0
# Cap on how much a match against the DI *value* (rather than the key) may
# contribute. Only composite fields use this fallback (their ``key`` is empty
# and the part-labels live in the value), so the long-checkbox-blob inflation
# risk is already gone. Must stay ABOVE ``fuzzy_threshold`` (60) or composites
# get pre-gated out before scoring — that dropped every ZIP/COUNTRY/name field.
VALUE_MATCH_CAP = 90.0
# A used field is excluded from a later rule unless no unused candidate clears
# the acceptance floor; this hard-stops the HISPANIC/RACE double-bind.
USED_FIELD_PENALTY = -1000.0


def _rule_expected_kind(rule: PrintRule) -> str:
    """Coarse expected DI kind for a rule: ``checkbox_group`` or ``text``.

    Checkbox rules are those explicitly typed ``CHECKBOX`` or whose Excel example
    parsed into an option list (``expected_kind == 'checkbox_group'``). Everything
    else (text, date, static) expects a text/composite field.
    """
    if getattr(rule, "expected_kind", None) == "checkbox_group":
        return "checkbox_group"
    if (rule.rule_type or "").upper() == "CHECKBOX":
        return "checkbox_group"
    return "text"


def _kinds_compatible(rule_kind: str, di_kind: Optional[str]) -> bool:
    """True when a rule's expected kind agrees with the DI field's kind.

    ``text`` rules accept ``text``/``composite`` DI fields; ``checkbox_group``
    rules accept only ``checkbox_group`` DI fields. Unknown DI kinds are treated
    as compatible (don't penalize what we can't classify).
    """
    if not di_kind:
        return True
    if rule_kind == "checkbox_group":
        return di_kind == "checkbox_group"
    return di_kind in ("text", "composite")


class RuleFieldMatcher:
    """Align each PrintRule to its best DI structured_field.

    Scoring combines key/item similarity (primary), section & subsection
    agreement (bonus/penalty), kind compatibility, and a small sentinel penalty.
    Consume-once (hard) ensures two rules don't both claim the same DI field.
    A final acceptance floor leaves weak matches unbound (honest FAIL) instead of
    attaching a rule to the wrong field.
    """

    def __init__(self, structured_fields: List[dict], fuzzy_threshold: int = 60):
        self.threshold = fuzzy_threshold
        self.fields: List[dict] = []
        for order, f in enumerate(structured_fields or []):
            self.fields.append(
                {
                    "order": order,
                    "kind": f.get("kind"),
                    "key": clean_text(f.get("key") or ""),
                    "value": _field_value_str(f),
                    "section": clean_text(f.get("section") or ""),
                    "subsection": clean_text(f.get("subsection") or ""),
                    "page": f.get("page", 1),
                    "bbox": f.get("bbox") or [],
                    "confidence": f.get("confidence"),
                    "raw": f,
                }
            )
        self._used: set[int] = set()

    @staticmethod
    def _text_score(labels: List[str], target: str) -> float:
        target_u = (target or "").upper()
        if not target_u:
            return 0.0
        best = 0.0
        for lab in labels:
            lu = (lab or "").upper()
            if not lu:
                continue
            if lu in target_u or target_u in lu:
                best = max(best, 100.0)
            else:
                best = max(best, float(fuzz.token_set_ratio(lu, target_u)))
        return best

    def match(self, rule: PrintRule, consume: bool = True) -> Optional[dict]:
        labels = [clean_text(x) for x in (rule.item, rule.label_printed) if x]
        labels = [l for l in labels if len(l) >= 3]
        if not labels or not self.fields:
            return None
        rule_sec = norm(rule.section or "")
        rule_sub = norm(rule.subsection or "")
        rule_kind = _rule_expected_kind(rule)

        scored = []
        for it in self.fields:
            key_score = self._text_score(labels, it["key"])
            # Value fallback: only for composite fields (part-labels live in the
            # value) and capped so a long checkbox blob can't inflate the score.
            if it["kind"] == "composite":
                value_score = min(self._text_score(labels, it["value"]), VALUE_MATCH_CAP)
                key_score = max(key_score, value_score - 10.0)
            if key_score < self.threshold:
                continue
            score = key_score
            # Kind compatibility gate (Fix 1).
            if not _kinds_compatible(rule_kind, it["kind"]):
                score += KIND_MISMATCH_PENALTY
            # Section / subsection agreement (Fix 4): bonus on match, real
            # penalty on mismatch so cross-party/section bleed is broken.
            if rule_sec and it["section"]:
                agree = rule_sec in norm(it["section"]) or norm(it["section"]) in rule_sec
                score += SECTION_MATCH_BONUS if agree else SECTION_MISMATCH_PENALTY
            if rule_sub and it["subsection"]:
                agree = rule_sub in norm(it["subsection"]) or norm(it["subsection"]) in rule_sub
                score += SUBSECTION_MATCH_BONUS if agree else SUBSECTION_MISMATCH_PENALTY
            if is_sentinel(it["value"]):
                score -= 5.0
            # Hard consume-once (Fix 3): a used field is effectively excluded.
            if it["order"] in self._used:
                score += USED_FIELD_PENALTY
            scored.append((score, it))

        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best = scored[0]
        # Acceptance floor (Fix 2): don't bind weak/wrong matches.
        if best_score < MIN_ACCEPT_SCORE:
            return None
        if consume:
            self._used.add(best["order"])
        best["_match_score"] = round(best_score, 2)
        return best

    def unused_fields(self) -> List[dict]:
        return [f for f in self.fields if f["order"] not in self._used]


_MAX_CHARS_RE = re.compile(r"(\d{1,3})")

# ---------------------------------------------------------------------------
# Checkbox option parsing/alignment helpers (Phase B).
# ---------------------------------------------------------------------------
_STATE_RE = re.compile(r":?\s*(?:un)?selected\s*:?", re.IGNORECASE)


def _parse_di_checkbox_options(value: str) -> List[dict]:
    """Parse a DI checkbox-group value into ``[{label, state}, ...]``.

    Handles the common serialized shapes, e.g.::

        "Divorce :unselected: Death :selected: Annulment :unselected:"
        "Divorce:unselected, Death:selected"
        "No -selected- Yes -unselected-"

    State defaults to ``unselected`` when a label has no explicit marker.
    """
    if not value:
        return []
    text = str(value)
    options: List[dict] = []
    if "," in text and _STATE_RE.search(text):
        chunks = text.split(",")
    else:
        # Keep each label attached to its trailing state marker.
        chunks = re.split(r"(?<=selected)[:\-\s]+", text, flags=re.IGNORECASE)
    for chunk in chunks:
        c = clean_text(chunk)
        if not c:
            continue
        m = re.search(r"(un)?selected", c, re.IGNORECASE)
        state = "unselected"
        if m:
            state = "unselected" if m.group(1) else "selected"
        label = _STATE_RE.sub("", c).strip(" :,-\u2013\u2014")
        if label:
            options.append({"label": label, "state": state})
    return options


def _best_option_match(option: str, di_options: List[dict]) -> tuple[Optional[dict], float]:
    """Return the DI option best matching *option* by fuzzy label ratio."""
    best, best_score = None, 0.0
    opt_u = (option or "").upper()
    for di in di_options:
        lu = (di["label"] or "").upper()
        if not lu:
            continue
        if lu == opt_u or lu in opt_u or opt_u in lu:
            score = 100.0
        else:
            score = float(fuzz.token_set_ratio(lu, opt_u))
        if score > best_score:
            best, best_score = di, score
    return best, best_score


# Fields whose top edge sits within this fraction of the page height are
# candidate page titles/headers (e.g. certificate name, department line).
_HEADER_TOP_Y = 0.12


def _detect_boilerplate_orders(fields: List[dict]) -> set[int]:
    """Return ``order`` ids of DI fields that are page titles/headers.

    Two generic signals, no per-form hardcoding:
      * top-of-page position (top edge within ``_HEADER_TOP_Y``), or
      * the same static text repeating on 2+ pages (running headers/footers).
    Such fields carry no printable rule and should be hidden from the UI.
    """
    by_text: dict[str, set[int]] = {}
    top_orders: set[int] = set()
    for f in fields:
        text = norm(f["value"] or f["key"] or "")
        if not text:
            continue
        by_text.setdefault(text, set()).add(f["page"])
        bbox = f.get("bbox") or []
        if len(bbox) == 4 and bbox[1] <= _HEADER_TOP_Y:
            top_orders.add(f["order"])

    repeated_texts = {t for t, pages in by_text.items() if len(pages) >= 2}
    boilerplate: set[int] = set(top_orders)
    for f in fields:
        text = norm(f["value"] or f["key"] or "")
        if text and text in repeated_texts:
            boilerplate.add(f["order"])
    return boilerplate


class ComparisonEngine:
    """Produce a ComparisonSummary aligning rules ↔ DI structured_fields."""

    def __init__(self, sheet: str, rules: List[PrintRule], structured_fields: List[dict]):
        self.sheet = sheet
        self.rules = rules
        self.structured_fields = structured_fields or []
        self.matcher = RuleFieldMatcher(self.structured_fields)

    # ---- individual checks ------------------------------------------------
    @staticmethod
    def _check_presence(field: Optional[dict]) -> CheckResult:
        if field is None:
            return CheckResult(name="presence", status=Status.FAIL,
                               expected="field printed", actual="no matching field",
                               message="No DI field matched this rule.")
        value = field["value"]
        # Field located but blank -> MISSING_DATA. Field located but value is a
        # placeholder/sentinel (e.g. "Unknown", "N/A") -> UNKNOWN_DATA.
        if not clean_text(value or ""):
            return CheckResult(name="presence", status=Status.MISSING_DATA,
                               expected="printed value", actual="(blank)",
                               message="Field located but no value is printed.")
        if is_sentinel(value):
            return CheckResult(name="presence", status=Status.UNKNOWN_DATA,
                               expected="printed value", actual=value,
                               message="Field located but value is a placeholder (unknown).")
        return CheckResult(name="presence", status=Status.PASS,
                           expected="printed value", actual=value, message="Field present.")

    @staticmethod
    def _check_date(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        pattern = infer_date_pattern(rule.instruction, rule.example, rule.item)
        if not pattern:
            return None
        actual = field["value"] if field else ""
        ok = validate_date(actual, pattern)
        return CheckResult(name="date_pattern",
                           status=Status.PASS if ok else Status.FAIL,
                           expected=pattern, actual=actual or "(none)",
                           message="Date matches pattern." if ok else "Date missing or pattern mismatch.")

    @staticmethod
    def _check_checkbox_alignment(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        """Deterministically align Excel checkbox options with DI options.

        Runs only when the Excel rule's example parsed into a checkbox option
        list (``expected_kind == 'checkbox_group'``) and the matched DI field is
        a checkbox group. Each Excel option is matched to its closest DI option
        by fuzzy label similarity; the DI-reported selected/unselected state is
        reported per option. There is no "correct" answer for which box should
        be ticked (single/multi/none are all valid) — we simply report the
        actual selected options, or state that none are selected.

        Phase C.1: the LLM is asked to align labels (using
        section/subsection/item/example as context) and its mapping REPLACES the
        fuzzy match — but only for DI labels that actually exist, and it never
        affects the DI-reported state. Runs whenever Azure OpenAI is configured;
        otherwise the deterministic fuzzy match is used.
        """
        if not field or field.get("kind") != "checkbox_group":
            return None
        if rule.expected_kind != "checkbox_group" or not rule.expected_options:
            return None

        di_options = _parse_di_checkbox_options(field["value"])
        if not di_options:
            return None

        # Optional LLM alignment map: {excel_option -> di_label or None}.
        # Phase C.1 runs unconditionally; it no-ops safely when Azure OpenAI is
        # not configured (align_checkbox_options returns None).
        llm_map: dict = {}
        used_llm = False
        try:
            from modules.checkbox_llm import align_checkbox_options

            aligned_llm = align_checkbox_options(
                section=rule.section or "",
                subsection=rule.subsection or "",
                item=rule.item or "",
                example=rule.example or "",
                excel_options=rule.expected_options,
                di_options=di_options,
            )
            if aligned_llm and aligned_llm.get("pairs"):
                used_llm = True
                llm_map = {
                    p["excel"]: p.get("di")
                    for p in aligned_llm["pairs"]
                    if isinstance(p, dict) and p.get("excel")
                }
        except Exception:
            used_llm = False

        di_by_label = {d["label"]: d for d in di_options}

        aligned: List[str] = []
        missing: List[str] = []
        selected_labels: List[str] = []
        for opt in rule.expected_options:
            best = None
            # Prefer the LLM's mapping when present and valid.
            if used_llm and opt in llm_map:
                di_label = llm_map.get(opt)
                best = di_by_label.get(di_label) if di_label else None
            # Fall back to deterministic fuzzy match.
            if best is None and not (used_llm and opt in llm_map):
                cand, score = _best_option_match(opt, di_options)
                best = cand if (cand is not None and score >= 70) else None

            if best is None:
                missing.append(opt)
                continue
            state = best["state"]
            aligned.append(f"{opt}↔{best['label']} ({state})")
            if state == "selected":
                selected_labels.append(opt)

        if selected_labels:
            selected_msg = "Selected: " + ", ".join(selected_labels) + "."
        else:
            selected_msg = "No checkboxes are selected."
        parts = [selected_msg]
        if missing:
            parts.append("Options with no DI match: " + ", ".join(missing) + ".")
        if used_llm:
            parts.append("(LLM-aligned)")

        # Status precedence:
        #   * an expected option not found in the PDF at all -> FAIL (discrepancy)
        #   * all options aligned but NONE are marked        -> MISSING_DATA
        #   * at least one option is marked                  -> PASS
        if missing:
            status = Status.FAIL
        elif not selected_labels:
            status = Status.MISSING_DATA
        else:
            status = Status.PASS
        return CheckResult(
            name="checkbox_alignment",
            status=status,
            expected=", ".join(rule.expected_options),
            actual="; ".join(aligned) if aligned else "(no options aligned)",
            message=" ".join(parts),
        )

    @staticmethod
    def _check_max_chars(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        if not rule.max_chars or not field:
            return None
        m = _MAX_CHARS_RE.search(rule.max_chars)
        if not m:
            return None
        limit = int(m.group(1))
        actual_len = len(field["value"] or "")
        ok = actual_len <= limit
        return CheckResult(name="max_chars",
                           status=Status.PASS if ok else Status.FAIL,
                           expected=f"<= {limit} chars", actual=f"{actual_len} chars",
                           message="Within limit." if ok else "Exceeds max characters.")

    @staticmethod
    def _check_bold(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        want = clean_text(rule.bold or "").lower()
        if want not in {"yes", "y", "true", "bold"} or not field:
            return None
        # ``field["raw"]`` is the DI structured_field. Its formatting may live
        # either directly under "formatting" or nested under a "formatting" key
        # inside the field's own value dict; probe both, plus the raw KV.
        raw = field.get("raw") if isinstance(field.get("raw"), dict) else {}
        fmt = raw.get("formatting")
        if not isinstance(fmt, dict):
            value = raw.get("value")
            if isinstance(value, dict) and isinstance(value.get("formatting"), dict):
                fmt = value["formatting"]
        fmt = fmt or {}
        actual = fmt.get("is_bold")
        if actual is None:
            # DI didn't report font weight — skip rather than emit a status.
            return None
        return CheckResult(name="bold", status=Status.PASS if actual else Status.FAIL,
                           expected="bold", actual="bold" if actual else "not bold",
                           message="Bold matches." if actual else "Expected bold, got non-bold.")

    @staticmethod
    def _check_instruction_llm(rule: PrintRule, field: Optional[dict],
                               precomputed: Optional[dict] = None) -> Optional[CheckResult]:
        """Phase D: LLM judges the printed value against the print instruction.

        Runs for matched, non-checkbox fields that have an instruction. Phase E:
        when the printed value is an unknown/placeholder AND the rule has an
        ``if_unknown`` rule, the unknown rule is also evaluated (text fields
        only — checkbox fields are handled by the alignment check instead).
        Fail-safe: no-ops (returns None) when the LLM is unavailable.

        When *precomputed* (the parallel pre-pass result for this rule) is
        supplied it is used directly instead of making a fresh LLM call.
        """
        if not field:
            return None
        if field.get("kind") == "checkbox_group":
            return None  # checkboxes are validated by alignment, not text rules
        instruction = clean_text(rule.instruction or "")
        if not instruction and not clean_text(rule.if_unknown or ""):
            return None

        value = field.get("value") or ""
        res = precomputed
        if res is None:
            unknown = is_sentinel(value)
            try:
                from modules.checkbox_llm import validate_instruction

                res = validate_instruction(
                    item=rule.item or "",
                    instruction=instruction,
                    printed_value=value,
                    example=rule.example or "",
                    max_chars=rule.max_chars or "",
                    char_size=rule.char_size or "",
                    bold=rule.bold or "",
                    font=rule.font or "",
                    is_unknown_value=unknown,
                    if_unknown=rule.if_unknown or "",
                )
            except Exception:
                res = None
        if not res:
            return None

        verdict = res.get("verdict")
        reason = res.get("reason") or ""
        # PASS/FAIL are used directly. For any other verdict (e.g. the LLM says
        # correctness can't be judged from the PDF alone), fall back to a
        # format check against the rule's ``example``: same format -> PASS,
        # different -> FAIL. We never judge the value's correctness, only that it
        # follows the example's shape.
        if verdict == "pass":
            status = Status.PASS
        elif verdict == "fail":
            status = Status.FAIL
        else:
            fmt_ok = _format_matches_example(value, rule.example or "")
            if fmt_ok is True:
                status = Status.PASS
                reason = (reason + " " if reason else "") + "Value format matches the example."
            elif fmt_ok is False:
                status = Status.FAIL
                reason = (reason + " " if reason else "") + "Value format does not match the example."
            else:
                # No example to compare against -> accept (nothing to validate).
                status = Status.PASS
                reason = (reason + " " if reason else "") + "No example format to compare; instruction followed."
        return CheckResult(
            name="instruction",
            status=status,
            expected=instruction or (rule.if_unknown or ""),
            actual=value or "(blank)",
            message=f"[LLM] {reason}" if reason else "[LLM] instruction check.",
        )

    def _precompute_instructions(
        self, field_by_rule: Dict[str, Optional[dict]]
    ) -> Dict[str, dict]:
        """Run Phase D/E instruction-validation LLM calls in parallel.

        Returns ``{rule.id: validate_instruction(...) result}``. Only rules that
        are matched, non-checkbox, and carry an instruction (or an ``if_unknown``
        rule) are dispatched. Fail-safe: any error yields no entry for that rule
        (the per-rule check then simply no-ops). No-ops entirely when the LLM is
        unavailable.
        """
        try:
            from modules.checkbox_llm import validate_instruction, _get_helper
        except Exception:
            return {}
        try:
            if not _get_helper().enabled:
                return {}
        except Exception:
            return {}

        jobs = []  # (rule, field, value, unknown)
        rule_by_id = {r.id: r for r in self.rules}
        for rid, field in field_by_rule.items():
            rule = rule_by_id.get(rid)
            if not rule or not field:
                continue
            if field.get("kind") == "checkbox_group":
                continue
            instruction = clean_text(rule.instruction or "")
            if not instruction and not clean_text(rule.if_unknown or ""):
                continue
            value = field.get("value") or ""
            jobs.append((rule, value, is_sentinel(value)))

        if not jobs:
            return {}

        def _one(job):
            rule, value, unknown = job
            try:
                return rule.id, validate_instruction(
                    item=rule.item or "",
                    instruction=clean_text(rule.instruction or ""),
                    printed_value=value,
                    example=rule.example or "",
                    max_chars=rule.max_chars or "",
                    char_size=rule.char_size or "",
                    bold=rule.bold or "",
                    font=rule.font or "",
                    is_unknown_value=unknown,
                    if_unknown=rule.if_unknown or "",
                )
            except Exception:
                return rule.id, None

        results: Dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for rid, res in pool.map(_one, jobs):
                if res:
                    results[rid] = res
        return results

    # ---- roll-up ----------------------------------------------------------
    @staticmethod
    def _rollup(checks: List[CheckResult], matched: bool) -> Status:
        # No matching DI field at all -> FAIL.
        if not matched:
            return Status.FAIL
        # Precedence: any real discrepancy (FAIL) wins; then blank/no-value
        # (MISSING_DATA); then placeholder value (UNKNOWN_DATA); else PASS.
        order = [Status.FAIL, Status.MISSING_DATA, Status.UNKNOWN_DATA, Status.PASS]
        present = {c.status for c in checks}
        for s in order:
            if s in present:
                return s
        return Status.PASS

    def run(self) -> ComparisonSummary:
        comparisons: List[FieldComparison] = []
        unmatched_rules: List[Dict[str, Any]] = []

        # Phase D/E: pre-compute instruction-validation LLM calls in parallel.
        # We first bind each rule to its DI field (consume-once, document order),
        # then fan out the LLM calls, then build comparison rows using the cached
        # results. ``instr_by_rule`` maps rule.id -> validate_instruction() dict.
        instr_by_rule: Dict[str, dict] = {}
        field_by_rule: Dict[str, Optional[dict]] = {}
        for rule in self.rules:
            if rule.rule_type == "NO_PRINT_RULE":
                field_by_rule[rule.id] = None
                continue
            field_by_rule[rule.id] = self.matcher.match(rule, consume=True)
        instr_by_rule = self._precompute_instructions(field_by_rule)

        # Boilerplate = page titles/headers (top-of-page or repeated across
        # pages) plus any DI field whose static text also appears verbatim in
        # the Excel rules (those are labels, not printable data). Such fields
        # are hidden from the UI entirely (never shown as unmatched).
        boilerplate_orders = _detect_boilerplate_orders(self.matcher.fields)
        rule_texts = {
            norm(t)
            for rule in self.rules
            for t in (rule.item, rule.label_printed, rule.section, rule.subsection)
            if t and len(norm(t)) >= 4
        }
        for f in self.matcher.fields:
            text = norm(f["value"] or f["key"] or "")
            if text and text in rule_texts:
                boilerplate_orders.add(f["order"])

        for i, rule in enumerate(self.rules, start=1):
            if rule.rule_type == "NO_PRINT_RULE":
                # Nothing to validate: surface as NOT_VALIDATED so the row is
                # still auditable but hidden from the reviewer's status filters.
                comparisons.append(
                    FieldComparison(
                        id=f"CMP-{i:04d}",
                        status=Status.NOT_VALIDATED,
                        sheet=self.sheet,
                        rule_id=rule.id,
                        rule_type=rule.rule_type,
                        section=rule.section,
                        subsection=rule.subsection,
                        item=rule.item,
                        matched=False,
                        checks=[
                            CheckResult(
                                name="no_print_rule",
                                status=Status.NOT_VALIDATED,
                                expected="no printing instructions",
                                actual="skipped",
                                message="No printing instructions present.",
                            )
                        ],
                        message="No printing instructions present.",
                    )
                )
                continue
            field = field_by_rule.get(rule.id)

            presence = self._check_presence(field)
            checks: List[CheckResult] = [presence]
            # When the field is present but blank (MISSING_DATA) or a placeholder
            # (UNKNOWN_DATA), those states are terminal for the row: skip the
            # other checks so the row always reports MISSING/UNKNOWN rather than
            # being overridden by a format/instruction verdict on a non-value.
            if presence.status in (Status.MISSING_DATA, Status.UNKNOWN_DATA):
                pass
            else:
                for maybe in (
                    self._check_checkbox_alignment(rule, field),
                    self._check_date(rule, field),
                    self._check_max_chars(rule, field),
                    self._check_bold(rule, field),
                    self._check_instruction_llm(rule, field, instr_by_rule.get(rule.id)),
                ):
                    if maybe is not None:
                        checks.append(maybe)

            status = self._rollup(checks, matched=field is not None)
            comparison = FieldComparison(
                id=f"CMP-{i:04d}",
                status=status,
                sheet=self.sheet,
                rule_id=rule.id,
                rule_type=rule.rule_type,
                section=rule.section,
                subsection=rule.subsection,
                item=rule.item,
                matched=field is not None,
                match_score=field["_match_score"] if field else 0.0,
                di_kind=field["kind"] if field else None,
                di_section=field["section"] if field else None,
                di_subsection=field["subsection"] if field else None,
                di_key=field["key"] if field else None,
                di_value=field["value"] if field else None,
                page=field["page"] if field else None,
                bbox=_field_bbox(field["raw"]) if field else None,
                confidence=field["confidence"] if field else None,
                checks=checks,
                message="; ".join(c.message for c in checks if c.message),
            )
            comparisons.append(comparison)
            if field is None:
                unmatched_rules.append(
                    {"rule_id": rule.id, "section": rule.section,
                     "subsection": rule.subsection, "item": rule.item,
                     "rule_type": rule.rule_type}
                )

        unmatched_fields = [
            {"kind": f["kind"], "section": f["section"], "subsection": f["subsection"],
             "key": f["key"], "value": f["value"], "page": f["page"]}
            for f in self.matcher.unused_fields()
            if f["order"] not in boilerplate_orders
        ]

        status_counts: Dict[str, int] = {}
        for c in comparisons:
            status_counts[c.status.value] = status_counts.get(c.status.value, 0) + 1

        # Phase C.2: final LLM verification of matched K-V pairs. Runs
        # unconditionally; flags irregularities only, never changes
        # values/states. No-ops safely when Azure OpenAI is not configured.
        llm_findings: List[Dict[str, Any]] = []
        try:
            from modules.checkbox_llm import verify_kv_pairs

            rule_by_id = {r.id: r for r in self.rules}
            records = []
            for c in comparisons:
                if not c.matched:
                    continue
                rule = rule_by_id.get(c.rule_id)
                records.append(
                    {
                        "rule_id": c.rule_id,
                        "section": c.section or "",
                        "subsection": c.subsection or "",
                        "item": c.item or "",
                        "example": (rule.example if rule else "") or "",
                        "di_value": c.di_value or "",
                    }
                )
            findings = verify_kv_pairs(records)
            if findings:
                llm_findings = findings
        except Exception:
            llm_findings = []

        return ComparisonSummary(
            sheet=self.sheet,
            total_rules=len(self.rules),
            total_fields=len(self.structured_fields),
            matched_count=sum(1 for c in comparisons if c.matched),
            comparisons=comparisons,
            unmatched_rules=unmatched_rules,
            unmatched_fields=unmatched_fields,
            status_counts=status_counts,
            llm_findings=llm_findings,
        )
