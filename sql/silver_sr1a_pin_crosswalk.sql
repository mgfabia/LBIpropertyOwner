-- Silver layer: SR1A pams_pin crosswalk
--
-- Resolves the SR1A pams_pin encoding mismatch for Long Beach Twp (1518).
--
-- Problem: The NJ Treasury SR1A fixed-width files encode LBT parcels with
-- block=00001, lot=00001, and the section identifier in block_suffix. The
-- block_suffix is NOT unique across base blocks (e.g., suffix 01 maps to 15
-- different base blocks like 1.01, 10.01, 11.01, etc.), so there is no
-- deterministic formula to reconstruct the correct pams_pin from SR1A fields.
--
-- Solution: Use property_location (street address) to join SR1A orphan records
-- to bronze_cadastral, which has the correct section-based pams_pin format.
-- This resolves ~95% of 1518 SR1A sales. The remaining ~5% are either:
--   - property_location = 'X' (unknown address in source data)
--   - Address format mismatches (unit suffixes like U-1, C1, compound ranges)
--
-- Non-1518 municipalities pass through with their original pams_pin, which
-- joins correctly to both bronze_tax_board and bronze_cadastral at 91-99%.
--
-- Populated by build_sr1a_crosswalk.py. Upsert on serial_number.

CREATE TABLE silver_sr1a_pin_crosswalk (
    serial_number       TEXT PRIMARY KEY,
    sr1a_pams_pin       TEXT NOT NULL,
    resolved_pams_pin   TEXT NOT NULL,
    municipality_code   TEXT NOT NULL,
    property_location   TEXT,
    resolution_method   TEXT NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sr1a_xwalk_resolved_pin ON silver_sr1a_pin_crosswalk (resolved_pams_pin);
CREATE INDEX idx_sr1a_xwalk_method ON silver_sr1a_pin_crosswalk (resolution_method);
CREATE INDEX idx_sr1a_xwalk_muni ON silver_sr1a_pin_crosswalk (municipality_code);

ALTER TABLE silver_sr1a_pin_crosswalk ENABLE ROW LEVEL SECURITY;
