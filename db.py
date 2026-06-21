"""Shared Supabase Postgres connection via the IPv4 pooler (Supavisor).

The *direct* connection host (db.<ref>.supabase.co) is IPv6-only and is
unreachable from IPv4-only networks ("No route to host"). The connection
pooler (aws-0-<region>.pooler.supabase.com) is reachable over IPv4, so use
this for any raw SQL — DDL, PostGIS UPDATEs — that the Supabase REST API
can't run. Regular data loads go through supabase-py's REST client (HTTPS) and
do NOT need this module.

Env vars (in .env):
    SUPABASE_URL          - https://<ref>.supabase.co        (already required)
    SUPABASE_DB_PASSWORD  - database password
                            (Supabase Dashboard > Project Settings > Database)
    SUPABASE_DB_REGION    - optional, e.g. us-east-1. Auto-detected if unset;
                            set it afterwards to skip the region probe.
    SUPABASE_DB_HOST      - optional, full pooler host (e.g.
                            aws-1-us-east-1.pooler.supabase.com). If set, used
                            directly and the region/cluster probe is skipped.

Note: this is the database password, NOT the service-role JWT. The service-role
key authenticates against the REST API only; it is not a Postgres password.
"""

import logging
import os
import re

# Pooler regions to probe when SUPABASE_DB_REGION is unset. Ordered US-first
# since this project's data is NJ/Ocean County. Wrong-region poolers reject the
# tenant quickly, so the probe is fast.
POOLER_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1",
    "eu-west-1", "eu-west-2", "eu-central-1", "sa-east-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-south-1",
]

# Pooler cluster prefixes. Newer projects live on "aws-1", older on "aws-0".
POOLER_PREFIXES = ["aws-1", "aws-0"]

# Session-mode pooler port (supports DDL and prepared statements). Transaction
# mode (6543) does not support prepared statements, which psycopg2 may use.
POOLER_PORT = 5432


def project_ref(supabase_url):
    """Extract the project ref from a Supabase URL (https://<ref>.supabase.co)."""
    m = re.match(r"https://([^.]+)\.supabase\.co", supabase_url)
    if not m:
        raise ValueError(f"Cannot parse project ref from SUPABASE_URL: {supabase_url!r}")
    return m.group(1)


def get_connection(autocommit=False, connect_timeout=10):
    """Open a psycopg2 connection to Supabase Postgres via the IPv4 pooler.

    Auto-detects the region if SUPABASE_DB_REGION is unset. The resolved region
    is stored on the connection as `conn.supabase_region` for logging.
    """
    import psycopg2

    url = os.environ.get("SUPABASE_URL")
    password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not url:
        raise RuntimeError("Missing SUPABASE_URL in environment / .env")
    if not password:
        raise RuntimeError(
            "Missing SUPABASE_DB_PASSWORD in environment / .env. Get it from "
            "Supabase Dashboard > Project Settings > Database > Database password "
            "(this is the DB password, not the service-role key)."
        )

    ref = project_ref(url)
    user = f"postgres.{ref}"

    # An explicit full host short-circuits the probe.
    explicit_host = os.environ.get("SUPABASE_DB_HOST")
    if explicit_host:
        hosts = [explicit_host]
    else:
        region = os.environ.get("SUPABASE_DB_REGION")
        regions = [region] if region else POOLER_REGIONS
        hosts = [f"{p}-{r}.pooler.supabase.com" for r in regions for p in POOLER_PREFIXES]

    last_err = None
    for host in hosts:
        try:
            conn = psycopg2.connect(
                host=host,
                port=POOLER_PORT,
                dbname="postgres",
                user=user,
                password=password,
                sslmode="require",
                connect_timeout=connect_timeout,
            )
            conn.autocommit = autocommit
            # psycopg2 connections don't accept custom attributes, so report the
            # resolved host via the logger instead of stashing it on `conn`.
            logging.getLogger(__name__).info("Connected via pooler host: %s", host)
            return conn
        except psycopg2.OperationalError as e:
            # Wrong region/cluster rejects the tenant ("not found"); a bad
            # password fails everywhere.
            last_err = e
            if "password authentication failed" in str(e).lower():
                raise RuntimeError(
                    "Password authentication failed against the pooler. Check "
                    "SUPABASE_DB_PASSWORD (DB password, not the service-role key)."
                ) from e
            continue

    raise RuntimeError(
        f"Could not connect via the pooler ({len(hosts)} host(s) tried). Set "
        "SUPABASE_DB_HOST explicitly (e.g. aws-1-us-east-1.pooler.supabase.com). "
        f"Last error: {last_err}"
    )
