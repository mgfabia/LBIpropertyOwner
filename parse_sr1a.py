#!/usr/bin/env python3
"""
Download, unzip, filter, and parse NJ Treasury SR1A sales files for LBI.

Reads fixed-width SR1A bulk files (663 bytes/record), filters to the 6 LBI
municipality codes, parses per the official layout, and writes JSONL.

Usage:
    # Download all available years, parse, write JSONL
    python parse_sr1a.py

    # Specific years only
    python parse_sr1a.py --years 2024 2025

    # Re-parse already-downloaded files (skip download)
    python parse_sr1a.py --skip-download

    # Parse a single local text file
    python parse_sr1a.py --input data/sr1a/raw/Sales2024.txt

    # Dry run (parse and show stats, don't write JSONL)
    python parse_sr1a.py --dry-run
"""

import argparse
import json
import logging
import subprocess
import zipfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://www.nj.gov/treasury/taxation/lpt/statdata"

AVAILABLE_FILES = {
    "2020": "Sales2020.zip",
    "2021": "Sales2021.zip",
    "2022": "Sales2022.zip",
    "2023": "Sales2023.zip",
    "2024": "Sales2024.zip",
    "2025": "Sales2025.zip",
    "2026": "YTDSR1A2026.zip",
}

LBI_MUNI_CODES = {"1502", "1504", "1510", "1518", "1529", "1532"}

LBI_MUNI_NAMES = {
    "1502": "Barnegat Light",
    "1504": "Beach Haven",
    "1510": "Harvey Cedars",
    "1518": "Long Beach Twp",
    "1529": "Ship Bottom",
    "1532": "Surf City",
}

DATA_DIR = Path(__file__).parent / "data" / "sr1a"
RAW_DIR = DATA_DIR / "raw"


# ---------------------------------------------------------------------------
# SR1A fixed-width layout (1-indexed positions from official PDF)
# ---------------------------------------------------------------------------

SR1A_FIELDS = [
    # (output_key, start_1indexed, length, type)
    ("county_code",          1,   2, "str"),
    ("district_code",        3,   2, "str"),
    ("total_assessment",     5,  12, "int"),
    ("usable_code",         34,   1, "str"),
    ("non_usable_reason",   35,   3, "str"),
    ("reported_sale_price", 38,   9, "int"),
    ("verified_sale_price", 47,   9, "int"),
    ("assessed_land_value", 56,   9, "int"),
    ("assessed_bldg_value", 65,   9, "int"),
    ("assessed_total_value",74,   9, "int"),
    ("sales_ratio",         83,   5, "decimal2"),
    ("realty_transfer_fee", 88,   9, "decimal2"),
    ("serial_number",       99,   7, "str"),
    ("grantor_name",       110,  35, "str"),
    ("grantee_name",       204,  35, "str"),
    ("grantee_street",     239,  25, "str"),
    ("grantee_city_state", 264,  25, "str"),
    ("grantee_zip",        289,   9, "str"),
    ("property_location",  298,  25, "str"),
    ("aging_date",         323,   6, "str"),
    ("deed_book",          329,   5, "str"),
    ("deed_page",          334,   5, "str"),
    ("deed_date",          339,   6, "str"),
    ("date_recorded",      345,   6, "str"),
    ("block",              351,   5, "str"),
    ("block_suffix",       356,   4, "str"),
    ("lot",                360,   5, "str"),
    ("lot_suffix",         365,   4, "str"),
    ("qualification_codes",620,   5, "str"),
    ("assess_year",        625,   2, "str"),
    ("property_class",     627,   3, "str"),
    ("year_built",         653,   4, "int"),
    ("living_space",       657,   7, "int"),
]


def parse_field(line, start, length, field_type):
    raw = line[start - 1 : start - 1 + length]
    if field_type == "str":
        val = raw.strip()
        return val if val else None
    if field_type == "int":
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            return None
    if field_type == "decimal2":
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val) / 100
        except ValueError:
            return None
    return raw


def mint_pams_pin(county_code, district_code, raw_block, raw_lot, raw_lot_suffix):
    muni_code = county_code + district_code
    block = raw_block.lstrip("0") or "0"
    lot = raw_lot.lstrip("0") or "0"
    suffix = raw_lot_suffix.strip() if raw_lot_suffix else ""
    if suffix:
        lot = f"{lot}.{suffix}"
    return f"{muni_code}_{block}_{lot}"


def parse_line(line):
    record = {}
    for key, start, length, ftype in SR1A_FIELDS:
        record[key] = parse_field(line, start, length, ftype)

    record["municipality_code"] = (record["county_code"] or "") + (record["district_code"] or "")
    record["pams_pin"] = mint_pams_pin(
        record.get("county_code") or "",
        record.get("district_code") or "",
        str(record.get("block") or "0"),
        str(record.get("lot") or "0"),
        record.get("lot_suffix"),
    )
    record["municipality"] = LBI_MUNI_NAMES.get(record["municipality_code"])

    return record


# ---------------------------------------------------------------------------
# Download + unzip
# ---------------------------------------------------------------------------

def download_file(year, force=False):
    filename = AVAILABLE_FILES.get(year)
    if not filename:
        log.warning("No SR1A file available for year %s", year)
        return None

    dest = RAW_DIR / filename
    if dest.exists() and not force:
        log.info("  %s already exists, skipping download", filename)
        return dest

    url = f"{BASE_URL}/{filename}"
    log.info("  Downloading %s ...", url)
    result = subprocess.run(
        [
            "curl", "-sL", "-o", str(dest),
            "-w", "%{http_code}",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            url,
        ],
        capture_output=True, text=True,
    )
    http_code = result.stdout.strip()
    if http_code != "200":
        log.error("  Download failed: HTTP %s for %s", http_code, url)
        dest.unlink(missing_ok=True)
        return None

    size_mb = dest.stat().st_size / 1_048_576
    log.info("  Downloaded %s (%.1f MB)", filename, size_mb)
    return dest


def unzip_file(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        txt_files = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not txt_files:
            log.error("  No .txt files found in %s", zip_path.name)
            return None
        txt_name = txt_files[0]
        dest = RAW_DIR / txt_name
        if dest.exists():
            log.info("  %s already extracted", txt_name)
            return dest
        zf.extract(txt_name, RAW_DIR)
        log.info("  Extracted %s", txt_name)
        return dest


# ---------------------------------------------------------------------------
# Filter + parse
# ---------------------------------------------------------------------------

def process_file(txt_path, source_label):
    total_lines = 0
    lbi_records = []

    with open(txt_path, encoding="latin-1") as f:
        for line in f:
            total_lines += 1
            if len(line.rstrip("\n\r")) < 4:
                continue
            muni_code = line[0:4]
            if muni_code in LBI_MUNI_CODES:
                record = parse_line(line)
                record["source_file"] = source_label
                lbi_records.append(record)

    log.info(
        "  %s: %d total lines → %d LBI records",
        txt_path.name, total_lines, len(lbi_records),
    )
    return lbi_records


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(records):
    by_muni = {}
    usable = 0
    non_usable = 0
    usable_prices = []

    for r in records:
        muni = r.get("municipality") or r.get("municipality_code", "?")
        by_muni[muni] = by_muni.get(muni, 0) + 1

        if r.get("usable_code") == "U":
            usable += 1
            price = r.get("verified_sale_price")
            if price and price > 0:
                usable_prices.append(price)
        else:
            non_usable += 1

    log.info("")
    log.info("=== SR1A Parse Summary ===")
    log.info("Total LBI records: %d", len(records))
    log.info("")
    log.info("By municipality:")
    for muni in sorted(by_muni):
        log.info("  %-20s %d", muni, by_muni[muni])
    log.info("")
    log.info("Usable (arm's-length):  %d", usable)
    log.info("Non-usable:             %d", non_usable)
    if usable_prices:
        avg = sum(usable_prices) / len(usable_prices)
        log.info(
            "Usable price range:     $%s – $%s (avg $%s)",
            f"{min(usable_prices):,.0f}",
            f"{max(usable_prices):,.0f}",
            f"{avg:,.0f}",
        )

    serial_numbers = [r.get("serial_number") for r in records if r.get("serial_number")]
    unique_serials = set(serial_numbers)
    if len(serial_numbers) != len(unique_serials):
        log.warning(
            "Duplicate serial numbers: %d total, %d unique",
            len(serial_numbers), len(unique_serials),
        )
    else:
        log.info("Serial numbers:         %d (all unique)", len(unique_serials))

    pams_pins = set(r.get("pams_pin") for r in records)
    log.info("Unique parcels:         %d", len(pams_pins))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download, filter, and parse NJ SR1A sales files for LBI"
    )
    parser.add_argument(
        "--years", nargs="+",
        help="Specific years to process (default: all available)",
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Skip download, re-parse already-downloaded files",
    )
    parser.add_argument(
        "--force-download", action="store_true",
        help="Re-download even if ZIP already exists locally",
    )
    parser.add_argument(
        "--input", "-i",
        help="Parse a single local .txt file instead of downloading",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and show stats, but don't write JSONL output",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    all_records = []

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            log.error("File not found: %s", input_path)
            return
        records = process_file(input_path, input_path.name)
        all_records.extend(records)

        if not args.dry_run:
            out_path = DATA_DIR / f"sr1a_{input_path.stem}.jsonl"
            with open(out_path, "w") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            log.info("Wrote %d records to %s", len(records), out_path)
    else:
        years = args.years or sorted(AVAILABLE_FILES.keys())
        year_outputs = {}

        for year in years:
            if year not in AVAILABLE_FILES:
                log.warning("No SR1A file for year %s, skipping", year)
                continue

            log.info("Processing year %s:", year)

            if args.skip_download:
                zip_name = AVAILABLE_FILES[year]
                txt_candidates = list(RAW_DIR.glob("*.txt"))
                txt_path = None
                for candidate in txt_candidates:
                    if year in candidate.name or AVAILABLE_FILES[year].replace(".zip", "") in candidate.name:
                        txt_path = candidate
                        break
                if not txt_path:
                    zip_path = RAW_DIR / zip_name
                    if zip_path.exists():
                        txt_path = unzip_file(zip_path)
                if not txt_path:
                    log.warning("  No local file found for year %s, skipping", year)
                    continue
            else:
                zip_path = download_file(year, force=args.force_download)
                if not zip_path:
                    continue
                txt_path = unzip_file(zip_path)
                if not txt_path:
                    continue

            records = process_file(txt_path, AVAILABLE_FILES[year])
            year_outputs[year] = records
            all_records.extend(records)

            if not args.dry_run:
                out_path = DATA_DIR / f"sr1a_{year}.jsonl"
                with open(out_path, "w") as f:
                    for r in records:
                        f.write(json.dumps(r) + "\n")
                log.info("  Wrote %d records to %s", len(records), out_path)

        if not args.dry_run and len(year_outputs) > 1:
            combined_path = DATA_DIR / "sr1a_all.jsonl"
            with open(combined_path, "w") as f:
                for r in all_records:
                    f.write(json.dumps(r) + "\n")
            log.info("Wrote %d combined records to %s", len(all_records), combined_path)

    print_stats(all_records)

    if args.dry_run and all_records:
        log.info("")
        log.info("Sample record (first):")
        sample = {k: v for k, v in all_records[0].items()}
        log.info(json.dumps(sample, indent=2))


if __name__ == "__main__":
    main()
