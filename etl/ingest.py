"""Load a raw snapshot into Postgres.

Idempotent: running it twice on the same snapshot is a no-op. Postgres is
always rebuildable from data/raw/<date>/ with no network calls.

Usage:
    python -m etl.ingest <raw_dir> [--dsn ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import psycopg

from . import features, parse
from .gazetteer import build_aliases, build_tree

DEFAULT_DSN = "postgresql://trez:trez_local_dev@127.0.0.1:5544/trez_agent"


def load_snapshot(raw_dir: Path) -> tuple[list[dict], list[dict]]:
    listings = json.loads((raw_dir / "listings.json").read_text("utf-8"))
    rejected_path = raw_dir / "rejected-listings.json"
    rejected = json.loads(rejected_path.read_text("utf-8")) if rejected_path.exists() else []
    return listings, rejected


def to_row(rec: dict) -> dict:
    """Raw record -> typed listing row, re-deriving from raw strings.

    Note what is NOT read: `facts.price.amount` (the scraper's guess, which was
    wrong by 1000x on 'PKR 90 Thousand') and `inventory.agentApproved` (dropped
    entirely — approval is a human decision that belongs in Postgres, not in
    scrape output).
    """
    facts = rec.get("facts") or {}
    purpose = parse.parse_purpose(facts, rec.get("source"))
    price = parse.parse_pkr((facts.get("price") or {}).get("display"))
    return {
        "id": int(rec["id"]),
        "purpose": purpose,
        "price_pkr": price,
        "price_period": "monthly" if purpose == "rent" else None,
        "property_type": parse.parse_property_type(facts.get("propertyType")),
        "area_sqyd": parse.parse_area_sqyd(facts.get("area")),
        # NULL is data, not a reason to exclude. Plots legitimately have no
        # bedrooms; some flats simply have the field unfilled on Zameen.
        "bedrooms": facts.get("bedrooms"),
        "bathrooms": facts.get("bathrooms"),
        "location_id": parse.parse_location_id(rec.get("url", "")),
        "title": rec.get("title"),
        "description": rec.get("description"),
        "url": rec.get("url"),
        "media": [m for m in (rec.get("downloadedMedia") or []) if not m.get("error")],
        "features": features.extract(rec),
    }


def ingest(raw_dir: Path, dsn: str = DEFAULT_DSN) -> dict:
    listings, rejected = load_snapshot(raw_dir)
    scraped_on = max(r["scrapedAt"] for r in listings)[:10]
    seen_on = date.fromisoformat(scraped_on)

    nodes = build_tree(listings)
    aliases = build_aliases(nodes)
    rows = [to_row(r) for r in listings]

    skipped = [r for r in rows if r["price_pkr"] is None or r["location_id"] is None]
    rows = [r for r in rows if r not in skipped]

    stats: dict[str, object] = {
        "raw_dir": str(raw_dir),
        "scraped_on": scraped_on,
        "locations": len(nodes),
        "aliases": len(aliases),
        "listings": len(rows),
        "rejected_by_scraper": len(rejected),
        "skipped_unusable": [r["id"] for r in skipped],
    }

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ingest_runs (raw_dir, scraped_on, listing_count, rejected_count)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (str(raw_dir), seen_on, len(rows), len(rejected)),
        )
        run_id = cur.fetchone()[0]

        # --- locations: parents before children, so the FK is satisfied -----
        for node in sorted(nodes.values(), key=lambda n: n["depth"]):
            cur.execute(
                """INSERT INTO locations (id, name, parent_id, path, depth)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE
                     SET name = EXCLUDED.name,
                         parent_id = EXCLUDED.parent_id,
                         path = EXCLUDED.path,
                         depth = EXCLUDED.depth""",
                (node["id"], node["name"], node["parent_id"], node["path"], node["depth"]),
            )

        # --- aliases: regenerated wholesale; rules are deterministic --------
        cur.execute("DELETE FROM location_aliases WHERE source IN ('rule','abbr')")
        cur.executemany(
            """INSERT INTO location_aliases (alias_norm, location_id, source)
               VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
            aliases,
        )

        # --- listings -------------------------------------------------------
        for r in rows:
            cur.execute("SELECT price_pkr FROM listings WHERE id = %s", (r["id"],))
            existing = cur.fetchone()

            cur.execute(
                """INSERT INTO listings (id, purpose, price_pkr, price_period,
                       property_type, area_sqyd, bedrooms, bathrooms, location_id,
                       title, description, url, is_active,
                       first_seen_on, last_seen_on, ingest_run_id)
                   VALUES (%(id)s,%(purpose)s,%(price_pkr)s,%(price_period)s,
                       %(property_type)s,%(area_sqyd)s,%(bedrooms)s,%(bathrooms)s,
                       %(location_id)s,%(title)s,%(description)s,%(url)s,true,
                       %(seen)s,%(seen)s,%(run)s)
                   ON CONFLICT (id) DO UPDATE SET
                       purpose=EXCLUDED.purpose, price_pkr=EXCLUDED.price_pkr,
                       price_period=EXCLUDED.price_period,
                       property_type=EXCLUDED.property_type,
                       area_sqyd=EXCLUDED.area_sqyd, bedrooms=EXCLUDED.bedrooms,
                       bathrooms=EXCLUDED.bathrooms, location_id=EXCLUDED.location_id,
                       title=EXCLUDED.title, description=EXCLUDED.description,
                       url=EXCLUDED.url, is_active=true,
                       last_seen_on=EXCLUDED.last_seen_on,
                       ingest_run_id=EXCLUDED.ingest_run_id""",
                {**r, "seen": seen_on, "run": run_id},
            )

            if existing is None or existing[0] != r["price_pkr"]:
                cur.execute(
                    """INSERT INTO price_history (listing_id, seen_on, price_pkr)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (r["id"], seen_on, r["price_pkr"]),
                )

            # Features are rebuilt every run — titles get corrected upstream.
            cur.execute("DELETE FROM listing_features WHERE listing_id = %s", (r["id"],))
            cur.executemany(
                "INSERT INTO listing_features (listing_id, feature) VALUES (%s,%s)",
                [(r["id"], f) for f in r["features"]],
            )

            cur.execute("DELETE FROM listing_media WHERE listing_id = %s", (r["id"],))
            cur.executemany(
                """INSERT INTO listing_media (listing_id, asset_id, seq, file, source_url)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [
                    (r["id"], m["assetId"], i, m["file"], m.get("url"))
                    for i, m in enumerate(r["media"])
                    if m.get("assetId")
                ],
            )

        # --- deactivate what vanished from the source -----------------------
        # NEVER DELETE: leads.matched_listing_ids references these rows, and a
        # relisted property should keep its first_seen_on and price trail.
        cur.execute(
            """UPDATE listings SET is_active = false
               WHERE is_active AND id <> ALL(%s) RETURNING id""",
            ([r["id"] for r in rows],),
        )
        stats["deactivated"] = [row[0] for row in cur.fetchall()]

        cur.execute("UPDATE ingest_runs SET finished_at = now() WHERE id = %s", (run_id,))
        conn.commit()

    stats["run_id"] = run_id
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_dir", type=Path)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    args = ap.parse_args()
    for k, v in ingest(args.raw_dir, args.dsn).items():
        print(f"  {k:22} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
