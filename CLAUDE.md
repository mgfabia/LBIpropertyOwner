# LBI Property Owner Platform

## What this project is

A data platform to determine property ownership on Long Beach Island, NJ. LBI spans 6 municipalities in Ocean County: Barnegat Light, Beach Haven, Harvey Cedars, Long Beach Twp, Ship Bottom, and Surf City.

## Key constraint: Daniel's Law

NJ P.L. 2020, c. 125 redacts owner names from all state-level data sources. OWNER_NAME is blank on ArcGIS, MOD-IV, and SR1A files. The Ocean County Tax Board website also removed owner names. Owner mailing addresses ARE still available from the tax board — this is the best programmatic proxy for ownership identity (resident vs. absentee, in-state vs. out-of-state). Actual owner names require either the Ocean County Clerk deed records (currently broken) or an OPRA request.

## Data sources

See `docs/data-sources-inventory.md` for the full inventory with URLs, field lists, and access details. The short version:

- **NJ Cadastral MapServer** — parcel geometry, addresses, assessments, deed info. ArcGIS REST API, no auth.
- **NJ Treasury SR1A** — sale history. Fixed-width bulk files. Sample in `samples/lbi_sales_2024.txt`.
- **NJ Treasury MOD-IV** — bulk assessment data. Fixed-width bulk files.
- **Ocean County Tax Board** — mailing addresses, building details, assessment history. Scraped via `scrape_tax_board.py`.
- **FEMA / USGS / NOAA** — flood zones, elevation, sea level rise. All free APIs.
- **NJ DCA Socrata** — construction permits back to 1991.

## Join key

`PAMS_PIN = {muni_code}_{block}_{lot}` is the universal key across all NJ parcel systems. Municipality codes: 1502 (Barnegat Light), 1504 (Beach Haven), 1510 (Harvey Cedars), 1518 (Long Beach Twp), 1529 (Ship Bottom), 1532 (Surf City).

## Project structure

```
scrape_tax_board.py      # Ocean County Tax Board scraper (search + detail pages)
load_to_supabase.py      # Load tax board JSONL into Supabase bronze layer
parse_sr1a.py            # Download, filter, parse NJ Treasury SR1A sales files for LBI
load_sr1a_to_supabase.py # Load SR1A JSONL into Supabase bronze layer
build_sr1a_crosswalk.py  # Build silver_sr1a_pin_crosswalk (resolves 1518 pams_pin)
build_silver_parcels.py  # Build silver_parcels (unified parcel table with ownership)
scrape_clerk.py          # Ocean County Clerk deed scraper (owner names via Playwright)
load_clerk_to_supabase.py # Load clerk deed JSONL into Supabase bronze layer
db.py                    # Shared Postgres connection via IPv4 pooler (for raw SQL)
apply_sql.py             # Run a .sql file (DDL etc.) against Supabase via the pooler
sql/                     # DDL for Supabase tables (version controlled)
docs/                    # Data source documentation
samples/                 # Raw sample data files (SR1A fixed-width)
data/                    # Scraper output (JSONL, CSV) — gitignored
.venv/                   # Python virtual environment
```

## Working with the scraper

Activate the venv before running:
```
source .venv/bin/activate
```

The scraper has two phases: discovery (enumerate block/lots via search) and detail (fetch each property page). It supports `--resume` to continue interrupted runs and `--export-csv` to convert JSONL output. See `scrape_tax_board.py` docstring for full usage.

The tax board caps search results at 100, so the scraper searches block-by-block. Full LBI is ~10,000-15,000 parcels.

## Supabase bronze layer

Data is scraped locally to JSONL, then loaded into a Supabase Postgres database (separate project from any other Supabase usage). Architecture follows a medallion pattern: bronze (raw ingestion) → silver (joined/cleaned) → gold (analytics).

**Bronze tables**:

- `bronze_tax_board` — one row per property, keyed on `pams_pin`. Schema DDL in `sql/bronze_tax_board.sql`. Stores parsed typed columns for key fields plus `raw_record` JSONB preserving the full source record. Upsert on re-load (latest only; local JSONL is the historical archive).

- `bronze_sr1a_sales` — one row per sale transaction, keyed on `serial_number`. Schema DDL in `sql/bronze_sr1a_sales.sql`. A parcel can have multiple sales. Joins to `bronze_tax_board` via `pams_pin`. Source files: NJ Treasury SR1A bulk downloads (2020–2026 YTD).

- `bronze_cadastral` — one row per parcel with polygon geometry, keyed on `pams_pin`. Schema DDL in `sql/bronze_cadastral.sql`. From NJ Cadastral ArcGIS REST API. Overlaps 96% with `bronze_tax_board` on `pams_pin`.

- `bronze_clerk_deeds` — one row per deed document, keyed on composite `(deed_book, deed_page)`. Schema DDL in `sql/bronze_clerk_deeds.sql`. **This is the Daniel's Law workaround**: county deed records are not redacted, so they carry owner names the state sources blank. Scraped from the Ocean County Clerk portal by `scrape_clerk.py` (looks up each parcel's deed of record by book/page from `silver_parcels`). 17,269 deeds; 15,609 distinct parcels have ≥1 owner name. The source `party_code` field encodes role: `*` = grantor (seller), `''` = grantee (buyer = **current owner**) — verified against the clerk document detail view, both roles present on 15,260 of 15,261 deeds. The loader extracts `grantors[]`, `grantees[]` (current owners), and `parties[]` (union) as array columns alongside `raw_record`. Join to parcels via the `pams_pins[]` array, or match a parcel's `deed_book`/`deed_page` back to this table.

**Silver layer**:

- `silver_sr1a_pin_crosswalk` — one row per SR1A sale, keyed on `serial_number`. DDL in `sql/silver_sr1a_pin_crosswalk.sql`. Maps each SR1A `serial_number` to a `resolved_pams_pin` that joins correctly to `bronze_tax_board` / `bronze_cadastral`. Uses address-based matching via `bronze_cadastral.prop_loc` for 1518 orphans. Resolution rate: 97.4% overall (passthrough for non-1518, ~95% address-resolved for 1518). Populated by `build_sr1a_crosswalk.py`; rebuild after reloading bronze data.

- `silver_parcels` — one row per parcel (19,958 rows), keyed on `pams_pin`. Schema DDL in `sql/silver_parcels.sql`. Merges `bronze_tax_board` + `bronze_cadastral` via full outer join (18,278 both sources, 707 tax-board-only, 973 cadastral-only). Includes PostGIS geometry, normalized mailing address, and derived ownership columns (`is_absentee`, `mailing_state`, `is_nj_resident`). Tax board wins for assessment/building data; cadastral wins for spatial/classification data. Populated by `build_silver_parcels.py`; rebuild after reloading bronze data. Joins to SR1A sales via `silver_sr1a_pin_crosswalk.resolved_pams_pin`.

**Loading tax board data**:
```
source .venv/bin/activate
python load_to_supabase.py                    # load default file
python load_to_supabase.py --dry-run          # transform only, no DB writes
python load_to_supabase.py --file data/other.jsonl
```

**SR1A sales pipeline** (two-phase: parse then load):
```
source .venv/bin/activate
python parse_sr1a.py                          # download all years, parse to JSONL
python parse_sr1a.py --years 2024 2025        # specific years only
python parse_sr1a.py --skip-download          # re-parse already-downloaded ZIPs
python load_sr1a_to_supabase.py               # load combined JSONL to Supabase
python load_sr1a_to_supabase.py --file data/sr1a/sr1a_2024.jsonl  # specific year
python build_sr1a_crosswalk.py               # rebuild silver crosswalk from bronze
python build_sr1a_crosswalk.py --dry-run     # compute stats without writing
```

**Clerk deed pipeline** (two-phase: scrape then load):
```
source .venv/bin/activate
python scrape_clerk.py                        # scrape deed records (~7h full run; --resume to continue)
python load_clerk_to_supabase.py             # load deed JSONL to bronze_clerk_deeds
python load_clerk_to_supabase.py --dry-run   # transform + coverage stats, no DB writes
```

**Silver parcels pipeline** (rebuild after any bronze data changes):
```
source .venv/bin/activate
python build_silver_parcels.py               # full rebuild from bronze tables
python build_silver_parcels.py --dry-run     # compute merge stats without writing
```

**1518 join issue (resolved)**: Long Beach Twp (1518) uses a section-based sub-block system (`1.01`, `1.47`, `20.178`) while the SR1A fixed-width files encode all 1518 parcels with `block=00001` and put the section number in a `block_suffix` field that is NOT unique across base blocks (suffix `01` maps to 15 different base blocks). This made a deterministic formula impossible. Resolution: `silver_sr1a_pin_crosswalk` matches SR1A orphan records to `bronze_cadastral` via `property_location` (street address), resolving ~95% of 1518 sales (4,095 of 4,330 orphan records). The remaining ~5% are `property_location='X'` or address-format mismatches. Non-1518 municipalities have ~99 orphan pins across all 5 munis (mostly historical parcels that were consolidated/renumbered).

Requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env` (see `.env.example`).

**Database connections**: Data loads use supabase-py's REST client (HTTPS, IPv4) and need only the service-role key. Raw SQL — DDL (`apply_sql.py`) and PostGIS UPDATEs (`build_silver_parcels.py`) — can't go through REST and needs a real Postgres connection. The *direct* host (`db.<ref>.supabase.co`) is IPv6-only and unreachable from IPv4-only networks, so `db.py` connects via the IPv4 **pooler** (`aws-1-us-east-1.pooler.supabase.com` for this project). Raw SQL therefore also requires `SUPABASE_DB_PASSWORD` (the database password, NOT the service-role key) and `SUPABASE_DB_HOST` in `.env`. The service-role key is an API token, not a Postgres password, and can't authenticate a DB connection; PostgREST also doesn't expose DDL — hence both the pooler and the DB password are needed for table creation.

## Style and conventions

- Python 3.13, dependencies managed via `.venv` (requests, beautifulsoup4, lxml, supabase, python-dotenv, psycopg2-binary)
- Output format: JSONL (one JSON record per line) as primary, CSV as export
- Scraper uses 1-second default delay between requests; configurable via `--delay`
- Progress tracked in `data/tax_board_progress.json` for resume capability
