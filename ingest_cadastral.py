#!/usr/bin/env python3
"""
Ingest NJ Cadastral MapServer parcel data directly into Supabase.

Pulls all LBI parcels from the ArcGIS REST API (with geometry) and upserts
into the bronze_cadastral table. No local intermediate files — the API is
fast and idempotent.

Usage:
    python ingest_cadastral.py                  # all 6 LBI municipalities
    python ingest_cadastral.py --muni 1510      # single municipality by code
    python ingest_cadastral.py --dry-run        # fetch and transform, no DB writes
"""

import argparse
import json
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

CADASTRAL_URL = (
    "https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0/query"
)

LBI_MUNICIPALITIES = {
    "1502": "BARNEGAT LIGHT BORO",
    "1504": "BEACH HAVEN BORO",
    "1510": "HARVEY CEDARS BORO",
    "1518": "LONG BEACH TWP",
    "1529": "SHIP BOTTOM BORO",
    "1532": "SURF CITY BORO",
}

OUT_FIELDS = [
    "PAMS_PIN", "PCL_MUN", "PCLBLOCK", "PCLLOT", "PCLQCODE", "MUN_NAME",
    "PROP_CLASS", "PROP_LOC", "PROP_USE", "BLDG_CLASS", "BLDG_DESC",
    "LAND_DESC", "CALC_ACRE", "LAND_VAL", "IMPRVT_VAL", "NET_VALUE",
    "LAST_YR_TX", "SALE_PRICE", "SALES_CODE", "DEED_BOOK", "DEED_PAGE",
    "DEED_DATE", "YR_CONSTR", "DWELL", "ST_ADDRESS", "CITY_STATE",
    "ZIP_CODE", "OWNER_NAME",
]

PAGE_SIZE = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def fetch_parcels(mun_name):
    """Paginate through all parcels for a municipality."""
    offset = 0
    all_features = []

    while True:
        params = {
            "where": f"MUN_NAME='{mun_name}'",
            "outFields": ",".join(OUT_FIELDS),
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",
        }

        for attempt in range(3):
            try:
                resp = requests.get(CADASTRAL_URL, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    log.warning("  Request failed (%s), retrying in %ds", e, wait)
                    time.sleep(wait)
                else:
                    raise

        features = data.get("features", [])
        if not features:
            break

        all_features.extend(features)
        log.info("  Fetched %d parcels (offset %d, total so far: %d)",
                 len(features), offset, len(all_features))

        if not data.get("properties", {}).get("exceededTransferLimit", False):
            break

        offset += PAGE_SIZE

    return all_features


def geojson_polygon_to_wkt(geometry):
    """Convert a GeoJSON Polygon to WKT."""
    if not geometry or geometry.get("type") != "Polygon":
        return None
    rings = geometry["coordinates"]
    ring_strs = []
    for ring in rings:
        points = ", ".join(f"{x} {y}" for x, y in ring)
        ring_strs.append(f"({points})")
    return f"POLYGON({', '.join(ring_strs)})"


def transform_feature(feature):
    """Transform a GeoJSON feature into a row for bronze_cadastral."""
    props = feature["properties"]
    geom = feature.get("geometry")

    return {
        "pams_pin": props["PAMS_PIN"],
        "pcl_mun": props["PCL_MUN"],
        "mun_name": props.get("MUN_NAME"),
        "pclblock": props["PCLBLOCK"],
        "pcllot": props["PCLLOT"],
        "pclqcode": props.get("PCLQCODE") or None,
        "prop_loc": props.get("PROP_LOC"),
        "prop_class": props.get("PROP_CLASS"),
        "prop_use": props.get("PROP_USE"),
        "bldg_class": props.get("BLDG_CLASS"),
        "bldg_desc": props.get("BLDG_DESC"),
        "land_desc": props.get("LAND_DESC"),
        "calc_acre": props.get("CALC_ACRE"),
        "land_val": props.get("LAND_VAL"),
        "imprvt_val": props.get("IMPRVT_VAL"),
        "net_value": props.get("NET_VALUE"),
        "last_yr_tx": props.get("LAST_YR_TX"),
        "sale_price": props.get("SALE_PRICE"),
        "sales_code": props.get("SALES_CODE"),
        "deed_book": props.get("DEED_BOOK"),
        "deed_page": props.get("DEED_PAGE"),
        "deed_date": props.get("DEED_DATE"),
        "yr_constr": props.get("YR_CONSTR"),
        "dwell": props.get("DWELL"),
        "st_address": props.get("ST_ADDRESS"),
        "city_state": props.get("CITY_STATE"),
        "zip_code": props.get("ZIP_CODE"),
        "geom": geojson_polygon_to_wkt(geom),
        "raw_record": props,
    }


def upsert_batch(client, rows, batch_size=500):
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table("bronze_cadastral").upsert(batch).execute()
                total += len(batch)
                log.info("  Upserted batch %d: %d rows (total: %d)",
                         i // batch_size + 1, len(batch), total)
                break
            except Exception as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    log.warning("  Batch %d failed (%s), retrying in %ds",
                                i // batch_size + 1, e, wait)
                    time.sleep(wait)
                else:
                    log.error("  Batch %d failed after 3 attempts: %s",
                              i // batch_size + 1, e)
                    raise
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Ingest NJ Cadastral parcel data into Supabase"
    )
    parser.add_argument(
        "--muni",
        help="Single municipality code to ingest (e.g. 1510). Default: all LBI.",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform without writing to Supabase",
    )
    args = parser.parse_args()

    if args.muni:
        if args.muni not in LBI_MUNICIPALITIES:
            log.error("Unknown municipality code: %s. Valid: %s",
                      args.muni, ", ".join(sorted(LBI_MUNICIPALITIES)))
            sys.exit(1)
        munis = {args.muni: LBI_MUNICIPALITIES[args.muni]}
    else:
        munis = LBI_MUNICIPALITIES

    all_rows = []
    for code, mun_name in sorted(munis.items()):
        log.info("=== %s (%s) ===", mun_name, code)
        features = fetch_parcels(mun_name)
        log.info("  %d parcels retrieved", len(features))

        rows = []
        errors = 0
        for f in features:
            try:
                rows.append(transform_feature(f))
            except Exception as e:
                errors += 1
                log.warning("  Transform error: %s — %s",
                            f.get("properties", {}).get("PAMS_PIN", "?"), e)

        if errors:
            log.warning("  %d transform errors for %s", errors, mun_name)
        all_rows.extend(rows)

    log.info("Total: %d rows across %d municipalities", len(all_rows), len(munis))

    if args.dry_run:
        sample = {k: v for k, v in all_rows[0].items()
                  if k not in ("raw_record", "geom")}
        log.info("Dry run complete. Sample row:\n%s", json.dumps(sample, indent=2))
        return

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
        sys.exit(1)

    client = create_client(url, key)
    total = upsert_batch(client, all_rows, batch_size=args.batch_size)
    log.info("Load complete: %d rows upserted to bronze_cadastral", total)

    resp = client.table("bronze_cadastral").select("pams_pin", count="exact").execute()
    log.info("Verification: %d rows in bronze_cadastral", resp.count)


if __name__ == "__main__":
    main()
