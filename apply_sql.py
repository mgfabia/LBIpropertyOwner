#!/usr/bin/env python3
"""
Run a .sql file against Supabase Postgres via the IPv4 pooler.

Use for DDL (CREATE TABLE, indexes) and other raw SQL the REST API can't run.
Connects through the pooler (see db.py) because the direct DB host is IPv6-only.

Usage:
    python apply_sql.py sql/bronze_clerk_deeds.sql
    python apply_sql.py sql/bronze_clerk_deeds.sql --autocommit

Requires SUPABASE_URL and SUPABASE_DB_PASSWORD in environment / .env.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from db import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run a .sql file via the Supabase pooler")
    parser.add_argument("sql_file", help="Path to the .sql file to execute")
    parser.add_argument(
        "--autocommit",
        action="store_true",
        help="Run in autocommit mode (needed for statements that can't run in a transaction)",
    )
    args = parser.parse_args()

    path = Path(args.sql_file)
    if not path.exists():
        log.error("File not found: %s", path)
        sys.exit(1)

    sql = path.read_text()
    load_dotenv()

    log.info("Connecting to Supabase pooler...")
    conn = get_connection(autocommit=args.autocommit)
    log.info("Connected (region: %s)", getattr(conn, "supabase_region", "?"))
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        if not args.autocommit:
            conn.commit()
        log.info("Applied %s", path)
    except Exception as e:
        conn.rollback()
        log.error("Failed to apply %s: %s", path, e)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
