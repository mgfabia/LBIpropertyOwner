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
scrape_tax_board.py    # Ocean County Tax Board scraper (search + detail pages)
load_to_supabase.py    # Load tax board JSONL into Supabase bronze layer
parse_sr1a.py          # Download, filter, parse NJ Treasury SR1A sales files for LBI
load_sr1a_to_supabase.py # Load SR1A JSONL into Supabase bronze layer
sql/                   # DDL for Supabase tables (version controlled)
docs/                  # Data source documentation
samples/               # Raw sample data files (SR1A fixed-width)
data/                  # Scraper output (JSONL, CSV) — gitignored
.venv/                 # Python virtual environment
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
```

**Known join issue**: Long Beach Twp (1518) uses a section-based sub-block system in the tax board (`1.01`, `1.15`, etc.) while SR1A uses the state's integer blocks (`5`, `10`, `20`). This causes ~485 orphan SR1A records that don't match `bronze_tax_board`. Other municipalities match at 91–99%. Resolution requires a block crosswalk built from the Cadastral layer at the silver tier.

Requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env` (see `.env.example`).

## Style and conventions

- Python 3.13, dependencies managed via `.venv` (requests, beautifulsoup4, lxml, supabase, python-dotenv)
- Output format: JSONL (one JSON record per line) as primary, CSV as export
- Scraper uses 1-second default delay between requests; configurable via `--delay`
- Progress tracked in `data/tax_board_progress.json` for resume capability
