# Remote MCP server — shared Valta connector

The stdio server in `src/mcp_server.py` only works for whoever is sitting at the
Mac that runs it. This is the same database, same tools, reachable by anyone at
Valta as a Claude custom connector.

Both transports register their tools from `src/kdb_tools.py`, so a tool behaves
identically whichever way it is reached. Only the trust model differs.

| | stdio (`src/mcp_server.py`) | remote (`src/http_server.py`) |
|---|---|---|
| Who can reach it | whoever is on that Mac | anyone with a token |
| Credentials | `KDB_MCP_SECRETS=0` removes `get_secret` | per-token `secrets` flag |
| Where the DSN lives | `.env` on the Mac | the host's environment |

## Tokens

Auth is a per-person token, not one shared string, so a departure means deleting
one line instead of re-issuing to everybody, and the logs name who called what.

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

`KDB_TOKENS` is a JSON object of them:

```json
{
  "<48 hex chars>": {"name": "marcus",  "secrets": true},
  "<48 hex chars>": {"name": "va-team", "secrets": false}
}
```

`secrets: false` means `get_secret` refuses that caller before touching the
database — they still get every plaintext field and search. Grant `true` only to
people who need door codes and WiFi logins. The server refuses to start with
`KDB_TOKENS` unset or with any token shorter than 24 characters.

A token can arrive two ways. `Authorization: Bearer <token>` is the better one
and is what a client with a header field should use. Claude's custom connector
UI takes only a URL, so the token goes in the path instead — which means it
lands in host access logs. Rotate a URL-form token if the logs are ever shared.

## Deploy

```bash
npm i -g vercel
vercel link                 # once, in this directory

vercel env add DATABASE_URL production   # the Neon pooler DSN from .env
vercel env add SECRETS_KEY production    # only if any token has secrets:true
vercel env add KDB_TOKENS production     # the JSON above, one line

vercel deploy --prod
```

`requirements.txt` is the server's dependencies only — `pandas`, `openpyxl` and
`sqlalchemy` moved to `requirements-dev.txt` because none of them run on the
server and every megabyte is cold-start time. Rebuild the local venv from
`requirements-dev.txt`, not `requirements.txt`.

Use the **pooler** endpoint in `DATABASE_URL` (the `-pooler` host already in
`.env`). Serverless opens a connection per cold start; the direct endpoint runs
out of connections and the pooler does not.

`SECRETS_KEY` on the host is what lets the server decrypt. Setting it is the
moment credential access leaves your Mac — skip it and set every token to
`secrets: false` if that trade is not worth it.

### On a long-lived host instead

Render, Fly or any box running uvicorn skips `api/` and `vercel.json` entirely:

```bash
uvicorn --app-dir src http_server:app --host 0.0.0.0 --port $PORT
```

That path is the better-supported one. The MCP SDK starts its session manager
from the ASGI lifespan event, which a normal server sends and a serverless
platform may not; `http_server.py` starts it on first request when the host
stays quiet, but a long-lived process never needs that fallback.

## Add the connector in Claude

Each person, on Pro or Max: **Settings → Connectors → Add custom connector**,
URL `https://<app>.vercel.app/mcp/<their token>`. On Team or Enterprise an Owner
adds it once at org level and members enable it individually.

Check it with `curl https://<app>.vercel.app/health` — that route needs no token
and returns `{"ok": true}`.

## Revoke

Delete the person's line from `KDB_TOKENS`, re-set the variable, redeploy. Their
next call gets a 401. No other token changes.

## Untested

Everything here was verified locally: auth accepts and rejects correctly across
both token forms, `get_secret` is refused for a token without the flag before
any query runs, and the server comes up both with and without a lifespan event.

Two things could not be tested from a sandbox and want checking on first deploy:
the live Neon round trip (no route to `neon.tech` from either sandbox), and
Vercel's own routing of `vercel.json` into `api/index.py`. `/health` returning
`{"ok": true}` clears the second; any tool call clears the first.
