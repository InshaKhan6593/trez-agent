# trez-agent

Data pipeline for a WhatsApp property agent that answers customer queries and
qualifies leads for **Trez Enterprises** (Karachi real estate).

Zameen scraper → immutable raw snapshots → Postgres → LangGraph agent.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design: retrieval
strategy, location resolution, intent handling, schema, and the data gotchas
that will bite you.

---

## Quickstart

```bash
npm install                       # playwright + chromium
docker compose up -d              # Postgres 18 on 127.0.0.1:5544
```

```bash
# 1. Scrape into a dated, immutable snapshot
node scrape-zameen.mjs \
  --output-dir data/raw/$(date +%Y-%m-%d) \
  --media-store data/media \
  --download-media

# 2. Validate it
node validate-zameen-data.mjs \
  --input-dir data/raw/$(date +%Y-%m-%d) --require-downloaded-media

# 3. Load into Postgres
python -m etl.ingest data/raw/$(date +%Y-%m-%d)
```

Connect: `postgresql://trez:trez_local_dev@127.0.0.1:5544/trez_agent`
(local dev credentials only — use `127.0.0.1`, not `localhost`.)

---

## Layout

```
scrape-zameen.mjs        Playwright scraper, robots-aware, agency-verified
validate-zameen-data.mjs snapshot validator (errors block, warnings don't)
db/001_schema.sql        schema, auto-applied on first container start
etl/parse.py             re-derives price/area/type from RAW strings
etl/gazetteer.py         location tree + alias generation (no LLM, no curation)
etl/features.py          boolean feature extraction from listing text
etl/ingest.py            snapshot -> Postgres, with diff and deactivation
data/                    git-ignored — see below
```

## Two rules that matter

**1. Raw is immutable; the ETL re-derives.**
`data/raw/<date>/` is never edited. Postgres must always be rebuildable from it
with zero network calls. The ETL deliberately ignores the scraper's own parsed
`facts.price.amount` — that field once read `"PKR 90 Thousand"` as `90`, and the
fix was only verifiable because the source string was preserved beside it.

**2. `data/` is never committed.**
It holds third-party content (zameen.com listings and images) and a client's
live inventory. It is fully reproducible by running the scraper.

## Daily operation

Always scrape **fresh** into a new dated directory. Never use `--resume` for the
daily job — it re-fetches everything *and* keeps listings that have disappeared
from the source, so sold properties would never go away. It exists only to
recover an interrupted run.

Listings absent from a new snapshot are marked `is_active = false`, never
deleted: `leads.matched_listing_ids` still references them, and a relisted
property keeps its `first_seen_on` and price history.
