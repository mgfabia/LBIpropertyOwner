-- Bronze layer: raw ingestion from Ocean County Clerk public search portal.
-- One row per deed book/page document looked up (output of scrape_clerk.py).
-- Keyed on the composite (deed_book, deed_page); upsert on re-load.
--
-- Each deed names 2+ parties (grantors + grantees). The party role is carried
-- in the source `party_code` field: '*' = grantor (seller), '' = grantee
-- (buyer = current owner). This was verified against the clerk document detail
-- view (deed 19596/398: party_code '*' parties matched the site's Grantor list,
-- '' matched Grantee) and holds as a both-roles-present split on 15,260 of
-- 15,261 deeds. The loader extracts these into `grantors` and `grantees`.
-- `grantees` of a parcel's deed of record = the current owner(s). `parties` is
-- the deduplicated union of all names. See CLAUDE.md.
--
-- A deed can cover more than one parcel, so `pams_pins` is an array. Join to
-- silver_parcels / bronze_cadastral by unnesting pams_pins, or match a parcel's
-- deed_book/deed_page back to this table.

CREATE TABLE bronze_clerk_deeds (
    deed_book       TEXT NOT NULL,
    deed_page       TEXT NOT NULL,
    pams_pins       TEXT[] NOT NULL DEFAULT '{}',
    parties         TEXT[] NOT NULL DEFAULT '{}',
    grantors        TEXT[] NOT NULL DEFAULT '{}',
    grantees        TEXT[] NOT NULL DEFAULT '{}',
    result_count    INTEGER NOT NULL DEFAULT 0,
    has_results     BOOLEAN NOT NULL DEFAULT FALSE,
    error           TEXT,
    scraped_at      TEXT,
    raw_record      JSONB NOT NULL,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (deed_book, deed_page)
);

-- GIN index on pams_pins for array-containment joins (e.g. pams_pins @> ARRAY['1502_10_10'])
CREATE INDEX idx_bronze_clerk_deeds_pams_pins ON bronze_clerk_deeds USING GIN (pams_pins);
-- GIN index on parties for owner-name search (e.g. parties @> ARRAY['MAKI WILLIAM M'])
CREATE INDEX idx_bronze_clerk_deeds_parties ON bronze_clerk_deeds USING GIN (parties);
-- GIN index on grantees (current owners) for owner lookups
CREATE INDEX idx_bronze_clerk_deeds_grantees ON bronze_clerk_deeds USING GIN (grantees);
CREATE INDEX idx_bronze_clerk_deeds_has_results ON bronze_clerk_deeds (has_results);
CREATE INDEX idx_bronze_clerk_deeds_raw_gin ON bronze_clerk_deeds USING GIN (raw_record);

-- Row Level Security: lock table to the service role only (loaders use the
-- service role key, which bypasses RLS). No policies = no anon/public access.
ALTER TABLE bronze_clerk_deeds ENABLE ROW LEVEL SECURITY;
