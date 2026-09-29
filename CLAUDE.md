# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ingests Valta's per-property **Property Onboarding** workbooks from Google Drive into a
**Neon-hosted Postgres** database, so property facts (access, WiFi, utilities, cleaning,
maintenance) are queryable instead of scattered across dozens of xlsx files.

## Which database

**Two** Neon projects are kept in sync, both loaded from the same import:

| `.env` key | Project | Endpoint |
|---|---|---|
| `DATABASE_URL` | `bold-shadow-32986422` ("Listing Knowledge Database", org Valta Realty) | `ep-twilight-grass-axuzl5tw` |
| `WEATHERED_URL` | `falling-voice-12713841` | `ep-weathered-dream-axp1hcow` |

`--db <key>` selects the target and every write prints the endpoint first, so nothing lands
in a database by default. Loading both is two runs of the same command; they are compared by
row count afterwards, not assumed equal.

`falling-voice-12713841` once showed "project not found" in the console and was written off as
abandoned. It is reachable and connects fine — the schema was there, empty. Do not re-add that
note; check with the query below before believing any claim about which project is live.

`neon_auth.*` tables live alongside in this project and are unrelated to this pipeline.
Leave them alone.

To confirm which database a session is actually attached to:

```sql
select current_setting('neon.project_id'), current_setting('neon.endpoint_id');
```

## Commands

`./kdb <script.py> [args]` runs a `src/` script under the project venv from any directory.
Plain `python src/...` picks up the system interpreter, which has no psycopg — that
`ModuleNotFoundError` means the wrong interpreter, not a broken install.

```bash
./kdb import_onboarding.py --dry-run           # the common case

./kdb db.py                                    # connectivity + table list
./kdb discover.py [substring]                  # Active listings + the workbook each resolved to
./kdb inspect_index.py                         # columns and sample rows of the index sheet
./kdb inspect_missing.py                       # for unresolved listings, what is actually on disk
./kdb parse_workbook.py X.xlsx                 # parse one workbook, write nothing

./kdb import_onboarding.py --dry-run           # parse everything, write nothing
./kdb import_onboarding.py                     # -> DATABASE_URL
./kdb import_onboarding.py --db WEATHERED_URL  # the second project
./kdb import_onboarding.py --property OSBR     # one property
./kdb import_onboarding.py --dir inputs/       # loose xlsx, ignoring the index

./kdb reset_db.py --db DATABASE_URL --yes      # delete all rows, keep the schema

# (Re)apply schema — every statement is idempotent (create ... if not exists).
./venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from db import connect, ROOT
with connect() as c: c.execute((ROOT/'schema.sql').read_text()); c.commit()"
```

Import output goes to `out/`, which is gitignored — it contains live property data.

`psql` is installed via `brew install libpq` (keg-only, so `/opt/homebrew/opt/libpq/bin/psql`).
`src/db.py` remains the path of least resistance.

Import is idempotent per property: `load_db.load()` deletes and rebuilds that property's
listings (attrs and secrets cascade), so a field deleted from a workbook disappears from the
database instead of lingering. Re-running the whole import is always safe.

That idempotence is keyed on `properties.nickname`, which is why it cannot clean up after a
change in *shape*. When OSBR's cottages became listings under one property, the 20 old
`Cottage 1` / `Beachwood 4` / `Bellevue 2323 Main` property rows stopped being written to and
survived every re-import. `reset_db.py` exists for exactly that; re-import alone will not fix
a nickname that no longer occurs.

## The index defines the structure, not the workbooks

`Cohost_Property_PPTs_Locations.xlsx`, sheet `PropertyFolder`, is authoritative.
Columns: `Property.folder`, `Listings`, `Property`, `Term`, `Status`, `File` —
**one row per listing**, not per property.

Two arrangements both occur, so neither can be assumed:

| Property | Listings | Workbooks |
|---|---|---|
| `Beachwood` | `Beachwood 1` … `Beachwood 10` | ten, one per listing |
| `Bellevue 14507` | `U1`, `U2`, `U3` | one, shared |

A `/` inside `Property.folder` is **ambiguous** — three different things, distinguishable only
by looking at the disk:

| Cell | Meaning |
|---|---|
| `… (OSBR)/Cottage 1`, `… Beachwood …/Unit 1` | a real subdirectory holding that listing's workbook |
| `… 710 N 97th St. - Isabell/710 ADU` | no such directory; the workbook is in the parent |
| `… Scott & Debra/Unit E1 - live 7/17/26` | part of the directory's own name |

So `discover.descend()` treats every `/` as a *candidate* boundary and tries the longest run
first: a name that exists as written beats any split of it. Prefix guessing is allowed only on
a lone trailing segment — otherwise the parent folder prefix-matches the whole cell and eats
the subdirectory it was supposed to lead to. It returns `(deepest dir, leftover)`; a leftover
like `710 ADU` is the signal to search the parent.

The third case is not cosmetic: POSIX forbids `/` in a filename, so Drive stores that folder as
`Unit E1 - live 7:17:26` and Finder renders the `:` back as `/`. `_norm()` folds `:` to `/` so
the index text and the real name compare equal.

Never split the cell eagerly. Stripping after the first `/` appears to work only because
`resolve_file` then `rglob`s the whole property tree — which is how archived copies win over
the live sheet.

`src/build.py` joins the two: the index supplies each listing's identity and `Term`; the
workbook supplies field values, matched to a listing by nickname. Every Active index listing
gets a `listings` row even when its workbook is missing or has no column for it — an empty
row is countable, a dropped one is invisible.

`listings.role` is inferred, not recorded anywhere: a listing whose bedrooms and sleeps equal
the sum of the other listings' is the whole property (`main`). Column order is NOT evidence —
`Bellevue 2323` puts the combined listing third. Properties with no such listing (Beachwood's
ten independent units) get **no** `main` at all, which the schema permits; do not promote an
arbitrary one to fill the gap.

## Reading the source files

Workbooks live on the local Google Drive Desktop mount. `/Users/ylin/Google Drive` is a
**symlink** into `~/Library/CloudStorage/GoogleDrive-billing@valtarealty.com`, which macOS
protects with TCC — reading it requires **Full Disk Access** for the host app. Without it
every path under there fails with `Operation not permitted`, which looks like a missing file
but is not. The R reporting scripts work because RStudio already holds that grant.

Scope comes from `Cohost_Property_PPTs_Locations.xlsx`, sheet `PropertyFolder`, filtered to
`Status == 'Active'` with a non-empty `File` — the same filter the R scripts use, so both
cover the same properties. `src/discover.py` then looks for `*onboarding*.xlsx` under
`** Properties ** -- Valta/<Property.folder>/`, skipping `ZZZ_`, `~$`, "Do not use", and
template copies, and picks the most recently modified match.

## Source data shape — the thing to get right

One workbook per property, three tabs. **Values run horizontally, one column per object** —
this is the single most important fact about the source format:

| Tab | Layout | Handling |
|---|---|---|
| Summary | `A=Field, B=Value` | **Derived — do not import.** Reproduced by the `listing_summary` view. |
| Owner Client Info | `A=Field, B=Owner, C=Owner 2, …` | → `owners` + `property_owners` |
| Property Info | `A=Field, B=Main listing, C=Child listing 1, …` | → `listings` + `listing_attrs` + `listing_secrets` |

**A property is not a listing.** `Seattle 10057` has three Property Info columns — Whole /
Lower / Upper — sharing an address but with different cleaning fees, door codes, and bed
counts. Anything keyed per-property will silently collapse them.

Field labels are hierarchical: `Maintenance – WiFi – Internet password` →
`category / subcategory / label`. Note the **en-dash**; separator and spacing vary across
template versions, so always match on a normalized label, never a literal string.

Workbook filenames come in four incompatible generations (`{Place} {Number} Onboarding
Sheets.xlsx`, `Property Onboarding {Address}.xlsx`, `Property Onboarding-Yacinde {Unit}.xlsx`,
`{Place} {Number} Valta_Client_Onboarding.xlsx`), each with a slightly different field set —
which is why the long-tail fields live in `listing_attrs` rather than as columns.

## Secrets handling — non-negotiable

The workbooks contain live credentials: door/lockbox codes, WiFi passwords, utility portal
logins, security-camera logins.

Fields listed in `config/sensitive_fields.yml` go to **`listing_secrets`**, encrypted with
`pgp_sym_encrypt()` under `SECRETS_KEY`. Everything else goes to `listing_attrs` as plaintext.

Template generations spell the same field differently, so one field can fragment into several
labels — which splits `search` results and makes `get_secret` miss. `fields._LABEL_ALIASES`
folds the known spellings to one canonical name, applied by `canonical_label()` in
`_parse_listings`. The backup door code was consolidated this way on 2026-09-27:
`Backup Code` (96 workbooks) and `Guest Backup Code` (Yacinde) now store as
**`Maintenance - Access - Guest Access Backup Code`** — 86 rows under one label in both
projects. Only fold labels that are genuinely the same field: verified first that no workbook
of the 98 contains more than one of the three spellings, so nothing could be merged away.
Alias it in `fields.py`, and keep `is_sensitive` checking BOTH the raw and canonical label so
a workbook using an old spelling can never fall through to plaintext.

Deliberately NOT in `label_key`/`normalize_label`: `discover.py` runs `label_key` over the
INDEX's column headers, and folding aliases there would reach index parsing.

The source workbooks were then brought in line on 2026-09-27 (`tools/edit_label.py`): all 96
say `Guest Access Backup Code`, and in the 53 whose archive openpyxl can load, the row moved
from 45 to 42 so it sits with the other Guest Access fields (41 code / 42 backup / 43 location).
The other 43 keep it at row 45 — see below. The alias makes both layouts parse identically, so
this changed nothing downstream: a full `--dry-run` still matched both databases exactly.

**Editing these workbooks: openpyxl is only safe on some of them.**
`tools/edit_label.py` therefore has two paths, and the rule is that anything touching all 96
must not require the full reader.

- `rename()` rewrites the one shared string through raw zip surgery, copying every other entry
  byte-for-byte. It works on all 96 and preserves drawings, data validations and `xl/metadata`.
  It resolves the label **via the A-column cell's `t="s"` index**, not by text search: the
  shared-string table often holds duplicate copies of the same label (one file has it at si#69
  and si#182 with only #182 referenced), so a text match is ambiguous and editing an orphan
  would report success while changing nothing. It refuses if the `si` is shared with any other
  cell, which would rename an unrelated cell too.
- `move_row()` uses openpyxl and so only runs on the 53 loadable files. Its save **drops**
  `xl/drawings/drawing1-3.xml`, both `worksheets/_rels`, and `xl/metadata` (18 zip entries ->
  12). Verified safe here only because those drawings are empty 775-byte stubs with zero
  anchored shapes — check that again before reusing it. Data validations, merged cells and
  dimensions do survive.

- `SECRETS_KEY` lives in `.env` and is **never** written to the database — a Neon dump alone
  does not expose these values. Do not add it to a table, a migration, or a default. The
  corollary: lose the key and all 1057 encrypted values are gone for good, in both projects,
  recoverable only by re-importing from Drive.
- Always go through `src/secrets_io.py`; it passes the key as a bind parameter per call.
- When adding a field to the template, decide plaintext vs. encrypted **before** first import —
  a value written to `listing_attrs` has already leaked into backups and query logs.

`.env` is covered by `Claude_projects/.gitignore`; `.env.example` is the tracked template.

## MCP server

Tools are defined once in `src/kdb_tools.py` and served over two transports:

| | |
|---|---|
| `src/mcp_server.py` | stdio, local. Wired into the Claude desktop app via `mcpServers` in `~/Library/Application Support/Claude/claude_desktop_config.json`, and into Claude Code here via `.mcp.json`. |
| `src/http_server.py` | streamable HTTP, remote. The shared Valta connector — per-person tokens, deployed. See `REMOTE_MCP.md`. |

Restarting the app is what picks up a stdio config change; editing the file
mid-session does nothing.

Neither sandbox can reach Neon — the desktop VM cannot resolve `neon.tech` and
the cloud container has no route to port 5432 — so anything needing a live query
has to run on the Mac or on the deployed server. A tool that fails only with a
DNS or connection error in a sandbox is not broken.

Tools are shaped queries, not table access, because the schema misleads: a caller that treats
a property as a listing collapses `Seattle 10057`'s Whole/Upper/Lower into one answer, and raw
table access would drag `listing_secrets` into questions that never asked for a credential.

| Tool | |
|---|---|
| `list_properties` / `get_property` | the property → listing structure |
| `get_listing` | one listing's plaintext fields + the NAMES of its secret fields |
| `search` | trigram search over labels and values — the usual entry point |
| `list_secret_fields` | which credentials exist, never their values |
| `get_secret` | decrypts ONE credential; requires both listing and label |
| `sql` | a single SELECT/WITH in a read-only transaction |

`get_secret` needs `SECRETS_KEY`, so the server inherits `.env` through `src/db.py` and the
key never reaches the client. `KDB_MCP_SECRETS=0` leaves that tool unregistered; `KDB_MCP_DB`
picks the target (default `DATABASE_URL`).

The read-only guard on `sql` is belt and braces — a regex for SELECT/WITH, a rejection of a
second statement, and `set transaction read only` so a write fails at the server rather than
on trust.

## Schema notes

- `listings.role` is `main` or `child`; a partial unique index enforces exactly one `main`
  per property. `ordinal` 0 = main, 1..n = child, matching column order in the tab.
- `listing_attrs` carries a `gin_trgm_ops` index on `value`, so free-text questions
  ("which houses trip the circuit breaker?") are indexed rather than full scans.
- `pgcrypto`, `pg_trgm` installed. `vector` 0.8.6 is available but unused — semantic search
  over `listing_attrs` would not require moving off Neon.
- Deleting a property cascades to listings, attrs, and secrets.

## Loaded (2026-09-27)

Full import, both projects, verified identical by row count:

| | |
|---|---|
| properties / owners | 69 / 89 |
| listings | 111 |
| listing_attrs / listing_secrets | 3907 / 1106 |
| listings with no attrs | 4 |

2026-09-27 added `Bellevue 15746`, `Renton 18823`, re-imported `Sammamish 5124`, refreshed all
10 `Yacinde` units (access codes filled in since August: secrets 92 -> 114), consolidated the
backup-code label, and refreshed `Keaau 15-1542` + `Lynnwood 17506`. A full `--dry-run` now
reproduces these totals exactly, so every property is current against its workbook.

Encryption re-verified against live rows: `listing_secrets.value_enc` reads as pgp bytea
(`c30d0407…`), decrypts to the door code under `SECRETS_KEY`, and raises
`Wrong key or corrupt data` under any other key.

Those 5 empty listings are **source gaps, not failures**, and the empty row is the point —
it is countable, and a later import fills it in without a rebuild:

- `Bellevue 14507` U1–U4 — the shared `Bellevue 14507 Onboarding Sheets.xlsx` does not exist
  anywhere under the 4plex folder. Not a name mismatch; there is no onboarding sheet at all.
- `Sammamish 5124` — **fixed in the index, 2026-09-27.** The row was split into
  `Sammamish 5124-1` (Active) and `Sammamish 5124-2` (Inactive), so `_match_column` now
  resolves `-1` to its own column and the Active filter leaves `-2` out. Re-importing the
  property cleared the old empty `Sammamish 5124` row, as `load()` rebuilds its listings.
  The one remaining empty-listing case is Bellevue 14507.

Tab names on the real files are exactly `Summary`, `Owner`, `Property`. Owner rows are often
sparse (name only, no email/phone) — that is the source data, not a parse failure. So is
`Microsoft 14645-C19`'s owner phone, which holds `wechat ID: a31601022`.

## Traps these workbooks set

Both were found the hard way; neither announces itself as an error.

**Bogus `<dimension>` records.** Some workbooks declare a single cell. openpyxl's read_only
mode trusts that and stops iterating after row 1 — no exception, just a file that looks
empty and fails with "no 'Field' header row". Always read sheets through
`xlsx.sheet_rows()`, which calls `reset_dimensions()` first. Dropping `read_only` is not the
fix: some of the same files reference `xl/drawings/drawing1.xml` that is missing from the
archive, and the full reader raises KeyError on load.

**Shared-string overruns.** A cell can point past the end of `xl/sharedStrings.xml`
(`Cottage 7 Onboarding Sheets.xlsx` does). `WorkSheetParser.parse_cell` indexes that table
directly, so the read dies with a bare `IndexError: list index out of range` naming neither
file nor sheet. Switching to `read_only=False` is **not** a workaround — both readers run the
same `parse_cell`. `xlsx.sheet_rows()` instead swaps in a list subclass that reads out-of-range
references as `""` and counts them; `parse()` reports the count as a note. Those cells are
genuinely absent from the archive, so nothing can recover them — the note is the only signal
that a field went missing rather than being left blank by the author.

Keep tracebacks unabridged in `import_onboarding.py`: `print_exc(limit=N)` prints the
*outermost* N frames and drops the one that raised.

**Index folder names drift from real directory names** — double spaces (`14507 NE 7Th  PL
Unit 1` against the index's single space), dash style, casing. An exact `root / name` join
silently yields "no onboarding workbook" for a property whose sheet is sitting right there.
`discover.resolve_folder()` falls back to a normalized match, then to an unambiguous prefix
match. When reporting gaps, keep "folder not found" and "folder has no workbook" separate;
they have different fixes — `inspect_missing.py` prints what is actually on disk for each,
including every `*onboarding*.xlsx` at the property level, which distinguishes "the sheet was
never made" from "the `File` cell does not match what it is called".

## Reading the source files from a sandbox

`~/Library/CloudStorage` is TCC-protected, so **whether Drive is readable depends on which app
runs the command**. Claude Code's own Bash tool is not granted Full Disk Access and gets
`PermissionError: Operation not permitted`; the user's terminal is. Anything touching Drive —
`discover.py`, `inspect_missing.py`, any real import — has to be run by the user.

Do not ask them to paste output. Have them redirect it into `out/` (gitignored) and read the
file; the terminal-reading tool sees Claude's own integrated terminal, which is the one
without access.

## Open items

- Only one template generation (`... Onboarding Sheets.xlsx`, ~45KB) has been parsed against
  in detail. The ~112KB `Property Onboarding ...` generation now imports (Yacinde's 10 units,
  348 attrs) but its label set has not been audited — extend `config/sensitive_fields.yml` and
  `_LISTING_COLUMNS` rather than special-casing.
  Auditing `Property Onboarding Belleve 15746.xlsx` and `Renton 18823 Property Onboarding.xlsx`
  label-by-label (2026-09-27) found their label set otherwise identical to the common template,
  with one addition: `Maintenance - Access - Guest Access Backup Code`, a door code that would
  have been the first credential in `listing_attrs` as plaintext. Now in `sensitive_fields.yml`.
  The Yacinde generation then produced a THIRD spelling of the same field,
  `Maintenance - Access - Guest Backup Code` (no "Access"), found on the 2026-09-27 refresh —
  also a live door code, also headed for plaintext. Both are now listed. Each new template
  generation should be swept label-by-label before its first write; grep the parsed labels for
  code/password/login/pin and check every hit is classified SECRET.
  A low attr/secret count is not by itself a parse failure — `Sammamish 5124-1` legitimately
  has 27/4 because the owner self-manages and most access/utility cells are blank. Compare
  labels, not counts.
- Yacinde's index rows have an empty `File` cell; they resolve only through the
  "exactly one `*onboarding*.xlsx` in the folder" fallback. A second sheet appearing in any of
  those folders makes it ambiguous and drops all 10 to empty rows.
  The same fragility bit from the other direction on 2026-09-27: the B1-B4 folders had been
  renamed on Drive (`Unit B1 - target live 8.20` -> `Unit B1 - live 8.20`), and with no `File`
  cell to fall back on, all four resolved to `path=None`. **A re-import would have rebuilt them
  as empty rows, dropping 137 attrs and 34 secrets, while printing `ok ... 0 failed`.** The
  `<- N listing(s) with no workbook data` marker is the only warning, so for Yacinde always
  diff per-listing attr/secret counts against the database before writing. `resolve_folder`
  cannot bridge this: "target " sits mid-name, so it is neither an exact nor a prefix match.
  Fixed in the index (four `Property.folder` cells), not in the matcher. Note `Unit B4` really
  does have a double space before its dash.
- `Unit F4` has an onboarding sheet on Drive but no index row, so it is deliberately not in the
  database (asked and confirmed 2026-09-27). Same shape as `Sammamish 5124-2`.
- Daily sync is explicitly not wanted — imports are run on demand.
