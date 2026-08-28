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

    def score_candidates(self, rule: PrintRule) -> List[tuple]:
        """Score every DI field for *rule* without consuming anything.

        Returns ``[(score, field), ...]`` sorted best-first. Separated from
        ``match`` so the engine can assign globally: scoring all rules against
        all fields first, then resolving conflicts by descending score, rather
        than letting whichever rule is reached first take a field it only
        weakly deserves.

        That ordering bug is what let the page-title rule "WISCONSIN MARRIAGE
        LICENSE APPLICATION" claim LICENSE FEE / 50.00 at 77.78 (both contain
        "LICENSE"), forcing the real LICENSE FEE rule onto REISSUE LICENSE FEE
        and leaving the REISSUE rule with nothing -- one bad grab corrupting
        three rows.
        """
        labels = [clean_text(x) for x in (rule.item, rule.label_printed) if x]
        labels = [l for l in labels if len(l) >= 3]
        if not labels or not self.fields:
            return []
        rule_sec = norm(rule.section or "")
        rule_sub = norm(rule.subsection or "")
        rule_kind = _rule_expected_kind(rule)

        scored = []
        for it in self.fields:
            key_score = self._text_score(labels, it["key"])
            if it["kind"] == "composite":
                value_score = min(self._text_score(labels, it["value"]), VALUE_MATCH_CAP)
                key_score = max(key_score, value_score - 10.0)
            if key_score < self.threshold:
                continue
            score = key_score
            if not _kinds_compatible(rule_kind, it["kind"]):
                score += KIND_MISMATCH_PENALTY
            # Section agreement. The client confirms Section is always present
            # in the sheet, but DI leaves it null on many fields, so we only
            # apply the bonus/penalty when BOTH sides actually carry one.
            if rule_sec and it["section"]:
                agree = rule_sec in norm(it["section"]) or norm(it["section"]) in rule_sec
                score += SECTION_MATCH_BONUS if agree else SECTION_MISMATCH_PENALTY
            if rule_sub and it["subsection"]:
                agree = rule_sub in norm(it["subsection"]) or norm(it["subsection"]) in rule_sub
                score += SUBSECTION_MATCH_BONUS if agree else SUBSECTION_MISMATCH_PENALTY
            if is_sentinel(it["value"]):
                score -= 5.0
            scored.append((score, it))

        scored.sort(key=lambda t: t[0], reverse=True)
        return scored

    def assign_all(self, rules: List[PrintRule]) -> Dict[str, dict]:
        """Bind every rule to its best DI field via global best-first matching.

        Builds the full (rule, field, score) space, then walks it in descending
        score order, giving each field to the rule that wants it most. A rule
        whose top choice was taken falls through to its next-best candidate
        instead of being stranded, which is what consume-once-in-document-order
        could not do.

        Multi-part rules additionally absorb their sibling fields on the same
        printed line (see ``modules.field_grouping``).
        """
        from modules.field_grouping import collect_group, merge_group

        pairs = []
        for rule in rules:
            if rule.rule_type == "NO_PRINT_RULE":
                continue
            for score, field in self.score_candidates(rule):
                if score >= MIN_ACCEPT_SCORE:
                    pairs.append((score, rule, field))
        pairs.sort(key=lambda t: (-t[0], t[1].id))

        assigned: Dict[str, dict] = {}
        for score, rule, field in pairs:
            if rule.id in assigned or field["order"] in self._used:
                continue
            group = collect_group(field, rule.part_labels, self.fields, self._used)
            merged = merge_group(
                group, rule.part_labels, rule.part_separators,
                canonical_key=rule.item,
            )
            merged["_match_score"] = round(score, 2)
            assigned[rule.id] = merged
            for order in merged.get("_group_orders", [field["order"]]):
                self._used.add(order)
        return assigned

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
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
# "4 or Shrink to fit" in the char-size cell means shrinking is permitted even
# when no separate shrink-size column is populated.
_SHRINK_WORDS_RE = re.compile(r"shrink\s*to\s*fit|shrink", re.IGNORECASE)
# A month name or a numeric date -- evidence that an EXAMPLE really is a date.
_DATE_EXAMPLE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}\b",
    re.IGNORECASE,
)


def _looks_like_date(text: str) -> bool:
    """True when *text* contains real date evidence (month name or numerics)."""
    return bool(_DATE_EXAMPLE_RE.search(text or ""))



def _first_number(text: Optional[str]) -> Optional[float]:
    """First number in a spec cell, e.g. "10 pt" -> 10.0. None when absent."""
    m = _NUMBER_RE.search(clean_text(text or ""))
    return float(m.group(1)) if m else None


def _field_formatting(field: dict) -> dict:
    """Best-effort formatting dict for a DI field.

    DI puts formatting either directly on the structured_field or nested inside
    its ``value`` dict, so probe both. Returns ``{}`` when nothing is reported --
    callers must treat that as "unknown", never as a failure.
    """
    raw = field.get("raw") if isinstance(field.get("raw"), dict) else {}
    fmt = raw.get("formatting")
    if not isinstance(fmt, dict):
        value = raw.get("value")
        if isinstance(value, dict) and isinstance(value.get("formatting"), dict):
            fmt = value["formatting"]
    return fmt if isinstance(fmt, dict) else {}


def _reported_font_size(field: dict) -> Optional[float]:
    """Estimated printed font size in points, or None.

    IMPORTANT: Azure DI does not report a font size. ``azure_doc_intelligence``
    derives ``font_size_pt_estimate`` from the value's bounding-box HEIGHT
    (height_fraction * page_height * 72). That is the height of the text's box,
    including ascender/descender slack -- it is not the true point size and is
    routinely off by 1-3 pt.

    Callers must therefore treat this as ADVISORY only and never raise a hard
    FAIL from it alone; see ``_check_char_size``.
    """
    fmt = _field_formatting(field)
    val = fmt.get("font_size_pt_estimate")
    return _first_number(str(val)) if val is not None else None


def _reported_font_name(field: dict) -> str:
    """Font family from the STYLE_FONT add-on, or "" when not reported.

    Frequently ``None``: STYLE_FONT may be unavailable (see
    ``features_degraded``), in which case font can't be validated at all.
    """
    fmt = _field_formatting(field)
    for key in ("font_family", "font_style"):
        val = fmt.get(key)
        if val:
            return clean_text(str(val))
    return ""


# ---------------------------------------------------------------------------
# Checkbox option parsing/alignment helpers (Phase B).
# ---------------------------------------------------------------------------
_STATE_RE = re.compile(r":?\s*(?:un)?selected\s*:?", re.IGNORECASE)
# The post-processor serializes a checkbox group as
#   "Yes-unselected;No-selected"      (label-state, semicolon separated)
# while raw DI key_value_pairs use
#   "Divorce: :unselected:"           (label: :state:)
# Matching "<label><sep><state>" directly handles both, and -- critically --
# anchors the state to the END of each chunk so a label containing the word
# "selected" cannot be mistaken for the state marker.
_OPTION_RE = re.compile(
    r"^(?P<label>.*?)[\s:;\-\u2013\u2014]*:?\s*(?P<state>(?:un)?selected)\s*:?$",
    re.IGNORECASE,
)


def _parse_di_checkbox_options(value: str) -> List[dict]:
    """Parse a DI checkbox-group value into ``[{label, state}, ...]``.

    Handles both serializations in play:

        "Yes-unselected;No-selected"                    (structured_fields)
        "Divorce :unselected: Death :selected:"         (raw key_value_pairs)
        "Divorce:unselected, Death:selected"

    Splitting is done on the SEPARATORS BETWEEN options (``;`` or ``,``), or --
    when neither is present -- immediately after each state word. The previous
    implementation split on ``(?<=selected)[:\\-\\s]+``, which does not match the
    hyphen-joined ``label-state`` form the post-processor actually produces: the
    whole value collapsed into one bogus option, so every checkbox row aligned
    against garbage labels.

    State defaults to ``unselected`` when a label carries no explicit marker.
    """
    if not value:
        return []
    text = str(value).strip()
    if not text:
        return []

    if ";" in text:
        chunks = text.split(";")
    elif "," in text and _STATE_RE.search(text):
        chunks = text.split(",")
    else:
        # No explicit separator: break right after each state word.
        chunks = re.split(r"(?<=selected)\s*:?\s+", text, flags=re.IGNORECASE)

    options: List[dict] = []
    for chunk in chunks:
        c = clean_text(chunk)
        if not c:
            continue
        m = _OPTION_RE.match(c)
        if m:
            label = clean_text(m.group("label")).strip(" :,-\u2013\u2014")
            state = "unselected" if m.group("state").lower().startswith("un") else "selected"
        else:
            # No state marker at all -- treat the whole chunk as an unticked option.
            label = _STATE_RE.sub("", c).strip(" :,-\u2013\u2014")
            state = "unselected"
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

    def __init__(self, sheet: str, rules: List[PrintRule], structured_fields: List[dict],
                 key_value_pairs: Optional[List[dict]] = None):
        """*key_value_pairs* are the RAW DI pairs (``analysis.key_value_pairs``).

        They are optional so existing callers keep working, but without them the
        geometry-driven checkbox regrouping cannot run and we fall back to the DI
        group -- whose parent keys are demonstrably unreliable on this form.
        """
        self.sheet = sheet
        self.key_value_pairs = key_value_pairs or []
        # Attachment E merges the Section / Sub-Section cells, so all but the
        # first row of each block arrives blank. Forward-fill before matching:
        # without it the section/subsection bonuses never fire and the ~20 item
        # names duplicated between LICENSE - PARTY A and LICENSE - PARTY B score
        # identically, letting consume-once bind one party's values to the other
        # party's rules. Fail-safe: on any error we keep the rules as parsed.
        try:
            from modules.rule_normalizer import normalize_rules

            rules = normalize_rules(list(rules or []))
        except Exception:
            pass
        self.rules = rules
        self.structured_fields = structured_fields or []
        self.matcher = RuleFieldMatcher(self.structured_fields)

    # ---- individual checks ------------------------------------------------
    @staticmethod
    def _check_presence(field: Optional[dict]) -> CheckResult:
        if field is None:
            # Per the client's universal rule: if a rule exists in the sheet,
            # the field EXISTS in the PDF. So "no matching field" is never a
            # property of the document -- it is our matcher failing to locate
            # it. Reporting FAIL would blame the PDF for our own miss, so this
            # is MISSING_DATA (no value found) with the cause made explicit.
            return CheckResult(name="presence", status=Status.MISSING_DATA,
                               expected="printed value",
                               actual="(field not located)",
                               message=("Could not confidently locate this field "
                                        "in the PDF; no value was read."))
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
        """Hard check: printed value matches the rule's date pattern.

        Guarded by the rule's own EXAMPLE. ``infer_date_pattern`` also reads the
        instruction, and multi-part FORMAT clauses share a vocabulary with date
        formats ("<First> + <Middle> + <Last> + comma <Title>" contains the same
        "+ comma + space" wording as "<month> + space <dd> + comma"), so it fired
        on ISSUING OFFICIAL and OFFICIANT MAILING ADDRESS and hard-FAILed a name
        and an address for not being dates.

        The example is the reliable discriminator: a genuine date row shows
        "NOVEMBER 30, 1999", a name row shows "JOHN ALLEN JOHANSSEN".
        """
        pattern = infer_date_pattern(rule.instruction, rule.example, rule.item)
        if not pattern:
            return None
        example = clean_text(rule.example or "")
        if example and not _looks_like_date(example):
            return None
        actual = field["value"] if field else ""
        ok = validate_date(actual, pattern)
        return CheckResult(name="date_pattern",
                           status=Status.PASS if ok else Status.FAIL,
                           expected=pattern, actual=actual or "(none)",
                           message="Date matches pattern." if ok else "Date missing or pattern mismatch.")

    def _check_checkbox_positional(self, rule: PrintRule, field: dict,
                                   label_bbox) -> Optional[CheckResult]:
        """Assess a checkbox rule that declares no expected options.

        Answers only "which boxes are printed on this rule's label row, and is
        any of them ticked?" -- no name alignment, because there is no option
        list to align against. Two outcomes matter:

          * The boxes found on the row do not match the ones the DI group
            attributed to this field -> the group is mis-attributed. Reported as
            NOT_VALIDATED with both lists shown, so the reviewer can see the
            discrepancy. Deliberately NOT a FAIL: a DI extraction fault is not a
            defect in the printed document.
          * No boxes on the row at all -> we cannot say anything; return None and
            leave the row as it was.

        Without this, such rules received no checkbox check whatsoever and passed
        on presence alone (CMP-0021 / CMP-0040).
        """
        if not self.key_value_pairs or len(label_bbox) != 4:
            return None
        try:
            from modules.checkbox_geometry import boxes_on_label_row

            found = boxes_on_label_row(
                label_bbox=label_bbox,
                page=field.get("page", 1),
                checkbox_kvs=self.key_value_pairs,
            )
        except Exception:
            return None
        if not found:
            di_options = _parse_di_checkbox_options(field.get("value") or "")
            if di_options:
                # The DI group claims checkboxes, yet none are printed on this
                # field's own row. Observed on BIRTHPLACE - U.S. State/Territory
                # (CMP-0021 label y 0.2543-0.2631, CMP-0040 y 0.4793-0.4883)
                # whose attributed Parent/Mother/Father boxes sit at cy 0.3020
                # and 0.5242 -- a different line entirely. Reporting this stops
                # the row passing on "Field present." alone.
                return CheckResult(
                    name="checkbox_position",
                    status=Status.NOT_VALIDATED,
                    expected="checkboxes on this field's row",
                    actual=", ".join(sorted(o["label"] for o in di_options)),
                    message=(
                        "No checkboxes are printed on this field's row, yet "
                        "Document Intelligence attributed ["
                        + ", ".join(sorted(o["label"] for o in di_options))
                        + "] to it. The group belongs to a different field; "
                        "this row could not be validated."
                    ),
                )
            return None

        di_options = _parse_di_checkbox_options(field.get("value") or "")
        di_labels = {norm(o["label"]) for o in di_options}
        row_labels = {norm(o["label"]) for o in found}

        selected = [o["label"] for o in found if o["state"] == "selected"]
        printed = ", ".join(f"{o['label']} ({o['state']})" for o in found)

        if di_labels and row_labels and di_labels != row_labels:
            return CheckResult(
                name="checkbox_position",
                status=Status.NOT_VALIDATED,
                expected="checkboxes belonging to this field",
                actual=printed,
                message=(
                    "Checkbox group looks mis-attributed: the boxes printed on "
                    "this field's row are [" + printed + "] but Document "
                    "Intelligence assigned this field ["
                    + ", ".join(sorted(o["label"] for o in di_options))
                    + "]. Verify visually; the extraction, not the PDF, is the "
                    "likely fault."
                ),
            )

        if selected:
            return CheckResult(
                name="checkbox_position",
                status=Status.PASS,
                expected="at least one option selected",
                actual=printed,
                message="Selected: " + ", ".join(selected) + ".",
            )
        return CheckResult(
            name="checkbox_position",
            status=Status.MISSING_DATA,
            expected="at least one option selected",
            actual=printed,
            message="No checkboxes are selected on this field's row.",
        )

    def _check_checkbox_alignment(self, rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
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
        raw = field.get("raw") if isinstance(field.get("raw"), dict) else {}
        label_bbox = raw.get("label_bbox") or raw.get("bbox") or []
        if rule.expected_kind != "checkbox_group" or not rule.expected_options:
            # No declared option list, so options cannot be aligned by name.
            # Previously this returned None -- the row then carried NO checkbox
            # check at all and rolled up to PASS on "Field present." alone, which
            # is how BIRTHPLACE - U.S. State/Territory (CMP-0021 / CMP-0040)
            # reported PASS while showing Parent/Mother/Father boxes belonging to
            # a different field. Fall back to the purely positional check so the
            # row is still assessed against what is actually printed on its line.
            return self._check_checkbox_positional(rule, field, label_bbox)

        # Prefer options rebuilt from raw DI geometry over the DI group's own
        # membership. The group KEY on this form is routinely mis-attributed
        # (PROOF OF STERILITY carrying Groom/Bride/Spouse, LICENSE FEE carrying
        # the ten issuance-method boxes), so its member list cannot be trusted
        # either. The individual boxes are read correctly, so we re-derive the
        # group from the rule's own option names plus proximity to the rule's
        # matched label box. Falls back to the DI group whenever geometry finds
        # nothing -- never worse than before.
        regrouped = None
        if self.key_value_pairs and len(label_bbox) == 4:
            try:
                from modules.checkbox_geometry import regroup

                regrouped = regroup(
                    rule_options=rule.expected_options,
                    label_bbox=label_bbox,
                    page=field.get("page", 1),
                    checkbox_kvs=self.key_value_pairs,
                )
            except Exception:
                regrouped = None

        if regrouped:
            di_options = [
                {"label": o["label"], "state": o["state"]}
                for o in regrouped["options"]
            ]
            source_note = (
                f"(re-grouped by geometry: {regrouped['matched']}/"
                f"{regrouped['expected']} options located)"
            )
        else:
            di_options = _parse_di_checkbox_options(field["value"])
            source_note = ""
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
        if source_note:
            parts.append(source_note)
        if used_llm:
            parts.append("(LLM-aligned)")

        # Status precedence (per spec):
        #   * NO option marked                    -> MISSING_DATA (no data given)
        #   * at least one option marked          -> PASS
        #
        # An expected option we could not locate among the DI members is NOT a
        # PDF defect: the client's position is that DI mis-GROUPS checkboxes
        # (right boxes, wrong parent), so the option is on the page under
        # another group. Reporting that as FAIL blames the document for an
        # extraction fault, so unmatched options are surfaced in the message
        # only and never change the verdict on their own.
        if selected_labels:
            status = Status.PASS
        else:
            status = Status.MISSING_DATA
        return CheckResult(
            name="checkbox_alignment",
            status=status,
            expected=", ".join(rule.expected_options),
            actual="; ".join(aligned) if aligned else "(no options aligned)",
            message=" ".join(parts),
        )

    # Option labels that demand an accompanying free-text entry when selected.
    _SPECIFY_HINTS = ("specify", "other, specify", "please specify", "if other")

    @classmethod
    def _check_specify_text(cls, rule: PrintRule, field: Optional[dict],
                            selected_labels: List[str],
                            all_fields: List[dict]) -> Optional[CheckResult]:
        """Hard check: a selected "Other ... Specify" box must carry its text.

        Per spec the free text is APPENDED INTO the checkbox group's own value
        rather than living in a separate labelled field (confirmed by the sample
        form: "Yes, other Spanish/Hispanic/Latino(a) (Specify) NIVI"). So we look
        first inside this field's value for text that is neither an option label
        nor a selection state, then fall back to a same-page text field whose key
        mentions "specify".

        A tick with no text is a genuine defect -> FAIL.
        """
        if not field or not selected_labels:
            return None
        specify_opts = [
            o for o in selected_labels
            if any(h in norm(o) for h in cls._SPECIFY_HINTS)
        ]
        if not specify_opts:
            return None

        # 1) Look inside the group's own serialized value for trailing free text.
        raw_value = str(field.get("value") or "")
        option_labels = {norm(d["label"]) for d in _parse_di_checkbox_options(raw_value)}
        leftovers = []
        for chunk in re.split(r"[;,]", _STATE_RE.sub(" ", raw_value)):
            token = clean_text(chunk)
            if not token:
                continue
            if norm(token) in option_labels:
                continue
            if norm(token) in ("selected", "unselected", "specify"):
                continue
            leftovers.append(token)
        if leftovers:
            return CheckResult(
                name="specify_text",
                status=Status.PASS,
                expected=f"free text for {', '.join(specify_opts)}",
                actual="; ".join(leftovers),
                message="'Specify' option is selected and its text is printed.",
            )

        # 2) Fall back to a same-page text field explicitly keyed "specify".
        page = field.get("page")
        for other in all_fields:
            if other.get("page") != page or other.get("kind") == "checkbox_group":
                continue
            if "specify" not in norm(other.get("key") or ""):
                continue
            if clean_text(other.get("value") or ""):
                return CheckResult(
                    name="specify_text",
                    status=Status.PASS,
                    expected=f"free text for {', '.join(specify_opts)}",
                    actual=other.get("value") or "",
                    message="'Specify' option is selected and its text is printed.",
                )

        return CheckResult(
            name="specify_text",
            status=Status.FAIL,
            expected=f"free text for {', '.join(specify_opts)}",
            actual="(blank)",
            message=(
                "A 'Specify' option is selected but no accompanying text was "
                "printed."
            ),
        )

    @staticmethod
    def _check_char_size(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        """Advisory check: printed character size vs. ``char_size``.

        Per spec, shrink-to-fit is permitted only when the rule says so -- either
        a populated shrink column, or wording like "4 or Shrink to fit" in the
        char-size cell itself. Otherwise the size must match exactly.

        Reported as NOT_VALIDATED rather than FAIL on mismatch, because the only
        size DI gives us is a bounding-box-height ESTIMATE (see
        ``_reported_font_size``), which is not accurate enough to condemn a
        document. The tester sees the numbers and the discrepancy, and decides.
        """
        if not field:
            return None
        want = _first_number(rule.char_size)
        if want is None:
            return None
        actual = _reported_font_size(field)
        if actual is None:
            return None

        shrink_allowed = bool(_SHRINK_WORDS_RE.search(clean_text(rule.char_size or "")))
        floor = _first_number(rule.shrink_size)
        if floor is not None or shrink_allowed:
            low = floor if floor is not None else 0.0
            ok = low - 1.5 <= actual <= want + 1.5
            expected = (
                f"{low}-{want} pt (shrink-to-fit allowed)" if floor is not None
                else f"<= {want} pt (shrink-to-fit allowed)"
            )
        else:
            ok = abs(actual - want) <= 1.5
            expected = f"{want} pt (exact; shrink not permitted)"

        return CheckResult(
            name="char_size",
            status=Status.PASS if ok else Status.NOT_VALIDATED,
            expected=expected,
            actual=f"~{actual} pt (estimated from text height)",
            message=(
                "Character size is consistent with the rule."
                if ok
                else (
                    "Estimated character size looks outside the allowed range. "
                    "DI does not report true font size -- this figure is derived "
                    "from the text's height, so please confirm visually."
                )
            ),
        )

    @staticmethod
    def _check_font(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        """Hard check: printed font family vs. the rule's ``font`` cell."""
        want = clean_text(rule.font or "")
        if not want or not field:
            return None
        actual = _reported_font_name(field)
        if not actual:
            return None
        # Compare loosely: DI returns names like "Arial-BoldMT" for "Arial".
        ok = norm(want) in norm(actual) or norm(actual) in norm(want)
        return CheckResult(
            name="font",
            status=Status.PASS if ok else Status.FAIL,
            expected=want,
            actual=actual,
            message="Font matches." if ok else "Printed font differs from the rule.",
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
        if_unknown = clean_text(rule.if_unknown or "")
        if not instruction and not if_unknown:
            return None

        value = field.get("value") or ""
        unknown = is_sentinel(value)
        # The 'if unknown' column is a SECOND stage that only ever applies to a
        # placeholder value on a text row. A BLANK cell in that column is
        # meaningful and explicitly documented by the clients as: "do nothing
        # special, just print what is in the field" -- i.e. fall back to the
        # print instruction alone. So we pass it through only when the value is
        # actually a placeholder AND the cell is non-blank; otherwise the
        # instruction verdict stands by itself.
        effective_if_unknown = if_unknown if (unknown and if_unknown) else ""
        res = precomputed
        if res is None:
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
                    if_unknown=effective_if_unknown,
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
            # Mirror ``_check_instruction_llm`` exactly: the 'if unknown' rule is
            # forwarded ONLY for a placeholder value on a text row with a
            # non-blank cell. A blank cell means "just follow the instruction".
            # If this diverged from the per-row path, a rule's verdict would
            # depend on whether the pre-pass cache hit -- a reproducibility bug.
            if_unknown = clean_text(rule.if_unknown or "")
            effective_if_unknown = if_unknown if (unknown and if_unknown) else ""
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
                    if_unknown=effective_if_unknown,
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
    def _check_part_gaps(rule: PrintRule, field: Optional[dict]) -> Optional[CheckResult]:
        """Report a blank sub-field sitting between two populated ones.

        For "First + Middle + Last + Suffix", a blank Suffix is normal -- the
        value simply ends. A blank MIDDLE with a populated Last is a gap: the
        print job skipped a value it should have supplied.
        """
        if not field:
            return None
        from modules.field_grouping import find_part_gaps

        gaps = find_part_gaps(field)
        if not gaps:
            return None
        return CheckResult(
            name="part_gaps",
            status=Status.FAIL,
            expected="every sub-field before the last populated one to be filled",
            actual="blank: " + ", ".join(gaps),
            message=(
                "Gap in a multi-part field: " + ", ".join(gaps) +
                " is blank but a later part is printed."
            ),
        )

    @staticmethod
    def _rollup(checks: List[CheckResult], matched: bool) -> Status:
        # NOTE: deliberately no "if not matched -> FAIL" short-circuit. Per the
        # client's rule that every sheet row corresponds to a field that EXISTS
        # in the PDF, a failure to locate one is OUR matching gap, not a defect
        # in the document -- ``_check_presence`` already reports that honestly as
        # MISSING_DATA. Overriding it here re-blamed the PDF and put 17 rows in
        # FAIL whose only finding was "could not locate".
        #
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
        # Global best-first assignment: score every rule against every field,
        # then resolve conflicts by descending score. Replaces the previous
        # document-order loop, where an early rule could consume a field a later
        # rule deserved far more.
        assigned = self.matcher.assign_all(self.rules)
        for rule in self.rules:
            if rule.rule_type == "NO_PRINT_RULE":
                field_by_rule[rule.id] = None
                continue
            field_by_rule[rule.id] = assigned.get(rule.id)
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
            # Only a field's *static text* can be an Excel label echoed onto the
            # page, and static text means the field has no value of its own.
            # Deliberately do NOT fall back to ``key`` here: a located-but-blank
            # field whose KEY matches an Excel item is the "empty box that should
            # have been filled" case, and suppressing it hides a real finding.
            text = norm(f["value"] or "")
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
            # A blank field (MISSING_DATA) has no printed value, so there is
            # nothing to validate a print rule against: presence is the finding.
            #
            # A placeholder (UNKNOWN / UNNAMED / 99999) is DIFFERENT -- it IS a
            # printed value and must still be validated against the print rule,
            # which is the primary contract. The ``if_unknown`` column is then a
            # SECOND stage layered on top, and applies to text rows only (never
            # checkbox rows). When the ``if_unknown`` cell is blank there is
            # nothing special to do and the instruction verdict stands alone.
            #
            # Previously UNKNOWN_DATA short-circuited alongside MISSING_DATA, so
            # every placeholder row skipped instruction validation entirely --
            # 18 rows in the last run reported UNKNOWN_DATA without their print
            # rule ever being checked.
            if presence.status is Status.MISSING_DATA:
                pass
            else:
                for maybe in (
                    self._check_checkbox_alignment(rule, field),
                    self._check_part_gaps(rule, field),
                    self._check_date(rule, field),
                    self._check_max_chars(rule, field),
                    self._check_char_size(rule, field),
                    self._check_font(rule, field),
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
                metadata=(
                    {
                        "candidate_reconstruction_source": field.get("_candidate_reconstruction_source"),
                        "observed_anchor_key": field.get("_observed_anchor_key"),
                        "composite_expected_parts": field.get("_composite_expected_parts", []),
                        "composite_detected_parts": field.get("_composite_detected_parts", []),
                        "composite_complete": field.get("_composite_complete"),
                        "expected_subsection": rule.subsection,
                        "observed_subsection": field.get("subsection"),
                        "expected_options": list(rule.expected_options or []),
                        "if_unknown_rule": rule.if_unknown,
                        "instruction_validation_mode": (
                            "FORMAT_EXAMPLE_ONLY" if "DROPDOWN VALUE SELECTED" in norm(rule.instruction or "")
                            and not (rule.expected_options or []) else "STANDARD"
                        ),
                        "checkbox_payload_valid": (
                            (field.get("raw") or {}).get("checkbox_payload_valid")
                            if isinstance(field.get("raw"), dict) else None
                        ),
                        "subsection_spec_conflict": bool(
                            rule.subsection and field.get("subsection")
                            and norm(rule.subsection) != norm(field.get("subsection"))
                        ),
                    } if field else {}
                ),
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

        # ---- Full-coverage pass: every printed value reaches the output ------
        # The loop above emits one row per RULE, so a DI field that no rule
        # claimed would only survive as a bare dict in ``unmatched_fields`` --
        # invisible in the comparison table/overlay. That is exactly the
        # "missed value" failure mode. Here we emit a real FieldComparison for
        # each leftover (non-boilerplate) field so the reviewer sees 100% of the
        # extracted values, geometry included.
        #
        # These rows are EXTRA PRINT OUTPUT: the PDF prints something the Excel
        # spec has no rule for. There is no rule to validate against, so the
        # status is NOT_VALIDATED (auditable, but never counted as a pass/fail
        # against a rule that doesn't exist). Blank/placeholder values are still
        # distinguished so a stray empty box is visible as such.
        next_index = len(comparisons) + 1
        for f in self.matcher.unused_fields():
            if f["order"] in boilerplate_orders:
                continue
            value = f["value"] or ""
            if not clean_text(value):
                note = "Extra printed field located but no value is printed."
            elif is_sentinel(value):
                note = "Extra printed field with a placeholder (unknown) value."
            else:
                note = "Value printed in the PDF with no matching Excel print rule."
            comparisons.append(
                FieldComparison(
                    id=f"CMP-{next_index:04d}",
                    status=Status.NOT_VALIDATED,
                    sheet=self.sheet,
                    rule_id="",
                    rule_type="UNMATCHED_FIELD",
                    section=None,
                    subsection=None,
                    item=f["key"] or value,
                    matched=False,
                    match_score=0.0,
                    di_kind=f["kind"],
                    di_section=f["section"],
                    di_subsection=f["subsection"],
                    di_key=f["key"],
                    di_value=value,
                    page=f["page"],
                    bbox=_field_bbox(f["raw"]),
                    confidence=f["confidence"],
                    checks=[
                        CheckResult(
                            name="unmatched_field",
                            status=Status.NOT_VALIDATED,
                            expected="a matching Excel print rule",
                            actual=value or "(blank)",
                            message=note,
                        )
                    ],
                    message=note,
                )
            )
            next_index += 1

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
