"""Extract boolean features from listing text.

This is what replaces vector search. "west open flat chahiye" becomes an exact
indexed predicate instead of a fuzzy similarity score, and every match can be
traced to the listing text that produced it.

MUST be re-run on every ingest, not only on insert: listing 50535334 was
corrected upstream from "West Open" to "East Open" between two scrapes, so
extract-once would leave the agent confidently stating the wrong orientation.
"""

from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern] = {
    "west_open":       re.compile(r"\bwest\s*open\b", re.I),
    "east_open":       re.compile(r"\beast\s*open\b", re.I),
    "corner":          re.compile(r"\bcorner\b", re.I),
    "park_facing":     re.compile(r"\bpark\s*(facing|view)\b|\bnext to park\b|\bnear park\b", re.I),
    "sea_view":        re.compile(r"\bsea\s*(facing|view)\b|\bmarina view\b|\bbeach\s*front\b", re.I),
    "boulevard_facing": re.compile(r"\bboulevard\s*facing\b|\bmain boulevard\b", re.I),
    "furnished":       re.compile(r"\bfurnished\b", re.I),
    "on_installments": re.compile(r"\binstall?ments?\b|\binstalments?\b", re.I),
    "ready_to_move":   re.compile(r"\bready to move\b", re.I),
    "brigadier_house": re.compile(r"\bbrigadier\b", re.I),
    "ground_floor":    re.compile(r"\bground floor\b", re.I),
    "brand_new":       re.compile(r"\bbrand\s*new\b", re.I),
    "servant_quarter": re.compile(r"servant quarters?", re.I),
    "near_mosque":     re.compile(r"\bmosque\b|\bmasjid\b", re.I),
    "swimming_pool":   re.compile(r"swimming pool", re.I),
    "gym":             re.compile(r"\bgym\b", re.I),
    "prayer_room":     re.compile(r"prayer room", re.I),
    "study_room":      re.compile(r"study room", re.I),
    "parking":         re.compile(r"parking spaces?", re.I),
}


def extract(record: dict) -> list[str]:
    """Search title + description + rawAttributes.

    rawPageText is deliberately excluded: it carries the 'Similar Houses'
    block describing OTHER agencies' properties, so matching against it would
    tag a listing with a neighbour's features.
    """
    blob = " ".join(
        [
            record.get("title") or "",
            record.get("description") or "",
            " ".join(record.get("rawAttributes") or []),
        ]
    )
    return sorted(name for name, rx in PATTERNS.items() if rx.search(blob))
