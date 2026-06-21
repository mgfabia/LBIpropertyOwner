#!/usr/bin/env python3
"""
Build the silver_parcels table.

Reads bronze_tax_board and bronze_cadastral from Supabase, merges them into
a single canonical row per parcel, normalizes mailing addresses, and derives
ownership classification (absentee/resident, owner state).

Geometry is populated via a post-load SQL UPDATE (not through the REST API)
to avoid PostGIS serialization issues.

Usage:
    # Full rebuild (truncate + reload)
    python build_silver_parcels.py

    # Dry run (compute and report stats, no DB writes)
    python build_silver_parcels.py --dry-run

    # Custom batch size
    python build_silver_parcels.py --batch-size 200
"""

import argparse
import collections
import logging
import os
import re
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

PAGE_SIZE = 1000

LBI_LOCAL_ZIPS = {"08006", "08008"}

STREET_ABBREVIATIONS = {
    "BOULEVARD": "BLVD",
    "STREET": "ST",
    "AVENUE": "AVE",
    "DRIVE": "DR",
    "ROAD": "RD",
    "LANE": "LN",
    "COURT": "CT",
    "PLACE": "PL",
    "TERRACE": "TER",
    "CIRCLE": "CIR",
    "HIGHWAY": "HWY",
    "PARKWAY": "PKWY",
    "THOROUGHFARE": "THFR",
}


def fetch_all_rows(client, table, select, filters=None, order_by=None):
    """Fetch all rows from a Supabase table, handling pagination.

    order_by is required for stable pagination — without it, the REST API
    may return duplicate or skipped rows across pages.
    """
    rows = []
    offset = 0
    while True:
        query = client.table(table).select(select)
        if order_by:
            query = query.order(order_by)
        if filters:
            for col, val in filters:
                query = query.eq(col, val)
        query = query.range(offset, offset + PAGE_SIZE - 1)
        resp = query.execute()
        batch = resp.data
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def parse_city_state_zip(raw):
    """Parse 'CITY STATE ZIP' or 'CITY, STATE ZIP' into (city, state, zip5).

    Handles observed patterns:
      - "BEACH HAVEN NJ 08008"
      - "BEACH HAVEN, NJ 08008"
      - "BEACH HAVEN N.J. 08008"
      - "ALLENTOWN NJ 085019438" (9-digit ZIP)
      - "BEACH HAVEN, N J 08008" (space in state)
    """
    if not raw:
        return None, None, None

    s = raw.strip().upper()

    # Strip periods: N.J. -> NJ, W.VA. -> WVA
    s = s.replace(".", "")

    # Collapse single-char-space-single-char before digits: "N J 08008" -> "NJ 08008"
    s = re.sub(r"\b([A-Z]) ([A-Z])\s+(\d)", r"\1\2 \3", s)

    # Try to match: CITY [,] STATE(2-letter) ZIP(5+ digits)
    m = re.match(r"^(.+?)[,\s]+([A-Z]{2})\s+(\d{5})\d*\s*$", s)
    if m:
        city = m.group(1).strip().rstrip(",").strip()
        state = m.group(2)
        zip5 = m.group(3)
        return city, state, zip5

    # Try without ZIP: CITY [,] STATE(2-letter)
    m = re.match(r"^(.+?)[,\s]+([A-Z]{2})\s*$", s)
    if m:
        city = m.group(1).strip().rstrip(",").strip()
        state = m.group(2)
        return city, state, None

    return s, None, None


def normalize_street(addr):
    """Normalize a street address for absentee comparison."""
    if not addr:
        return ""
    s = addr.strip().upper()
    # Strip unit/apt/suite designators
    s = re.sub(r"\s*(APT|UNIT|STE|SUITE|#)\s*\S*", "", s)
    # Normalize abbreviations
    for full, abbr in STREET_ABBREVIATIONS.items():
        s = re.sub(r"\b" + full + r"\b", abbr, s)
    return s.strip()


def is_po_box(addr):
    """Check if an address is a PO Box."""
    if not addr:
        return False
    s = addr.strip().upper()
    return bool(re.match(r"^P\.?\s*O\.?\s*BOX\b", s))


def classify_absentee(property_location, mailing_address, mailing_zip=None):
    """Determine if owner is absentee by comparing addresses.

    Returns True (absentee), False (local), or None (can't determine).

    PO Box addresses with a local LBI ZIP are treated as resident — small
    island towns (especially Barnegat Light) use PO Boxes for mail delivery,
    so a PO Box at the local ZIP is a resident, not an absentee.
    """
    if not property_location or not mailing_address:
        return None

    if is_po_box(mailing_address) and mailing_zip in LBI_LOCAL_ZIPS:
        return False

    prop = normalize_street(property_location)
    mail = normalize_street(mailing_address)

    if not prop or not mail:
        return None

    return not (prop in mail or mail in prop)


def fetch_tax_board(client):
    """Fetch all bronze_tax_board rows, extract needed fields."""
    log.info("Fetching bronze_tax_board...")
    rows = fetch_all_rows(
        client,
        "bronze_tax_board",
        "pams_pin, municipality_code, municipality, block, lot, qualifier, "
        "property_location, mailing_address, mailing_city_state, "
        "prop_class, land_value, improvement_value, net_value, last_year_taxes, "
        "exemption_code, year_built, sfla, bedrooms, bathrooms, "
        "sale_price, deed_date, zone, raw_record",
        order_by="pams_pin",
    )
    log.info("  Fetched %d tax board records", len(rows))

    # Build dict keyed on pams_pin, extracting raw_record fields we need
    lookup = {}
    for row in rows:
        raw = row.get("raw_record") or {}
        row["building_type"] = raw.get("building_type")
        row["stories"] = raw.get("stories")
        row["land_desc_raw"] = raw.get("land_desc")
        del row["raw_record"]
        lookup[row["pams_pin"]] = row

    return lookup


def fetch_cadastral(client):
    """Fetch all bronze_cadastral rows (excluding geometry)."""
    log.info("Fetching bronze_cadastral...")
    rows = fetch_all_rows(
        client,
        "bronze_cadastral",
        "pams_pin, pcl_mun, mun_name, pclblock, pcllot, pclqcode, "
        "prop_loc, prop_class, prop_use, bldg_class, bldg_desc, land_desc, "
        "calc_acre, land_val, imprvt_val, net_value, last_yr_tx, "
        "sale_price, sales_code, deed_book, deed_page, deed_date, "
        "yr_constr, dwell, st_address, city_state, zip_code",
        order_by="pams_pin",
    )
    log.info("  Fetched %d cadastral records", len(rows))

    lookup = {}
    for row in rows:
        lookup[row["pams_pin"]] = row

    return lookup


def merge_parcel(pams_pin, tb, cad):
    """Merge a tax_board record and cadastral record into a silver_parcels row."""
    sources = []
    if tb:
        sources.append("bronze_tax_board")
    if cad:
        sources.append("bronze_cadastral")

    # Parse mailing address
    mailing_cs_raw = None
    if tb:
        mailing_cs_raw = tb.get("mailing_city_state")
    if not mailing_cs_raw and cad:
        mailing_cs_raw = cad.get("city_state")

    city, state, zip5 = parse_city_state_zip(mailing_cs_raw)

    # Fallback ZIP from cadastral
    if not zip5 and cad:
        cad_zip = (cad.get("zip_code") or "")[:5]
        if len(cad_zip) == 5 and cad_zip.isdigit():
            zip5 = cad_zip

    prop_loc = (tb or {}).get("property_location") or (cad or {}).get("prop_loc")
    mail_addr = (tb or {}).get("mailing_address") or (cad or {}).get("st_address")

    return {
        "pams_pin": pams_pin,
        "municipality_code": (tb or {}).get("municipality_code") or (cad or {}).get("pcl_mun"),
        "municipality_name": (cad or {}).get("mun_name"),
        "block": (tb or {}).get("block") or (cad or {}).get("pclblock"),
        "lot": (tb or {}).get("lot") or (cad or {}).get("pcllot"),
        "qualifier": (tb or {}).get("qualifier") or (cad or {}).get("pclqcode"),
        "property_location": prop_loc,
        # geom populated via SQL UPDATE after load
        "mailing_address": mail_addr,
        "mailing_city_state_raw": mailing_cs_raw,
        "mailing_city": city,
        "mailing_state": state,
        "mailing_zip": zip5,
        "is_absentee": classify_absentee(prop_loc, mail_addr, zip5),
        "is_nj_resident": (state == "NJ") if state else None,
        "prop_class": (tb or {}).get("prop_class") or (cad or {}).get("prop_class"),
        "land_value": (tb or {}).get("land_value") or (cad or {}).get("land_val"),
        "improvement_value": (tb or {}).get("improvement_value") or (cad or {}).get("imprvt_val"),
        "net_value": (tb or {}).get("net_value") or (cad or {}).get("net_value"),
        "last_year_taxes": (tb or {}).get("last_year_taxes") or (cad or {}).get("last_yr_tx"),
        "exemption_code": (tb or {}).get("exemption_code"),
        "prop_use": (cad or {}).get("prop_use"),
        "bldg_class": (cad or {}).get("bldg_class"),
        "bldg_desc": (cad or {}).get("bldg_desc"),
        "land_desc": (tb or {}).get("land_desc_raw") or (cad or {}).get("land_desc"),
        "calc_acre": (cad or {}).get("calc_acre"),
        "zone": (tb or {}).get("zone"),
        "year_built": (tb or {}).get("year_built") or (cad or {}).get("yr_constr"),
        "sfla": (tb or {}).get("sfla"),
        "bedrooms": (tb or {}).get("bedrooms"),
        "bathrooms": (tb or {}).get("bathrooms"),
        "building_type": (tb or {}).get("building_type"),
        "stories": (tb or {}).get("stories"),
        "dwell": (cad or {}).get("dwell"),
        "deed_book": (cad or {}).get("deed_book"),
        "deed_page": (cad or {}).get("deed_page"),
        "deed_date": (tb or {}).get("deed_date") or (cad or {}).get("deed_date"),
        "sale_price": (tb or {}).get("sale_price") or (cad or {}).get("sale_price"),
        "sales_code": (cad or {}).get("sales_code"),
        "source_tables": sources,
    }


def upsert_batch(client, table, rows, batch_size=500):
    """Upsert rows with retry."""
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table(table).upsert(batch).execute()
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


def populate_geometry(supabase_url, service_role_key):
    """Populate geom column from bronze_cadastral via direct SQL.

    Uses a raw Postgres connection (the Supabase REST API can't run arbitrary
    SQL, and PostGIS geometry doesn't round-trip cleanly through REST). Connects
    via the IPv4 pooler (see db.py), since the direct DB host is IPv6-only.
    """
    log.info("Populating geometry from bronze_cadastral...")

    manual_hint = (
        "  Run the geometry UPDATE manually via the Supabase SQL editor:\n"
        "  UPDATE silver_parcels sp SET geom = bc.geom FROM bronze_cadastral bc "
        "WHERE sp.pams_pin = bc.pams_pin AND bc.geom IS NOT NULL;"
    )

    try:
        from db import get_connection
        conn = get_connection()
    except Exception as e:
        log.warning("  Could not connect via pooler (%s).", e)
        log.warning(manual_hint)
        return

    log.info("  Connected via pooler (region: %s)", getattr(conn, "supabase_region", "?"))
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE silver_parcels sp
                SET geom = bc.geom
                FROM bronze_cadastral bc
                WHERE sp.pams_pin = bc.pams_pin
                  AND bc.geom IS NOT NULL
            """)
            log.info("  Geometry populated: %d rows updated", cur.rowcount)
        conn.commit()
    finally:
        conn.close()


def populate_owner_names():
    """Populate owner_names / owner_grantors from bronze_clerk_deeds.

    Each parcel's deed of record is recorded in bronze_clerk_deeds with the
    parties split by role (grantees = current owners, grantors = prior owners).
    A parcel appears in exactly one deed's pams_pins[], so the unnest join is
    1:1. Parcels with no clerk deed match stay NULL. Runs via the pooler (raw
    SQL), like geometry. Skips gracefully if bronze_clerk_deeds is absent.
    """
    log.info("Populating owner names from bronze_clerk_deeds...")

    try:
        from db import get_connection
        conn = get_connection()
    except Exception as e:
        log.warning("  Could not connect via pooler (%s). Skipping owner names.", e)
        return

    log.info("  Connected via pooler (region: %s)", getattr(conn, "supabase_region", "?"))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.bronze_clerk_deeds')")
            if cur.fetchone()[0] is None:
                log.warning("  bronze_clerk_deeds does not exist yet. Skipping "
                            "owner names (run load_clerk_to_supabase.py first).")
                return
            cur.execute("""
                UPDATE silver_parcels sp
                SET owner_names = d.grantees,
                    owner_grantors = d.grantors
                FROM (
                    SELECT unnest(pams_pins) AS pams_pin, grantees, grantors
                    FROM bronze_clerk_deeds
                ) d
                WHERE sp.pams_pin = d.pams_pin
            """)
            log.info("  Owner names populated: %d rows updated", cur.rowcount)
            cur.execute(
                "SELECT count(*) FROM silver_parcels "
                "WHERE array_length(owner_names, 1) >= 1"
            )
            log.info("  Parcels with >=1 current owner name: %d", cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Build silver_parcels from bronze tables"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute merge and report stats without writing to Supabase",
    )
    args = parser.parse_args()

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment / .env")
        sys.exit(1)

    client = create_client(url, key)

    # Step 1: Fetch bronze data
    tb_lookup = fetch_tax_board(client)
    cad_lookup = fetch_cadastral(client)

    # Step 2: Full outer join — union of all pams_pin values
    all_pins = sorted(set(tb_lookup.keys()) | set(cad_lookup.keys()))
    log.info("Union of pams_pin values: %d parcels", len(all_pins))

    # Step 3: Merge
    results = []
    stats = {"both": 0, "tax_board_only": 0, "cadastral_only": 0}
    absentee_stats = {"absentee": 0, "local": 0, "unknown": 0}
    state_counter = collections.Counter()

    for pin in all_pins:
        tb = tb_lookup.get(pin)
        cad = cad_lookup.get(pin)

        if tb and cad:
            stats["both"] += 1
        elif tb:
            stats["tax_board_only"] += 1
        else:
            stats["cadastral_only"] += 1

        row = merge_parcel(pin, tb, cad)
        results.append(row)

        if row["is_absentee"] is True:
            absentee_stats["absentee"] += 1
        elif row["is_absentee"] is False:
            absentee_stats["local"] += 1
        else:
            absentee_stats["unknown"] += 1

        if row["mailing_state"]:
            state_counter[row["mailing_state"]] += 1

    # Report
    total = len(results)
    log.info("")
    log.info("=== Silver Parcels Merge Summary ===")
    log.info("Total parcels:         %d", total)
    log.info("")
    log.info("Source coverage:")
    for key_name in ("both", "tax_board_only", "cadastral_only"):
        count = stats[key_name]
        pct = 100.0 * count / total if total else 0
        log.info("  %-20s %5d  (%5.1f%%)", key_name, count, pct)
    log.info("")
    log.info("Ownership classification:")
    for key_name in ("absentee", "local", "unknown"):
        count = absentee_stats[key_name]
        pct = 100.0 * count / total if total else 0
        log.info("  %-20s %5d  (%5.1f%%)", key_name, count, pct)
    log.info("")
    log.info("Owner states (top 10):")
    for state, count in state_counter.most_common(10):
        pct = 100.0 * count / total if total else 0
        log.info("  %-5s %5d  (%5.1f%%)", state, count, pct)

    if args.dry_run:
        log.info("")
        log.info("Sample row:")
        sample = next((r for r in results if r.get("is_absentee") is True), results[0])
        for k, v in sample.items():
            log.info("  %-25s %s", k, v)
        log.info("")
        log.info("Dry run complete. No rows written.")
        return

    # Step 4: Truncate
    log.info("")
    log.info("Truncating silver_parcels...")
    client.table("silver_parcels").delete().neq("pams_pin", "").execute()

    # Step 5: Upsert
    total_upserted = upsert_batch(client, "silver_parcels", results, batch_size=args.batch_size)
    log.info("Load complete: %d rows upserted to silver_parcels", total_upserted)

    # Step 6: Populate geometry via SQL
    populate_geometry(url, key)

    # Step 7: Populate owner names from clerk deeds via SQL
    populate_owner_names()

    # Verify
    resp = client.table("silver_parcels").select("pams_pin", count="exact").execute()
    log.info("Verification: %d rows in silver_parcels", resp.count)


if __name__ == "__main__":
    main()
