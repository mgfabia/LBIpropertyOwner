#!/usr/bin/env python3
"""
Load SR1A sales JSONL data into Supabase bronze layer.

Reads parsed SR1A records from data/sr1a/sr1a_all.jsonl (output of
parse_sr1a.py), transforms them for the bronze schema, and upserts
into the bronze_sr1a_sales table.

Usage:
    # Load combined file (all years)
    python load_sr1a_to_supabase.py

    # Load a specific year
    python load_sr1a_to_supabase.py --file data/sr1a/sr1a_2024.jsonl

    # Dry run (transform only, no database writes)
    python load_sr1a_to_supabase.py --dry-run

    # Custom batch size
    python load_sr1a_to_supabase.py --batch-size 200
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


def clean_numeric(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def clean_integer(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def transform_record(raw, source_file):
    serial = raw.get("serial_number")
    if not serial:
        raise ValueError("Missing serial_number")

    pams_pin = raw.get("pams_pin")
    if not pams_pin:
        raise ValueError("Missing pams_pin")

    return {
        "serial_number": serial,
        "pams_pin": pams_pin,
        "county_code": raw.get("county_code"),
        "district_code": raw.get("district_code"),
        "municipality_code": raw.get("municipality_code"),
        "block": raw.get("block"),
        "lot": raw.get("lot"),
        "property_location": raw.get("property_location"),
        "reported_sale_price": clean_numeric(raw.get("reported_sale_price")),
        "verified_sale_price": clean_numeric(raw.get("verified_sale_price")),
        "usable_code": raw.get("usable_code"),
        "non_usable_reason": raw.get("non_usable_reason"),
        "realty_transfer_fee": clean_numeric(raw.get("realty_transfer_fee")),
        "sales_ratio": raw.get("sales_ratio"),
        "total_assessment": clean_numeric(raw.get("total_assessment")),
        "assessed_land_value": clean_numeric(raw.get("assessed_land_value")),
        "assessed_bldg_value": clean_numeric(raw.get("assessed_bldg_value")),
        "assessed_total_value": clean_numeric(raw.get("assessed_total_value")),
        "deed_book": raw.get("deed_book"),
        "deed_page": raw.get("deed_page"),
        "deed_date": raw.get("deed_date"),
        "date_recorded": raw.get("date_recorded"),
        "grantor_name": raw.get("grantor_name"),
        "grantee_name": raw.get("grantee_name"),
        "grantee_city_state": raw.get("grantee_city_state"),
        "property_class": raw.get("property_class"),
        "assess_year": raw.get("assess_year"),
        "year_built": clean_integer(raw.get("year_built")),
        "living_space": clean_integer(raw.get("living_space")),
        "raw_record": raw,
        "source_file": source_file,
    }


def upsert_batch(client, rows, batch_size=500):
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table("bronze_sr1a_sales").upsert(batch).execute()
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
        description="Load SR1A sales JSONL to Supabase bronze layer"
    )
    parser.add_argument(
        "--file", "-f",
        default="data/sr1a/sr1a_all.jsonl",
        help="Path to JSONL file (default: data/sr1a/sr1a_all.jsonl)",
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
            errors.append((i + 1, raw.get("serial_number", "?"), str(e)))
            log.warning("  Line %d serial=%s: %s", i + 1, raw.get("serial_number", "?"), e)

    log.info("Transformed %d rows (%d errors)", len(rows), len(errors))

    seen = {}
    for r in rows:
        seen[r["serial_number"]] = r
    if len(seen) < len(rows):
        log.info("Deduplicated %d → %d rows (kept last occurrence per serial_number)", len(rows), len(seen))
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
    log.info("Load complete: %d rows upserted to bronze_sr1a_sales", total)

    resp = client.table("bronze_sr1a_sales").select("serial_number", count="exact").execute()
    db_count = resp.count
    log.info("Verification: %d rows in bronze_sr1a_sales", db_count)


if __name__ == "__main__":
    main()
