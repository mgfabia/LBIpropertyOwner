#!/usr/bin/env python3
"""
Ocean County Tax Board scraper for LBI properties.

Scrapes property records from tax.co.ocean.nj.us by municipality and block,
collecting detail page data including mailing addresses, assessments,
building characteristics, and sale history.

Usage:
    # Scrape all LBI municipalities
    python scrape_tax_board.py

    # Scrape a single municipality
    python scrape_tax_board.py --municipality "BARNEGAT LIGHT"

    # Scrape specific blocks
    python scrape_tax_board.py --municipality "BARNEGAT LIGHT" --blocks 12,13,14

    # Resume from where you left off (skips already-scraped detail IDs)
    python scrape_tax_board.py --resume

Output: data/tax_board_details.jsonl (one JSON record per property)
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://tax.co.ocean.nj.us/"
SEARCH_URL = BASE_URL + "frmTaxBoardTaxListSearch"
DETAIL_URL = BASE_URL + "frmTaxBoardTaxListDetail.aspx"

LBI_MUNICIPALITIES = {
    "BARNEGAT LIGHT": 2,
    "BEACH HAVEN": 4,
    "HARVEY CEDARS": 10,
    "LONG BEACH": 17,
    "SHIP BOTTOM": 29,
    "SURF CITY": 32,
}

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_FILE = DATA_DIR / "tax_board_details.jsonl"
PROGRESS_FILE = DATA_DIR / "tax_board_progress.json"

REQUEST_DELAY = 1.0  # seconds between requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class TaxBoardScraper:
    def __init__(self, delay=REQUEST_DELAY):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        })
        self.delay = delay
        self._viewstate = None
        self._viewstate_gen = None
        self._event_validation = None

    def _extract_asp_fields(self, html):
        soup = BeautifulSoup(html, "lxml")
        self._viewstate = soup.find("input", {"name": "__VIEWSTATE"})["value"]
        self._viewstate_gen = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"]
        self._event_validation = soup.find("input", {"name": "__EVENTVALIDATION"})["value"]

    def _init_session(self):
        """GET the search page to establish cookies and extract ASP.NET state."""
        resp = self.session.get(SEARCH_URL)
        resp.raise_for_status()
        self._extract_asp_fields(resp.text)
        log.info("Session initialized")

    def search(self, municipality_code, block="", lot="", street=""):
        """
        POST a search and return list of (detail_id, muni_abbrev, block, lot, qual, address, prop_class).
        """
        if self._viewstate is None:
            self._init_session()

        data = {
            "__VIEWSTATE": self._viewstate,
            "__VIEWSTATEGENERATOR": self._viewstate_gen,
            "__EVENTVALIDATION": self._event_validation,
            "ctl00$LogoFreeholders$FreeholderHistory$FreeholderAccordion_AccordionExtender_ClientState": "-1",
            "ctl00$MainContent$cmbDistrict": str(municipality_code),
            "ctl00$MainContent$txtBlock": str(block),
            "ctl00$MainContent$txtLot": str(lot),
            "ctl00$MainContent$txtQual": "",
            "ctl00$MainContent$cmbPropClass": " ",
            "ctl00$MainContent$txtOwner": "",
            "ctl00$MainContent$txtStreet": street,
            "ctl00$MainContent$btnSearch": "Search",
        }

        time.sleep(self.delay)
        resp = self.session.post(SEARCH_URL, data=data)
        resp.raise_for_status()

        # Update ASP.NET state for next request
        self._extract_asp_fields(resp.text)

        soup = BeautifulSoup(resp.text, "lxml")

        # Check for "collection stopped at 100" warning
        msg_span = soup.find("span", id="MainContent_lblMessage")
        hit_limit = False
        if msg_span and "100 records" in msg_span.get_text():
            hit_limit = True

        results = []
        for link in soup.find_all("a", href=re.compile(r"frmTaxBoardTaxListDetail\.aspx\?ID=\d+")):
            detail_id = re.search(r"ID=(\d+)", link["href"]).group(1)
            row = link.find_parent("tr")
            if row:
                cells = row.find_all("td")
                # cells: [link_cell, muni, block, lot, qual, address, prop_class]
                if len(cells) >= 7:
                    results.append({
                        "detail_id": detail_id,
                        "muni_abbrev": cells[1].get_text(strip=True),
                        "block": cells[2].get_text(strip=True),
                        "lot": cells[3].get_text(strip=True),
                        "qual": cells[4].get_text(strip=True),
                        "address": cells[5].get_text(strip=True),
                        "prop_class": cells[6].get_text(strip=True),
                    })

        return results, hit_limit

    def fetch_detail(self, detail_id, retries=3):
        """Fetch and parse a detail page, returning a dict of all fields."""
        url = f"{DETAIL_URL}?ID={detail_id}"
        for attempt in range(retries):
            time.sleep(self.delay)
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                return self._parse_detail(resp.text, detail_id)
            except (requests.RequestException, KeyError) as e:
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    log.warning("  Retry %d for detail %s after %s (wait %ds)",
                                attempt + 1, detail_id, e, wait)
                    time.sleep(wait)
                else:
                    raise

    def _parse_detail(self, html, detail_id):
        """Extract all property fields from a detail page."""
        soup = BeautifulSoup(html, "lxml")
        record = {"detail_id": detail_id}

        # Strategy: find all adjacent td pairs that look like label/value
        # The detail page uses a table layout with label cells and value cells
        all_cells = soup.find_all("td")
        i = 0
        raw_pairs = []
        while i < len(all_cells) - 1:
            label = all_cells[i].get_text(strip=True).rstrip(":")
            value = all_cells[i + 1].get_text(strip=True)
            if label and len(label) < 50:
                raw_pairs.append((label, value))
            i += 1

        # Map raw label/value pairs to structured fields.
        # Some labels appear multiple times (e.g., "City" for grantee and grantor).
        field_map = {
            "Municipality": "municipality",
            "Deed date": "deed_date",
            "Block / Lot": "block_lot",
            "Qual": "qualifier",
            "Mailing address": "mailing_address",
            "City/State": "mailing_city_state",
            "Location": "property_location",
            "Prop class": "prop_class",
            "Land val": "land_value",
            "Bldg desc": "building_desc",
            "Improvement val": "improvement_value",
            "Land desc": "land_desc",
            "Zone": "zone",
            "Map": "map",
            "Year blt": "year_built",
            "Net value": "net_value",
            "Book/page": "book_page",
            "Last yr taxes": "last_year_taxes",
            "Sale price": "sale_price",
            "Exmt Prop Code": "exemption_code",
            "Type/use": "building_type",
            "Story hgt": "stories",
            "Design": "design",
            "Roof type": "roof_type",
            "Roof mtrl": "roof_material",
            "Ext Finish": "exterior",
            "Foundation": "foundation",
            "Basement": "basement_sqft",
            "Heating src": "heating_source",
            "Heat system": "heating_system",
            "Electric": "electric",
            "A/C": "ac",
            "Plumbing": "plumbing",
            "Fireplace": "fireplace",
            "SFLA": "sfla",
            "Attic area": "attic_area",
            "Unf area": "unfinished_area",
            "# bedrooms": "bedrooms",
            "# bathrooms": "bathrooms",
            "Attchd items": "attached_items",
            "Detchd items": "detached_items",
        }

        # Track grantee/grantor fields (appear in sale history section)
        grantee_grantor_phase = 0  # 0=before, 1=saw grantee, 2=saw grantor

        for label, value in raw_pairs:
            if not value or value == "n/a":
                continue

            # Handle the duplicate labels in sale history
            if label == "Grantee street":
                record["grantee_street"] = value
                grantee_grantor_phase = 1
                continue
            if label == "Grantor street":
                record["grantor_street"] = value
                grantee_grantor_phase = 2
                continue
            if label == "City" and grantee_grantor_phase == 1:
                record["grantee_city_state"] = value
                continue
            if label == "City" and grantee_grantor_phase == 2:
                record["grantor_city_state"] = value
                continue
            if label == "Zip" and grantee_grantor_phase == 1:
                record["grantee_zip"] = value
                grantee_grantor_phase = 0
                continue
            if label == "Zip" and grantee_grantor_phase == 2:
                record["grantor_zip"] = value
                grantee_grantor_phase = 0
                continue
            if label == "Rec date":
                record["recording_date"] = value
                continue
            if label == "Assessment total":
                record["sale_assessment_total"] = value
                continue

            mapped = field_map.get(label)
            if mapped and mapped not in record:
                record[mapped] = value

        # Parse block/lot from combined field
        if "block_lot" in record:
            parts = record["block_lot"].split("/")
            if len(parts) == 2:
                record["block"] = parts[0].strip()
                record["lot"] = parts[1].strip()

        # Parse assessment history from the table
        record["assessment_history"] = self._parse_assessment_history(soup)

        return record

    def _parse_assessment_history(self, soup):
        """Extract multi-year assessment history."""
        history = []
        # Look for the assessment history section
        # Pattern: Year, Prop cls, Land Value, Imprv Val, Net Value
        for label in soup.find_all("span", string=re.compile("Assessment History")):
            table = label.find_parent("div")
            if not table:
                continue
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 5:
                    year = cells[0].get_text(strip=True)
                    if year.isdigit() and len(year) == 4:
                        history.append({
                            "year": year,
                            "prop_class": cells[1].get_text(strip=True),
                            "land_value": cells[2].get_text(strip=True),
                            "improvement_value": cells[3].get_text(strip=True),
                            "net_value": cells[4].get_text(strip=True),
                        })
        return history

    def discover_blocks(self, municipality_code):
        """
        Discover all blocks in a municipality by searching with no filters
        and then probing for higher block numbers.
        """
        log.info("Discovering blocks for municipality code %d", municipality_code)

        # Start by getting initial results with no filter
        results, hit_limit = self.search(municipality_code)
        known_blocks = set()
        all_results = {}
        for r in results:
            known_blocks.add(r["block"])
            all_results[r["detail_id"]] = r

        if hit_limit:
            log.info("Hit 100-record limit on unfiltered search, will probe by block")

        # Now search block by block. Start from the blocks we already found,
        # and probe outward. Block numbers on LBI are typically small integers
        # but can include decimals (e.g., "12.02").
        # Strategy: probe integer blocks 1-200, plus any decimal blocks we discover.
        probed = set()
        consecutive_empty = 0
        for block_num in range(1, 501):
            block_str = str(block_num)
            if block_str in probed:
                continue
            probed.add(block_str)

            results, hit_limit = self.search(municipality_code, block=block_str)
            for r in results:
                known_blocks.add(r["block"])
                all_results[r["detail_id"]] = r

            if results:
                consecutive_empty = 0
                log.info("  Block %s: %d results%s",
                         block_str, len(results),
                         " (HIT LIMIT)" if hit_limit else "")

                if hit_limit:
                    log.warning("  Block %s exceeded 100 results, searching by lot", block_str)
                    empty_lots = 0
                    for lot_num in range(1, 501):
                        lot_results, _ = self.search(
                            municipality_code, block=block_str, lot=str(lot_num)
                        )
                        for r in lot_results:
                            all_results[r["detail_id"]] = r
                        if lot_results:
                            empty_lots = 0
                            log.info("    Block %s Lot %d: %d results",
                                     block_str, lot_num, len(lot_results))
                        else:
                            empty_lots += 1
                            if empty_lots >= 20:
                                break
            else:
                consecutive_empty += 1
                if consecutive_empty >= 15:
                    log.info("  15 consecutive empty blocks after block %d, stopping",
                             block_num - 14)
                    break

        log.info("Discovery complete: %d unique detail IDs across %d blocks",
                 len(all_results), len(known_blocks))
        return list(all_results.values())


def load_progress():
    """Load set of already-scraped detail IDs."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return set(json.load(f).get("scraped_ids", []))
    return set()


def save_progress(scraped_ids):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"scraped_ids": sorted(scraped_ids)}, f)


CSV_COLUMNS = [
    "detail_id", "municipality", "block", "lot", "qualifier",
    "property_location", "prop_class", "zone", "map",
    "mailing_address", "mailing_city_state",
    "land_value", "improvement_value", "net_value",
    "building_desc", "land_desc", "year_built",
    "building_type", "stories", "design", "sfla", "bedrooms", "bathrooms",
    "exterior", "roof_type", "roof_material", "foundation",
    "basement_sqft", "heating_source", "heating_system", "ac", "fireplace",
    "attached_items", "detached_items",
    "deed_date", "book_page", "sale_price", "recording_date",
    "sale_assessment_total",
    "grantee_street", "grantee_city_state", "grantee_zip",
    "grantor_street", "grantor_city_state", "grantor_zip",
    "last_year_taxes", "exemption_code",
]


def export_jsonl_to_csv():
    csv_file = DATA_DIR / "tax_board_details.csv"
    count = 0
    with open(OUTPUT_FILE) as fin, open(csv_file, "w", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for line in fin:
            record = json.loads(line)
            writer.writerow(record)
            count += 1
    log.info("Exported %d records to %s", count, csv_file)


def main():
    parser = argparse.ArgumentParser(description="Scrape Ocean County Tax Board for LBI properties")
    parser.add_argument("--municipality", "-m", help="Single municipality name (e.g., 'BARNEGAT LIGHT')")
    parser.add_argument("--blocks", "-b", help="Comma-separated block numbers to scrape")
    parser.add_argument("--resume", action="store_true", help="Skip already-scraped detail IDs")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="Seconds between requests")
    parser.add_argument("--discover-only", action="store_true", help="Only run discovery, skip detail fetch")
    parser.add_argument("--export-csv", action="store_true", help="Convert existing JSONL to CSV and exit")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    if args.export_csv:
        export_jsonl_to_csv()
        return

    munis = LBI_MUNICIPALITIES
    if args.municipality:
        name = args.municipality.upper()
        if name not in munis:
            log.error("Unknown municipality: %s. Valid: %s", name, ", ".join(munis.keys()))
            sys.exit(1)
        munis = {name: munis[name]}

    scraped_ids = load_progress() if args.resume else set()
    scraper = TaxBoardScraper(delay=args.delay)

    # Phase 1: Discovery
    all_search_results = []
    for muni_name, muni_code in munis.items():
        log.info("=== %s (code %d) ===", muni_name, muni_code)

        if args.blocks:
            block_list = args.blocks.split(",")
            muni_results = []
            for block in block_list:
                results, hit_limit = scraper.search(muni_code, block=block.strip())
                muni_results.extend(results)
                log.info("  Block %s: %d results", block.strip(), len(results))
        else:
            muni_results = scraper.discover_blocks(muni_code)

        all_search_results.extend(muni_results)
        log.info("  Total for %s: %d properties", muni_name, len(muni_results))

    # Deduplicate by detail_id
    unique = {r["detail_id"]: r for r in all_search_results}
    log.info("Total unique properties found: %d", len(unique))

    if args.discover_only:
        discovery_file = DATA_DIR / "tax_board_discovery.json"
        with open(discovery_file, "w") as f:
            json.dump(list(unique.values()), f, indent=2)
        log.info("Discovery results saved to %s", discovery_file)
        return

    # Phase 2: Fetch details
    to_fetch = [did for did in unique if did not in scraped_ids]
    log.info("Detail pages to fetch: %d (skipping %d already scraped)",
             len(to_fetch), len(unique) - len(to_fetch))

    fetched = 0
    errors = 0
    with open(OUTPUT_FILE, "a") as out:
        for detail_id in to_fetch:
            try:
                record = scraper.fetch_detail(detail_id)
                # Merge search-level fields
                search_info = unique[detail_id]
                record["search_municipality"] = search_info.get("muni_abbrev", "")
                record["search_address"] = search_info.get("address", "")

                out.write(json.dumps(record) + "\n")
                scraped_ids.add(detail_id)
                fetched += 1

                if fetched % 25 == 0:
                    save_progress(scraped_ids)
                    log.info("  Progress: %d/%d fetched (%d errors)",
                             fetched, len(to_fetch), errors)

            except Exception as e:
                log.error("  Error fetching detail %s: %s", detail_id, e)
                errors += 1
                if errors > 20:
                    log.error("Too many errors, stopping")
                    break

    save_progress(scraped_ids)
    log.info("Done. %d records fetched, %d errors. Output: %s", fetched, errors, OUTPUT_FILE)


if __name__ == "__main__":
    main()
