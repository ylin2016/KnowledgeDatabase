"""Vercel entrypoint for the remote MCP server.

Vercel routes every request into this one function, so the path Vercel hands us
("/api/index/<token>") is not the path the app expects ("/mcp/<token>"). This
shim rewrites it and delegates. On a long-lived host (Render, Fly, uvicorn) run
`src/http_server.py:app` directly — none of this is needed there.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from http_server import MCP_PATH, app as _app  # noqa: E402

_PREFIXES = ("/api/index", "/api/mcp", "/api", MCP_PATH)


def _normalise(path: str) -> str:
    """Whatever Vercel routed, express it as /mcp or /mcp/<token>."""
    for prefix in _PREFIXES:
        if path == prefix:
            return MCP_PATH
        if path.startswith(prefix + "/"):
            tail = path[len(prefix) + 1:].strip("/")
            return f"{MCP_PATH}/{tail}" if tail else MCP_PATH
    return path


async def app(scope, receive, send):
    if scope["type"] == "http":
        path = _normalise(scope.get("path", "/"))
        scope = {**scope, "path": path, "raw_path": path.encode()}
    await _app(scope, receive, send)
