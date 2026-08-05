"""
Centralized key-value matcher for printiq V1.2.

In V1.2 the Azure engine emits *only* key_value_pair elements. Many KV keys
repeat across a form ("No", "Yes", "Middle", "Last", "Suffix", "Parent",
"SOCIAL SECURITY NUMBER", Party A vs Party B ...), so a naive first/shortest
match anchors rules to the wrong instance.

``KVMatcher`` replaces the old greedy ``_locate`` / ``_find_kv`` logic with a
single multi-signal scorer:

* fuzzy key similarity (primary signal),
* party scope (Party A vs Party B) using occurrence order within a key group,
* value plausibility by rule type (dates look like dates, checkboxes look like
  selection marks),
* a small sentinel penalty (placeholder values such as UNKNOWN/UNNAMED/99999),
* consume-once: once a KV is bound to a rule it is preferentially skipped for
  later rules (rules are processed in document order).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from utils.text_utils import clean_text

# Placeholder / test values that should not be treated as genuine printed data.
_SENTINEL_TOKENS = {"UNKNOWN", "UNNAMED", "NONE"}
_SENTINEL_EXACT = {"", "N/A", "NA", "NONE", "99999"}
_MONTHS = (
    "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    "SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"
)


def is_sentinel(value: str) -> bool:
    """True when *value* is a placeholder rather than real source data."""
    v = clean_text(value)
    if not v:
        return True
    if all(ch in ("\u0018",) for ch in v):
        return True
    up = v.upper().strip(": ")
    if up in _SENTINEL_EXACT:
        return True
    tokens = [t for t in re.split(r"[\s,]+", up) if t]
    if tokens and all(
        (t in _SENTINEL_TOKENS) or set(t) == {"9"} for t in tokens
    ):
        return True
    return False


def detect_party(*texts: str) -> Optional[str]:
    """Infer whether a rule scopes to Party A or Party B from its text."""
    blob = " ".join(clean_text(t) for t in texts if t).upper()
    if not blob:
        return None
    if "PARTY B" in blob or "BRIDE" in blob:
        return "B"
    if "PARTY A" in blob or "GROOM" in blob:
        return "A"
    return None


class KVMatcher:
    def __init__(self, analysis, fuzzy_threshold: int = 78):
        self.threshold = fuzzy_threshold
        self.items: List[Dict[str, Any]] = []

        kvs = getattr(analysis, "key_value_pairs", None) or []
        for order, kv in enumerate(kvs):
            key = clean_text(kv.get("key", ""))
            self.items.append(
                {
                    "order": order,
                    "key": key,
                    "norm_key": key.upper(),
                    "value": clean_text(kv.get("value", "")),
                    "page": kv.get("page", 1),
                    "bbox": kv.get("bbox") or [],
                    "value_bbox": kv.get("value_bbox") or kv.get("bbox") or [],
                    "confidence": kv.get("confidence"),
                    "formatting": kv.get("formatting") or {},
                }
            )

        # Occurrence index within each normalized key group, used for the
        # Party A / Party B disambiguation heuristic.
        groups: Dict[str, List[int]] = {}
        for it in self.items:
            groups.setdefault(it["norm_key"], []).append(it["order"])
        for it in self.items:
            grp = groups[it["norm_key"]]
            it["group_size"] = len(grp)
            it["group_index"] = grp.index(it["order"])

        self._used: set[int] = set()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Clear the consumed-KV set (call before re-running validation)."""
        self._used.clear()

    def _key_score(self, labels: List[str], it: Dict[str, Any]) -> float:
        key = it["norm_key"]
        if not key:
            return 0.0
        best = 0.0
        for lab in labels:
            l = (lab or "").upper()
            if not l:
                continue
            if l in key or key in l:
                best = max(best, 100.0)
            else:
                best = max(best, float(fuzz.token_set_ratio(l, key)))
        return best

    def _party_bonus(self, it: Dict[str, Any], party: Optional[str]) -> float:
        if not party or it["group_size"] < 2:
            return 0.0
        half = it["group_size"] / 2.0
        first_half = it["group_index"] < half
        if party == "A":
            return 10.0 if first_half else -10.0
        if party == "B":
            return -10.0 if first_half else 10.0
        return 0.0

    @staticmethod
    def _plausibility_bonus(it: Dict[str, Any], rule_type: Optional[str]) -> float:
        if not rule_type:
            return 0.0
        v = it["value"]
        vl = v.lower()
        vu = v.upper()
        if rule_type == "CHECKBOX":
            return 15.0 if ("selected" in vl) else -6.0
        if rule_type == "DATE_FORMAT":
            has_date = bool(re.search(r"\d{4}", vu) or re.search(r"\d{1,2}/\d{1,2}", vu)
                            or re.search(_MONTHS, vu))
            return 15.0 if has_date else -4.0
        return 0.0

    def match(
        self,
        labels: List[str],
        party: Optional[str] = None,
        rule_type: Optional[str] = None,
        consume: bool = True,
        avoid_sentinel: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Return the best-scoring KV item for *labels*, or None.

        When ``consume`` is True the chosen KV is marked used so later rules
        prefer a different instance (resolves duplicate keys in document order).
        """
        labels = [clean_text(l) for l in labels if l and len(clean_text(l)) >= 3]
        if not labels or not self.items:
            return None

        scored = []
        for it in self.items:
            ks = self._key_score(labels, it)
            if ks < self.threshold:
                continue
            score = ks
            score += self._party_bonus(it, party)
            score += self._plausibility_bonus(it, rule_type)
            if it["order"] in self._used:
                score -= 40.0
            if avoid_sentinel and is_sentinel(it["value"]):
                score -= 6.0
            # Prefer tighter (shorter) key labels on ties.
            score -= 0.01 * len(it["norm_key"])
            scored.append((score, it))

        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best = scored[0][1]
        if consume:
            self._used.add(best["order"])
        return best
