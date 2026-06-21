#!/usr/bin/env python3
"""
Scrape Ocean County Clerk deed records to get owner names.

Uses Playwright to drive the free search portal at
https://sng.co.ocean.nj.us/publicsearch/

Looks up each parcel by deed book/page and extracts grantor/grantee names
from recorded deeds. reCAPTCHA v3 tokens are obtained through the browser
context (invisible, no user interaction needed).

Output: data/clerk/clerk_deeds.jsonl

Usage:
    python scrape_clerk.py                    # full run (~7 hours for all LBI)
    python scrape_clerk.py --resume           # resume interrupted run
    python scrape_clerk.py --limit 100        # scrape first 100 only
    python scrape_clerk.py --delay 2.0        # 2 second delay between requests
    python scrape_clerk.py --export-csv       # convert JSONL to CSV

Requires: playwright (pip install playwright && playwright install chromium)
Requires: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SEARCH_URL = "https://sng.co.ocean.nj.us/publicsearch/"
RECAPTCHA_SITEKEY = "6LciR2kkAAAAAB0mCQA50PvunV2_uuLRwnHpSaRh"
RECAPTCHA_ACTION = "Search_bookPageSearchForm"

OUTPUT_DIR = Path("data/clerk")
OUTPUT_FILE = OUTPUT_DIR / "clerk_deeds.jsonl"
PROGRESS_FILE = OUTPUT_DIR / "clerk_progress.json"

PAGE_SIZE = 1000

METADATA_KEYS = {"_start_row", "_end_row", "_total_rows", "_max_rows", "_headers", "rowid"}


def fetch_deed_lookups(client):
    """Fetch unique deed_book/deed_page pairs with associated pams_pins."""
    log.info("Fetching deed lookups from silver_parcels...")
    rows = []
    offset = 0
    while True:
        resp = (
            client.table("silver_parcels")
            .select("pams_pin, deed_book, deed_page")
            .order("pams_pin")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows.extend(resp.data)
        if len(resp.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    lookups = {}
    skipped = 0
    for row in rows:
        book = (row.get("deed_book") or "").strip()
        page = (row.get("deed_page") or "").strip()
        if not book or not page or book == "00000":
            skipped += 1
            continue
        key = f"{book}:{page}"
        if key not in lookups:
            lookups[key] = {"deed_book": book, "deed_page": page, "pams_pins": []}
        lookups[key]["pams_pins"].append(row["pams_pin"])

    log.info(
        "  %d parcels -> %d unique book/page pairs (%d skipped)",
        len(rows),
        len(lookups),
        skipped,
    )
    return list(lookups.values())


def load_progress():
    """Load set of completed lookup keys from progress file."""
    if not PROGRESS_FILE.exists():
        return set()
    with open(PROGRESS_FILE) as f:
        data = json.load(f)
    return set(data.get("completed", []))


def save_progress(completed):
    """Save completed lookup keys to progress file."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(
            {"completed": sorted(completed), "count": len(completed)},
            f,
        )


def init_browser(pw):
    """Launch browser and navigate to search page."""
    log.info("Launching browser...")
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_function(
        "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'",
        timeout=15000,
    )
    log.info("  Browser ready, reCAPTCHA loaded")
    return browser, page


def get_recaptcha_token(page):
    """Get a fresh reCAPTCHA v3 token from the browser context."""
    return page.evaluate(
        "(args) => grecaptcha.execute(args.key, {action: args.action})",
        {"key": RECAPTCHA_SITEKEY, "action": RECAPTCHA_ACTION},
    )


def search_deed(page, deed_book, deed_page):
    """Search for a deed by book/page via the clerk API."""
    token = get_recaptcha_token(page)

    page_num = deed_page.lstrip("0") or "0"

    result = page.evaluate(
        """
        async (args) => {
            const resp = await fetch('/publicsearch/api/search', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    BookType: 'O',
                    Book: args.book,
                    Page: args.page,
                    RecaptchaResponseV3: args.token
                })
            });
            if (!resp.ok) {
                const text = await resp.text();
                return {_error: true, status: resp.status, body: text};
            }
            return await resp.json();
        }
    """,
        {"book": deed_book, "page": page_num, "token": token},
    )

    return result


def clean_result_row(row):
    """Strip grid metadata keys from a result row."""
    return {k: v for k, v in row.items() if k not in METADATA_KEYS}


def extract_parties(results):
    """Extract unique party names from search results."""
    if not isinstance(results, list):
        return []
    parties = set()
    for row in results:
        name = (row.get("party_name") or "").strip()
        cross = (row.get("cross_party_name") or "").strip()
        if name:
            parties.add(name)
        if cross:
            parties.add(cross)
    return sorted(parties)


def reload_page(page):
    """Reload the search page and wait for reCAPTCHA."""
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_function(
        "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'",
        timeout=15000,
    )


def scrape(args):
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
        sys.exit(1)

    client = create_client(url, key)
    lookups = fetch_deed_lookups(client)

    if args.resume:
        completed = load_progress()
        log.info("Resuming: %d already completed", len(completed))
    else:
        completed = set()

    pending = [
        l
        for l in lookups
        if f"{l['deed_book']}:{l['deed_page']}" not in completed
    ]

    if args.limit:
        pending = pending[: args.limit]

    if not pending:
        log.info("Nothing to do — all lookups completed")
        return

    est_seconds = len(pending) * args.delay
    est_hours = est_seconds / 3600
    log.info(
        "%d lookups pending (of %d total), estimated %.1f hours at %.1fs delay",
        len(pending),
        len(lookups),
        est_hours,
        args.delay,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume else "w"
    errors = 0
    consecutive_errors = 0
    start_time = time.time()

    with sync_playwright() as pw:
        browser, page = init_browser(pw)

        try:
            with open(OUTPUT_FILE, mode) as f:
                for i, lookup in enumerate(pending):
                    book = lookup["deed_book"]
                    pg = lookup["deed_page"]
                    lk = f"{book}:{pg}"

                    for attempt in range(3):
                        try:
                            results = search_deed(page, book, pg)

                            is_error = isinstance(results, dict) and results.get(
                                "_error"
                            )

                            if is_error:
                                if attempt < 2:
                                    log.warning(
                                        "  API error on %s (attempt %d): %s",
                                        lk,
                                        attempt + 1,
                                        results.get("body", "")[:200],
                                    )
                                    reload_page(page)
                                    time.sleep(2)
                                    continue
                                log.warning("  Failed after 3 attempts: %s", lk)
                                errors += 1
                                consecutive_errors += 1

                            cleaned = (
                                []
                                if is_error
                                else [clean_result_row(r) for r in results]
                            )

                            record = {
                                "deed_book": book,
                                "deed_page": pg,
                                "pams_pins": lookup["pams_pins"],
                                "results": cleaned,
                                "result_count": len(cleaned),
                                "parties": extract_parties(results),
                                "error": str(results) if is_error else None,
                                "scraped_at": datetime.now(timezone.utc).isoformat(),
                            }

                            f.write(json.dumps(record) + "\n")
                            f.flush()
                            completed.add(lk)
                            if not is_error:
                                consecutive_errors = 0
                            break

                        except Exception as e:
                            if attempt < 2:
                                log.warning(
                                    "  Exception on %s (attempt %d): %s",
                                    lk,
                                    attempt + 1,
                                    e,
                                )
                                try:
                                    reload_page(page)
                                except Exception:
                                    browser.close()
                                    browser, page = init_browser(pw)
                                time.sleep(2)
                            else:
                                log.error("  Failed after 3 attempts: %s — %s", lk, e)
                                errors += 1
                                consecutive_errors += 1

                    if consecutive_errors >= 10:
                        log.error(
                            "10 consecutive errors — stopping. Use --resume to continue."
                        )
                        break

                    if consecutive_errors >= 5:
                        log.warning("5 consecutive errors — reloading browser")
                        browser.close()
                        browser, page = init_browser(pw)
                        consecutive_errors = 0

                    if (i + 1) % 50 == 0:
                        save_progress(completed)
                        elapsed = time.time() - start_time
                        rate = (i + 1) / elapsed
                        remaining = (len(pending) - i - 1) / rate
                        log.info(
                            "  Progress: %d/%d (%.1f%%) — %.1f/sec — ~%.1f hours remaining",
                            i + 1,
                            len(pending),
                            100 * (i + 1) / len(pending),
                            rate,
                            remaining / 3600,
                        )

                    if (i + 1) % 500 == 0:
                        log.info("  Preventive page reload at %d lookups", i + 1)
                        reload_page(page)

                    time.sleep(args.delay)

        finally:
            save_progress(completed)
            browser.close()

    elapsed = time.time() - start_time
    log.info("")
    log.info("=== Scrape Summary ===")
    log.info("Completed:  %d lookups", len(completed))
    log.info("Errors:     %d", errors)
    log.info("Elapsed:    %.1f minutes", elapsed / 60)
    log.info("Output:     %s", OUTPUT_FILE)


def export_csv(args):
    """Convert JSONL output to CSV with one row per parcel."""
    if not OUTPUT_FILE.exists():
        log.error("No JSONL file found at %s", OUTPUT_FILE)
        sys.exit(1)

    csv_file = OUTPUT_DIR / "clerk_deeds.csv"
    rows = 0

    with open(OUTPUT_FILE) as f_in, open(csv_file, "w", newline="") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(
            [
                "pams_pin",
                "deed_book",
                "deed_page",
                "parties",
                "doc_type",
                "rec_date",
                "result_count",
            ]
        )

        for line in f_in:
            record = json.loads(line)
            if record.get("error"):
                continue

            doc_type = ""
            rec_date = ""
            if record["results"]:
                doc_type = record["results"][0].get("doc_type", "")
                rec_date = record["results"][0].get("rec_date", "")

            parties_str = "; ".join(record.get("parties", []))

            for pin in record["pams_pins"]:
                writer.writerow(
                    [
                        pin,
                        record["deed_book"],
                        record["deed_page"],
                        parties_str,
                        doc_type,
                        rec_date,
                        record["result_count"],
                    ]
                )
                rows += 1

    log.info("Exported %d rows to %s", rows, csv_file)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Ocean County Clerk deed records for owner names"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume interrupted scrape (skip completed lookups)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of lookups to perform",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds between requests (default: 1.5)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Convert existing JSONL output to CSV (no scraping)",
    )
    args = parser.parse_args()

    if args.export_csv:
        export_csv(args)
    else:
        scrape(args)


if __name__ == "__main__":
    main()
