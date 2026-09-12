"""The knowledge database as a remote MCP server, for Claude custom connectors.

One deployed process serves everyone at Valta, so unlike the stdio server it
cannot rely on the machine it runs on being trusted. Two things follow:

  * Every request carries a token. Tokens are issued per person in KDB_TOKENS,
    so one person can be revoked without rotating everyone, and the logs name
    who called what.
  * Credential decryption is a per-token right, not a property of the server.
    A token without "secrets" gets a refusal from get_secret; the tool still
    lists, because one process serves callers with different rights.

The token may arrive either as `Authorization: Bearer <token>` or as the last
path segment (`/mcp/<token>`). The path form exists because Claude's custom
connector UI takes a URL and no headers; it also means the token lands in
access logs, so treat a URL-form token as the weaker of the two.

    KDB_MCP_DB   .env key naming the target database (default DATABASE_URL)
    KDB_TOKENS   JSON: {"<token>": {"name": "Marcus", "secrets": false}, ...}
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kdb_tools import build_server

log = logging.getLogger("kdb.http")

DB = os.environ.get("KDB_MCP_DB", "DATABASE_URL")
MCP_PATH = "/mcp"

_caller: contextvars.ContextVar[dict] = contextvars.ContextVar("caller", default={})


def _load_tokens() -> dict[str, dict]:
    raw = os.environ.get("KDB_TOKENS", "").strip()
    if not raw:
        raise RuntimeError(
            "KDB_TOKENS is not set. Refusing to start an unauthenticated server "
            "in front of a database holding live door codes."
        )
    tokens = json.loads(raw)
    if not isinstance(tokens, dict) or not tokens:
        raise RuntimeError("KDB_TOKENS must be a non-empty JSON object")
    for tok, meta in tokens.items():
        if len(tok) < 24:
            raise RuntimeError(
                f"token for {meta.get('name', '?')!r} is {len(tok)} chars; "
                "use at least 24 (see `openssl rand -hex 24`)"
            )
    return tokens


TOKENS = _load_tokens()


def _guard() -> str | None:
    """Refuse get_secret unless this request's token was granted credentials."""
    caller = _caller.get()
    if caller.get("secrets"):
        log.info("get_secret allowed for %s", caller.get("name"))
        return None
    log.warning("get_secret refused for %s", caller.get("name", "unknown"))
    return (
        "This connector token is not authorised to decrypt credentials. "
        "Ask a Valta admin for a token with credential access."
    )


mcp = build_server(DB, secrets_enabled=True, secrets_guard=_guard)

# stateless: no session is carried between requests, so any instance can serve
# any call. json_response: plain JSON bodies rather than an SSE stream, which
# survives hosts that buffer responses.
inner = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
)


# --- lifespan -----------------------------------------------------------
# The SDK's HTTP app starts its session manager from the ASGI lifespan event and
# raises "Task group is not initialized" on the first request otherwise. A
# long-lived host (uvicorn, Render, Fly) sends lifespan and this is a no-op. A
# serverless host that skips it would break every request, so the first request
# starts the manager itself and leaves it running for the life of the instance.

_started = asyncio.Event()
_start_lock: asyncio.Lock | None = None


async def _run_lifespan() -> None:
    """Drive the inner app's lifespan and hold it open for this process."""
    never = asyncio.Event()

    async def receive():
        if not _started.is_set():
            return {"type": "lifespan.startup"}
        await never.wait()  # hold the app open; no shutdown in serverless
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] in ("lifespan.startup.complete", "lifespan.startup.failed"):
            _started.set()

    await inner({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)


async def _ensure_started() -> None:
    global _start_lock
    if _started.is_set():
        return
    if _start_lock is None:
        _start_lock = asyncio.Lock()
    async with _start_lock:
        if _started.is_set():
            return
        asyncio.create_task(_run_lifespan())
        await asyncio.wait_for(_started.wait(), timeout=10)


async def _unauthorized(send, detail: str) -> None:
    body = json.dumps({"error": "unauthorized", "detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b'Bearer realm="valta-knowledge-db"'),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _extract(scope) -> tuple[str | None, str]:
    """(token, rewritten path). Accepts /mcp/<token> or an Authorization header."""
    path = scope.get("path", "") or "/"
    prefix = MCP_PATH + "/"
    if path.startswith(prefix):
        tail = path[len(prefix):].strip("/")
        if tail:
            return tail, MCP_PATH

    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip(), path
    return None, path


async def app(scope, receive, send):
    """ASGI entrypoint: authenticate, then hand off to the MCP transport."""
    if scope["type"] == "lifespan":
        _started.set()  # the host drives it; skip the lazy path entirely
        await inner(scope, receive, send)
        return

    if scope["type"] != "http":
        await inner(scope, receive, send)
        return

    if scope.get("path") in ("/", "/health"):
        body = json.dumps({"ok": True, "server": "valta-knowledge-db"}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
        return

    token, path = _extract(scope)
    if token is None:
        await _unauthorized(send, "no token; use Authorization: Bearer or /mcp/<token>")
        return

    meta = TOKENS.get(token)
    if meta is None:
        log.warning("rejected unknown token (%d chars)", len(token))
        await _unauthorized(send, "unrecognised token")
        return

    await _ensure_started()

    caller = {"name": meta.get("name", "unnamed"), "secrets": bool(meta.get("secrets"))}
    _caller.set(caller)
    log.info("%s -> %s", caller["name"], scope.get("method"))

    await inner({**scope, "path": path, "raw_path": path.encode()}, receive, send)
