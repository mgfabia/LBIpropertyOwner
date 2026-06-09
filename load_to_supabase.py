#!/usr/bin/env python3
"""
Load tax board JSONL data into Supabase bronze layer.

Reads scraped property records from data/tax_board_details.jsonl,
transforms them (mints PAMS_PIN, cleans numeric values), and upserts
into the bronze_tax_board table.

Usage:
    # Load default file
    python load_to_supabase.py

    # Load a specific file
    python load_to_supabase.py --file data/tax_board_details.jsonl

    # Dry run (transform only, no database writes)
    python load_to_supabase.py --dry-run

    # Custom batch size
    python load_to_supabase.py --batch-size 200
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MUNICIPALITY_TO_PAMS = {
    "Barn Lgt": "1502",
    "Bch Hvn": "1504",
    "Hrv Cdrs": "1510",
    "Long Beach": "1518",
    "Ship Btm": "1529",
    "Surf City": "1532",
    "BARN LGT": "1502",
    "BCH HVN": "1504",
    "HRV CDRS": "1510",
    "LONG BEACH": "1518",
    "SHIP BTM": "1529",
    "SURF CITY": "1532",
    "Bngt Lt": "1502",
    "BNGT LT": "1502",
    "Shp Btm": "1529",
    "SHP BTM": "1529",
    "BARNEGAT LIGHT": "1502",
    "BEACH HAVEN": "1504",
    "HARVEY CEDARS": "1510",
    "LONG BEACH TWP": "1518",
    "SHIP BOTTOM": "1529",
    "Lng Bch": "1518",
    "LNG BCH": "1518",
    "Lng Bch Twp": "1518",
    "LNG BCH TWP": "1518",
}


def resolve_municipality_code(record):
    muni = record.get("municipality", "")
    code = MUNICIPALITY_TO_PAMS.get(muni)
    if code:
        return code
    search_muni = record.get("search_municipality", "")
    code = MUNICIPALITY_TO_PAMS.get(search_muni)
    if code:
        return code
    raise ValueError(
        f"Unknown municipality: municipality='{muni}', "
        f"search_municipality='{search_muni}'. "
        f"Add mapping to MUNICIPALITY_TO_PAMS."
    )


def mint_pams_pin(muni_code, block, lot, qualifier):
    pin = f"{muni_code}_{block}_{lot}"
    if qualifier:
        pin = f"{pin}_{qualifier}"
    return pin


def clean_numeric(value):
    if not value or not isinstance(value, str) or value.strip() == "":
        return None
    cleaned = value.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def clean_integer(value):
    if not value or not isinstance(value, str) or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def transform_record(raw, source_file):
    muni_code = resolve_municipality_code(raw)
    qualifier = raw.get("qualifier") or None

    return {
        "pams_pin": mint_pams_pin(muni_code, raw["block"], raw["lot"], qualifier),
        "detail_id": raw["detail_id"],
        "municipality_code": muni_code,
        "municipality": raw.get("municipality"),
        "block": raw["block"],
        "lot": raw["lot"],
        "qualifier": qualifier,
        "property_location": raw.get("property_location"),
        "mailing_address": raw.get("mailing_address"),
        "mailing_city_state": raw.get("mailing_city_state"),
        "prop_class": raw.get("prop_class"),
        "land_value": clean_numeric(raw.get("land_value")),
        "improvement_value": clean_numeric(raw.get("improvement_value")),
        "net_value": clean_numeric(raw.get("net_value")),
        "last_year_taxes": clean_numeric(raw.get("last_year_taxes")),
        "exemption_code": raw.get("exemption_code"),
        "year_built": clean_integer(raw.get("year_built")),
        "sfla": clean_integer(raw.get("sfla")),
        "bedrooms": clean_integer(raw.get("bedrooms")),
        "bathrooms": clean_integer(raw.get("bathrooms")),
        "sale_price": clean_numeric(raw.get("sale_price")),
        "deed_date": raw.get("deed_date"),
        "zone": raw.get("zone"),
        "raw_record": raw,
        "source_file": source_file,
    }


def upsert_batch(client, rows, batch_size=500):
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table("bronze_tax_board").upsert(batch).execute()
                total += len(batch)
                log.info(
                    "  Upserted batch %d: %d rows (total: %d)",
                    i // batch_size + 1,
                    len(batch),
                    total,
                )
                break
            except Exception as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    log.warning("  Batch %d failed (%s), retrying in %ds", i // batch_size + 1, e, wait)
                    time.sleep(wait)
                else:
                    log.error("  Batch %d failed after 3 attempts: %s", i // batch_size + 1, e)
                    raise
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Load tax board JSONL to Supabase bronze layer"
    )
    parser.add_argument(
        "--file", "-f",
        default="data/tax_board_details.jsonl",
        help="Path to JSONL file (default: data/tax_board_details.jsonl)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Transform and validate without writing to Supabase",
    )
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        log.error("File not found: %s", file_path)
        sys.exit(1)

    source_file = file_path.name

    raw_records = []
    with open(file_path) as f:
        for line in f:
            line = line.strip()
            if line:
                raw_records.append(json.loads(line))
    log.info("Read %d records from %s", len(raw_records), file_path)

    rows = []
    errors = []
    for i, raw in enumerate(raw_records):
        try:
            rows.append(transform_record(raw, source_file))
        except Exception as e:
            errors.append((i + 1, raw.get("detail_id", "?"), str(e)))
            log.warning("  Line %d detail_id=%s: %s", i + 1, raw.get("detail_id", "?"), e)

    log.info("Transformed %d rows (%d errors)", len(rows), len(errors))

    seen = {}
    for r in rows:
        seen[r["pams_pin"]] = r
    if len(seen) < len(rows):
        log.info("Deduplicated %d → %d rows (kept last occurrence per pams_pin)", len(rows), len(seen))
    rows = list(seen.values())

    if args.dry_run:
        log.info("Dry run complete. Sample transformed row:")
        sample = {k: v for k, v in rows[0].items() if k != "raw_record"}
        log.info(json.dumps(sample, indent=2))
        return

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment / .env")
        sys.exit(1)

    client = create_client(url, key)
    total = upsert_batch(client, rows, batch_size=args.batch_size)
    log.info("Load complete: %d rows upserted to bronze_tax_board", total)

    resp = client.table("bronze_tax_board").select("pams_pin", count="exact").execute()
    db_count = resp.count
    log.info("Verification: %d rows in bronze_tax_board", db_count)


if __name__ == "__main__":
    main()
