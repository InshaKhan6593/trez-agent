# Trez Enterprises — WhatsApp Property Agent

Data pipeline and retrieval design for a WhatsApp agent that answers property
queries and qualifies leads for Trez Enterprises (Karachi).

Stack: Zameen scraper (Playwright) → raw JSON snapshots → Postgres → LangGraph
agent. DeepEval for evaluation, LangSmith for tracing.

---

## 1. Core principle: raw first, derive later

```
┌─ BRONZE — immutable, append-only, never edited ────────────────┐
│  data/raw/<date>/listings/<id>.json     full scrape record     │
│  data/raw/<date>/listings.json          aggregate              │
│  data/raw/<date>/rejected-listings.json what was excluded, why │
│  data/media-store/<assetId>.jpeg        shared, deduped photos │
└────────────────────────────────────────────────────────────────┘
                │   ETL — pure function, re-runnable, versioned
                ▼
┌─ SILVER — Postgres, typed, queryable, rebuildable ─────────────┐
│  locations · location_aliases · listings · listing_features    │
│  listing_media · price_history · leads                         │
└────────────────────────────────────────────────────────────────┘
```

**The rule: Postgres must always be reconstructible from bronze with zero
network calls.** If you cannot drop the database and rebuild it from
`data/raw/`, the ETL has a hidden dependency.

### Why this matters — a real case

On 2026-09-03 we found the scraper parsed `"PKR 90 Thousand"` as `90` instead
of `90,000` — a 1000× error on a rental.

```
DERIVED   facts.price.amount  : 90                            ← the bug
PRESERVED price.display       : "PKR 90 Thousand Bath(s) 4"   ← truth survives
PRESERVED rawAttributes[]     : "PricePKR90 Thousand"
PRESERVED rawPageText         : 2 occurrences
```

Because the source string was kept beside the derived number, the fix was
verified by replaying all 57 raw records — 1 changed, 56 identical — **without
a single request to Zameen.**

> **Consequence for the ETL:** treat `facts.*` as a convenience copy that may be
> wrong. Re-derive price, area and beds from `price.display` / `rawAttributes` /
> `rawPageText`. Any parsing the scraper does is a place a bug can hide.

---

## 2. The corpus

Snapshot of 2026-09-03 (full agency catalogue, scoped by `agent_id=200295`):

| | |
|---|---|
| Listings | 57 (55 sale, 2 rent) |
| Types | Flat, House, Residential Plot |
| Area unit | `Sq. Yd` on 100% — no Kanal/Marla |
| Price units seen | Crore (61), Lakh (29), Thousand (3) |
| Locations | 31 nodes, 100% Karachi |
| Coordinates | **none** — no lat/lng anywhere |
| Total raw size | ~671 KB for 57 listings |

**Churn is high.** Between 2026-08-21 and 2026-09-03 (13 days):

```
+19 new    -18 disappeared    ~9 price changes (all increases)
```

37 of 56 listings changed state — **~1 in 3 would have been stale** if served
from a 13-day-old snapshot. Daily scraping is not optional, and `is_active`
handling is the most important part of the ETL.

### There is no geographic data

`rawPageText` contains a "Nearby Schools / Nearby Hospitals / Distance From
Airport" section, but these are **unfilled checkbox labels with no values and no
distances**. Do not plan around them.

---

## 3. Scraper

### Running it

```bash
# Daily: fresh dated snapshot + shared media pool
node scrape-zameen.mjs \
  --output-dir data/raw/$(date +%Y-%m-%d) \
  --media-store data/media-store \
  --download-media
```

| Flag | Notes |
|---|---|
| `--output-dir` | Must be **empty**. Enforced by `assertFreshOutputDirectory` |
| `--media-store` | Shared content-addressed pool. Each photo downloaded once, ever |
| `--download-media` | Off by default |
| `--sources` | `sales,rentals` (default) |
| `--min/max-delay-ms` | 2500 / 4500. Politeness — do not lower |
| `--resume` | **Do not use for the daily job.** See below |

`robots.txt` is checked before every run.

### ⚠️ `--resume` is wrong for daily updates

It sounds incremental but is not. It pre-loads existing records into a map;
the loop still re-fetches every listing. Worse, records from the previous run
**stay in the map even when the listing is gone from Zameen** — so sold
properties would never disappear.

`--resume` is for recovering an *interrupted* run only. The daily job always
scrapes fresh into a new dated directory; Postgres accumulates history.

### Every run is a full crawl

There is no incremental mode. At ~57 listings that is fine (~60 page loads,
5–10 min). Two crawls with different rules:

- **Index crawl — must always be complete.** The results pages are the only way
  to learn what is new *and* what is gone. The set difference **is** the update.
- **Detail crawl — where the cost is.** Optimise here if the catalogue grows.

```
IDs on Zameen  −  IDs in DB      =  NEW          → fetch detail
IDs in DB      −  IDs on Zameen  =  DISAPPEARED  → is_active = false (no fetch)
in both                          =  EXISTING     → fetch if stale
```

### Fixes applied 2026-09-03

1. **`parsePkrAmount`** — added `thousand`/`k` (×1,000) and `arab`
   (×1,000,000,000). Verified 8/8 unit tests; replay over 57 raw records
   changed exactly 1 listing.
2. **Content-addressed media.** Files were named by position
   (`001_image.jpeg`) — unstable if the agency reorders the gallery. Now named
   by Zameen's stable asset id (`303458333.jpeg`). Records carry
   `assetId` + `file`; **the ETL should use those, not `localFile`**, which
   bakes in a machine path.
3. **Skip-if-exists** on media download. With `--media-store`, re-runs fetch
   only genuinely new images.

### The agency guard is load-bearing

`collectResultLinks` grabs every `/Property/` link on the page — including
"similar properties" cards. The 2026-09-03 run pulled **23 listings from other
agencies** (Lahore, Islamabad); all were rejected by the
`listing.agency !== AGENCY` check. Without that guard you would ingest
properties your client does not represent.

---

## 4. Retrieval: Postgres, not a vector store

Decision: **Postgres only. No vector store for v1.**

1. **Query shape.** Real-estate queries are numeric and boolean — budget ranges,
   bed counts, area, location containment. There is no embedding for "under 5
   crore". Vector search cannot filter exactly, count, or sort by price.
2. **Scale.** 57 rows. ANN indexes exist to avoid scanning millions.
3. **Filtered-ANN recall collapse.** A typical query
   ("3-bed flat in Askari 5 under 5 crore") has ~2% selectivity. Filtered vector
   search degrades badly in exactly that regime, and returns a full-looking
   result set that is mostly wrong — undetectable by checking result count.

### No LLM-authored SQL

The dominant NL2SQL failure is **value hallucination** — the model writes a
filter value that does not exist and gets zero rows with no error. On WhatsApp,
a false "nothing available" silently kills the lead.

Instead: **constrained tool calls.** The model fills a typed form; our code
compiles it to parameterised SQL.

```
resolve_location(text)   -> [{location_id, name, path, confidence}]
search_listings(purpose*, property_type, location_ids, min/max_price,
                min/max_beds, min/max_area, features, sort, limit)
get_listing(id)          -> full row + media
capture_lead(intent, ...)-> writes the leads row
                                          * purpose is REQUIRED, never defaulted
```

---

## 5. Schema

```sql
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Place tree, built automatically from Zameen breadcrumb IDs
CREATE TABLE locations (
  id             int PRIMARY KEY,        -- Zameen's own id (6655, 12243, ...)
  name           text NOT NULL,          -- property-type suffix stripped
  parent_id      int REFERENCES locations(id),
  path           ltree NOT NULL,         -- '2.525.6654.6655'
  centroid       point,                  -- nullable; never blocks
  geo_confidence text                    -- 'ok' | 'low' | 'missing'
);
CREATE INDEX ON locations USING gist (path);

CREATE TABLE location_aliases (
  alias_norm  text NOT NULL,
  location_id int NOT NULL REFERENCES locations(id),
  source      text NOT NULL,             -- 'rule' | 'abbr' | 'query_log'
  PRIMARY KEY (alias_norm, location_id)
);
CREATE INDEX ON location_aliases USING gin (alias_norm gin_trgm_ops);

CREATE TABLE listings (
  id             bigint PRIMARY KEY,     -- Zameen listing id
  purpose        text NOT NULL CHECK (purpose IN ('sale','rent')),
  price_pkr      bigint NOT NULL,
  price_period   text
    CHECK ((purpose='sale' AND price_period IS NULL)
        OR (purpose='rent' AND price_period IS NOT NULL)),
  property_type  text NOT NULL,          -- house | flat | plot
  area_sqyd      int,
  bedrooms       int,
  bathrooms      int,
  location_id    int REFERENCES locations(id),
  title          text NOT NULL,
  description    text,
  url            text NOT NULL,
  is_active      boolean NOT NULL DEFAULT true,
  first_seen_at  date NOT NULL,
  last_seen_at   date NOT NULL,
  raw_run        text NOT NULL           -- which bronze snapshot produced this
);

CREATE TABLE listing_features (
  listing_id bigint REFERENCES listings(id),
  feature    text NOT NULL,
  PRIMARY KEY (listing_id, feature)
);

CREATE TABLE listing_media (
  listing_id bigint REFERENCES listings(id),
  seq        int NOT NULL,
  asset_id   text NOT NULL,              -- Zameen asset id = file name stem
  PRIMARY KEY (listing_id, asset_id)
);

CREATE TABLE price_history (
  listing_id bigint REFERENCES listings(id),
  seen_on    date NOT NULL,
  price_pkr  bigint NOT NULL,
  PRIMARY KEY (listing_id, seen_on)
);

CREATE TABLE leads (
  id                  bigserial PRIMARY KEY,
  phone               text NOT NULL,
  intent              text NOT NULL
    CHECK (intent IN ('buy','rent','sell','rent_out')),
  property_type       text,
  location_id         int REFERENCES locations(id),
  location_text       text,              -- what they actually typed
  min_price           bigint,
  max_price           bigint,
  bedrooms            int,
  area_sqyd           int,
  expected_price      bigint,            -- sell / rent_out
  matched_listing_ids bigint[],
  had_no_inventory    boolean,           -- gap report for the client
  status              text NOT NULL DEFAULT 'new',
  created_at          timestamptz NOT NULL DEFAULT now()
);
```

The `price_period` CHECK makes the rent/sale price collision **structurally
impossible** — see §7.

`had_no_inventory` is quietly valuable: after a month it tells the client
"37 people asked for rentals in Gulshan and we had nothing."

---

## 6. Location resolution — no coordinates, no paid geocoding, no hand curation

Four layers, all automatic.

**Layer 0 — Gazetteer from Zameen's own IDs.** Every listing URL ends
`-<listingId>-<locationId>-<n>.html`, and breadcrumb `item` URLs carry the
ancestor chain. This yields a clean, de-duplicated tree with stable integer
keys, free. It also solves the `"Askari 5 - Sector J"` vs
`"Askari 5 - Sector J Flats"` split — same id `17289`, so the ID is the
identity and the text is just a label.

Containment is one index lookup:
```sql
WHERE location_id IN (SELECT id FROM locations WHERE path <@ '2.525.6654.6655')
```
→ every listing under Askari 5 at any depth, without naming the sectors.

**Layer 1 — Rule-generated aliases.** ~195 rows from 31 nodes, zero hand-written:
- character variants: `askari6`, `gulshan e iqbal`, `gulshaneiqbal`
- prefix abbreviations: `Askari 6` → `ask 6`, `aska 6` *(a real listing is
  titled "…in Ask 6")*
- word-drop: `Gulistan-e-Jauhar` → `jauhar`; `DHA Defence` → `defence`;
  `Bahria Town Karachi` → `bahria`, `bahria town`
- leaf + parent, both orders; sector/block tails (`sector j`, `sec j`)

**Layer 2 — Abbreviation dictionary.** Three entries, fixed size, domain
vocabulary — *not* a per-location list:

```js
const ABBR = { cantonment: ["cantt","cant"], society: ["soc"], cooperative: ["coop"] };
```

`Cantonment → cantt` is not a prefix or substring, so no rule derives it. Any
future `"X Cantonment"` gets `"X cantt"` free.

**Layer 3 — `pg_trgm` fuzzy match** over layers 1–2 for typos.

### Two mandatory guards

Aggressive rules are not free. Both of these were real bugs:

1. **Never emit an all-generic alias.** `Gadap Town` → drop-first → `"town"`
   matched `"2 bed in bahria town"` at 1.00. Generic: `town, block, phase,
   sector, road, society, housing, scheme, karachi`.
2. **Never emit an alias equal to another node's canonical name.**
   `"Bahria Town Karachi"` must not generate `karachi` pointing at node 3138.

### Ranking rules

- **Specificity wins**: equal score → more alias words → deeper node.
- **Stock-aware**: prefer a node with inventory over an empty one.
- Accept if top score ≥ 0.60 and gap to runner-up > 0.12; otherwise **ask**.

### Measured: 12/13 on realistic queries

```
"flat in askari 5"          ACCEPT  Askari 5 (16)
"ask 6 me ghar chahiye"     ACCEPT  Askari 6 (15)
"3 bed apartment sector j"  ACCEPT  Askari 5 - Sector J (12)
"gulshan iqbal block 2"     ACCEPT  Gulshan-e-Iqbal - Block 2
"dha phase 8 ka flat"       ACCEPT  DHA Phase 8 (6)
"jauhar me flat chahiye"    ACCEPT  Gulistan-e-Jauhar (5)
"malir cantt"               ACCEPT  Malir Cantonment (33)
"koi flat hai defence me"   ACCEPT  DHA Defence (6)
"askri 5 me house"          ASK     Askari 5 @ 0.56  (typo, below threshold)
"house near garden east"    NO MATCH → pivot
"2 bed in bahria town"      NO MATCH → pivot
```

An A/B test showed **LLM-generated aliases add nothing** over rules + the
3-entry dictionary (both 12/13). The LLM belongs at the *message* level
(understanding Roman Urdu, extracting slots), not in the gazetteer. It also
transliterates Urdu script during extraction, so the resolver only ever sees
Roman input.

### New locations onboard themselves

The 2026-09-03 run brought one: `11579 — Navy Housing Scheme Karsaz`.

```
1. Nightly diff finds a location_id not in `locations`
2. Breadcrumb supplies name + parent chain → INSERT with ltree path
3. Rules generate aliases  (karsaz, navy, navy housing scheme karsaz, …)
4. Guards applied
5. Geocode via Nominatim (free, OSM) — optional, never blocking
```

Zero human involvement.

### "Near <unknown place>"

`resolve_location` returns NO MATCH → the agent says what it *does* have:

> "We don't have anything in Garden East. Our properties are mainly in Malir
> Cantt (Askari 5 & 6), Gulistan-e-Jauhar, DHA Phase 8 and Gulshan-e-Iqbal.
> Should I show you those?"

Measured distances confirm this is honest, not a cop-out — nearest inventory to
Garden East is 7.3 km. For a lead-qualification agent this beats a fake
"nearby" match and still captures the preference.

**"Near park" is not a geo query** — it is a listing attribute
(`park_facing`, `near_mosque`), extracted from the title.

---

## 7. Intents — four, not three

| Intent | Signal | Action | Inventory |
|---|---|---|---|
| **buy** | *chahiye, dikhao, kharidna* | 🔍 search `purpose='sale'` | 55 |
| **rent** (tenant) | *kiraye pe chahiye, rent pe lena* | 🔍 search `purpose='rent'` | **2** |
| **sell** (owner) | *bechna hai* + **mera** | 📝 lead only | n/a |
| **rent_out** (landlord) | *kiraye pe **dena** hai* | 📝 lead only | n/a |

The bottom two **never touch `listings`**. A seller message is new inventory for
the agency — arguably the most valuable message the bot can receive. Replying
"no properties found" to a seller throws away the best lead of the day.

**Disambiguating "rent"** — two reliable tells:
- **verb**: *dena* (give) = supply → lead; *lena/chahiye* (take/want) = demand → search
- **possessive**: *mera ghar*, *my flat*, *I have* → supply side

### ⚠️ `purpose` is mandatory, never defaulted

All 57 listings share one price column. The single cheapest "house" in the
whole catalogue is the **rental**, in Askari 6:

```
53934091   rent   135,000/month   house   5 bed   ← cheapest "house"
50815130   sale   70,000,000      house   4 bed   ← real cheapest for sale
```

A query missing `purpose` offers a customer a 5-bed house for 1.35 lakh —
off by ~518×. 55 of 57 being for sale makes defaulting *tempting* and *wrong*.

Use price magnitude as a hint to **ask a sharper question**, not to decide
silently. One line on WhatsApp removes all doubt.

---

## 8. Known data issues

| Issue | Detail | Handling |
|---|---|---|
| `price.display` polluted | All 57 contain `"Bath(s) 6"`, installment plans, dev fees | `price_pkr` parses correctly; ignore the rest of the string |
| Descriptions contain wrong prices | `47381834` prose says 97,500,000; real is 107,500,000 | **Never read price from description** |
| Titles change between runs | `50535334`: *"West Open"* → *"East Open"* (a correction) | **Re-extract features every run**, not on insert only |
| Nulls | 2 listings no bedrooms, 6 no bathrooms | Nullable columns; never filter them out silently |
| URL suffix varies | Some URLs end `-2.html` / `-4.html`, not `-1.html` | Parse `-(\d+)-(\d+)-\d+\.html$` — a hardcoded `-1` silently orphans listings |

---

## 9. Daily runbook

```
1. Scrape fresh into data/raw/<date>/ with --media-store data/media-store
2. Validate:  node validate-zameen-data.mjs --input-dir data/raw/<date> \
                --require-downloaded-media
3. ETL: re-derive typed rows from raw → upsert Postgres
     present → upsert, update last_seen_at, append price_history on change
     absent  → is_active = false        (NEVER DELETE)
     new location_id → onboard (§6)
     re-extract features for every listing
4. Retain raw snapshots; prune after N days
```

**Never `DELETE` a listing.** `leads.matched_listing_ids` references it, and if
it relists you keep `first_seen_at` and the price trail. Searches add
`AND is_active = true`.

---

## 10. Evaluation

Measure **execution accuracy** — did the tool return the right listing IDs? —
not answer similarity. A reply can read perfectly while being wrong.

- 60–100 gold `(query → expected listing IDs)` pairs, weighted to Roman Urdu
- **Track empty-result rate as a first-class metric.** A false "nothing found"
  is the failure that kills a WhatsApp agent
- Test intent classification directly on the hard pairs:

```
"flat rent pe chahiye"    → RENT      (search)
"flat rent pe dena hai"   → RENT_OUT  (lead)
"ghar bechna hai"         → SELL      (lead)
"mera plot hai bahria me" → SELL      (possessive is the tell)
```

At 57 listings every case can be hand-verified. Do it while that is cheap.

---

## 11. Open decisions

1. **Rentals are effectively an empty product** — 2 listings. Either the agency
   starts listing rentals, or the bot treats every rent query as pure lead
   capture. The client should decide knowingly.
2. **Seller leads need a human SLA.** The bot captures in 60 seconds; if nobody
   calls for three days the lead is dead anyway.
3. **`data/data/` double nesting** — the npm `scrape` script writes to
   `data/data`. Worth flattening to `data/raw/<date>` when adopting the runbook.
