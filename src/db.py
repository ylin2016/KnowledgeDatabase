"""Neon Postgres connection helper.

Reads DATABASE_URL from .env at the project root. Never hardcode credentials.
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")


def get_dsn(name: str = "DATABASE_URL") -> str:
    """Connection string from .env. `name` selects which one.

    More than one Neon project holds this schema, so the target is named rather
    than assumed — see CLAUDE.md on which is current.
    """
    dsn = os.environ.get(name)
    if not dsn:
        raise RuntimeError(f"{name} not set; expected it in {ROOT / '.env'}")
    return dsn


def describe(dsn: str) -> str:
    """The endpoint host, for printing before a write. Never shows the password."""
    host = dsn.split("@", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0]


def connect(name: str = "DATABASE_URL") -> psycopg.Connection:
    return psycopg.connect(get_dsn(name))


if __name__ == "__main__":
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select version(), current_database(), current_user")
        version, database, user = cur.fetchone()
        print(f"connected  db={database}  user={user}")
        print(version)

        cur.execute(
            """
            select table_schema, table_name
            from information_schema.tables
            where table_schema not in ('pg_catalog', 'information_schema')
            order by 1, 2
            """
        )
        rows = cur.fetchall()
        print(f"\nexisting tables ({len(rows)}):")
        for schema, table in rows:
            print(f"  {schema}.{table}")
