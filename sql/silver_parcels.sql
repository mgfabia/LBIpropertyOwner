-- Silver layer: canonical unified parcel table
--
-- Merges bronze_tax_board and bronze_cadastral into one row per pams_pin
-- with full outer join semantics (includes parcels from either source).
-- Adds derived ownership classification (absentee/resident, owner state).
--
-- Does NOT include raw_record (that's what bronze is for).
-- Does NOT include SR1A sale summaries (join via silver_sr1a_pin_crosswalk).
--
-- Populated by build_silver_parcels.py. Upsert on pams_pin.

CREATE TABLE silver_parcels (
    -- Primary key: NJ universal parcel key
    pams_pin            TEXT PRIMARY KEY,

    -- Identity
    municipality_code   TEXT NOT NULL,
    municipality_name   TEXT,
    block               TEXT NOT NULL,
    lot                 TEXT NOT NULL,
    qualifier           TEXT,
    is_condo_unit       BOOLEAN GENERATED ALWAYS AS (qualifier IS NOT NULL) STORED,

    -- Location
    property_location   TEXT,
    geom                GEOMETRY(Polygon, 4326),

    -- Ownership proxy (derived from mailing address)
    mailing_address         TEXT,
    mailing_city_state_raw  TEXT,
    mailing_city            TEXT,
    mailing_state           TEXT,
    mailing_zip             TEXT,
    is_absentee             BOOLEAN,
    is_nj_resident          BOOLEAN,

    -- Owner names (from Ocean County Clerk deeds; Daniel's Law workaround).
    -- Sourced from bronze_clerk_deeds by matching the parcel's deed of record.
    -- NULL where no deed was found. See build_silver_parcels.populate_owner_names.
    owner_names             TEXT[],   -- grantees of the deed = current owner(s)
    owner_grantors          TEXT[],   -- grantors of the deed = prior owner(s)

    -- Assessment values
    prop_class          TEXT,
    land_value          NUMERIC,
    improvement_value   NUMERIC,
    net_value           NUMERIC,
    last_year_taxes     NUMERIC,
    exemption_code      TEXT,

    -- Classification and use
    prop_use            TEXT,
    bldg_class          TEXT,
    bldg_desc           TEXT,
    land_desc           TEXT,
    calc_acre           DOUBLE PRECISION,
    zone                TEXT,

    -- Building characteristics
    year_built          INTEGER,
    sfla                INTEGER,
    bedrooms            INTEGER,
    bathrooms           INTEGER,
    building_type       TEXT,
    stories             TEXT,
    dwell               SMALLINT,

    -- Deed / last sale (from assessment records, NOT SR1A)
    deed_book           TEXT,
    deed_page           TEXT,
    deed_date           TEXT,
    sale_price          NUMERIC,
    sales_code          TEXT,

    -- Metadata
    source_tables       TEXT[] NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_silver_parcels_muni ON silver_parcels (municipality_code);
CREATE INDEX idx_silver_parcels_state ON silver_parcels (mailing_state);
CREATE INDEX idx_silver_parcels_absentee ON silver_parcels (is_absentee);
CREATE INDEX idx_silver_parcels_nj_resident ON silver_parcels (is_nj_resident);
CREATE INDEX idx_silver_parcels_prop_class ON silver_parcels (prop_class);
CREATE INDEX idx_silver_parcels_geom ON silver_parcels USING GIST (geom);
CREATE INDEX idx_silver_parcels_owner_names ON silver_parcels USING GIN (owner_names);
CREATE INDEX idx_silver_parcels_net_value ON silver_parcels (net_value);
CREATE INDEX idx_silver_parcels_qualifier ON silver_parcels (qualifier) WHERE qualifier IS NOT NULL;

ALTER TABLE silver_parcels ENABLE ROW LEVEL SECURITY;
