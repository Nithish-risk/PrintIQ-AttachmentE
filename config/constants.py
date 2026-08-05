from enum import Enum

class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    # Field located on the PDF but its value is a placeholder/sentinel
    # (e.g. "Unknown", "N/A").
    UNKNOWN_DATA = "UNKNOWN_DATA"
    # Field located on the PDF but it has no value (blank), or a checkbox group
    # where nothing is marked.
    MISSING_DATA = "MISSING_DATA"
    EXCEL_RULE_ISSUE = "EXCEL_RULE_ISSUE"
    NOT_VALIDATED = "NOT_VALIDATED"
    INFO = "INFO"

STATUS_COLORS = {
    Status.PASS: (0.10, 0.70, 0.20),
    Status.FAIL: (0.90, 0.10, 0.10),
    Status.WARNING: (1.00, 0.72, 0.05),
    Status.UNKNOWN_DATA: (0.55, 0.35, 0.85),   # purple
    Status.MISSING_DATA: (0.10, 0.35, 0.95),   # blue
    Status.EXCEL_RULE_ISSUE: (1.00, 0.90, 0.05),
    Status.NOT_VALIDATED: (0.50, 0.50, 0.50),
    Status.INFO: (0.20, 0.60, 0.90),
}

DEFAULT_COORD_TOLERANCE_PX = 5
DEFAULT_OVERLAP_THRESHOLD = 0.80
DEFAULT_FUZZY_THRESHOLD = 85
DATE_MONTHS = "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"

# ---------------------------------------------------------------------------
# Section / sub-section headers used to spatially tag every extracted KV pair.
# Each detected header line (found in the layout output) becomes an anchor; a KV
# pair inherits the nearest section/sub-section header that appears above it in
# reading order. Order longer/more-specific entries first so they win matching.
# These are tunable per form family.
# ---------------------------------------------------------------------------
SECTION_HEADERS = [
    "LICENSE - PARTY A",
    "LICENSE - PARTY B",
    "PARTY A - ATTRIBUTES",
    "PARTY B - ATTRIBUTES",
    "STATISTICAL INFORMATION",
    "AFFIRMATION",
    "ELIGIBILITY",
    "MARRIAGE",
    "LICENSE",
]

SUBSECTION_HEADERS = [
    "GROOM/SPOUSE",
    "BRIDE/SPOUSE",
    "LOCAL OFFICIAL",
    "OFFICIANT",
    "PARENTS",
    "PARTY A",
    "PARTY B",
    "OTHER",
]
