"""Map arbitrary client spreadsheet headers onto canonical rule roles.

25 clients x 5 modules means the header text will drift ("Item Name" vs
"Field Label" vs "Data Element"), but the *roles* are stable:

    item         the printed label to locate on the page
    instruction  the print rule -- the primary thing we validate against
    example      a sample printed value; for checkbox rows it also enumerates
                 the option labels, e.g. "[Yes, No]" / "[Divorce, Death, Annulment]"
    if_unknown   fallback rule, applied ONLY to text rows whose printed value is
                 a placeholder (UNKNOWN / UNNAMED / 99999 ...)
    max_chars / char_size / shrink_char_size / bold / font
                 formatting constraints the instruction check may consult

Resolution is three-tier so a new client normally needs no code change:
  1. exact match on a normalized alias,
  2. token-subset match (handles "Printing Instructions (PRINT)" etc.),
  3. fuzzy match above ``_FUZZY_FLOOR``.

Anything unresolved is returned in ``unmapped`` rather than silently dropped --
a column we failed to understand is a reviewable event, not a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from rapidfuzz import fuzz

from utils.text_utils import clean_text, norm


# Canonical role -> alias phrases seen (or plausibly seen) across clients.
# Order matters only for readability; matching is scored across all roles.
ROLE_ALIASES: Dict[str, tuple] = {
    "section": ("section", "form section", "block"),
    "subsection": ("sub section", "subsection", "sub-section", "group"),
    "item": (
        "item name and number", "item name number", "item name", "item",
        "field name", "field label", "label", "data element", "element name",
        "field", "printed label",
    ),
    # Some clients split the number into its own column; it is metadata, not the
    # label to match on, so it gets its own role and is never confused for `item`.
    "item_number": ("item number", "item no", "item #", "line number", "row number"),
    "instruction": (
        "printing instructions", "printing instruction", "print instructions",
        "print instruction", "print rule", "print rules", "instructions",
        "instruction", "printing rule", "printing rules", "rule",
    ),
    "example": (
        "example", "examples", "sample", "sample value", "example value",
        "sample output", "example output", "values",
    ),
    "if_unknown": (
        "if unknown print", "if unknown", "if = unknown", "if=unknown",
        "if value unknown", "unknown rule", "if unknown value", "when unknown",
    ),
    "if_missing": (
        "if missing blank print", "if missing print", "if missing", "if blank",
        "if = blank", "if=blank", "if value missing", "when blank",
        "missing rule",
    ),
    "max_chars": (
        "max chars", "max characters", "maximum characters", "char limit",
        "character limit", "max length",
    ),
    "shrink_char_size": (
        "if shrink to fit needed char size", "shrink to fit char size",
        "shrink char size", "shrink to fit",
    ),
    "char_size": ("char size", "character size", "font size", "point size"),
    "bold": ("bold", "is bold", "bold?", "font weight"),
    "font": ("font", "font name", "typeface", "font family"),
    "label_printed": (
        "label printed", "printed label text", "label text", "label on form",
    ),
}

# Roles a sheet must supply for validation to mean anything. ``example`` is not
# required (some modules omit it) but its absence weakens checkbox alignment,
# so it is reported separately by ``ResolvedSchema.warnings``.
REQUIRED_ROLES = ("item", "instruction")

_FUZZY_FLOOR = 82.0


# Client headers often carry an explanatory note appended to the real name, e.g.
#   "If = Unknown; Note: If this column is blank then do nothing special just
#    print what is in the field."
# The note text contains words belonging to OTHER roles ("blank" here), so it
# must be cut before matching or `if_unknown` and `if_missing` will swap. We
# keep only the text before the first note delimiter.
_NOTE_DELIMS = (";", " note:", " note ", "(note", " - note", "\n")


def _strip_note(header: str) -> str:
    """Return the header with any trailing explanatory note removed."""
    text = clean_text(header or "")
    low = text.lower()
    cut = len(text)
    for delim in _NOTE_DELIMS:
        idx = low.find(delim)
        if idx > 0:
            cut = min(cut, idx)
    return text[:cut].strip(" ,:-")


def _canon(header: str) -> str:
    """Normalize a header for comparison: casefold, strip punctuation/spacing."""
    s = norm(_strip_note(header))
    for ch in ("_", "-", "/", "\\", ".", ":", "(", ")", "[", "]", "=", "?"):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _tokens(text: str) -> frozenset:
    return frozenset(_canon(text).split())


@dataclass
class ResolvedSchema:
    """Outcome of resolving one sheet's headers."""

    # role -> the client's actual column header
    mapping: Dict[str, str] = field(default_factory=dict)
    # headers we could not confidently assign to any role
    unmapped: List[str] = field(default_factory=list)
    # required roles that no header satisfied
    missing_roles: List[str] = field(default_factory=list)
    # non-fatal notes (e.g. no example column -> weaker checkbox alignment)
    warnings: List[str] = field(default_factory=list)
    # role -> (header, score) for auditability of *why* a column was chosen
    evidence: Dict[str, tuple] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return not self.missing_roles

    def column_for(self, role: str) -> Optional[str]:
        return self.mapping.get(role)


def _score_header(header: str, aliases: tuple) -> float:
    """Best similarity between one header and one role's alias list."""
    h_canon = _canon(header)
    if not h_canon:
        return 0.0
    h_tokens = _tokens(header)
    best = 0.0
    for alias in aliases:
        a_canon = _canon(alias)
        if h_canon == a_canon:
            return 100.0
        a_tokens = _tokens(alias)
        # Token subset: "printing instructions print" contains "printing
        # instructions", so the client's decorated header still resolves.
        if a_tokens and a_tokens <= h_tokens:
            best = max(best, 95.0)
            continue
        best = max(best, float(fuzz.token_set_ratio(a_canon, h_canon)))
    return best


def resolve_schema(headers: List[str]) -> ResolvedSchema:
    """Assign each canonical role at most one of *headers*.

    Uses global best-first assignment rather than per-role greedy matching: a
    header is claimed by the role that scores highest for it overall, so
    "Char Size" cannot be stolen by ``shrink_char_size`` merely because that
    role is examined first.
    """
    result = ResolvedSchema()
    clean_headers = [h for h in (headers or []) if clean_text(h or "")]
    if not clean_headers:
        result.missing_roles = list(REQUIRED_ROLES)
        return result

    # Score every (role, header) pair, then assign greedily by descending score
    # with both sides consumed once.
    pairs = []
    for role, aliases in ROLE_ALIASES.items():
        for header in clean_headers:
            score = _score_header(header, aliases)
            if score >= _FUZZY_FLOOR:
                pairs.append((score, role, header))
    pairs.sort(key=lambda t: t[0], reverse=True)

    taken_roles: set = set()
    taken_headers: set = set()
    for score, role, header in pairs:
        if role in taken_roles or header in taken_headers:
            continue
        result.mapping[role] = header
        result.evidence[role] = (header, round(score, 2))
        taken_roles.add(role)
        taken_headers.add(header)

    result.unmapped = [h for h in clean_headers if h not in taken_headers]
    result.missing_roles = [r for r in REQUIRED_ROLES if r not in result.mapping]

    if "example" not in result.mapping:
        result.warnings.append(
            "No example column resolved: checkbox option lists and format "
            "comparison will be unavailable for this sheet."
        )
    if "if_unknown" not in result.mapping:
        result.warnings.append(
            "No 'if unknown' column resolved: placeholder values will be "
            "validated against the print rule only."
        )
    if result.unmapped:
        result.warnings.append(
            "Unrecognized columns (ignored): " + ", ".join(result.unmapped)
        )
    return result


def build_row_extractor(schema: ResolvedSchema):
    """Return ``fn(row_dict) -> {role: value}`` for a resolved schema.

    Keeps every downstream consumer (rule parser, comparison engine, LLM
    prompts) free of client-specific header strings.
    """
    mapping = dict(schema.mapping)

    def extract(row: Dict[str, object]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for role, header in mapping.items():
            out[role] = clean_text(row.get(header) or "")
        return out

    return extract
