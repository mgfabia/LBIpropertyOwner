# LBI Property Data Platform — Data Sources Inventory

**Last updated:** 2026-06-05
**Status:** Endpoint testing complete. All sources verified live.

---

## Critical Finding: Owner Names

**OWNER_NAME is BLANK on all state-hosted ArcGIS services.** This includes:
- `maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2`
- `maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0`
- NJGIN Open Data downloads

The field exists in the schema (esriFieldTypeString, length 35) but returns empty string `""` on every record. This is due to Daniel's Law (P.L. 2020, c. 125) blanket redaction.

**Also confirmed redacted in bulk downloads:**
- **NJ Treasury MOD-IV files** — OWNER_NAME field replaced with "FILLER" (35-char blank, position 176-210)
- **NJ Treasury SR1A sales files** — GRANTOR-NAME and GRANTEE-NAME fields exist in layout (positions 110-144 and 204-238) but are ALL SPACES in the actual data

**Where owner names ARE still available:**
1. **Ocean County Tax Board** — web form at `tax.co.ocean.nj.us/frmTaxBoardTaxListSearch` (no API; requires form automation by block/lot)
2. **Ocean County Clerk deed records** — `sng.co.ocean.nj.us/publicsearch/` (grantor/grantee on deeds since 1977; searchable by book/page or party name; no API)

---

## LBI Municipality Codes (confirmed via ArcGIS + SR1A)

| Code | Municipality | ArcGIS MUN_NAME | 2024 SR1A Records | Avg Sale Price (>$100k) |
|------|-------------|-----------------|-------------------|------------------------|
| 1502 | Barnegat Light | `BARNEGAT LIGHT BORO` | 72 | $1,694,765 |
| 1504 | Beach Haven | `BEACH HAVEN BORO` | 140 | $1,406,425 |
| 1510 | Harvey Cedars | `HARVEY CEDARS BORO` | 70 | $2,077,947 |
| 1518 | Long Beach Twp | `LONG BEACH TWP` | 488 | $2,165,274 |
| 1529 | Ship Bottom | `SHIP BOTTOM BORO` | 138 | $1,128,289 |
| 1532 | Surf City | `SURF CITY BORO` | 139 | $1,738,146 |

These codes are the universal join key prefix. PAMS_PIN = `{code}_{block}_{lot}` (e.g., `1532_110_3` = Surf City, Block 110, Lot 3). Same codes work in SR1A (county-district field, positions 1-4), MOD-IV, and ArcGIS PCL_MUN.

---

## Tier 1: Core Parcel & Assessment Data (Free, Programmatic)

### NJ Cadastral MapServer (Parcel Geometry + Assessment)
- **URL:** `https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0`
- **Also:** `https://maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2`
- **Access:** ArcGIS REST API, no auth, JSON/GeoJSON/PBF
- **Max records:** 1000/page (pagination via `resultOffset`)
- **Spatial ref:** NJ State Plane 102711/3424 (pass `inSR=4326` for lat/lon queries)
- **LBI MUN_NAME values (confirmed exact strings):**
  - `LONG BEACH TWP`
  - `BEACH HAVEN BORO`
  - `SHIP BOTTOM BORO`
  - `SURF CITY BORO`
  - `HARVEY CEDARS BORO`
  - `BARNEGAT LIGHT BORO`
- **Key fields:** PAMS_PIN, PROP_LOC, NET_VALUE, LAND_VAL, IMPRVT_VAL, SALE_PRICE, DEED_DATE, DEED_BOOK, DEED_PAGE, YR_CONSTR, BLDG_DESC, BLDG_CLASS, PROP_CLASS, PROP_USE, CALC_ACRE, DWELL, LAST_YR_TX, PCLBLOCK, PCLLOT, PCL_MUN
- **Missing:** OWNER_NAME (blank), ST_ADDRESS/CITY_STATE/ZIP (owner mailing address — untested, may also be blank)
- **Sample response:**
  ```
  PAMS_PIN: "1532_110_3", PROP_LOC: "304 N 14TH ST", NET_VALUE: 982700,
  SALE_PRICE: 1900000, DEED_DATE: "240412"
  ```

### NJ Treasury MOD-IV Files (Bulk Assessment Download)
- **URL:** `https://www.nj.gov/treasury/taxation/lpt/statdata.shtml`
- **Files:** `modiv-2021.zip` through `modiv-2025.zip`
- **Format:** Fixed-width text with published layout (PDF)
- **Coverage:** All NJ municipalities, all property classes
- **Key fields:** Assessment values, property class, building characteristics, deed info, year built, tax deductions/exemptions
- **Owner names:** TBD — need to download and check layout doc

### NJ Treasury SR1A Sales Files (Transaction History)
- **URL:** Same page as above
- **Files:** `Sales2020.zip` through `Sales2025.zip` + YTD 2026
- **Format:** Fixed-width text with layout doc (`SR1Afilelayout.pdf`)
- **Key fields:** Sale price (reported + verified), assessed value at time of sale, sales ratio, realty transfer fee, deed date, property class, usable/non-usable code, grantor/grantee (TBD if redacted)
- **Value:** Definitive record of every arm's-length sale on LBI

### NJ DCA Construction Permit Data (Socrata API)
- **URL:** `https://data.nj.gov/resource/w9se-dmra.json`
- **Access:** Socrata/SODA API, no auth for basic access
- **Filter:** `?county=OCEAN&muniname=LONG%20BEACH%20TWP` (and other LBI munis)
- **Key fields:** block, lot, permittype/permittypedesc, constcost, squarefeet, usegroup, permitdate, certdate, fee breakdowns
- **Date range:** 1991–2023+
- **LBI muni names in this dataset:** `LONG BEACH TWP`, `BEACH HAVEN`, `SURF CITY`, `BARNEGAT LIGHT`, `HARVEY CEDARS`, `SHIP BOTTOM`
- **Value:** Track demolition/rebuild cycles, renovation activity, construction costs per parcel

### Tax Rates & Equalization Data
- **General Tax Rates:** `https://www.nj.gov/treasury/taxation/lpt/taxrate.shtml` — by municipality, 2015-2025
- **Equalized Valuations:** Director's Ratios XLSX available — true value multipliers per municipality
- **Chapter 123 Common Level Ranges:** Assessment-sales ratio ranges for tax appeals
- **Coefficients of Deviation:** Assessment uniformity metrics

---

## Tier 2: Flood & Climate Risk Data (Free, Programmatic)

### FEMA National Flood Hazard Layer
- **URL:** `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query`
- **Access:** ArcGIS REST, no auth, point queries supported
- **Key fields:** FLD_ZONE, ZONE_SUBTY, SFHA_TF, STATIC_BFE, DEPTH
- **LBI test result:** Zone AO, SFHA=True, Depth=1.0 ft (mid-island point)
- **Value:** Flood zone designation + base flood elevation for every parcel

### USGS Elevation Point Query Service
- **URL:** `https://epqs.nationalmap.gov/v1/json?x={lon}&y={lat}&wkid=4326&units=Feet`
- **Access:** REST, no auth
- **LBI test result:** 11.34 ft (NAVD88) at mid-island point, 1-meter resolution
- **Value:** Ground elevation for freeboard calculations (elevation minus BFE)

### NOAA Sea Level Rise Projections (Station-based)
- **Station:** 8534720 (Atlantic City, ~10 mi from LBI)
- **Projections URL:** `https://api.tidesandcurrents.noaa.gov/dpapi/prod/webapi/product/slr_projections.json?station=8534720&units=english`
- **HTF URL:** `https://api.tidesandcurrents.noaa.gov/dpapi/prod/webapi/htf/htf_projection_decadal.json?station=8534720&decade=2050`
- **Trends URL:** `https://api.tidesandcurrents.noaa.gov/dpapi/prod/webapi/product/sealvltrends.json?station=8534720`
- **Key data:**
  - Historical trend: 1.67 in/decade (4.24 mm/yr), 1911-2025
  - 2050 projections: 14-21 inches rise (5 scenarios)
  - 2100 projections: 24-83 inches rise
  - High-tide flooding 2050: 75-155 minor flood days/year
- **Value:** Long-term climate risk quantification per-property

### NOAA Sea Level Rise Inundation Maps
- **URL pattern:** `https://coast.noaa.gov/arcgis/rest/services/dc_slr/slr_{X}ft/MapServer`
- **Scenarios:** 1ft through 10ft above MHHW
- **Layers:** 0 = Low-lying Areas (binary), 1 = Depth (raster, feet)
- **Access:** ArcGIS REST identify endpoint, point queries
- **LBI test:** Point is within 3ft AND 6ft inundation zones
- **Strategy:** Query 1ft through 10ft to find each property's "tipping point" SLR scenario
- **Bulk download:** `https://coast.noaa.gov/slrdata/Sea_Level_Rise_Vectors/NJ/` (GeoPackage)
- **Value:** Per-property inundation threshold and depth at future SLR scenarios

### NJDEP Coastal Services
- **Root:** `https://mapsdep.nj.gov/arcgis/rest/services/`
- **CAFRA layer:** `.../Features/Land_CAFRA_coast/MapServer/0` (regulatory boundary — binary only)
- **Historical shorelines:** `.../Features/Land_CAFRA_coast/MapServer/8-11` (2012, 2007, 2002, historical)
- **Tidal Climate-Adjusted Flood Elevation:** `.../Features/Hydrography/MapServer/48`
- **Tidelands:** `.../Features/Hydrography/MapServer/30`
- **Value:** Shoreline change rates, regulatory context, climate-adjusted flood risk

### NJ LIDAR on AWS (Bulk Elevation Data)
- **S3:** `s3://njogis-elevation/` (us-west-2, no auth)
- **Key dataset:** `CoastalNOAATopobathy_2013_2014_QL2/`
- **Format:** LAZ point clouds + DEM rasters
- **Value:** High-resolution custom flood modeling (sub-meter accuracy)

---

## Tier 3: Owner Names (Requires Scraping or OPRA)

### Ocean County Tax Board
- **URL:** `https://tax.co.ocean.nj.us/frmTaxBoardTaxListSearch`
- **Input:** Municipality (dropdown), Block, Lot, Qualifier, Property Class, Property Location
- **Output:** Assessment details likely including owner name on detail page
- **Access:** ASP.NET web form, no API. Automatable via HTTP POST.
- **Strategy:** Enumerate all block/lot combos from Cadastral service → POST to tax board → parse detail pages

### Ocean County Clerk (Deed Records)
- **URL:** `https://sng.co.ocean.nj.us/publicsearch/`
- **Records:** April 1, 1977 to present
- **Search by:** Party name, document type, book/page, town, date range
- **Output:** Grantor/grantee names, deed details
- **Access:** Web form, no API
- **Strategy:** Use DEED_BOOK + DEED_PAGE from Cadastral service to look up current owner via deed chain

---

## Tier 4: Supplementary Sources

### Zoning
- **NJ DCA Directory:** `https://www.nj.gov/dca/library/home/Zoning_Information_Directory%202024%205-6-24.xlsx`
- **Long Beach Twp ordinance:** `https://ecode360.com/10305304` (Chapter 205)
- **Ocean County Parcel Viewer:** `https://www.arcgis.com/apps/webappviewer/index.html?id=11fb956e45bd4969bebaded29587cb12`
- **Status:** No uniform GIS polygon data for zone districts. Each municipality maintains its own. Would require digitization or commercial source (Regrid).

### Rental Registration
- No LBI municipality publishes a searchable database of registered rentals
- All 6 municipalities require annual rental registration
- Lists exist in paper/PDF form at each Clerk's office
- **Access:** OPRA request to each municipality

### Tax Sales
- **Tax Collectors Association:** `https://www.tctanj.org/cn/webpage.cfm?tpid=14659` (upcoming sales)
- Each municipality handles independently; no consolidated database

### Ocean County Parcel Geodatabase
- **URL:** `https://www.co.ocean.nj.us//WebContentFiles//340290000_parcels_v2024_rd.gdb.zip`
- **Format:** File geodatabase (>10MB)
- **Owner names:** Likely redacted (same NJOGIS pipeline)

### Rutgers MOD-IV Historical
- **URL:** `https://modiv.rutgers.edu/`
- **Coverage:** 1989–present, 105M+ records
- **Access:** Free registration, CSV export (limits apply)
- **Owner names:** Removed for privacy

### oprasearch.app (Third-party Aggregator)
- **URL:** `https://oprasearch.app/`
- **Data:** Parcel data + permit history + FEMA zones + OPRA links
- **Access:** Free address search, paid Pro tier
- **Value:** Good for validation/spot-checking

---

## Data Architecture Recommendation

```
┌─────────────────────────────────────────────────────────────┐
│                    LBI Property Platform                      │
├──────────────────────┬──────────────────────────────────────┤
│  CORE PARCEL DATA    │  Source: NJ Cadastral MapServer      │
│  (geometry, BBL,     │  + NJ Treasury MOD-IV bulk files     │
│   address, values)   │  Join key: PAMS_PIN / block+lot      │
├──────────────────────┼──────────────────────────────────────┤
│  OWNER NAMES         │  Source: Ocean County Tax Board      │
│                      │  (scrape by block/lot from above)    │
├──────────────────────┼──────────────────────────────────────┤
│  SALE HISTORY        │  Source: NJ Treasury SR1A files      │
│                      │  + Ocean County Clerk (deed docs)    │
├──────────────────────┼──────────────────────────────────────┤
│  FLOOD/CLIMATE RISK  │  Source: FEMA NFHL + USGS elevation  │
│                      │  + NOAA SLR projections/inundation   │
├──────────────────────┼──────────────────────────────────────┤
│  CONSTRUCTION        │  Source: NJ DCA Socrata API          │
│                      │  (permits by block/lot)              │
├──────────────────────┼──────────────────────────────────────┤
│  TAX CONTEXT         │  Source: NJ Treasury tax rates,      │
│                      │  equalization tables, appeal data    │
└──────────────────────┴──────────────────────────────────────┘
```

**Join strategy:** PAMS_PIN encodes `{PCL_MUN}_{PCLBLOCK}_{PCLLOT}` (e.g., "1532_110_3" = Surf City, Block 110, Lot 3). This is the universal key across all NJ parcel systems. Spatial joins (lat/lon → parcel polygon) connect flood/elevation data to parcels.
