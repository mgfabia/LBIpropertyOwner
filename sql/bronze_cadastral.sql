-- Bronze layer: raw ingestion from NJ Cadastral MapServer (ArcGIS REST API)
-- One row per parcel with polygon geometry.
-- Upsert on pams_pin; re-pull from API anytime.

CREATE EXTENSION IF NOT EXISTS postgis SCHEMA extensions;

CREATE TABLE bronze_cadastral (
    pams_pin        TEXT PRIMARY KEY,
    pcl_mun         TEXT NOT NULL,
    mun_name        TEXT,
    pclblock        TEXT NOT NULL,
    pcllot          TEXT NOT NULL,
    pclqcode        TEXT,
    prop_loc        TEXT,
    prop_class      TEXT,
    prop_use        TEXT,
    bldg_class      TEXT,
    bldg_desc       TEXT,
    land_desc       TEXT,
    calc_acre       DOUBLE PRECISION,
    land_val        INTEGER,
    imprvt_val      INTEGER,
    net_value       INTEGER,
    last_yr_tx      DOUBLE PRECISION,
    sale_price      INTEGER,
    sales_code      TEXT,
    deed_book       TEXT,
    deed_page       TEXT,
    deed_date       TEXT,
    yr_constr       SMALLINT,
    dwell           SMALLINT,
    st_address      TEXT,
    city_state      TEXT,
    zip_code        TEXT,
    geom            GEOMETRY(Polygon, 4326),
    raw_record      JSONB NOT NULL,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_bronze_cadastral_pcl_mun ON bronze_cadastral (pcl_mun);
CREATE INDEX idx_bronze_cadastral_prop_class ON bronze_cadastral (prop_class);
CREATE INDEX idx_bronze_cadastral_geom ON bronze_cadastral USING GIST (geom);
CREATE INDEX idx_bronze_cadastral_raw_gin ON bronze_cadastral USING GIN (raw_record);

-- Row Level Security: lock table to the service role only (loaders use the
-- service role key, which bypasses RLS). No policies = no anon/public access.
ALTER TABLE bronze_cadastral ENABLE ROW LEVEL SECURITY;
