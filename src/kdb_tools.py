"""Tool definitions for the property knowledge database, shared by both transports.

`src/mcp_server.py` (stdio, local) and `src/http_server.py` (streamable HTTP,
remote) both call `build_server()` so there is exactly one definition of what a
tool does. The only axis that varies is `secrets_enabled`: a server built with
it False never registers `get_secret` at all, so the tool cannot be called by a
caller who was never meant to reach credentials — it is absent from tools/list
rather than guarded at call time.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from db import connect
from secrets_io import get_key

MAX_ROWS = 200

INSTRUCTIONS = (
    "Valta property onboarding data in Postgres. A PROPERTY groups one or "
    "more LISTINGS: 'Seattle 10057' has Whole/Upper/Lower listings that share "
    "an address but have different door codes, bed counts and cleaning fees, "
    "so answer per listing unless asked about the building. Most facts live "
    "in listing_attrs as label/value text — use search() first."
)

_SECRETS_NOTE = (
    " Credentials (door codes, WiFi and utility logins) are encrypted in "
    "listing_secrets and only get_secret() can read them."
)

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def build_server(
    db: str,
    *,
    secrets_enabled: bool,
    name: str = "valta-knowledge-db",
    secrets_guard: Callable[[], str | None] | None = None,
) -> MCPServer:
    """An MCPServer with the shaped read-only tools registered against `db`.

    `secrets_enabled=False` leaves `get_secret` unregistered entirely — the tool
    does not exist and cannot be called. That is the stdio switch.

    `secrets_guard` is the remote equivalent, checked per call because one HTTP
    process serves callers with different rights: it returns an error string to
    refuse the call, or None to allow it. Absent, every caller may decrypt.
    """

    def _query(sql: str, params: tuple = (), limit: int | None = None) -> list[dict[str, Any]]:
        with connect(db) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    mcp = MCPServer(
        name=name,
        instructions=INSTRUCTIONS + (_SECRETS_NOTE if secrets_enabled else ""),
    )

    @mcp.tool()
    def list_properties() -> list[dict]:
        """Every property, with its listing count. Start here to find the right name."""
        return _query("""
            select p.nickname, p.address, p.property_type,
                   count(l.listing_id) as listings,
                   count(*) filter (where l.role = 'main') as whole_property_listing
            from properties p left join listings l using (property_id)
            group by p.property_id, p.nickname, p.address, p.property_type
            order by p.nickname
        """)

    @mcp.tool()
    def get_property(nickname: str) -> dict:
        """One property: its owners and every listing with the promoted columns.

        `nickname` matches case-insensitively and on a partial name.
        """
        props = _query(
            "select * from properties where nickname ilike %s order by nickname",
            (f"%{nickname}%",),
        )
        if not props:
            return {"error": f"no property matching {nickname!r}", "hint": "call list_properties"}
        if len(props) > 1:
            return {"error": "ambiguous", "matches": [p["nickname"] for p in props]}

        p = props[0]
        pid = p["property_id"]
        return {
            "property": p,
            "owners": _query("""
                select o.full_name, o.email, o.phone, po.ordinal
                from property_owners po join owners o using (owner_id)
                where po.property_id = %s order by po.ordinal
            """, (pid,)),
            "listings": _query("""
                select listing_id, nickname, role, ordinal, rental_type, title, address,
                       bedrooms, bathrooms, sleeps, square_footage,
                       cleaning_fee, cleaning_cost
                from listings where property_id = %s order by ordinal
            """, (pid,)),
        }

    @mcp.tool()
    def get_listing(nickname: str, category: str | None = None) -> dict:
        """Every plaintext field of one listing, plus the NAMES of its secret fields.

        Secret values are not included. `category` narrows to one section of the
        workbook (Maintenance, Access, Utilities...).
        """
        rows = _query("select listing_id, nickname, role from listings where nickname ilike %s",
                      (f"%{nickname}%",))
        if not rows:
            return {"error": f"no listing matching {nickname!r}"}
        if len(rows) > 1:
            return {"error": "ambiguous", "matches": [r["nickname"] for r in rows]}

        lid = rows[0]["listing_id"]
        where, params = "listing_id = %s", [lid]
        if category:
            where += " and category ilike %s"
            params.append(f"%{category}%")

        return {
            "listing": rows[0],
            "fields": _query(
                f"select category, subcategory, label, value from listing_attrs "
                f"where {where} order by category nulls first, subcategory nulls first, label",
                tuple(params),
            ),
            "secret_fields": [
                r["label"] for r in
                _query("select label from listing_secrets where listing_id = %s order by label", (lid,))
            ],
        }

    @mcp.tool()
    def search(query: str, limit: int = 25) -> list[dict]:
        """Free-text search across every listing's field labels and values.

        'circuit breaker', 'garbage day' or a partial address all work. Returns
        which listing each hit belongs to.
        """
        return _query("""
            select p.nickname as property, l.nickname as listing, l.role,
                   a.category, a.subcategory, a.label, a.value
            from listing_attrs a
            join listings l using (listing_id)
            join properties p using (property_id)
            where a.value ilike %s or a.label ilike %s
            order by p.nickname, l.ordinal, a.label
        """, (f"%{query}%", f"%{query}%"), limit=min(limit, MAX_ROWS))

    @mcp.tool()
    def list_secret_fields(listing: str | None = None) -> list[dict]:
        """Which credential fields exist, and for which listing. Values are NOT returned."""
        if listing:
            return _query("""
                select p.nickname as property, l.nickname as listing, s.label
                from listing_secrets s join listings l using (listing_id)
                join properties p using (property_id)
                where l.nickname ilike %s order by l.nickname, s.label
            """, (f"%{listing}%",), limit=MAX_ROWS)
        return _query("""
            select s.label, count(*) as listings
            from listing_secrets s group by s.label order by count(*) desc, s.label
        """)

    @mcp.tool()
    def sql(query: str, limit: int = 50) -> list[dict] | dict:
        """Run one read-only SELECT for questions the shaped tools cannot answer.

        Runs inside a read-only transaction, so a write fails at the server
        rather than on trust. Tables: properties, owners, property_owners,
        listings, listing_attrs (label/value text), listing_secrets (value_enc
        bytea, opaque).
        """
        if not _READ_ONLY.match(query):
            return {"error": "only SELECT / WITH queries are accepted"}
        if ";" in query.rstrip().rstrip(";"):
            return {"error": "one statement at a time"}
        try:
            with connect(db) as conn, conn.cursor() as cur:
                cur.execute("set transaction read only")
                cur.execute(query)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchmany(min(limit, MAX_ROWS))]
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    if secrets_enabled:

        @mcp.tool()
        def get_secret(listing: str, label: str) -> dict:
            """Decrypt ONE credential — a door code, WiFi or utility login.

            Both arguments are required so a single call cannot dump a
            property's credentials; call list_secret_fields first to see what
            exists. The key is passed per query, never stored in the database.
            """
            if secrets_guard is not None:
                refusal = secrets_guard()
                if refusal:
                    return {"error": refusal}
            rows = _query("""
                select p.nickname as property, l.nickname as listing, s.label,
                       pgp_sym_decrypt(s.value_enc, %s) as value
                from listing_secrets s join listings l using (listing_id)
                join properties p using (property_id)
                where l.nickname ilike %s and s.label ilike %s
            """, (get_key(), f"%{listing}%", f"%{label}%"), limit=10)
            if not rows:
                return {"error": f"no secret {label!r} on a listing matching {listing!r}",
                        "hint": "call list_secret_fields"}
            return {"results": rows}

    return mcp
