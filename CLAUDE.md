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

- `SECRETS_KEY` lives in `.env` and is **never** written to the database — a Neon dump alone
  does not expose these values. Do not add it to a table, a migration, or a default. The
  corollary: lose the key and all 1057 encrypted values are gone for good, in both projects,
  recoverable only by re-importing from Drive.
- Always go through `src/secrets_io.py`; it passes the key as a bind parameter per call.
- When adding a field to the template, decide plaintext vs. encrypted **before** first import —
  a value written to `listing_attrs` has already leaked into backups and query logs.

`.env` is covered by `Claude_projects/.gitignore`; `.env.example` is the tracked template.

## Schema notes

- `listings.role` is `main` or `child`; a partial unique index enforces exactly one `main`
  per property. `ordinal` 0 = main, 1..n = child, matching column order in the tab.
- `listing_attrs` carries a `gin_trgm_ops` index on `value`, so free-text questions
  ("which houses trip the circuit breaker?") are indexed rather than full scans.
- `pgcrypto`, `pg_trgm` installed. `vector` 0.8.6 is available but unused — semantic search
  over `listing_attrs` would not require moving off Neon.
- Deleting a property cascades to listings, attrs, and secrets.

## Loaded (2026-08-17)

Full import, both projects, verified identical by row count:

| | |
|---|---|
| properties / owners | 67 / 86 |
| listings | 109 — 61 `main`, 48 `child` |
| listing_attrs / listing_secrets | 3802 / 1057 |
| listings with no attrs | 5 |

Encryption re-verified against live rows: `listing_secrets.value_enc` reads as pgp bytea
(`c30d0407…`), decrypts to the door code under `SECRETS_KEY`, and raises
`Wrong key or corrupt data` under any other key.

Those 5 empty listings are **source gaps, not failures**, and the empty row is the point —
it is countable, and a later import fills it in without a rebuild:

- `Bellevue 14507` U1–U4 — the shared `Bellevue 14507 Onboarding Sheets.xlsx` does not exist
  anywhere under the 4plex folder. Not a name mismatch; there is no onboarding sheet at all.
- `Sammamish 5124` — the index has one listing, the workbook has two columns
  (`Sammamish 5124-1`, `-2`). The name is a prefix of both, so `_match_column` refuses. The
  fix belongs in the index (split the row), not in the matcher.

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
- Yacinde's index rows have an empty `File` cell; they resolve only through the
  "exactly one `*onboarding*.xlsx` in the folder" fallback. A second sheet appearing in any of
  those folders makes it ambiguous and drops all 10 to empty rows.
- Daily sync is explicitly not wanted — imports are run on demand.
