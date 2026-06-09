-- Bronze layer: raw ingestion from NJ Treasury SR1A sales files
-- One row per sale transaction (a parcel can have multiple sales).
-- Upsert on serial_number; local JSONL files are the historical archive.

CREATE TABLE bronze_sr1a_sales (
    -- Primary key: unique per sale event
    serial_number       TEXT PRIMARY KEY,

    -- Parcel identity (join key to bronze_tax_board / bronze_cadastral)
    pams_pin            TEXT NOT NULL,
    county_code         TEXT NOT NULL,
    district_code       TEXT NOT NULL,
    municipality_code   TEXT NOT NULL,
    block               TEXT NOT NULL,
    lot                 TEXT NOT NULL,

    -- Sale details
    property_location   TEXT,
    reported_sale_price NUMERIC,
    verified_sale_price NUMERIC,
    usable_code         TEXT,
    non_usable_reason   TEXT,
    realty_transfer_fee NUMERIC,
    sales_ratio         NUMERIC,

    -- Assessment at time of sale
    total_assessment    NUMERIC,
    assessed_land_value NUMERIC,
    assessed_bldg_value NUMERIC,
    assessed_total_value NUMERIC,

    -- Deed info
    deed_book           TEXT,
    deed_page           TEXT,
    deed_date           TEXT,
    date_recorded       TEXT,

    -- Grantor/Grantee (blank per Daniel's Law, stored for completeness)
    grantor_name        TEXT,
    grantee_name        TEXT,
    grantee_city_state  TEXT,

    -- Property characteristics
    property_class      TEXT,
    assess_year         TEXT,
    year_built          INTEGER,
    living_space        INTEGER,

    -- Full source record
    raw_record          JSONB NOT NULL,

    -- Housekeeping
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_file         TEXT
);

CREATE INDEX idx_sr1a_pams_pin ON bronze_sr1a_sales (pams_pin);
CREATE INDEX idx_sr1a_muni_code ON bronze_sr1a_sales (municipality_code);
CREATE INDEX idx_sr1a_deed_date ON bronze_sr1a_sales (deed_date);
CREATE INDEX idx_sr1a_usable ON bronze_sr1a_sales (usable_code);
CREATE INDEX idx_sr1a_sale_price ON bronze_sr1a_sales (verified_sale_price);
CREATE INDEX idx_sr1a_raw_gin ON bronze_sr1a_sales USING GIN (raw_record);
