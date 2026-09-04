"""Database layer — pure functions, no LLM anywhere.

Everything here is deterministic and testable on its own. If these are wrong,
no agent built on top of them can be right, so test them directly first.
"""

from __future__ import annotations

import os
import re
import unicodedata

import psycopg
from dotenv import load_dotenv

load_dotenv()

DSN = os.getenv("DATABASE_URL")
if not DSN:
    raise RuntimeError("DATABASE_URL missing — check .env")


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def fmt_pkr(n: int) -> str:
    """107500000 -> '10.75 Crore'. Nobody in Karachi reads raw digits."""
    if n >= 10_000_000:
        return f"{n / 10_000_000:.2f}".rstrip("0").rstrip(".") + " Crore"
    if n >= 100_000:
        return f"{n / 100_000:.2f}".rstrip("0").rstrip(".") + " Lakh"
    return f"{n:,}"


# --------------------------------------------------------------------------
# location resolution
# --------------------------------------------------------------------------

# Words that carry no identity alone. Without this guard "2 bed in bahria town"
# matches "Gadap Town" at 1.00 on the shared word "town".
GENERIC = {
    "town", "city", "phase", "block", "sector", "road", "society",
    "complex", "housing", "scheme", "area", "new", "old",
}


# People write "askari six" as often as "askari 6", and place names here are
# numbered ("Askari 5", "DHA Phase 8", "Precinct 27"). Folding the words to
# digits means one stored alias covers both spellings.
_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}


def norm(s: str) -> str:
    """Same normalisation the aliases were stored with."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()
    return " ".join(_NUM_WORDS.get(w, w) for w in s.split())


def _windows(q: str, maxn: int = 4) -> list[str]:
    """'ask 6 me ghar chahiye' -> every 1..4-word slice.

    Necessary because similarity('ask 6', <whole sentence>) is low. We have to
    compare each alias against the PIECE of the message that names a place.
    """
    w = q.split()
    return list(
        {" ".join(w[i:i + n]) for n in range(1, maxn + 1) for i in range(len(w) - n + 1)}
        | {q}
    )


_RESOLVE_SQL = """
WITH q(win) AS (SELECT unnest(%(windows)s::text[]))
SELECT a.location_id, l.name, l.depth, a.alias_norm, a.source,
       similarity(a.alias_norm, q.win) AS score,
       (SELECT count(*) FROM listings li
         WHERE li.is_active
           AND li.location_id IN (SELECT id FROM descendants_of(l.id))) AS stock
FROM location_aliases a
JOIN locations l ON l.id = a.location_id
CROSS JOIN q
WHERE similarity(a.alias_norm, q.win) >= %(thr)s
ORDER BY similarity(a.alias_norm, q.win) DESC
LIMIT 100
"""


def resolve_location(text: str, threshold: float = 0.45) -> list[dict]:
    """'ask 6' -> Askari 6 (21109). Returns at most 3 candidates.

    Output size is independent of how big the alias table grows — the LLM
    always sees 3 rows, never the whole gazetteer.
    """
    q = norm(text)
    if not q:
        return []
    qt = {t for t in q.split() if len(t) >= 4}

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        rows = cur.execute(_RESOLVE_SQL, {"windows": _windows(q), "thr": threshold}).fetchall()

    best: dict[int, dict] = {}
    for loc_id, name, depth, alias, source, score, stock in rows:
        at = {t for t in alias.split() if len(t) >= 4}
        # must share a MEANINGFUL word, not just a generic one
        if at and not any(t not in GENERIC for t in (at & qt)) and score < 0.80:
            continue
        cand = {
            "location_id": loc_id, "name": name, "depth": depth,
            "via": alias, "source": source,
            "score": float(score), "spec": len(alias.split()), "stock": stock,
        }
        prev = best.get(loc_id)
        if prev is None or (cand["score"], cand["spec"]) > (prev["score"], prev["spec"]):
            best[loc_id] = cand

    # ties break toward the MORE SPECIFIC place, then the one holding stock
    return sorted(
        best.values(),
        key=lambda c: (-c["score"], -c["spec"], -c["depth"], -c["stock"]),
    )[:3]


# Trigram compares WHOLE strings, so a short word sitting inside a longer name
# scores badly: similarity('town', 'gadap town') is well under threshold. That
# made "town mai koi ghar hai?" return NO MATCH even though two real areas end
# in "Town". Observed in production 2026-09-04.
_ILIKE_SQL = """
SELECT l.id, l.name, l.depth,
       (SELECT count(*) FROM listings li
         WHERE li.is_active
           AND li.location_id IN (SELECT id FROM descendants_of(l.id))) AS stock
FROM locations l
WHERE l.name ILIKE %(pat)s
ORDER BY l.depth, l.name
LIMIT 6
"""


def substring_locations(text: str) -> list[dict]:
    """Fallback when trigram finds nothing: which real places CONTAIN this word?

    Returns candidates to offer the customer, never an answer to act on. A
    minimum length and a LIMIT keep '%a%' from dumping the whole gazetteer.
    """
    q = norm(text)
    words = [w for w in q.split() if len(w) >= 4]
    if not words:
        return []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        seen: dict[int, dict] = {}
        for w in words:
            for lid, name, depth, stock in cur.execute(_ILIKE_SQL, {"pat": f"%{w}%"}).fetchall():
                if stock:                       # only offer places with inventory
                    seen[lid] = {"location_id": lid, "name": name,
                                 "depth": depth, "stock": stock, "via": w}
    return sorted(seen.values(), key=lambda c: (-c["stock"], c["depth"]))[:5]


def decide(cands: list[dict]) -> str:
    """ACCEPT / ASK / NO_MATCH. Never guess when two places are close."""
    if not cands:
        return "NO_MATCH"
    a = cands[0]
    b = cands[1] if len(cands) > 1 else None
    if a["score"] >= 0.60 and (b is None or a["score"] - b["score"] > 0.12):
        return "ACCEPT"
    if a["score"] >= 0.60 and b and a["score"] == b["score"] and a["spec"] > b["spec"]:
        return "ACCEPT"
    if a["score"] >= 0.60 and b and a["stock"] > 0 and b["stock"] == 0:
        return "ACCEPT"
    return "ASK"


# --------------------------------------------------------------------------
# listing search
# --------------------------------------------------------------------------

SORTS = {
    "price_asc": "l.price_pkr ASC",
    "price_desc": "l.price_pkr DESC",
    "area_desc": "l.area_sqyd DESC NULLS LAST",
    "newest": "l.first_seen_on DESC",
}


def search_listings(
    purpose: str,
    property_type=None,
    location_ids=None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_beds: int | None = None,
    max_beds: int | None = None,
    features=None,
    sort: str = "price_asc",
    limit: int = 5,
) -> list[dict]:
    """Filtered inventory search.

    `purpose` is REQUIRED and never defaulted. Rent and sale prices share one
    column, so a missing purpose surfaces a 90,000/month rental as the cheapest
    "house" against a 7 Crore sale floor — an ~800x error.
    """
    if purpose not in ("sale", "rent"):
        raise ValueError("purpose must be 'sale' or 'rent' — never guess it")

    limit = max(1, min(int(limit), 10))  # cap so tool output stays bounded
    where = ["l.is_active", "l.purpose = %(purpose)s"]
    p: dict = {"purpose": purpose, "limit": limit}

    if property_type:
        where.append("l.property_type = ANY(%(ptypes)s)")
        p["ptypes"] = [property_type] if isinstance(property_type, str) else list(property_type)

    if location_ids:
        # one parent id expands to every descendant, so "Askari 5" covers
        # Sectors E/F/H/J without the caller naming them
        where.append(
            """l.location_id IN (
                 SELECT d.id FROM unnest(%(locs)s::int[]) AS r(rid),
                                  LATERAL descendants_of(r.rid) AS d)"""
        )
        p["locs"] = list(location_ids)

    for col, key, op, val in [
        ("price_pkr", "min_price", ">=", min_price),
        ("price_pkr", "max_price", "<=", max_price),
        ("bedrooms", "min_beds", ">=", min_beds),
        ("bedrooms", "max_beds", "<=", max_beds),
    ]:
        if val is not None:
            where.append(f"l.{col} {op} %({key})s")
            p[key] = val

    if features:  # must have ALL requested features
        where.append(
            """(SELECT count(*) FROM listing_features f
                 WHERE f.listing_id = l.id AND f.feature = ANY(%(feats)s))
               = cardinality(%(feats)s)"""
        )
        p["feats"] = list(features)

    # Only column/sort names are interpolated, and both come from fixed
    # literals above — every user-supplied value is a bound parameter.
    sql = f"""
      SELECT l.id, l.price_pkr, l.property_type, l.bedrooms, l.bathrooms,
             l.area_sqyd, loc.name AS area, l.title,
             COALESCE((SELECT array_agg(f.feature ORDER BY f.feature)
                       FROM listing_features f WHERE f.listing_id = l.id), '{{}}') AS features,
             (SELECT count(*) FROM listing_media m WHERE m.listing_id = l.id) AS photos
      FROM listings l
      LEFT JOIN locations loc ON loc.id = l.location_id
      WHERE {' AND '.join(where)}
      ORDER BY {SORTS.get(sort, SORTS['price_asc'])}
      LIMIT %(limit)s"""

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, p)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# inventory overview
# --------------------------------------------------------------------------

# Rolled up to depth=1 so output stays flat as the location tree grows.
# Ungrouped this is O(locations x types x purposes) and bloats the context.
_SUMMARY_SQL = """
SELECT top.name AS area, l.property_type, l.purpose, count(*) AS n,
       min(l.price_pkr) AS lo, max(l.price_pkr) AS hi
FROM listings l
JOIN locations leaf ON leaf.id = l.location_id
JOIN LATERAL (
  SELECT a.name FROM locations a
   WHERE leaf.path <@ a.path AND a.depth = 1
   ORDER BY a.depth LIMIT 1
) top ON true
WHERE l.is_active
GROUP BY 1, 2, 3
ORDER BY n DESC
LIMIT 15
"""


def inventory_overview() -> list[dict]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(_SUMMARY_SQL)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
