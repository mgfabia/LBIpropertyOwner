#!/usr/bin/env python3
"""
Build the silver_sr1a_pin_crosswalk table.

Reads bronze_sr1a_sales and bronze_cadastral from Supabase, resolves the
pams_pin encoding mismatch for Long Beach Twp (1518) via address matching,
and upserts the results into silver_sr1a_pin_crosswalk.

The SR1A fixed-width files encode all 1518 parcels with block=00001 and
lot=00001. The actual section identifier lives in block_suffix, but that
field is not unique across base blocks (e.g., suffix 01 maps to 15 different
base blocks). The only viable resolution is matching on property_location
against the cadastral layer, which has the correct section-based pams_pin.

Usage:
    # Full rebuild (truncate + reload)
    python build_sr1a_crosswalk.py

    # Dry run (compute and report stats, no DB writes)
    python build_sr1a_crosswalk.py --dry-run

    # Custom batch size
    python build_sr1a_crosswalk.py --batch-size 200
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Supabase client paginates at 1000 rows by default. We need all rows from
# the bronze tables, so we fetch in pages.
PAGE_SIZE = 1000


def fetch_all_rows(client, table, select, filters=None):
    """Fetch all rows from a Supabase table, handling pagination."""
    rows = []
    offset = 0
    while True:
        query = client.table(table).select(select).range(offset, offset + PAGE_SIZE - 1)
        if filters:
            for col, val in filters:
                query = query.eq(col, val)
        resp = query.execute()
        batch = resp.data
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def build_cadastral_address_lookup(client):
    """Build address -> pams_pin lookup for 1518 from bronze_cadastral."""
    log.info("Fetching bronze_cadastral for municipality 1518...")
    rows = fetch_all_rows(
        client,
        "bronze_cadastral",
        "pams_pin, prop_loc",
        filters=[("pcl_mun", "1518")],
    )
    log.info("  Fetched %d cadastral parcels for 1518", len(rows))

    # Deduplicate: where an address maps to multiple parcels, keep the first
    # alphabetically by pams_pin (matches the SQL DISTINCT ON behavior).
    lookup = {}
    for row in rows:
        addr = (row.get("prop_loc") or "").strip().upper()
        if not addr:
            continue
        pin = row["pams_pin"]
        if addr not in lookup or pin < lookup[addr]:
            lookup[addr] = pin

    log.info("  Built address lookup: %d unique addresses", len(lookup))
    return lookup


def build_cadastral_pin_set(client):
    """Build a set of all 1518 pams_pin values from bronze_cadastral."""
    log.info("Building cadastral pin set for 1518...")
    rows = fetch_all_rows(
        client,
        "bronze_cadastral",
        "pams_pin",
        filters=[("pcl_mun", "1518")],
    )
    pin_set = {row["pams_pin"] for row in rows}
    log.info("  %d cadastral pins for 1518", len(pin_set))
    return pin_set


def fetch_sr1a_sales(client):
    """Fetch all SR1A sale records."""
    log.info("Fetching bronze_sr1a_sales...")
    rows = fetch_all_rows(
        client,
        "bronze_sr1a_sales",
        "serial_number, pams_pin, municipality_code, property_location",
    )
    log.info("  Fetched %d SR1A sale records", len(rows))
    return rows


def resolve_pins(sr1a_rows, cadastral_pins_1518, address_lookup):
    """Resolve each SR1A record to a canonical pams_pin."""
    results = []
    stats = {"passthrough": 0, "direct_match": 0, "address_resolved": 0, "unresolved": 0}

    for row in sr1a_rows:
        serial = row["serial_number"]
        sr1a_pin = row["pams_pin"]
        muni = row["municipality_code"]
        prop_loc = row.get("property_location") or ""

        if muni != "1518":
            method = "passthrough"
            resolved = sr1a_pin
        elif sr1a_pin in cadastral_pins_1518:
            method = "direct_match"
            resolved = sr1a_pin
        else:
            addr_key = prop_loc.strip().upper()
            cadastral_pin = address_lookup.get(addr_key)
            if cadastral_pin:
                method = "address_resolved"
                resolved = cadastral_pin
            else:
                method = "unresolved"
                resolved = sr1a_pin

        stats[method] += 1
        results.append({
            "serial_number": serial,
            "sr1a_pams_pin": sr1a_pin,
            "resolved_pams_pin": resolved,
            "municipality_code": muni,
            "property_location": prop_loc if prop_loc else None,
            "resolution_method": method,
        })

    return results, stats


def upsert_batch(client, rows, batch_size=500):
    """Upsert rows into silver_sr1a_pin_crosswalk with retry."""
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table("silver_sr1a_pin_crosswalk").upsert(batch).execute()
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
        description="Build silver_sr1a_pin_crosswalk from bronze tables"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute crosswalk and report stats without writing to Supabase",
    )
    args = parser.parse_args()

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment / .env")
        sys.exit(1)

    client = create_client(url, key)

    # Step 1: Build lookup structures from cadastral
    cadastral_pins = build_cadastral_pin_set(client)
    address_lookup = build_cadastral_address_lookup(client)

    # Step 2: Fetch all SR1A sales
    sr1a_rows = fetch_sr1a_sales(client)

    # Step 3: Resolve pins
    results, stats = resolve_pins(sr1a_rows, cadastral_pins, address_lookup)

    # Report
    total = len(results)
    log.info("")
    log.info("=== Crosswalk Resolution Summary ===")
    log.info("Total SR1A records:    %d", total)
    log.info("")
    log.info("By resolution method:")
    for method in ("passthrough", "direct_match", "address_resolved", "unresolved"):
        count = stats[method]
        pct = 100.0 * count / total if total else 0
        log.info("  %-20s %5d  (%5.1f%%)", method, count, pct)
    log.info("")
    resolved_count = stats["passthrough"] + stats["direct_match"] + stats["address_resolved"]
    log.info("Resolved:              %d  (%.1f%%)", resolved_count, 100.0 * resolved_count / total if total else 0)
    log.info("Unresolved:            %d  (%.1f%%)", stats["unresolved"], 100.0 * stats["unresolved"] / total if total else 0)

    if args.dry_run:
        log.info("")
        log.info("Dry run complete. No rows written.")
        return

    # Step 4: Truncate and reload (full rebuild)
    log.info("")
    log.info("Truncating silver_sr1a_pin_crosswalk...")
    client.table("silver_sr1a_pin_crosswalk").delete().neq("serial_number", "").execute()

    # Step 5: Upsert
    total_upserted = upsert_batch(client, results, batch_size=args.batch_size)
    log.info("Load complete: %d rows upserted to silver_sr1a_pin_crosswalk", total_upserted)

    # Verify
    resp = client.table("silver_sr1a_pin_crosswalk").select("serial_number", count="exact").execute()
    log.info("Verification: %d rows in silver_sr1a_pin_crosswalk", resp.count)


if __name__ == "__main__":
    main()
