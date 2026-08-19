"""Read/write helpers for the encrypted listing_secrets table.

The key never touches the database: pgp_sym_encrypt/decrypt take it as a bind
parameter on each call, so it exists only in this process and in .env.
"""

import os

import psycopg

from db import ROOT  # noqa: F401  (ensures .env is loaded)


def get_key() -> str:
    key = os.environ.get("SECRETS_KEY")
    if not key:
        raise RuntimeError("SECRETS_KEY not set; expected it in .env")
    return key


def put_secret(
    conn: psycopg.Connection,
    listing_id: int,
    field: str,
    value: str,
    *,
    category: str | None = None,
    subcategory: str | None = None,
    label: str | None = None,
) -> None:
    conn.execute(
        """
        insert into listing_secrets
            (listing_id, field, category, subcategory, label, value_enc)
        values (%s, %s, %s, %s, %s, pgp_sym_encrypt(%s, %s))
        on conflict (listing_id, field) do update
            set value_enc  = excluded.value_enc,
                category   = excluded.category,
                subcategory = excluded.subcategory,
                label      = excluded.label,
                updated_at = now()
        """,
        (listing_id, field, category, subcategory, label, value, get_key()),
    )


def get_secret(conn: psycopg.Connection, listing_id: int, field: str) -> str | None:
    row = conn.execute(
        """
        select pgp_sym_decrypt(value_enc, %s)
        from listing_secrets
        where listing_id = %s and field = %s
        """,
        (get_key(), listing_id, field),
    ).fetchone()
    return row[0] if row else None


def list_secrets(conn: psycopg.Connection, listing_id: int) -> list[tuple[str, str]]:
    """All decrypted (field, value) pairs for one listing."""
    return conn.execute(
        """
        select field, pgp_sym_decrypt(value_enc, %s)
        from listing_secrets
        where listing_id = %s
        order by field
        """,
        (get_key(), listing_id),
    ).fetchall()
