"""MCP server exposing the property knowledge database over stdio, read-only.

Started by the Claude desktop app and by Claude Code in this repo; see CLAUDE.md
for wiring. The tools themselves live in `kdb_tools.py`, shared with the remote
HTTP server in `http_server.py` so there is one definition of each.

    KDB_MCP_DB       .env key naming the target database (default DATABASE_URL)
    KDB_MCP_SECRETS  "0" leaves the decryption tool unregistered entirely
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import describe, get_dsn
from kdb_tools import build_server

DB = os.environ.get("KDB_MCP_DB", "DATABASE_URL")
SECRETS_ENABLED = os.environ.get("KDB_MCP_SECRETS", "1") != "0"

mcp = build_server(DB, secrets_enabled=SECRETS_ENABLED)

if __name__ == "__main__":
    print(f"valta-knowledge-db -> {describe(get_dsn(DB))} "
          f"(secrets {'on' if SECRETS_ENABLED else 'off'})", file=sys.stderr)
    mcp.run(transport="stdio")
