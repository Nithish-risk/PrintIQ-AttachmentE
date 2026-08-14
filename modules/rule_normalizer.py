"""Normalize parsed Attachment E rules before matching.

Attachment E uses **merged cells** for ``Section`` and ``Sub-Section``: only the
first row of each block carries the text and every subsequent row is blank. Most
readers surface that as an empty string, so without this pass the majority of
rules reach ``RuleFieldMatcher`` with no section/subsection at all.

That silently disables the strongest disambiguation signal we have. The Wisconsin
marriage sheet repeats ~20 item names verbatim between ``LICENSE - PARTY A`` and
``LICENSE - PARTY B`` (CURRENT NAME, DATE OF BIRTH, ZIP CODE, COUNTY, PHONE
NUMBER, EMAIL, both PARENT birth names, ...). With the subsection blank the
matcher scores Party A's and Party B's rules identically and consume-once
arbitrarily assigns whichever DI field it reaches first -- so one party's values
are reported against the other party's rules.

This module is deliberately conservative:
  * forward-fill only ever *fills a blank*; a populated cell is never overwritten;
  * a new ``Section`` resets the ``Sub-Section`` carry, so ``LICENSE - PARTY B``
    cannot inherit ``OTHER`` left over from Party A;
  * rows that carry no item text and no instruction are marked NO_PRINT_RULE
    rather than dropped, so the audit trail stays complete.
"""

from __future__ import annotations

from typing import List

from models.rule_models import PrintRule
from utils.text_utils import clean_text

# Sentinel text used in the sheet to mean "nothing to print here".
_NO_RULE_MARKERS = (
    "NO PRINT RULES IN THIS SECTION",
)


def _blank(value) -> bool:
    return not clean_text(value or "")


def _is_no_print_marker(text: str) -> bool:
    up = clean_text(text or "").upper()
    return any(marker in up for marker in _NO_RULE_MARKERS)


def forward_fill_sections(rules: List[PrintRule]) -> List[PrintRule]:
    """Propagate merged-cell ``section``/``subsection`` down the rule list.

    Mirrors how a human reads the sheet: a section/sub-section heading applies to
    every row beneath it until the next heading. Returns the same list (mutated
    in place) so callers can drop this in without changing plumbing.
    """
    current_section = ""
    current_subsection = ""

    for rule in rules:
        section = clean_text(getattr(rule, "section", "") or "")
        subsection = clean_text(getattr(rule, "subsection", "") or "")

        # A new section invalidates the previous section's sub-section carry.
        if section:
            if section != current_section:
                current_subsection = ""
            current_section = section
        elif current_section:
            rule.section = current_section

        # ``No PRINT Rules in this Section`` sits in the Sub-Section column and
        # is prose, not a real sub-section -- don't carry it into later rows.
        if subsection and not _is_no_print_marker(subsection):
            current_subsection = subsection
        elif not subsection and current_subsection:
            rule.subsection = current_subsection

    return rules


def strip_leaked_markers(rules: List[PrintRule]) -> List[PrintRule]:
    """Remove the ``No PRINT Rules in this Section`` prose from ``subsection``.

    The upstream Excel parser forward-fills merged cells itself, so this marker
    -- which belongs only to the ``AFFIRMATION`` row -- leaks down into every
    later row, including ``PARTY A - ATTRIBUTES`` and ``PARTY B - ATTRIBUTES``.

    It must NEVER be treated as evidence that a rule has no print rule: those
    ATTRIBUTES rows carry real instructions (``PRINT <Party A Race> FORMAT -
    place X in boxes that matches boxes selected``). Killing them also strands
    their DI checkbox groups (HISPANIC ORIGIN / RACE / EDUCATION on page 2) as
    unvalidated extra fields. Here we only blank the bogus label.
    """
    for rule in rules:
        if _is_no_print_marker(getattr(rule, "subsection", "")):
            rule.subsection = ""
    return rules


def mark_empty_rules(rules: List[PrintRule]) -> List[PrintRule]:
    """Flag rows that describe no locatable, checkable field as ``NO_PRINT_RULE``.

    Two genuinely unvalidatable shapes exist in Attachment E:

    * **No item text.** ``RuleFieldMatcher.match`` needs ``item`` /
      ``label_printed`` to score against; with neither there is nothing to find.
      The page-header rows (``WISCONSIN MARRIAGE LICENSE APPLICATION``) are like
      this -- the title sits in the *instructions* column with no item. Left
      active, such a rule fuzzy-matches an arbitrary field: in the last run it
      bound to ``LICENSE FEE`` (``50.00``) at 77.78 and consumed it, which then
      pushed the real LICENSE FEE rule onto ``REISSUE LICENSE FEE`` and left the
      REISSUE rule with nothing. One bad bind corrupted three rows.

    * **Item but no instruction/example.** e.g. the ``This form must be signed in
      the presence of the County Clerk`` notice: printed static text with no
      rule to check it against. It previously consumed the ``COUNTY`` field.

    Marking is conservative -- anything with an instruction stays validated.
    """
    for rule in rules:
        if getattr(rule, "rule_type", "") == "NO_PRINT_RULE":
            continue
        has_item = not _blank(getattr(rule, "item", "")) or not _blank(
            getattr(rule, "label_printed", "")
        )
        has_rule = not _blank(getattr(rule, "instruction", "")) or not _blank(
            getattr(rule, "example", "")
        )
        if not has_item or not has_rule:
            rule.rule_type = "NO_PRINT_RULE"

    return rules


def normalize_rules(rules: List[PrintRule]) -> List[PrintRule]:
    """Run every normalization pass. Safe to call more than once."""
    if not rules:
        return rules
    return mark_empty_rules(strip_leaked_markers(forward_fill_sections(rules)))
