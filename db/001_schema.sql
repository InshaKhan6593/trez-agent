-- Trez WhatsApp property agent — schema
-- Runs automatically on first container start (docker-entrypoint-initdb.d).
-- Postgres must always be rebuildable from data/raw/<date>/ with no network calls.

CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- ---------------------------------------------------------------------------
-- Provenance: which raw snapshot produced the current rows
-- ---------------------------------------------------------------------------
CREATE TABLE ingest_runs (
  id            bigserial PRIMARY KEY,
  raw_dir       text        NOT NULL,          -- e.g. 'data/raw/2026-09-03'
  scraped_on    date        NOT NULL,
  listing_count int         NOT NULL,
  rejected_count int        NOT NULL DEFAULT 0,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  notes         text
);

-- ---------------------------------------------------------------------------
-- Location tree, built from Zameen's own breadcrumb ids
-- ---------------------------------------------------------------------------
CREATE TABLE locations (
  id             int    PRIMARY KEY,           -- Zameen's location id (6655, 12243…)
  name           text   NOT NULL,              -- property-type suffix stripped
  parent_id      int    REFERENCES locations(id),
  path           ltree  NOT NULL,              -- '2.525.6654.6655'
  depth          int    NOT NULL,
  lat            double precision,             -- nullable; geocoding never blocks
  lon            double precision,
  geo_confidence text   NOT NULL DEFAULT 'missing'
                 CHECK (geo_confidence IN ('ok','low','missing')),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX locations_path_gist ON locations USING gist (path);
CREATE INDEX locations_parent_idx ON locations (parent_id);

-- Every spelling a customer might type. Generated, never hand-written.
CREATE TABLE location_aliases (
  alias_norm  text NOT NULL,
  location_id int  NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  source      text NOT NULL CHECK (source IN ('rule','abbr','query_log')),
  PRIMARY KEY (alias_norm, location_id)
);
CREATE INDEX location_aliases_trgm ON location_aliases USING gin (alias_norm gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Listings
-- ---------------------------------------------------------------------------
CREATE TABLE listings (
  id            bigint PRIMARY KEY,            -- Zameen listing id
  purpose       text   NOT NULL CHECK (purpose IN ('sale','rent')),
  price_pkr     bigint NOT NULL CHECK (price_pkr > 0),
  -- Makes the rent/sale price collision structurally impossible:
  -- the cheapest "house" in the catalogue is a 1.35 lakh/month RENTAL.
  price_period  text
                CHECK ((purpose = 'sale' AND price_period IS NULL)
                    OR (purpose = 'rent' AND price_period IN ('monthly','yearly'))),
  property_type text   NOT NULL CHECK (property_type IN ('house','flat','plot','other')),
  area_sqyd     numeric,
  bedrooms      int,
  bathrooms     int,
  location_id   int    REFERENCES locations(id),
  title         text   NOT NULL,
  description   text,
  url           text   NOT NULL,
  is_active     boolean NOT NULL DEFAULT true,
  first_seen_on date   NOT NULL,
  last_seen_on  date   NOT NULL,
  ingest_run_id bigint REFERENCES ingest_runs(id),
  CHECK (last_seen_on >= first_seen_on)
);
CREATE INDEX listings_search_idx  ON listings (purpose, property_type, price_pkr)
  WHERE is_active;
CREATE INDEX listings_location_idx ON listings (location_id) WHERE is_active;
CREATE INDEX listings_beds_idx     ON listings (bedrooms)    WHERE is_active;

-- Re-extracted on EVERY run: titles get corrected upstream
-- (50535334 went "West Open" -> "East Open" between two scrapes).
CREATE TABLE listing_features (
  listing_id bigint NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  feature    text   NOT NULL,
  PRIMARY KEY (listing_id, feature)
);
CREATE INDEX listing_features_feature_idx ON listing_features (feature);

-- asset_id is Zameen's stable id and the file stem in the shared media store.
CREATE TABLE listing_media (
  listing_id bigint NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  asset_id   text   NOT NULL,
  seq        int    NOT NULL,
  file       text   NOT NULL,                  -- '302651164.jpeg'
  source_url text,
  PRIMARY KEY (listing_id, asset_id)
);
CREATE INDEX listing_media_seq_idx ON listing_media (listing_id, seq);

-- One row per observed price change. 9 increases in 13 days on this catalogue.
CREATE TABLE price_history (
  listing_id bigint NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  seen_on    date   NOT NULL,
  price_pkr  bigint NOT NULL,
  PRIMARY KEY (listing_id, seen_on)
);

-- ---------------------------------------------------------------------------
-- Leads — the actual product
-- ---------------------------------------------------------------------------
CREATE TABLE leads (
  id                  bigserial PRIMARY KEY,
  phone               text NOT NULL,
  intent              text NOT NULL
                      CHECK (intent IN ('buy','rent','sell','rent_out')),
  property_type       text,
  location_id         int REFERENCES locations(id),
  location_text       text,                    -- what they actually typed
  min_price           bigint,
  max_price           bigint,
  bedrooms            int,
  area_sqyd           numeric,
  expected_price      bigint,                  -- sell / rent_out side
  matched_listing_ids bigint[],
  had_no_inventory    boolean,                 -- inventory gap report for the client
  status              text NOT NULL DEFAULT 'new',
  notes               text,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX leads_phone_idx  ON leads (phone);
CREATE INDEX leads_intent_idx ON leads (intent, created_at DESC);

-- Every NO MATCH / ASK is a real customer telling us which alias is missing.
-- This is the alias feedback loop; better evidence than guessing in advance.
CREATE TABLE location_query_log (
  id            bigserial PRIMARY KEY,
  raw_text      text NOT NULL,
  resolved_id   int REFERENCES locations(id),
  outcome       text NOT NULL CHECK (outcome IN ('accept','ask','no_match')),
  top_score     real,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Containment helper. The ONE place ltree is used, so a port is one function.
-- descendants_of(6655) -> every location under Askari 5, itself included.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION descendants_of(p_location_id int)
RETURNS TABLE (id int)
LANGUAGE sql STABLE AS $$
  SELECT l.id
  FROM locations l, locations root
  WHERE root.id = p_location_id
    AND l.path <@ root.path;
$$;

-- Convenience view for the agent: everything it needs in one row.
CREATE VIEW active_listings AS
SELECT
  l.*,
  loc.name  AS location_name,
  loc.path  AS location_path,
  (SELECT array_agg(f.feature ORDER BY f.feature)
     FROM listing_features f WHERE f.listing_id = l.id) AS features,
  (SELECT count(*) FROM listing_media m WHERE m.listing_id = l.id) AS photo_count
FROM listings l
LEFT JOIN locations loc ON loc.id = l.location_id
WHERE l.is_active;
