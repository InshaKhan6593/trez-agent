"""Re-derive typed values from the RAW strings in a scrape record.

Deliberately does NOT trust `facts.price.amount`. The scraper parsed
"PKR 90 Thousand" as 90 for weeks; the fix was only possible because the
source string was preserved beside the derived number. Anything the scraper
computes is a place a bug can hide, so the ETL re-derives from
`price.display` / `rawAttributes` / `rawPageText`.
"""

from __future__ import annotations

import re

# --- price -----------------------------------------------------------------

_UNITS = {
    "arab": 1_000_000_000,
    "crore": 10_000_000,
    "cr": 10_000_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "lakh": 100_000,
    "lac": 100_000,
    "thousand": 1_000,
    "k": 1_000,
}

# Longest-first so "crore" wins over "cr", "thousand" over nothing.
_PRICE_RE = re.compile(
    r"([\d,.]+)\s*(arab|crore|cr\.?|lakh|lac|thousand|million|m\b|k\b)?",
    re.IGNORECASE,
)


def parse_pkr(display: str | None) -> int | None:
    """'PKR 10.75 Crore Bath(s) 6' -> 107500000.

    Matches the FIRST number+unit pair, which is always the headline price;
    installment plans ("Monthly Installment PKR 60 Thousand") come later in
    the string and are correctly ignored.
    """
    if not display:
        return None
    m = _PRICE_RE.search(" ".join(display.split()))
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if value <= 0:
        return None
    unit = (m.group(2) or "").lower().replace(".", "")
    return round(value * _UNITS.get(unit, 1))


# --- area ------------------------------------------------------------------

_AREA_TO_SQYD = {
    "sq. yd": 1.0,
    "sq yd": 1.0,
    "sq. ft": 1 / 9,
    "sq ft": 1 / 9,
    "marla": 30.25,      # standard 225 sq ft marla
    "kanal": 605.0,
    "sq. m": 1.19599,
    "sq m": 1.19599,
}


def parse_area_sqyd(area: dict | None) -> float | None:
    """Normalise any area unit to square yards. Today the corpus is 100%
    Sq. Yd, but Lahore/Islamabad stock would bring Marla and Kanal."""
    if not area or area.get("value") is None:
        return None
    unit = str(area.get("unit", "")).strip().lower()
    factor = _AREA_TO_SQYD.get(unit)
    if factor is None:
        return None
    return round(float(area["value"]) * factor, 2)


# --- enums -----------------------------------------------------------------

_TYPES = {
    "house": "house",
    "flat": "flat",
    "apartment": "flat",
    "upper portion": "house",
    "lower portion": "house",
    "residential plot": "plot",
    "commercial plot": "plot",
    "plot": "plot",
    "penthouse": "flat",
    "farm house": "house",
}


def parse_property_type(value: str | None) -> str:
    if not value:
        return "other"
    return _TYPES.get(value.strip().lower(), "other")


def parse_purpose(facts: dict, source: str | None) -> str:
    """Two independent signals agree on all 57 listings; prefer facts,
    fall back to which results page the listing came from."""
    purpose = (facts.get("purpose") or "").strip().lower()
    if purpose == "for rent":
        return "rent"
    if purpose == "for sale":
        return "sale"
    return "rent" if source == "rentals" else "sale"


def parse_location_id(url: str) -> int | None:
    """Zameen URLs end '-<listingId>-<locationId>-<n>.html'.

    The trailing number is NOT always 1 — this corpus has -2 and -4 — and a
    hardcoded '-1' silently orphans those listings from the location tree.
    """
    m = re.search(r"-(\d+)-(\d+)-\d+\.html$", url or "")
    return int(m.group(2)) if m else None
