#!/usr/bin/env python3
"""
Load Ocean County Clerk deed records JSONL into Supabase bronze layer.

Reads scraped deed lookups from data/clerk/clerk_deeds.jsonl (output of
scrape_clerk.py), transforms them for the bronze schema, and upserts into
the bronze_clerk_deeds table. One row per deed book/page document, keyed on
the composite (deed_book, deed_page).

Each deed names multiple parties (grantors + grantees). The clerk search does
not reliably label grantor vs grantee, so `parties` is the unroled set of all
names on the document — the current owner is among them but not flagged.

Usage:
    # Load default file
    python load_clerk_to_supabase.py

    # Load a specific file
    python load_clerk_to_supabase.py --file data/clerk/clerk_deeds.jsonl

    # Dry run (transform only, no database writes)
    python load_clerk_to_supabase.py --dry-run

    # Custom batch size
    python load_clerk_to_supabase.py --batch-size 200

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in environment / .env.
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


def transform_record(raw):
    book = (raw.get("deed_book") or "").strip()
    page = (raw.get("deed_page") or "").strip()
    if not book or not page:
        raise ValueError("Missing deed_book/deed_page")

    pams_pins = [p for p in (raw.get("pams_pins") or []) if p]
    parties = [p for p in (raw.get("parties") or []) if p]
    result_count = raw.get("result_count")
    if result_count is None:
        result_count = len(raw.get("results") or [])

    # Split parties by source role flag: party_code '*' = grantor (seller),
    # '' = grantee (buyer / current owner). Verified against the clerk document
    # detail view; see sql/bronze_clerk_deeds.sql.
    grantors, grantees = [], []
    seen_gor, seen_gee = set(), set()
    for res in raw.get("results") or []:
        name = (res.get("party_name") or "").strip()
        if not name:
            continue
        if res.get("party_code") == "*":
            if name not in seen_gor:
                seen_gor.add(name)
                grantors.append(name)
        else:
            if name not in seen_gee:
                seen_gee.add(name)
                grantees.append(name)

    error = raw.get("error")

    return {
        "deed_book": book,
        "deed_page": page,
        "pams_pins": pams_pins,
        "parties": parties,
        "grantors": sorted(grantors),
        "grantees": sorted(grantees),
        "result_count": result_count,
        "has_results": result_count > 0,
        "error": error,
        "scraped_at": raw.get("scraped_at"),
        "raw_record": raw,
    }


def upsert_batch(client, rows, batch_size=500):
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(3):
            try:
                client.table("bronze_clerk_deeds").upsert(
                    batch, on_conflict="deed_book,deed_page"
                ).execute()
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
                    log.warning(
                        "  Batch %d failed (%s), retrying in %ds",
                        i // batch_size + 1,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    log.error(
                        "  Batch %d failed after 3 attempts: %s",
                        i // batch_size + 1,
                        e,
                    )
                    raise
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Load Ocean County Clerk deed JSONL to Supabase bronze layer"
    )
    parser.add_argument(
        "--file",
        "-f",
        default="data/clerk/clerk_deeds.jsonl",
        help="Path to JSONL file (default: data/clerk/clerk_deeds.jsonl)",
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
            rows.append(transform_record(raw))
        except Exception as e:
            key = f"{raw.get('deed_book', '?')}:{raw.get('deed_page', '?')}"
            errors.append((i + 1, key, str(e)))
            log.warning("  Line %d deed=%s: %s", i + 1, key, e)

    log.info("Transformed %d rows (%d errors)", len(rows), len(errors))

    seen = {}
    for r in rows:
        seen[(r["deed_book"], r["deed_page"])] = r
    if len(seen) < len(rows):
        log.info(
            "Deduplicated %d → %d rows (kept last occurrence per deed_book/deed_page)",
            len(rows),
            len(seen),
        )
    rows = list(seen.values())

    # Coverage stats: distinct parcels and named parcels touched by these deeds.
    parcels = set()
    named_parcels = set()
    for r in rows:
        for pin in r["pams_pins"]:
            parcels.add(pin)
            if r["parties"]:
                named_parcels.add(pin)
    with_results = sum(1 for r in rows if r["has_results"])
    log.info(
        "Coverage: %d deeds (%d with results), %d distinct parcels (%d with ≥1 party name)",
        len(rows),
        with_results,
        len(parcels),
        len(named_parcels),
    )

    if args.dry_run:
        log.info("Dry run complete. Sample transformed row:")
        sample = {k: v for k, v in rows[0].items() if k != "raw_record"}
        log.info(json.dumps(sample, indent=2))
        return

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment / .env"
        )
        sys.exit(1)

    client = create_client(url, key)
    total = upsert_batch(client, rows, batch_size=args.batch_size)
    log.info("Load complete: %d rows upserted to bronze_clerk_deeds", total)

    resp = (
        client.table("bronze_clerk_deeds")
        .select("deed_book", count="exact")
        .execute()
    )
    log.info("Verification: %d rows in bronze_clerk_deeds", resp.count)


if __name__ == "__main__":
    main()
