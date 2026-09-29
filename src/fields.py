"""Field-label normalization and value coercion for onboarding workbooks.

Labels drift across the four template generations: en-dash vs hyphen, single vs
double spaces, trailing blanks. Every comparison goes through normalize_label so
`Maintenance – WiFi – Internet password` and `Maintenance - WiFi -  Internet
password` resolve to the same field.
"""

import re
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# Every dash codepoint seen in the templates, plus the plain hyphen.
_DASHES = "‐‑‒–—―−-"
_DASH_RE = re.compile(f"[{re.escape(_DASHES)}]")
_NULLISH = {"", "n/a", "na", "none", "-", "--", "tbd", "?"}


def normalize_label(raw) -> str:
    """Canonical form, case preserved: single spaces, ' - ' separators."""
    if raw is None:
        return ""
    s = str(raw).replace("\xa0", " ")
    s = _DASH_RE.sub("-", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def label_key(raw) -> str:
    """Lowercased normalized label — the join key for lookups."""
    return normalize_label(raw).lower()


def split_label(raw) -> tuple[str | None, str | None, str]:
    """'Maintenance - WiFi - Internet password' -> (Maintenance, WiFi, Internet password)."""
    s = normalize_label(raw)
    parts = s.split(" - ")
    if len(parts) >= 3:
        return parts[0], parts[1], " - ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return None, None, s


def clean_value(raw) -> str | None:
    """Trim a cell; treat blanks and N/A-style placeholders as missing."""
    if raw is None:
        return None
    s = str(raw).replace("\xa0", " ").strip()
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return None if s.lower() in _NULLISH else s


def parse_money(raw) -> float | None:
    """'$1,650.00' / '165' -> float. Returns None if no number is present."""
    s = clean_value(raw)
    if s is None:
        return None
    m = re.search(r"-?[\d,]*\.?\d+", s.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_number(raw) -> float | None:
    """First number in the cell. 'Two 1000 sqft units' -> 1000, '4' -> 4."""
    s = clean_value(raw)
    if s is None:
        return None
    m = re.search(r"-?[\d,]*\.?\d+", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_int(raw) -> int | None:
    n = parse_number(raw)
    return None if n is None else int(n)


@lru_cache(maxsize=1)
def sensitive_keys() -> frozenset[str]:
    """Normalized labels whose values must be encrypted, from config."""
    cfg = yaml.safe_load((ROOT / "config" / "sensitive_fields.yml").read_text())
    return frozenset(label_key(f) for f in cfg.get("sensitive", []))


def is_sensitive(raw) -> bool:
    return label_key(raw) in sensitive_keys()


# ---------------------------------------------------------------- aliases

# Template generations spell the same field differently. Left = a spelling that
# occurs in some workbook; right = the label everything is stored under.
#
# Only fold labels that are genuinely the SAME field. Verified across all 98
# resolvable workbooks (2026-09-27): no workbook contains more than one of these
# three spellings, so folding them cannot merge two distinct values.
_LABEL_ALIASES: dict[str, str] = {
    "maintenance - access - backup code":
        "Maintenance - Access - Guest Access Backup Code",
    "maintenance - access - guest backup code":
        "Maintenance - Access - Guest Access Backup Code",
}


def canonical_label(raw) -> str:
    """Normalized label, with per-generation spellings folded to one name.

    Applied where a field is stored (parse_workbook._parse_listings) rather than
    inside normalize_label/label_key, because discover.py runs label_key over the
    INDEX's column headers and must not be affected.
    """
    norm = normalize_label(raw)
    return _LABEL_ALIASES.get(norm.lower(), norm)
