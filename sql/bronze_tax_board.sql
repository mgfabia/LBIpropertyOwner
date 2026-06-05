-- Bronze layer: raw ingestion from Ocean County Tax Board
-- One row per property (block + lot + qualifier).
-- Upsert on pams_pin; local JSONL files are the historical archive.

CREATE TABLE bronze_tax_board (
    -- Primary key: NJ universal parcel key
    -- Format: {muni_code}_{block}_{lot} or {muni_code}_{block}_{lot}_{qualifier}
    pams_pin          TEXT PRIMARY KEY,

    -- Source identity
    detail_id         TEXT NOT NULL,
    municipality_code TEXT NOT NULL,
    municipality      TEXT,
    block             TEXT NOT NULL,
    lot               TEXT NOT NULL,
    qualifier         TEXT,

    -- Location
    property_location TEXT,

    -- Ownership proxy (Daniel's Law — no owner names available)
    mailing_address    TEXT,
    mailing_city_state TEXT,

    -- Assessment values (cleaned to numeric)
    prop_class        TEXT,
    land_value        NUMERIC,
    improvement_value NUMERIC,
    net_value         NUMERIC,
    last_year_taxes   NUMERIC,
    exemption_code    TEXT,

    -- Key building characteristics
    year_built        INTEGER,
    sfla              INTEGER,
    bedrooms          INTEGER,
    bathrooms         INTEGER,

    -- Sale info
    sale_price        NUMERIC,
    deed_date         TEXT,

    -- Zoning
    zone              TEXT,

    -- Full source record
    raw_record        JSONB NOT NULL,

    -- Housekeeping
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_file       TEXT
);

CREATE INDEX idx_bronze_tax_board_muni_code ON bronze_tax_board (municipality_code);
CREATE INDEX idx_bronze_tax_board_prop_class ON bronze_tax_board (prop_class);
CREATE INDEX idx_bronze_tax_board_mailing ON bronze_tax_board (mailing_city_state);
CREATE INDEX idx_bronze_tax_board_detail_id ON bronze_tax_board (detail_id);
CREATE INDEX idx_bronze_tax_board_zone ON bronze_tax_board (zone);
CREATE INDEX idx_bronze_tax_board_raw_gin ON bronze_tax_board USING GIN (raw_record);
