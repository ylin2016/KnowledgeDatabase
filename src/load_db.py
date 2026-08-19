"""Write a parsed workbook into Postgres.

Reload is destructive per property: listings (and their attrs/secrets, by
cascade) are deleted and rebuilt, so a field removed from the workbook does not
linger in the database. Properties themselves are upserted on nickname.
"""

from __future__ import annotations

import datetime as dt

import psycopg

from secrets_io import get_key


def _upsert_owner(conn: psycopg.Connection, owner: dict) -> int:
    row = conn.execute(
        """
        insert into owners (full_name, email, phone, comm_method, comm_handle)
        values (%(full_name)s, %(email)s, %(phone)s, %(comm_method)s, %(comm_handle)s)
        on conflict (lower(full_name), coalesce(lower(email), '')) do update
            set phone       = coalesce(excluded.phone, owners.phone),
                comm_method = coalesce(excluded.comm_method, owners.comm_method),
                comm_handle = coalesce(excluded.comm_handle, owners.comm_handle),
                updated_at  = now()
        returning owner_id
        """,
        owner,
    ).fetchone()
    return row[0]


def load(
    conn: psycopg.Connection,
    parsed: dict,
    *,
    source_file_id: str | None = None,
    source_modified_at: dt.datetime | None = None,
) -> dict:
    """Load one parsed workbook. Caller commits."""
    prop = parsed["property"]

    property_id = conn.execute(
        """
        insert into properties (nickname, address, property_type, nwmls_id,
                                source_file_id, source_file_name,
                                source_modified_at, synced_at)
        values (%s, %s, %s, %s, %s, %s, %s, now())
        on conflict (nickname) do update
            set address            = excluded.address,
                property_type      = excluded.property_type,
                nwmls_id           = excluded.nwmls_id,
                source_file_id     = excluded.source_file_id,
                source_file_name   = excluded.source_file_name,
                source_modified_at = excluded.source_modified_at,
                synced_at          = now(),
                updated_at         = now()
        returning property_id
        """,
        (
            prop["nickname"], prop["address"], prop["property_type"], prop["nwmls_id"],
            source_file_id, prop["source_file_name"], source_modified_at,
        ),
    ).fetchone()[0]

    # Owners: relink from scratch so a removed owner stops being associated,
    # while the owners row itself survives for other properties.
    conn.execute("delete from property_owners where property_id = %s", (property_id,))
    for owner in parsed["owners"]:
        owner_id = _upsert_owner(conn, owner)
        conn.execute(
            """
            insert into property_owners (property_id, owner_id, ordinal)
            values (%s, %s, %s)
            on conflict (property_id, owner_id) do update set ordinal = excluded.ordinal
            """,
            (property_id, owner_id, owner["ordinal"]),
        )

    # Listings are rebuilt wholesale; attrs and secrets cascade away with them.
    conn.execute("delete from listings where property_id = %s", (property_id,))

    key = get_key()
    n_attrs = n_secrets = 0

    for listing in parsed["listings"]:
        listing_id = conn.execute(
            """
            insert into listings (property_id, ordinal, role, nickname, title,
                                  rental_type, address, property_type, account_link,
                                  listing_3d_link, house_manual_link, square_footage,
                                  bedrooms, bathrooms, sleeps, cleaning_fee, cleaning_cost)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning listing_id
            """,
            (
                property_id, listing["ordinal"], listing["role"],
                listing.get("nickname"), listing.get("title"), listing.get("rental_type"),
                listing.get("address"), listing.get("property_type"),
                listing.get("account_link"), listing.get("listing_3d_link"),
                listing.get("house_manual_link"), listing.get("square_footage"),
                listing.get("bedrooms"), listing.get("bathrooms"), listing.get("sleeps"),
                listing.get("cleaning_fee"), listing.get("cleaning_cost"),
            ),
        ).fetchone()[0]

        if listing["attrs"]:
            conn.cursor().executemany(
                """
                insert into listing_attrs (listing_id, field, category, subcategory, label, value)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (listing_id, field) do update
                    set value = excluded.value, updated_at = now()
                """,
                [(listing_id, *a) for a in listing["attrs"]],
            )
            n_attrs += len(listing["attrs"])

        if listing["secrets"]:
            conn.cursor().executemany(
                """
                insert into listing_secrets (listing_id, field, category, subcategory,
                                             label, value_enc)
                values (%s, %s, %s, %s, %s, pgp_sym_encrypt(%s, %s))
                on conflict (listing_id, field) do update
                    set value_enc = excluded.value_enc, updated_at = now()
                """,
                [(listing_id, f, c, s, l, v, key) for f, c, s, l, v in listing["secrets"]],
            )
            n_secrets += len(listing["secrets"])

    return {
        "property_id": property_id,
        "nickname": prop["nickname"],
        "owners": len(parsed["owners"]),
        "listings": len(parsed["listings"]),
        "attrs": n_attrs,
        "secrets": n_secrets,
    }
