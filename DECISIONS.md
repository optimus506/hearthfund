# Decisions

Each entry: the decision, the alternatives, and why. Entries marked **Revisit** are worth a look.

Issue and pull-request numbers such as #87 that appear before 1.0 refer to the project's earlier private
tracker; those issues are not public, and the public tracker numbers its issues from #1 again.

## D-001 Generic git identity in this repo
- **Decision:** commits use `household-budget maintainers <maintainers@example.invalid>` (repo-local git config).
- **Alternatives:** your global identity (real name and personal email in every commit).
- **Why:** the repo must be public-safe and the privacy scan covers git history, including authors.
  **Revisit:** set your own public identity with `git config user.name/user.email` before publishing if you want credit.

## D-002 How the sub-agents run
- **Decision:** the agent definitions live in `.claude/agents/` so future sessions opened in this repo can use them
  by name. In the first build session (started outside this repository, where those files are not loaded) builders
  ran as forks of the main session (sharing its context of the original specification), and check agents ran as
  fresh general-purpose agents given their definition file as instructions (independent, no shared context).
- **Alternatives:** restart the session inside the new repo (not possible while working unattended).
- **Why:** keeps the requested builder / checker split and keeps reviewers independent of the builders.

## D-003 Privacy deny-list
- **Decision:** `scripts/privacy_scan.py` builds the deny-list in memory each run from BUDGET_PRIVATE_DIR (and
  BUDGET_PRIVATE_EXTRA for bank CSV folders). Words that also occur in the Python standard library's own text
  are treated as ordinary English and dropped; ALL-CAPS payee keywords that are ordinary words (e.g. a card
  brand that is also an English word) are reported as `review-brand` instead. Amounts are only matched when they
  have cents and are $10 or more. Synthetic fixtures use the years 2028-2031 so they cannot collide with real dates.
- **Alternatives:** a committed deny-list (forbidden: it would itself be personal data); an external word list.
- **Why:** no personal data is ever written to disk, and the scan stays useful without a dictionary download.

## D-004 Money is integer cents
- **Decision:** every amount in the core, the database and the API is an `int` number of cents. Files use dollars
  with at most two decimals; parsing goes through `Decimal`, never `float * 100`. Rates are basis points.
- **Alternatives:** `float` (rounding drift in running balances); `Decimal` everywhere (slower, awkward in SQLite/JSON).
- **Why:** the forecast must match a spreadsheet to the cent over 500+ rows.

## D-005 Ledger rows are materialized occurrences
- **Decision:** recurring series generate stored rows (like spreadsheet rows). Each row can be changed, removed,
  marked, cleared or reduced on its own; generation only adds missing occurrences and never deletes.
- **Alternatives:** compute occurrences on the fly from rules plus an exceptions table.
- **Why:** this is how the household actually works (per-row edits, rows cleared by the bank), it makes workbook
  import exact, and per-occurrence overrides are just edited rows.

## D-006 Ledger order is (order_date, seq), never due date alone
- **Decision:** every row has a `due_date` (shown) and an `order_date` (>= due date) plus `seq`. A bill whose series
  has `funded_by: <income>` is ordered after the first payday of that income on or after its due date. Generated rows
  on one date: income first, then by due date, then name.
- **Alternatives:** sort by due date with an "income first" tie-break (cannot express "the bill due on the 10th is
  paid from the 15th's paycheck"); a free manual position only (cannot be generated).
- **Why:** listing bills after the payday that funds them is the core rule of the original budget, and it changes
  the running balance and the low point.

## D-007 Workbook import reproduces the sheet exactly, with a fixed horizon
- **Decision:** imported rows get `order_date = running max of due dates down the sheet` and `seq = sheet order`;
  the household gets a fixed horizon ending at the last row; status comes from the Name cell colour. A warning is
  shown when a row sits more than 14 days after its due date.
- **Alternatives:** re-derive the order from inferred series (would change the balances); a rolling horizon at
  import (would add rows past the sheet and move the low point).
- **Why:** the definition of done requires the same start balance, low, low date and end balance as the workbook.
  The user can switch to a rolling horizon afterwards.

## D-008 The workbook importer does not guess spending lines
- **Decision:** positive rows are bills unless the user ticks names as "spending lines" (no status, "budget line")
  in the mapping step. Negative rows are income.
- **Alternatives:** guess from names like "grocery" (language-specific, often wrong).
- **Why:** a wrong guess silently hides bill status controls.

## D-009 Series inference is conservative
- **Decision:** rows are grouped by exact name and get a cadence only when the dates fit one cleanly (weekly,
  biweekly, monthly by day with month-end clamping, semimonthly, yearly, once). The series amount is the most common
  amount; rows with other amounts are marked overridden and keep their exact amount. Irregular names stay as one-off
  rows. Inference never changes an amount, a date or the order.
- **Alternatives:** fit the best pattern always; ask the user per name.
- **Why:** exactness first; series only matter when the horizon is extended later. **Revisit:** a two-paydays-a-month
  income with two different amounts becomes one semimonthly series with overridden rows.

## D-010 Forecast definitions
- **Decision:** low = the first row with the minimum running balance, the starting balance excluded; high includes
  the starting balance; end = after the last row; year end = after the last row ordered on or before Dec 31.
- **Alternatives:** include the start in the low; last occurrence of the minimum.
- **Why:** matches the workbook (`MIN` over the rows after the carry-over row, `VLOOKUP` returns the first match).

## D-011 Change / Remove / Undo take effect immediately
- **Decision:** on the Bills tab a change or removal updates the row at once (state `removed`, or the new amount with
  `amount_was` kept) and can be undone; an audit entry records who did it.
- **Alternatives:** a "pending change" that a weekly script applies to a spreadsheet later (how the earlier
  implementation worked).
- **Why:** the app's database is the master, so there is nothing to sync; the earlier implementation only worked
  that way because a spreadsheet was the master.

## D-012 Expected items and what-ifs are virtual forecast points
- **Decision:** open expected items are part of the shown forecast (stored, shared, matched at reconciliation as
  proposals); what-ifs are sent with a request and never stored. Both are placed after the real rows of their date
  (an item dated in the past hits the balance at once).
- **Alternatives:** store what-ifs per user; turn expected items into ledger rows.
- **Why:** same behaviour as the earlier implementation, and the ledger stays a clean record of budgeted rows.

## D-013 Payoff plan priority (bug in the earlier implementation) — **Revisit**
- **Decision:** extra money goes first to deferred-interest promos that are still running, then by the chosen
  method (avalanche: highest APR, then smallest balance; snowball: smallest balance, then highest APR), then name.
- **Found:** the earlier implementation's payoff page sorts with
  `urgent(b) - urgent(a) || method === 'avalanche' ? (APR order) : (balance order)`. JavaScript reads this as
  `(urgent(b) - urgent(a) || method === 'avalanche') ? ... : ...`, so (1) when one debt is a running deferred promo
  and another is not, the APR order is used whatever the method and the promo is **not** put first, and (2) in
  snowball mode with equal urgency the balance order is used correctly. That page can therefore show a payoff order
  that does not send extra money to the deferred promo first.
- **Why:** this implements the documented intent. Worth fixing in the earlier implementation too (add parentheses
  around the ternary).

## D-014 Reconciliation is a port of the weekly script, with a no-balance fallback
- **Decision:** same chain check, basis-by-balance, rule kinds, date window, tolerance (tested on the oldest
  candidate only), greedy card bundles and "proposals are never applied automatically" as the original weekly
  script. Added: accept as **reduce** (partial payment keeps
  the remainder on the row), expected-item proposals, and for exports without a balance column a basis by the last
  reconciled date plus transaction-id/hash dedup (the UI says the balance could not be checked).
- **Alternatives:** fuzzy matching or machine learning; always requiring a balance column.
- **Why:** deterministic and explainable; many banks (and OFX files) do not give a running balance.

## D-015 Masking keeps the last 4 digits; raw uploads are not kept
- **Decision:** descriptions are masked on import: runs of 5+ digits become `#` except the last 4, and digit groups
  joined by spaces, dashes or dots that hold 8+ digits in total (a card number written 4-4-4-4) count as one run.
  Filenames and OFX NAME/MEMO are masked the same way. Uploaded files
  are parsed in memory and discarded; only masked transactions and upload metadata are stored. OFX account ids are
  never stored. Columns marked `ignore_cols` are never read.
- **Alternatives:** mask every 4+ digit run like the original script (breaks rules that match "card ending 1234");
  keep raw files for re-parsing.
- **Why:** a last-4 suffix is not an account number and is what payee rules need; nothing sensitive stays on disk.

## D-016 Default card categories use generic words only
- **Decision:** shipped defaults are keyword lists like GROCERY, SUPERMARKET, FUEL, PHARMACY, RESTAURANT. Users add
  merchant names themselves.
- **Alternatives:** ship a list of real merchant names (better out of the box).
- **Why:** the repo must not contain real merchant names (privacy rule), and lists like that are region-specific.

## D-017 The schema has one canonical file
- **Decision:** `schema/bill-system.schema.json` is canonical; `src/hbudget/schema/` holds a byte-identical copy
  for the package (`make schema` copies, a test fails on drift).
- **Alternatives:** a symlink (breaks on some Windows checkouts and in sdists); only the packaged copy (harder to find).
- **Why:** readers find it at the top level; the installed app can still load it with `importlib.resources`.

## D-018 Stack: FastAPI + Jinja2 + vanilla JS, no build step
- **Decision:** server-rendered pages with page data as a non-executed JSON script tag, small ES modules, hand-written
  SVG charts and Sankey. No npm, no CDN.
- **Alternatives:** React/Vue SPA (build tooling, `npm audit` surface); htmx (another dependency, little gain).
- **Why:** small and boring, strict CSP (`script-src 'self'`), works offline, nothing to audit in node_modules.

## D-019 SQLite with stdlib sqlite3 and a tiny migration runner
- **Decision:** numbered `.sql` migrations, forward only, one transaction each, recorded in `schema_migrations`; an
  automatic copy of the database is made before pending migrations run on a database with data.
- **Alternatives:** SQLAlchemy + Alembic.
- **Why:** fewer dependencies; the automatic pre-migration backup makes upgrades safe to roll back.

## D-020 Password hashing, sessions and CSRF
- **Decision:** `hashlib.scrypt` (n=2^15, r=8, p=1) with per-user salt; server-side sessions (random token in an
  HttpOnly SameSite=Lax cookie, SHA-256 of it stored); Secure cookie automatically on HTTPS; synchronizer CSRF token
  checked on every state change plus an Origin check; 5 failed logins per user+IP per 15 minutes locks that pair.
- **Alternatives:** argon2 (extra C dependency); signed cookie sessions (cannot be revoked).
- **Why:** standard library only, revocable sessions, simple to audit.

## D-021 Roles are per household; the admin is not automatically a member
- **Decision:** owner, editor, contributor (bill status and "expected item posted" only), viewer. The admin manages
  users and households and is the owner of the first household.
- **Alternatives:** global roles.
- **Why:** a partner can be a contributor in one household; a self-hosted server may hold several households.

## D-022 Shared bill-system files need no real dates
- **Decision:** a schedule's `start` is optional when `day`, `days`, `month`+`day` or `weekday` defines it; it then
  resolves to the first matching date on or after `household.as_of` (default: the import day).
- **Alternatives:** require a start date everywhere.
- **Why:** example and shared files stay generic and work whenever they are imported.

## D-023 YAML dates stay strings
- **Decision:** YAML is read with a SafeLoader without the timestamp resolver, and aliases are rejected.
- **Alternatives:** plain `yaml.safe_load` (turns dates into `date` objects, so JSON and YAML validate differently).
- **Why:** one schema validation path for both formats; no alias bombs.

## D-024 OFX running balances are computed from the ledger balance
- **Decision:** when `LEDGERBAL/DTASOF` is on or after the newest posting, balances are computed backwards from
  `BALAMT` so the chain and basis checks work for OFX/QFX too; otherwise the no-balance fallback applies.
- **Why:** OFX has no per-transaction balance.

## D-025 Deterministic ids for series rows and hashed transactions
- **Decision:** generated **and** imported series rows use one scheme: `series_id:YYYY-MM-DD` of the **nominal**
  date (before the weekend shift), stored as `nominal_date`; an occurrence is identified by
  `(series_id, nominal_date)`, which is what generation, regeneration and the duplicate guard compare. Imported rows
  that do not belong to a series keep `r<sheet row>`. Transactions without a bank id are `h:` + SHA-256 of date,
  amount, description and an occurrence counter within the file.
- **Alternatives:** key by the shifted due date (a weekend-rule change would re-key rows and break marks); keep
  `r<row>` ids for imported series rows (generation could not tell an imported occurrence from a missing one).
- **Why:** re-generation and re-uploads are idempotent; genuine identical purchases on one day stay separate.

## D-026 Backups never contain passwords
- **Decision:** a backup holds the members' usernames and roles only. Restoring it does not create logins.
- **Why:** a backup file is easy to leak; password hashes do not belong in it.

## D-027 Optimistic concurrency on edits
- **Decision:** row edits send the amount the user saw; the server answers 409 when the row changed meanwhile.
- **Why:** two people (e.g. partners) may use the Bills tab at the same time.

## D-028 Deleting an upload
- **Decision:** deleting an upload removes its `upload_txns` links and, while it is waiting, the transactions no
  other upload links to; once imported (used in a reconciliation) its transactions stay in the history and only the
  upload entry is hidden.
- **Why:** the history behind past reconciliations and reports must not change underneath them.

## D-029 The docs-reviewer runs only once there is something to install
- **Decision:** the docs-reviewer (a fresh agent following README.md from scratch) is recorded as "not applicable"
  for Phases 1-4. It runs a first time at the end of Phase 5 (image and compose exist) and in full at Phase 6.
- **Alternatives:** run it every phase against partial docs.
- **Why:** before a README and an installable app exist it has nothing to follow; every phase gate still needs the
  other four reviewers to sign off.

## D-030 Partial payments are kept apart from user changes
- **Decision:** "reduce" records the payment in `paid` / `paid_txn_ids` and leaves `amount_was` alone; Change records
  `amount_was` and `paid_at_change`; Undo restores `amount_was - (paid - paid_at_change)`.
- **Alternatives:** reuse `amount_was` for reductions (Undo would give back money already paid).
- **Why:** Undo must never re-add a payment the bank already took.

## D-031 Two-decimal checks are semantic, not in the schema
- **Decision:** money and percentages are checked for at most two decimals by `billsystem.check` through `Decimal`,
  not with JSON Schema `multipleOf: 0.01`.
- **Alternatives:** `multipleOf: 0.01` in the schema.
- **Why:** tested with the jsonschema library, `multipleOf: 0.01` rejects valid values such as 0.07 and 1234.56
  because of binary floating point.

## D-032 User regexes use a restricted syntax
- **Decision:** payee-rule, skip-name and purchase-date regexes are limited to 200 characters and reject
  backreferences, lookarounds, nested quantifiers, quantified groups containing an alternation and large repeat
  counts; matched text is capped at 500 characters.
- **Alternatives:** a regex engine with timeouts (third-party), running matches in a subprocess, or no regexes.
- **Why:** Python's `re` has no timeout; these limits remove the catastrophic-backtracking patterns while keeping
  the useful ones. Plain "contains" text covers most rules anyway.

## D-033 Exports are protected against formula injection
- **Decision:** cells of text columns that start with `= + - @`, tab or carriage return get a leading `'` in CSV and
  XLSX; XLSX text cells are always written as strings. Numeric and date columns are never prefixed and stay numbers
  (a negative amount is still a number).
- **Why:** payee names and notes come from bank files and other users; opening an export must not run formulas.

## D-034 A biweekly schedule without a start date is allowed with a warning
- **Decision:** `weekday` alone resolves to the first such weekday on or after `as_of`, and the import shows a
  warning to set the start date.
- **Alternatives:** require `start` for biweekly in the schema (shared files would then need real dates).
- **Why:** the warning keeps shared files date-free while making the arbitrary phase visible. The example file avoids
  the case.

## D-035 First-run setup needs a one-time token
- **Decision:** with no users, the server writes a one-time random token to `<data dir>/setup-token` (mode 600) and
  logs only where to find it (never the token, never in a URL); `/setup` takes it in a form field, runs in a
  `BEGIN IMMEDIATE` transaction that re-checks for users, deletes the file and disappears once an admin exists. Host headers are
  checked against `HBUDGET_ALLOWED_HOSTS`, and CSRF origins against configured origins, not the request's Host.
- **Alternatives:** an open `/setup` (the first visitor on the LAN or tailnet becomes admin); loopback-only setup
  (awkward on a headless server).
- **Why:** closes the first-run race and DNS-rebinding attacks; the token stays out of logs, access logs and browser
  history, and only someone who can read the data folder can claim the server.

## D-036 Login throttling without lockouts
- **Decision:** per username: after 5 failures in 15 minutes a stored "reject until T" time (T grows 2 s, 4 s, ...
  up to 60 s) answers early attempts at once with 429 + `Retry-After` (the server never sleeps); per IP: 429 after 20
  failures in 15 minutes; proxy headers trusted only from `HBUDGET_FORWARDED_ALLOW_IPS`; at most 2 concurrent scrypt
  checks, a login waits at most about 2 s for a slot and otherwise gets 503; a dummy hash for unknown usernames.
- **Alternatives:** lock the username+IP pair for 15 minutes (easy to bypass by rotating IPs, or to abuse to lock the
  owner out with a spoofed header).
- **Why:** slows guessing without letting an attacker lock out the household, and keeps scrypt and sleeping requests
  from tying up the server (a denial of service).

## D-037 What is audited
- **Decision:** logins, logouts, user/membership/role changes, password changes, row edits and marks, configuration
  changes, uploads and deletes, reconciliations, imports, exports, backups and wipes. Wipe keeps the audit log.
  Entries hold ids and field names only. Login attempts are kept 30 days.
- **Why:** households share the app; "who changed this" must be answerable, without copying financial data into logs.

## D-038 Uploads keep their own transaction lists
- **Decision:** `bank_txns` holds one row per transaction (history, reports, dedup); `upload_txns` links every upload
  to all of its transactions with that file's own order, including the ones already stored by an earlier upload. Reconciliation always works on one upload's full list.
- **Alternatives:** a single `upload_id` per transaction (an overlapping later upload loses the postings it shares
  with the earlier one, so its basis is not found and file positions collide).
- **Why:** weekly exports usually overlap the previous week's.

## D-039 Generation keeps a per-series watermark and uses nominal dates
- **Decision:** each series stores `generated_through`; generation only adds occurrences after it and sets it to
  `max(generated_through, until)`. Inactive series keep advancing it to `max(generated_through, as_of - 1 day)`.
  Inferred series start with the import's horizon end (the sheet's latest due date), and a series that stops more
  than one period before that gets `schedule.end` at its last row. Occurrence
  matching, seasonal months and `funded_by` lookups use the nominal (before weekend shift) dates; rows store the
  actual date.
- **Alternatives:** fill every missing occurrence (re-creates removed rows, ended bills and gaps in imported sheets);
  use shifted dates everywhere (a weekend shift can move a bill to the payday after next or flip a seasonal amount).
- **Why:** generation must never undo a user's or a spreadsheet's deliberate gap.

## D-040 Mapping profiles are per household; built-ins are code
- **Decision:** `mapping_profiles` is keyed by (household, id); built-in profiles are read-only constants in
  `io.bank`; no global profiles in the database.
- **Why:** a profile id from another household can neither be read nor overwritten (no IDOR), and a restore cannot
  replace another household's profile.

## D-041 Bill-system merge mode
- **Decision:** `merge` adds new ids and updates existing ones (then regenerates those series from today); it never
  deletes and refuses documents with a `backup` section.
- **Why:** lets people share and re-import a setup file into an existing budget without losing their history.

## D-042 Non-members get 404
- **Decision:** any household URL a user is not a member of answers 404, not 403.
- **Why:** does not reveal which households exist on a shared server.

## D-043 Small implementation deviations in the core foundations (Phase 2, step 1)
- **Decision:**
  - `LedgerRow.nominal_date` comes after `seq`, because Python dataclasses need fields with defaults last. Build rows
    with keyword arguments.
  - `Settings.reconcile`/`alerts` and `Budget.settings` use `field(default_factory=...)` rather than a shared default
    instance.
  - Additions to the specified API: `money.bp_to_percent`, `dates.days_in_month`, `private_support.private_dir` and
    `private_support.check(field, got, want)` (an assert that never shows values).
  - Every private skip reason starts with "private regression test did not run:", so a skip is never mistaken for
    a pass.
  - pytest gets `pythonpath = ["src", "tests"]`, so `private_tests/` can `import private_support`.
  - ruff's UP031 (%-formatting) is turned off project-wide: %-formatting is the house style of the code being ported.
- **Alternatives:** keep the documented field order (impossible); a shared default instance (works for frozen
  classes, but default_factory is clearer).
- **Why:** these are needed to build the documented design, or make the tests safer; none changes behaviour.

## D-044 Core engine details settled while building it (Phase 2, step 2)
- **Decision:**
  - `reconcile.Applied` has three more fields, all with defaults: `result` (the ReconcileResult), `accepted` (the
    (proposal id, "clear"/"reduce") pairs) and `before` (the starting budget). `report()` and
    `alerts.evaluate(recent=...)` need them, to list proposals and unknowns and to compare posted amounts with the
    budgeted rows.
  - `reconcile.prepare(..., ignored=0)` takes the count of non-posted rows that the importer dropped, so that
    `Prepared.ignored` can be filled in.
  - Expected items (pass 3) consider only postings that no automatic match or proposal used. Each item gets at most
    one proposal (the closest amount, then the oldest), and a posting is offered to one item only.
  - `ledger.change_amount` and `ledger.add_row` take a magnitude (>= 0); the sign comes from the row kind, as with
    series amounts.
  - `ledger.regenerate_series` deletes open, not-overridden rows whose occurrence no longer exists (the user edited
    the series, which is an explicit action). It keeps `order_date >= due_date` when a due date moves later.
  - `ledger.set_month_amounts`: when two rules name one month, the first wins. Changed rows are not marked
    overridden, because the series' `month_amounts` now gives the same amount.
  - `cards.categorise(..., amount=None)`: a positive amount returns "Payment" (the doc said payments are
    "Payment" but gave no amount parameter).
  - `alerts.cap_totals`, `alerts.period_start` and `alerts.payee_key` are public. The report and the check-up
    reuse them.
  - `payoff.plan` never treats checking or savings accounts as debts.
- **Alternatives:** a separate report-input type instead of extending Applied; signed amounts for edits.
- **Why:** these are the smallest choices that make the documented behaviour buildable and testable. None
  changes a documented number.

## D-045 Importer details settled while building hbudget.io (Phase 3)
- **Decision:** small additions and deviations from section 12, all backward compatible with the signed-off design:
  - `infer.infer_series(rows, *, horizon_end=None)`: the import's horizon end is passed in (it is the watermark of
    every inferred series and decides `schedule.end`); default = the latest due date of the rows.
  - `billsystem.from_budget(..., household_name='Household')`: the household name is not part of the core
    `Budget`, so the caller passes it. `billsystem.bills_csv(data, *, today=None)`: a CSV row may leave `start`
    empty when `today` is given (resolved like the YAML file). `billsystem.parse()` and `schema()` are public
    helpers (parse without validation; the packaged schema). `to_budget(..., warnings=None)` collects warnings.
  - `WorkbookMapping.expected_end_cell` may be a column only ("E"): that column of the last data row, so a
    profile keeps working when rows are added or cleared.
  - `MappingProfile` lives in `io.bank` (it holds either mapping); built-in profiles are never exported.
  - Extra public helpers: `io.clean_filename` (upload names: no folders, safe characters, numbers masked),
    `bank.finish` / `bank.txn_id`, `workbook_import.cell_cents` / `parse_date`, `export.safe_text`.
  - ODS: empty repeated cells/rows are counted and only materialised when data follows, so the thousands of
    blank trailing cells and rows spreadsheet programs write never count against the caps; non-empty repeats are
    capped (1,024 columns / 20,000 rows per element). Cell backgrounds follow parent styles and column defaults.
  - XLSX: the file is opened twice by openpyxl (cached values, then formulas) to tell a formula without a cached
    value (reported, row skipped) from an empty cell.
  - Bank CSV: an oldest-first export is detected by the balance chain when the file has balances, else by
    comparing the first and last dates; then sorted newest first (stable within a day).
  - OFX: one account per file (several statements -> "export one account at a time"); a DTD or entity is refused;
    `read_card` also accepts OFX/QFX card statements.
  - `Series.month_amounts` is always sorted by month when read from a file, and written as compressed month ranges
    (so `{"5-9": 80, "10-4": 30}` comes back as `{"1-4": 30, "5-9": 80, "10-12": 30}`, the same amounts).
  - Built-in bank profiles recognise generic optional columns (`id`, `ref`, `reference`, `fitid`; `status`,
    `state`) by header name.
  - Series inference accepts a schedule when every row date is one of its occurrences and the rows cover at least
    `infer.MIN_COVERAGE = 0.75` of the occurrences between the first and last row (a few cleared or removed rows
    still give a series; sparser patterns stay standalone rows).
  - Phase 3 review fixes: ODS text caps (1,000 spaces per `text:s`, 10,000 characters per cell, 5,000,000 per
    sheet, repeat counts bounded); depth cap of 64 for ODS text/rows/columns and OFX aggregates; OFX values are
    entity-unescaped and decimal-comma / thousands amounts are converted, ambiguous ones refused; user patterns
    (skip names, purchase dates) go through `rules.safe_search`; filenames are masked before characters are
    replaced (`_ , +` between digits join one number); exported text that starts with a formula character after
    spaces is protected (CSV: leading apostrophe; XLSX: string cell with Excel's quote prefix, no visible
    apostrophe); overlapping seasonal ranges are a validation error.
- **Alternatives:** change the core models (not needed); keep everything private (the web layer needs them).
- **Why:** the design left these open, and the private regression needed the column-only end cell.

## D-046 Tighter regex and masking rules (Phase 3 security review)
- **Decision:**
  - `rules.compile_safe` also allows at most one "heavy" repeat per pattern, grouped or not. A heavy repeat is
    `*`, `+`, `{n,}`, or a count above 20. The 200-character limit stays.
  - Every core matcher cuts the text to 500 characters, and `rules.safe_search(pattern, text)` gives the io
    layer the same check and cut.
  - `textmask.mask` also treats `_ / , +` as group separators. Dates in ISO, m/d/yy and m/d/yyyy form are set
    aside first so they stay readable, unless they are glued to more digits by a dash or slash.
- **Alternatives:** a regex engine with timeouts (third-party), a subprocess per match.
- **Why:** adjacent unbounded repeats (`\w*\w*...`) backtrack badly even without nesting. One open-ended repeat
  covers the useful payee patterns (e.g. "TO CARD \S*1234").

## D-048 User regexes get a worst-case cost budget (replaces the repeat count of D-045)
- **Decision:** `rules.compile_safe` parses the pattern with the standard library's own regex parser and computes
  its worst-case branching (`rules.pattern_cost`). The scoring:
  - `?` counts 2, `{m,n}` counts n - m + 1, and `*`, `+` and `{n,}` count 501 (MAX_TEXT + 1).
  - Alternatives add up; sequences and nested repeats multiply.
  - A pattern costing more than 50,000 is rejected. One `\S*` with a few `?` passes; two open-ended repeats, or
    16 or more `?`, do not.
  - The structural rules of D-032 (no backreferences, lookarounds, atomic or possessive forms, or nested and
    alternation-holding repeated groups) and the 200-character limit stay.
  - Tests cover every pattern measured as slow. They also check that each accepted example runs in under 0.2 s
    on 500 characters of adversarial text.
  - Masking also accepts a separator with an optional space on each side between digit groups ("4000 - 1234").
- **Alternatives:** counting heavy repeats (D-045: chains of `?` and small bounded repeats got through); a regex
  engine with timeouts (third-party); running matches in a subprocess.
- **Why:** the product of choice points is a direct upper bound on how much the engine can backtrack, so long
  chains of light quantifiers are caught too.
- **Note:** it imports `re._parser`, a private stdlib module that is stable across CPython 3.11-3.14. A test
  catches any break.
- **Update (security re-check):**
  - The budget is now 5,000. A search also tries about 500 start positions, so a pattern costing ~28k took 0.4 s
    per transaction.
  - One open-ended repeat plus up to three `?` still passes (e.g. "ACME .*CO?R?P?").
  - Every accepted test pattern must run in under 0.05 s per search on ten 500-character adversarial texts.
  - Masking now joins digit groups across any run of 1-3 non-alphanumeric characters (`; : | · --`, spaced forms,
    `_`). Dates are still set aside first.
  - Code that a D-045 edit had left unreachable in the group-quantifier branch of the structural check is
    restored, so a quantifier after a group is consumed in one step again.

## D-047 Database layer details (Phase 4, db)
- **Decision:** settled while building `hbudget.db`, all within ARCHITECTURE 14:
  - Migrations: `0001_init.sql` (the 14.2 schema), `0002_indexes.sql` (indexes only) and
    `0003_upload_hidden_versions.sql` (`uploads.hidden`, `households.version`, `audit_log.by_name`, and a
    `card_upload_txns` link table back-filled from `card_txns.first_upload_id`). The runner runs each file
    plus its `schema_migrations` row as one script between BEGIN and COMMIT, so a failing migration leaves nothing
    behind; a database that already holds data is copied to `backups/hbudget-before-<version>-<UTC time>.db` first.
  - Schema additions beyond 14.2: `position` on `accounts` and `expected_items` (their order round-trips),
    `file_index` on `bank_txns` (the first upload's position, needed for a lossless backup), and
    `upload_txns -> uploads` foreign key (deleting an upload removes its links). Usernames are unique case-insensitively.
  - Connections: `db.connection.open` (alias of `connect`) sets WAL, `foreign_keys`, `busy_timeout`, `Row` factory,
    creates files under `umask 077` and sets mode 600 on the db, WAL and SHM. `transaction(conn, immediate=False)`
    nests (an inner one joins the outer), so repository functions can be combined in one transaction.
  - `db.users`: scrypt helpers (`hash_password`, `check_password`, `DUMMY_HASH`) live here so the web layer and the
    CLI share them; `create_first_admin` does the D-035 check-and-create in one BEGIN IMMEDIATE (`SetupDone` if a
    user exists); `too_many_attempts(conn, username, ip, *, now)` returns the Retry-After seconds or None.
  - `save_budget` is diff-based over every budget table and writes one audit entry whose detail is the per-table
    counts (ids and counts only). `load_budget` returns rows in ledger order.
  - Backups: the audit log is part of the backup and survives a wipe, so a restore into the **same** household skips
    entries it already has (same time, action, row and detail, compared with the log as it was before the restore),
    and a restore elsewhere stores them as imported. Restored entries keep the username as text
    (`audit_log.by_name`, `user_id` NULL), never linked to whichever user has that name on this server. A round trip therefore adds only its own audit entries
    (`backup.export`, `household.wipe`, `import.replace`); `tests/db/test_roundtrip.py` checks the documents are
    otherwise identical and the forecasts equal.
  - `seen_txn_ids` (for the no-balance fallback) = every transaction of an upload that has a reconciliation.
  - **Per-upload values (migration 0004):** a transaction is stored once in `bank_txns` (dedupe by id), but every
    `upload_txns` link also keeps that file's own `balance` and `category`. `upload_txns()` returns the link's
    values, so a corrected re-export of the same postings (different running balances, e.g. after a rejected file)
    is previewed with its own balances and no longer gets a false `chain_break`. 0004 back-fills the new columns
    from `bank_txns`, so existing previews keep working.
  - **What the history keeps:** `bank_txns` holds the values of the **newest upload that was not rejected**
    (recomputed on insert, status change and delete). `list_history` (Check-up, Flow, caps) leaves out
    transactions that only rejected uploads contain; un-rejecting an upload brings them back. Transactions with no
    upload link at all (older restores) stay in the history.
  - The backup's `upload_links` carry the per-file `balance` and `category` (optional schema fields; an older
    backup without them takes the transaction's own values).
  - `history_counts(conn, hid)` gives the restore dry run its numbers: `uploads` counts every upload (a restore
    removes hidden ones too) and `uploads_hidden` labels how many are hidden; the import page shows
    "N uploads (H of them hidden)".
  - Uploads follow D-028: `delete_upload(conn, hid, upload_id) -> 'deleted' | 'hidden'`. Only a **waiting** upload
    that no reconciliation used is deleted, with its links and the transactions nothing else holds (another
    upload's links, a cleared or paid ledger row). An imported or rejected upload, or one a reconciliation used, is
    only hidden (`hidden = 1`): its links and transactions stay. `list_uploads(..., include_hidden=False)`. Card
    transactions are linked to every upload that contained them (`card_upload_txns`), so deleting one upload never
    removes a card transaction another upload still holds. The backup format has no `hidden` field, so a restored
    hidden upload shows again (it can be hidden again; nothing is lost).
  - `edit_budget(conn, hid, fn, *, user_id, action, row_id=None, expected_version=None, now=None) -> (Budget,
    version)`: load, apply `fn`, save, all in one BEGIN IMMEDIATE transaction. Every `save_budget` adds 1 to
    `households.version` (`budget_version(conn, hid)`); with `expected_version` a stale edit raises
    `repo.Conflict` (the web layer answers 409).
  - The last owner cannot be removed or demoted (`set_role`, `remove_member`, `add_member` raise `repo.LastOwner`,
    checked inside the same BEGIN IMMEDIATE transaction).
  - `create_first_admin` hashes the password before taking the write lock.
  - Merge import keeps an updated series' watermark (then `regenerate_series` from today) and resets a new series'
    watermark so `generate` adds its rows.
- **Why:** lossless backups and safe concurrent first-run setup, without changing the agreed signatures.

## D-049 Web layer details (Phase 4, web)
- **Edits are atomic:** every budget change goes through `repo.edit_budget` (BEGIN IMMEDIATE, load, change, save);
  `repo.Conflict` is answered 409, `repo.LastOwner` 400. Settings use the budget version (`expected_version`); notes and
  row edits keep their own stale checks (`expected_updated_at`, `expected_amount`).
- **Extra routes not in the 15.2 table:** POST `/series/{id}/status` (contributor: the bill's default status, the
  Autopay list), GET/DELETE `/profiles` (editor: saved CSV layouts), GET `/audit` (owner: the household's activity, with
  the "restored" flag), GET `/api/admin/overview` (admin: users, households, server activity). Settings also carry the
  checkbook balance and as-of date, which the table did not list anywhere.
- **Upload delete:** any upload can be removed; the db deletes a waiting one and only hides a used one (D-028); the UI says
  Remove or Hide accordingly and the action is audited as `upload.delete` / `upload.hide`.
- **Time limit:** reconciliation preview/apply, rule tests and bank-file parsing run in a worker thread under
  `asyncio.wait_for` (10 s, `Settings.time_limit_seconds`); a timeout answers 503. A Python thread cannot be killed, so
  this bounds the request, not the CPU; the regex cost budget (D-048) is what bounds the work.
- **Throttling:** the back-off check, the password check and recording the attempt run together inside the 2-slot
  scrypt semaphore, so a parallel burst cannot pass the check before earlier failures are recorded.
- **CSRF on /login and /setup:** the pre-session double-submit cookie is accepted there even when a session cookie is also
  present (a signed-in user opening the login page).
- **CSP:** no inline scripts or style attributes; colours reach SVG as resolved values and swatches through the CSSOM.
- **Demo:** /setup "demo", the admin "demo" checkbox and `hbudget demo` load `hbudget.demo.demo_budget(today=)` when that
  module exists (Phase 6); until then they create an empty household (the CLI says the demo is not available).
- **Tests use a fixed clock:** `app.state.clock` (default `date.today`) is set by tests; households in tests are moved to
  the fixed date with `repo.save_budget`.

## D-050 Web review round 1 fixes (Phase 4)
- **"Today" never before the as-of date:** every household route uses max(real date, budget as-of) as today, so a budget
  that starts in the future shows its Bills window, "due within 7 days" alerts and payoff plan from its own start.
- **Report totals are server-side:** `/flow` returns money in / out / left over / short (and per month). The core's
  "Left over" group (no items) is dropped from `groups`; the page only draws. **Revisit:** core.flow could return these
  totals itself instead of the web layer recognising the group by name.
- **Reconcile apply** carries the preview's fingerprint (budget version + proposal ids, targets and kinds) and is refused
  with 409 when it no longer matches; refused choices from core (two alternatives, an invalid reduce) are 400.
- **Imports keep history:** workbook commit, bills CSV and a plain bill-system file replace or merge only the budget part
  inside `edit_budget`; uploads, bank/card history, reconciliations and profiles stay. Only a full backup (a file with a
  `backup` section) still goes through `repo.import_bill_system`, which empties the household before restoring.
- **A series cannot change kind** (income / bill / spending): add a new item instead (row signs follow the kind).
- **Rule targets** may name one-off rows (rows without a series) as well as series ids.
- **Card re-sort** keeps a stored category when no keyword matches and that category still exists (it may have come from
  the file's own bank category, which is not stored separately). Edge case: a purchase sorted by a keyword that was later
  deleted keeps that category for as long as a category with that name exists.
- **Backup restore needs confirmation:** the dry run reports `is_backup` and the counts of current rows and history that
  would be replaced; the real restore needs `confirm` = the household name (400 otherwise), like the wipe form.
- **Profiles** are listed by kind (`?kind=bank|card|workbook`); a workbook or wrong-kind profile for an upload is 400.
- **Bodies are read in full (up to the limit) before the app sees them**, so a chunked body over the limit is a clean 413.
- **Password change** is throttled like sign-in (back-off check and attempt recorded inside the hashing slot).
- **Browser smoke test:** `tests/e2e/test_browser_smoke.py` (playwright, optional `e2e` extra), skipped unless
  `HBUDGET_E2E=1`.

## D-051 Image layout: pinned base, hashed dependencies, one venv
- **Decision:** a two-stage build on `python:3.13-slim` pinned by the digest of its multi-arch index
  (`sha256:b3575dcf…c3b2`). The builder stage installs `requirements.lock` with `--require-hashes`, then the app
  with `--no-deps`, into `/opt/venv`; the runtime stage copies only that venv. `requirements.lock` is generated by
  `make lock` (`uv pip compile --universal --generate-hashes`), so both platforms resolve to the same hashed files.
  The build backend (setuptools) is pinned the same way in `requirements-build.lock` and the app is installed with
  `--no-build-isolation`, so the build downloads nothing unpinned; pip and setuptools are then removed from the
  venv. Result: 227 MB unpacked, of which 181 MB is the base image.
- **Alternatives:** an Alpine or distroless base (musl or no shell: harder healthchecks and wheels); installing
  from `pyproject.toml` ranges at build time (not reproducible); a separate wheel stage (no gain here).
- **Why:** reproducible, auditable builds (`pip-audit -r requirements.lock`) with no compilers in the runtime.
  Note: FastAPI depends on `opentelemetry-api`, the no-op API package only; there is no SDK or exporter, so it
  sends nothing.

## D-052 A one-shot `init` service fixes the ownership of ./data
- **Decision:** Compose runs a short `init` service (same image, root, only `CHOWN` and `FOWNER`, no network,
  read-only, `no-new-privileges`) that sets the owner of the `./data` folder itself to the app user and mode 700,
  then exits; the app service waits for it (`service_completed_successfully`) and runs as 10001 (or
  `HBUDGET_UID`/`HBUDGET_GID`).
- **Alternatives:** asking every user to `sudo chown 10001 data` first (breaks "one command"); a root entrypoint
  that drops privileges (the app process would start as root); a named volume (the prompt asks for `./data`).
- **Why:** `docker compose up -d` works on a clean machine, and the long-running app never has root or any
  capability.

## D-053 Container defaults
- **Decision:** the image sets `HBUDGET_HOST=0.0.0.0`, `HBUDGET_DATA_DIR=/data`, `HBUDGET_PORT=8080`, runs as
  10001:10001, declares `VOLUME /data`, uses a Python `HEALTHCHECK` on `/healthz`, and its entrypoint sets
  `umask 077` before `exec hbudget serve`. Compose publishes the port on `127.0.0.1` only (`HBUDGET_HOST_PORT`
  to change it), with `read_only`, a 64 MB `/tmp` tmpfs, `cap_drop: ALL`, `no-new-privileges` and
  `restart: unless-stopped`.
- **Why:** listening on all interfaces inside the container is required for the published port; exposure is
  controlled where it matters, at the host binding. When the host port is not 8080, `HBUDGET_PUBLIC_URL` must be
  set so the CSRF origin check accepts the browser's origin (documented in `.env.example` and docs/DEPLOY.md).

## D-054 Behind a proxy, the trusted address is the Compose network gateway
- **Decision:** docs/DEPLOY.md tells users to set `HBUDGET_FORWARDED_ALLOW_IPS` to the Compose network's gateway
  (checked: a request to the published port arrives from `172.18.0.1` on the default network).
- **Why:** a proxy on the host connects through the published port, so the container never sees `127.0.0.1`.

## D-055 Multi-arch builds and CI actions
- **Decision:** `make image-multiarch` and CI build `linux/amd64,linux/arm64` with buildx (CI uses
  `docker/setup-qemu-action`); arm64 is built and tested in CI, not on a developer's machine. Every GitHub Action
  is pinned by full commit SHA with the version in a comment, and the trivy image by digest; Dependabot proposes
  the updates weekly. Workflows use `persist-credentials: false` and read-only `contents` permission.
- **Why:** an arm64 build needs either emulation or an arm64 machine; CI provides the emulation. A tag such as
  `v4` can be moved to different code by whoever controls the action; a commit SHA cannot.

## D-056 Container tests run in throwaway Compose projects
- **Decision:** `scripts/container_test.py` (stdlib only) runs each test in a temp folder with its own
  `docker-compose.yml` copy, `.env` and clean `./data`, on ports 18080/18081, and removes them afterwards (the
  data is removed from a throwaway container, since it belongs to uid 10001). The upgrade test builds an image of
  an earlier commit (`make image-previous`, `PREVIOUS_REF`) and, from the current image, a "next" image with one
  extra no-op migration, so a real migration with its backup is exercised. CI runs the tests without the upgrade
  step, which `make container-test` covers locally.
- **Why:** the repo's own `./data` is never touched, and the tests prove the documented quick start literally.

## D-057 The image privacy scan includes the app package
- **Decision:** `privacy_scan.py --image` now scans `site-packages/hbudget/` (the app) as well as app, data and
  home folders and the image metadata, and fails if the app package was not found. Other third-party packages
  stay out of scope.
- **Why:** the app is installed into the venv's site-packages; the earlier filter skipped it entirely.

## D-058 arm64 is tested in CI only (user decision, 2026-10-27)
- **Decision:** the arm64 image is built and container-tested only in the GitHub CI workflow (buildx with QEMU there). No QEMU/binfmt emulation is needed on a development machine.
- **Alternatives:** register QEMU binfmt on the development host with a privileged helper container (reversible, but it changes the host kernel settings).
- **Why:** CI already has the emulation; changing a developer's host for it is not worth it. **Revisit:** when a local arm64 run is wanted, or on real arm64 hardware.

## D-060 What CI does with the image
- **Decision:** the `image` job (after the `test` job passes):
  1. checks out the full history (`fetch-depth: 0`) and sets up QEMU and buildx;
  2. builds `linux/amd64` and loads it as `household-budget:latest`, then builds `linux/arm64` under QEMU and loads
     it as `household-budget:arm64` (build-push-action with `load: true`; the two builds share the GitHub cache,
     so each platform is built once and nothing is pushed);
  3. builds `household-budget:previous` from `PREVIOUS_REF` (`make image-previous`, a git worktree of commit
     3e671f9, the Phase 4 release candidate) with the current Dockerfile;
  4. runs `scripts/container_test.py` on the amd64 image with `--previous` (fresh start, restart, down/up, upgrade
     previous -> current -> next with one extra migration, hardening, image metadata);
  5. runs the same script on the arm64 image under QEMU with `--no-upgrade` (fresh start, restart, hardening,
     metadata; the upgrade step is skipped explicitly, because building a second arm64 image under emulation would
     double the job time and the upgrade logic is not platform-specific);
  6. scans the merged filesystem of a `household-budget:latest` container with `trivy rootfs` and fails on HIGH or
     CRITICAL findings that have a fix (`.trivyignore` lists accepted exceptions, empty for now). Changed
     2026-11-02 after the first GitHub run: `trivy image` read each layer and reported pip's vendored packages
     (msgpack, urllib3, setuptools) from the base image's Python layer, although the runtime stage deletes pip in
     a later layer. The rootfs scan still covers the Debian packages and the app's venv.
- **Revisit:** the upgrade test should start from real tagged release images (`PREVIOUS_REF` = the last tag, or a
  pulled image) once releases exist; until then it builds the Phase 4 commit with the current Dockerfile.
- **Not run on this machine:** steps 2 (arm64) and 6 (trivy) need QEMU and the trivy image, which this build
  session did not install. Steps 3-4 were run locally with `make container-test`.
- **First GitHub run (2026-11-02, PR #1):** every step passes, arm64 included.

## D-059 Runtime image hardening details
- **Decision:** the runtime stage removes setuid/setgid bits from every file, uninstalls pip/setuptools/wheel from
  the system Python (and the venv has none), deletes the apt and dpkg programs but keeps `/var/lib/dpkg/status`
  (so scanners such as trivy can still list the OS packages), and uses a Python entrypoint that sets
  `umask 077` and `exec`s `hbudget <command>`. `/bin/sh` stays for `docker compose exec`. The Compose `init`
  service refuses `HBUDGET_UID`/`HBUDGET_GID` 0.
- **Alternatives:** distroless (no shell for maintenance, harder healthcheck); removing `/var/lib/dpkg` too (blinds
  vulnerability scanners).
- **Added 2026-11-02:** `ensurepip` is removed from the runtime Python as well; its bundled pip wheel carried old
  vendored copies of urllib3, msgpack and pkg_resources that scanners flag, and the app never uses it.

## D-051 Demo household and shareability (Phase 6)
- **Demo:** `hbudget.demo.install(conn, hid, today=, user_id=)` writes a made-up two-earner budget dated from today
  (as-of = ten days ago), about three months of reconciled bank history and card purchases, and one bank export
  waiting on the Reconcile page (automatic matches, one over-budget bill, one card-payment bundle, one tracked and
  one unknown posting). Every name and amount is invented; amounts are whole dollars.
- **HBUDGET_DEMO is kept:** `1` makes "The demo household" the default choice on the setup page, and fills the first
  household when the admin is created from `HBUDGET_ADMIN_USERNAME` / `HBUDGET_ADMIN_PASSWORD`. It never adds a demo
  to a server that already has users. Alternatives: remove the setting (one more step for people trying the app), or
  create a separate demo household at every start (surprising, and it would need an owner).
- **CLI:** `-` / `/dev/stdin` input is sniffed (JSON, OFX/QFX, xlsx, bills CSV, else YAML); `import-bills` also takes
  the simple bills CSV (`examples/bills.example.csv`).

## D-061 Optional LAN binding (user decision, 2026-11-02)
- **Decision:** Compose publishes the port on `${HBUDGET_BIND_ADDRESS:-127.0.0.1}`. The default stays localhost
  only; setting `HBUDGET_BIND_ADDRESS=0.0.0.0` in `.env` publishes it on every interface for use on a trusted home
  network over plain HTTP, together with `HBUDGET_ALLOWED_HOSTS` and `HBUDGET_PUBLIC_URL` set to the LAN address.
- **Why:** a common home setup is a small server or container with no outside connections, reached from devices on
  the home network without Tailscale or a reverse proxy. Making it a setting keeps D-053's safe default for
  everyone else. Docs state the trade-off: unencrypted on the LAN, Docker bypasses host firewalls, never forward
  the port on a router. **Revisit:** if the app is ever reachable from outside the home network.

## D-062 HTTPS in the app with your own certificate (user request, 2026-11-02)
- **Decision:** `HBUDGET_TLS_CERT` and `HBUDGET_TLS_KEY` (set both or neither) make uvicorn serve HTTPS on the same
  port. Compose mounts `${HBUDGET_CERTS_DIR:-./certs}` read-only at `/certs`; the repo keeps an empty `certs/`
  (ignored apart from `.gitkeep`) so Docker never creates it as root. `hbudget serve` loads the pair once before
  starting and stops with a plain reason for a missing or unreadable file, a mismatch, non-PEM input or a
  passphrase-protected key. The container healthcheck is now `docker/healthcheck.py` (stdlib only, copied into the image, not part
  of the app package, so it works with any app version, including the old code the upgrade test builds with
  the current Dockerfile): plain HTTP, or HTTPS that
  trusts exactly the configured certificate (partial-chain verification, no host name check since it connects to
  127.0.0.1), so an expired certificate shows as unhealthy. Compose no longer repeats the healthcheck: the image's
  `HEALTHCHECK` is used, so an older image started with a newer `docker-compose.yml` (the upgrade test, or a
  pinned `HBUDGET_IMAGE`) keeps the check it was built with. Secure cookies and HSTS follow the request scheme as
  before (`HBUDGET_COOKIE_SECURE=auto`).
- **Why:** a household on a home LAN (D-061) may want encryption there with certificates it supplies,
  without adding a reverse proxy. Alternatives: a Caddy sidecar in Compose (automatic certificates, but another
  image to pin, scan and update, and LAN-only names still need a private CA), or Tailscale/a proxy only (already
  documented, still the recommendation for access from outside the home). No HTTP-to-HTTPS redirect on the same
  port, and no key passphrase support: both add moving parts for little gain at home.
- **Revisit:** if certificate renewal without a restart is needed.

## D-063 Index the upload links by transaction (Phase 1)
- **Decision:** migration 0005 adds `upload_txns_txn (household_id, txn_id, upload_id)` and the same on
  `card_upload_txns`, and the history filter (`repo._IN_HISTORY`) uses nested `EXISTS` instead of a join, so both
  of its lookups start from the transaction's links. `upload_id` is in the index so it covers the inner lookup;
  without it SQLite kept choosing the primary key, which can only narrow by household.
- **Why:** the filter runs for the summary, Report and Check-up. With two years of overlapping weekly exports
  (about 36,000 link rows) it took 3.4 s per call before and 3 ms after (measured in memory, same data).

## D-064 Scheduled backups: `hbudget backup` (Phase 1)
- **Decision:** `hbudget backup [--keep 14] [--keep-upgrade 5] [--dir]` copies the live database with SQLite's
  online backup API (read-only source connection), switches the copy to `journal_mode=DELETE` so it is one
  self-contained file, runs `PRAGMA integrity_check`, and only then renames `.hbudget-<time>.db.partial` to
  `hbudget-<time>.db` (mode 600). Retention keeps the newest scheduled copies and, separately, the newest
  pre-upgrade copies; other files in the folder are never touched. Scheduling is left to the host (a systemd
  timer in DEPLOY.md), and encryption and offsite copies to host tools (age, vzdump).
- **Why:** the only whole-database backup documented before stopped the app, and nothing ran on a schedule;
  pre-upgrade copies were never removed. Alternatives: a scheduler inside the app (another moving part in a
  read-only container that must not make outbound calls), or encrypting inside the app (key management in the
  container; host tools do this better).

## D-065 Five-section navigation and the Home page (Phase 2)
- **Decision:** the thirteen tabs become five sections, Home · Bills (Bills, Check-up, Income and bills) · Plan
  (What if, Debts) · Money (Bank, Report, Card) · More (Rules, Notes, Import, Export, Members). A section with
  several pages shows them as a second row of links; on screens 620px and narrower the sections are a fixed
  bottom bar. `/h/{id}` and sign-in land on Home. The account links, theme switch, sign-out and household
  switcher move into one menu in the header. Home shows the summary, money left before the next payday and the
  bills due before it (`core.paydays`), the next 60 days of the balance, the paydays after that, and the alerts;
  other pages keep the summary strip but show only a link to the alerts.
- **Why:** thirteen tabs scrolled sideways on phones and gave no starting point; the alerts repeated on every
  page. A payday is the period the household actually plans in. Alternatives: a hamburger menu (hides every
  section behind a tap) or a sidebar (no room at 360px).

## D-066 Released images on GHCR (Phase 2, user decision 2026-11-03)
- **Decision:** a tag `vX.Y.Z` runs every CI check (ci.yml is reusable), then pushes a multi-arch image to
  `ghcr.io/optimus506/household-budget` as `X.Y.Z`, `X.Y` and `latest` with an SBOM and `mode=max` provenance,
  and makes a GitHub release from the CHANGELOG.md section. `__version__` is the single version source
  (pyproject reads it); `scripts/release.py prepare` bumps it, dates the changelog, commits and tags.
  `docker-compose.yml` runs the published image; `compose.build.yml` restores building from source.
  Dependabot watches the base image digest and the actions; CI also runs weekly.
- **Why:** the owner wants `docker compose pull` deployments, and the same path for other households later.
  Building on each server needed the source, build time and network access to PyPI and Debian mirrors there.
  Alternatives: Docker Hub (another account, rate limits), or keeping local builds (no tested, identical
  artifact between machines). GitHub artifact attestations were left out at the time (private repositories need
  GitHub Enterprise for them); BuildKit's provenance and SBOM attestations travel with the image instead.

## D-067 `scripts/upgrade.sh`: backup, pull, health wait, rollback (Phase 2)
- **Decision:** a POSIX shell script on the host. It tags the configured image `household-budget:rollback`
  before pulling, stops if the pull brought nothing new, backs up the database from the running container,
  starts the new image and waits for Docker's health status. If it is not healthy within the timeout it keeps
  the database the new version left (`hbudget-failed-upgrade-<time>.db`), restores the backup, and starts the
  rollback image. Tested against a local registry with a good and a deliberately unhealthy image.
- **Why:** a failed migration or start used to need manual steps under pressure. The rollback image is tagged
  *before* the pull because Docker's containerd image store deletes an image as soon as its last tag moves,
  and a running container's image id cannot be tagged afterwards (found in testing).

## D-068 API and agent access: tokens, /api/v1 and /mcp (Phase 2, user request 2026-11-03)
- **Decision:** owners make per-household access tokens (`hbt_` + 40 random characters; only the SHA-256 and the
  last four characters are stored; read or write scope; optional expiry; revocable; last-used time). A token acts
  with the lower of its scope's ceiling (read: viewer, write: editor) and its creator's current role, re-read on
  every request. `/api/v1` is a small, documented REST contract with its own OpenAPI file (for token holders and
  signed-in users only); `/mcp` is an MCP server (Streamable HTTP, stateless, JSON responses) whose tools call the
  same operations. These routes accept only `Authorization: Bearer` and never read cookies, so they are exempt
  from the CSRF check (the Origin check still applies). Writes are limited to marking bills, adding expected items
  and marking them; nothing deletes. (Widened in D-069.) Audit entries made with a token carry `via: token: <name>` (a context
  variable set around the write). 120 requests a minute per token, in memory. Tool results tell the agent that
  names and descriptions are data, not instructions. Implemented without the MCP SDK (a small JSON-RPC handler,
  no new dependency) and checked against the official Python SDK client.
- **Why:** the owner wants other programs and other people's Claude agents to use the app. Cookie sessions with
  CSRF suit browsers, not scripts; a separate versioned contract lets the pages' internal API keep changing.
  Alternatives: OAuth (needed for hosted connectors that call from the internet, which this LAN app does not allow
  by default; a separate decision), per-user tokens across households (a leaked token would reach every
  household), or the MCP SDK (a large dependency for 13 tools; its server expects its own ASGI app and sessions).

## D-069 Write tokens edit rows and reconcile (issues #20 and #21, 2026-11-03)
- **Decision:** write tokens also get the editor's row actions from the Bills page (change an amount, add a one-off
  row, remove, undo, move) and the Reconcile page's flow (upload a bank or card export, dry run, apply with the
  preview's fingerprint, delete an unused upload), in `/api/v1` and as MCP tools. They call the same functions as
  the pages, so the audit, the stale checks and the time limit are the same. Changing or removing a row needs the
  amount the caller last read (`expected_amount`, positive as the API shows it); apply needs the fingerprint, so a
  decision is never applied to proposals that changed. An export whose columns no saved layout matches is refused:
  the column mapping stays on the page. Over MCP the file travels as text in the JSON body, so the 1 MB JSON limit
  applies; REST takes a multipart upload up to the upload limit. Series, rules, settings and members stay page-only.
- **Why:** an agent that keeps the app in step with the household's spreadsheet needed host shell access for its two
  weekly writes (reconcile, and one-off row edits), which is far more than an editor token. Remove is undoable, a
  reconciled upload is only hidden, and a reconcile is applied only from a dry run the caller has seen, the same
  safeguards an editor has on the page.

## D-070 Tokens save CSV layouts, edit series details and payee rules (issues #23, #24, #26, 2026-11-03)
- **Decision:** widens D-069. `upload_bank_export` takes an optional column `mapping` and `save_as`, and the
  needs-mapping refusal includes a suggested mapping; write tokens can set a recurring item's card, status, group and
  notes (not its amount or schedule), and list, test, add, change, delete and reorder payee rules, through the same
  functions as the Rules page. A series edit that leaves the amount, schedule and active flag alone no longer
  regenerates its rows: open rows from today on only take the new name and card (`ledger.relabel_series`). Members,
  settings and the series' amounts and schedules stay page-only.
- **Why:** the household's agent keeps the app in step with their spreadsheet. Without these it had to convert the
  bank's own export by hand, could not change a rule when a payee moved to a budget row, and could only set a card by
  saving the series, which reset imported per-week amounts.

## D-071 Allowance rules (issue #25, 2026-11-03)
- **Decision:** a new payee-rule kind, `allowance`. Its payments are matched automatically: each takes its amount
  off the first open target row in its calendar month or later (a partial payment, as Reduce does), and one that
  uses the row up clears it and spills the rest into the next row. Once the export's last date is past the end of a
  row's month, the dry run proposes releasing what is left (`leftover`, no bank line); leaving it keeps the money in
  the forecast. Allowance matches do not raise bill-variance alerts, and the check-up compares the monthly total with
  the row. Telling one-off purchases through the same payee apart uses the existing amount bounds: one rule per
  amount (`min_amount` = `max_amount`), ahead of a `track` rule for the rest.
- **Why:** a subscription bundle, a fuel or grocery envelope or a school-lunch account is budgeted as one monthly row
  but paid in several debits. As `bill` the first debit cleared the whole row or fell out of tolerance; as `ask` each
  debit needed a decision, and nothing offered to release the leftover. Releasing stays a person's choice because
  a late charge for the month can still arrive.

## D-072 Undo a reconciliation; tokens change amounts and debts and read changes (#28, #29, #30, #35, 2026-11-03)
- **Decision:** applying a reconciliation now keeps what undoing it needs in its record (`result.undo`: the rows it
  changed before and after, the balance and dates before, the expected items it marked done). Only the newest one can
  be undone, and only while every row it touched and the balance are as it left them; undo restores those rows,
  deletes the record and sets the upload back to waiting, audited as `reconcile.undo`. Reconciliations from before
  this version or from a backup have no undo data and are refused. Write tokens can set a series' amount from a date
  (`ledger.set_series_amount`: open rows not changed by hand and not in a seasonal month) and its seasonal amounts,
  update a debt's figures (a new balance is as of today unless dated; `get_debts` flags balances older than 35 days),
  and read the audit log after a cursor (`list_changes`, editor). The log still holds ids and actions only (D-037),
  so a change lists the item, not old and new amounts. Balance history per debt is left for the Phase 3 burn-down.
- **Why:** an agent keeping the app in step with the household's records needed these weekly: rate changes and
  seasonal bills, statement balances, "did anyone change it since last time", and a way back from a wrong proposal.

## D-073 Versioned static paths (2026-11-03)
- **Decision:** pages link CSS, JavaScript, icons and the manifest under `/static/v<version>-<hash>/`, where the hash
  covers every static file, and that path is served with `Cache-Control: public, max-age=31536000, immutable`. The
  plain `/static/` path still works and is served with `no-cache`. JavaScript modules import each other by relative
  path, so a page's modules all come from the same version.
- **Why:** after an upgrade, browsers kept old JavaScript from heuristic caching and called endpoints that had
  changed. A path that changes with the files needs no cache-busting query strings and no build step.

## D-074 Rules and Settings split; caps and groups beside what they change (2026-11-03)
- **Decision:** the Rules page keeps only payee rules and the rule tester. A Settings page (More) holds the checkbook
  balance and date, alerts, reconcile matching, the forecast horizon, status colours and the week start. Spending
  caps move to the Check-up page, which shows their totals, and report groups to the Report page, which draws them;
  each has its own save (`PUT /caps`, `PUT /flow-groups`) that applies at once. `PUT /settings` keeps the caps and
  groups when it is sent without them.
- **Why:** one page mixed four jobs, and caps and groups were edited far from where their effect shows, then lost
  if "Save settings" was not pressed after adding one.

## D-075 Bill finder and bulk changes, all or nothing (2026-11-03)
- **Decision:** the Bills page searches every open row in the forecast by name (in the browser, from the ledger it
  already loads) and changes the ticked rows with one request, `POST /rows/bulk` (change amount, mark status or
  remove; up to 300 rows). It is one transaction: each row carries the amount the page read, and if any differs or a
  row is gone, nothing is saved and the answer names them. Marking a status needs a contributor, as one row does;
  changing or removing needs an editor. Each row's change is undoable on its own, as before.
- **Why:** a rate change or a bill moved to autopay meant opening each week's row in turn. Partial bulk saves were
  left out of the old board on purpose (ROADMAP, "Not carried over"): half-applied changes are harder to fix than
  none.

## D-076 Report months: full calendar months only, averaged over what the history covers (2026-11-03)
- **Decision:** the actual Report counts only full calendar months: a bank history that starts mid-month leaves that
  month out, and the per-month figures are divided by the number of full months covered (at most 12, or 2 in the new
  "last 2 months" view), not by 12. Less than one full month shows nothing yet, with a note. The Card page's "last 2
  months" counts purchases from two months before today; the Check-up shows "Track only" payees per week for the
  last 8 weeks, empty weeks included.
- **Why:** a household with four months of history saw every monthly figure at a third of its real size, and a
  partial first month pulled averages down further.

## D-077 Debt balance history on the account; burn-down from the payoff simulation (2026-11-03)
- **Decision:** each account keeps `history`, its past (as of, balance) pairs, oldest first, at most 120: saving an
  account with a new balance or date adds the new pair and, the first time, the previous one (`payoff.record`), one per
  date with the latest winning. It is stored on the account row (`history_json`, migration 0007) and exported in the
  bill system's `accounts[].history`, so backup, restore and wipe need nothing new. The payoff simulation also returns
  the total owed at the start and after each month (`balances`); the Debts page draws those for the chosen method,
  the other method and payments only, marks each payoff on the line, and adds an "Actual" line summed from the
  histories (each debt's latest balance on or before each date).
- **Why:** the Phase 3 burn-down needs real progress to compare with the plan, and the agent asked for balance
  history with its debt updates (#30). A separate table would have needed its own backup, restore and wipe code.

## D-078 Bills section: By payday, All bills, Debts, Check-up (2026-11-03)
- **Decision:** the Bills section's pages are By payday (was "Bills"), All bills (was "Income and bills", page key
  `series` unchanged), Debts (moved from Plan) and Check-up. Plan keeps only What if, so it shows no tab row. Still
  five sections (D-065).
- **Why:** the household could not find the list of every bill or the debts: the Bills tab looked like the payday
  view on Home, "Income and bills" was the third tab and off-screen on a phone, and debts sat under Plan.

## D-079 "Pay & bills": three lists on the recurring-items page (2026-11-04)
- **Decision:** the page of recurring items (key `series`) is called Pay & bills and shows three lists with their own
  headings: Pay (income), Bills (paid from checking) and Spending budgets. The kind choices read Pay / Bill /
  Spending budget, and the page says that debts (what is owed) are on the Debts tab. This revises the "All bills" name
  from D-078.
- **Why:** under "All bills" the paychecks looked like bills, and "bills" was read as "debts"; one mixed table did
  not say which was which.

## D-080 Weekly check-in on Home, kept in the audit log (2026-11-04)
- **Decision:** Home opens with a three-step check-in: (1) the bank file, linking to the waiting upload or to the
  upload page, with the reconciled-through date; (2) bills due in the next 7 days (late ones included) with no
  status, each with Scheduled / Autopay buttons; (3) what other members and tokens changed since your last check-in,
  in words ("partner marked Rent"), from the audit log, leaving out sign-ins, exports and the forecast extending
  itself. "Done for this week" adds a `checkin.done` audit entry (contributor or above); "this week" and "since" use
  the audit log's clock. No new table: who and when are already what the log keeps (D-037).
- **Why:** the weekly routine was spread over three pages, and members could not see each other's changes without
  the owner-only activity log. Kept gentle as decided for Phase 3: no streaks, no "you missed a week".

## D-081 Goals live in the settings; progress is computed, never stored (2026-11-04)
- **Decision:** goals are a list in the household's settings (`settings.goals`, kept in `settings_json`, exported as
  the bill system's top-level `goals`), so no migration and backups include them. Three kinds: `emergency` (target =
  months x the average monthly bills, from open bill and spending rows over the next 12 months), `savings` (a typed
  target) and `debt_free` (remembers the debt owed when it was set; progress = paid down since; the countdown is the
  avalanche plan's debt-free month, mortgage and debts left out of the plan excluded). Saved amounts are typed in:
  the app moves no money. With a date, the page shows what is needed each month. Editors edit them on Plan > Goals
  (`PUT /goals` replaces the list); Home shows up to four; agents read them with `get_goals`.
- **Why:** Phase 3 engagement: a visible target that moves when bills are paid and money is put aside, without
  linking bank accounts.

## D-082 Milestones: found on demand, celebrated once, kept in the settings (2026-11-04)
- **Decision:** `core.milestones.detect` finds what is true now: a debt with a balance in its history that is now 0;
  a goal reached; the latest unbroken run of finished weeks whose lowest bank balance stayed at or above the cushion,
  at 4, 8, 13, 26 and 52 weeks; a finished calendar month the history covers from the 1st in which money out (less
  "ignore" rules) was under what the budget planned for it (bill and spending rows, not removed, at their original
  amounts). Home shows the ones not yet in the history with a small animation (none with reduced motion); "Nice!"
  (contributor or above) adds them to `settings.milestones` (at most 200, in backups as `milestones`), so each is
  celebrated once for the household. `settings.celebrate` hides the card; wins are still kept.
- **Why:** Phase 3's gentle engagement: notice good news without tracking failures. Nothing is ever taken away:
  a broken run simply starts again, and no message mentions it.

## D-083 Card moves: stores on the category, single purchases marked by hand (2026-11-04)
- **Decision:** a card purchase's store is `cards.store(description)`: upper case, cut at the first `*` or `#`,
  words with a digit dropped, other punctuation turned to spaces (at most 40 characters), so branch and order numbers
  do not split a store. Moving a store adds that name to the target category's `merchants` (removed from any other);
  `categorise` tries those before keywords, then the export's bank category, then "Other". Moving to "Other" only
  takes the store out of the moves, so a matching keyword still wins, and the answer says so. A store move also
  takes over that store's purchases that were moved one by one (the latest action wins). A single purchase moved by
  hand gets `card_txns.by_hand`; re-sorting (saving the categories, a store move) leaves it alone while its category
  exists. Migration 0008 adds `card_categories.merchants_json` and `card_txns.by_hand`; both are in the bill
  system (`card_categories[].merchants`) and the backup (`card_transactions[].by_hand`), and wiped with the rest.
  Saving the categories without `merchants` keeps each category's moves, so an older page cannot drop them.
- **Why:** the Card page is where overspending shows, and a keyword list alone cannot fix one store or one odd
  purchase. Keeping store moves on the category means renaming or deleting a category carries them along, and
  they travel with the bill system as plain names, never transaction ids. The agent tools (#33) call the same
  functions as the page.

## D-084 Subscriptions are found on demand from the history (issue #34, 2026-11-04)
- **Decision:** `core.subscriptions.detect` groups money out by payee (bank: `alerts.payee_key`, as the "repeat"
  alert; card: `cards.store`), leaving out pay, card payments, transfers and ignored lines (by payee rule kind).
  Charges of one exact amount whose gaps are 25 to 35 days are monthly, 350 to 380 days yearly. A run of the same
  payee that starts one period after another run ended, at a different amount (half to double), continues it at a
  new price; every part but the latest needs two charges, so a store visited monthly for varying sums is never a
  subscription, while a price change shows from its first new charge. Next expected = last + one period; stopped
  when today is more than 10 days (yearly: 30) past it, listed for 180 days; new when first charged in the last 70
  days. Each one names the payee rule its latest bank charge matches and that rule's budget lines, or the card
  category. Nothing is stored, so there is no migration, no backup change and nothing to wipe.
- **Why:** the household found its subscriptions by reading receipt emails and missed a price rise and a stopped
  charge. Detecting from the history the app already holds needs no extra input and cannot drift out of date.
  Exact amounts keep it predictable; charges that vary every month (utilities) are bills, checked on Check-up.

## D-085 Card transfers are totals of card-funded rows, shown, not booked (issue #31, 2026-11-04)
- **Decision:** `paydays.transfers` groups a pay period's open card-funded rows (`row.card`, bills and spending,
  money out) by card; Home, Bills > By payday and `get_paydays` show each total as "Move to <card>".
  `paydays.transfer_plan(card, through)` gives the open rows due through a date, late ones included; the API's
  default is the day before the next payday. Nothing is added to the ledger: the forecast still counts each
  card-funded row on its due date, and reconcile still proposes the card bundle when the card payment posts.
- **Why:** the question asked every pay period is "how much do I move to the card?", and the rows already say. The
  issue's optional single "Transfer to Card" row would change how the forecast and reconcile treat card-funded
  rows; it is left for a later change, if the household wants the forecast to move the money on payday instead.

## D-086 "Not a subscription" hides a payee by source, kept in the settings (2026-11-04)
- **Decision:** the Subscriptions page, `PUT /recurring/hidden` and `hide_recurring_charge` add `(source, payee)`
  to `Settings.not_recurring`; `subscriptions.detect` lists those apart (`hidden`) and leaves them out of the
  totals. It is stored in the settings JSON (no migration), written to the bill system as top-level
  `not_recurring` (in backups and restores) and cleared with "Delete all budget data". At most 500.
- **Why:** detection finds only exact repeats, but a gym's day pass or a utility on a flat plan can look like a
  subscription. The payee, not the detected item's id, is the key, because the id changes when older history is
  uploaded or the price changes; bank and card are kept apart, so hiding a bank payee leaves a card store alone.

## D-087 "Move card money on payday" is a forecast setting, not ledger rows (issue #31, 2026-11-04)
- **Decision:** with `settings.card_transfers_on_payday` (off by default; Settings page, bill-system
  `settings.card_transfers_on_payday`), `forecast.compute` counts each open card-funded money-out row as 0 on its
  own date and adds one virtual "Move to <card>" item per card on the open payday that opens the row's pay period,
  after that day's rows. Rows before the first open payday stay on their dates. Totals, the end balance and each
  pay period's money out are unchanged; the money only leaves checking earlier.
- **Why:** it is the optional "Transfer to Card" row from #31 (D-085) without touching the ledger: the rows, their
  statuses and reconcile (the card bundle cleared when the card payment posts) work as before, and turning the
  setting off gives the old forecast back. Every view that uses the forecast (Home, Bills, alerts, exports, the
  API) follows it. The ledger table keeps listing the card-funded rows; "Move to <card>" shows on the chart, Home
  and Bills > By payday.

## D-088 Subscriptions: prices within 1%, bills apart, a skipped month, generic payees (#61, #62, 2026-11-05)
- **Decision:** `subscriptions.detect` treats prices within 1% as one price (no 1-cent "price change"); allows
  one skipped month (a 55 to 70 day gap) inside a run and between prices; a bank payee that a **bill** rule matches
  is a bill (`group: bill`): any new price continues it and its cost is counted apart (`bills_monthly`). A payee also
  paid other amounts (a restaurant) needs three charges of one price, not two. A late charge whose payee still
  charges amounts outside every subscription is `irregular`, not stopped. Payees that stand for many merchants
  (PayPal, Square, Amazon, ...) are `generic`: their prices are never joined.
- **Why:** the first version (D-084) read a cheaper phone plan or a skipped loan payment as "stopped", which says
  "cancelled" while the money still goes out, and its headline mixed the mortgage and loans with subscriptions.
  A bill rule is the household's own word that a payee is a bill, so it is the signal used; amounts alone cannot
  tell a loan from a subscription. Two equal restaurant bills a month apart are common, three are not.

## D-089 The service worker shows an offline page and stores no budget data (2026-11-05)
- **Decision:** `/sw.js` (root scope, `no-cache`) caches only `/offline`, the stylesheet and the icon. For a page
  load it goes to the network and shows `/offline` only when that fails; API calls and everything else are never
  intercepted or stored. `/offline` is a static page with no data that needs no sign-in. Pages register the
  worker only in a secure context (HTTPS or localhost); the cache is named after the static tag, so an upgrade
  replaces it.
- **Why:** an installed app on a phone outside the home network otherwise shows the browser's error page. Keeping
  balances or bills offline would put the household's data on every device that ever opened the app, and stale
  numbers in a budget app are worse than none.

## D-090 The recap is worked out from the history on demand, for a calendar month or year (2026-11-05)
- **Decision:** `core.recap` reads the bank and card history, the ledger and the milestones for one calendar month
  or year and the one before: money in and out (ignored lines left out; card payments are money out, but not a
  "payee"), pay, card spending by purchase date against the budget of the categories that have one, the top five
  payees, bills cleared in the period and bill rows due in it that are still open, subscriptions first charged or
  repriced in it (detected as of its last day) and the wins dated in it. Money > Recap, `GET /recap`,
  `get_recap` and `GET /api/v1/recap`; the default is the last full period with bank history.
- **Why:** a look back is what a weekly check-in leaves out, and everything it needs is already stored, so nothing
  new is kept and an upload or a reconciliation changes the recap at once. Calendar periods match bank statements.

## D-091 Bank transactions through Plaid, off unless the server and the household both turn it on (2026-11-09)
- **Decision:** `HBUDGET_PLAID=off|sandbox|production` (with a client ID and secret) enables it on the server; an
  owner then turns it on per household by typing the household name (More > Bank connection). Off, nothing of it
  is imported or routed. A sync is manual (the check-in or the Bank connection page) and becomes ordinary uploads:
  a waiting bank upload for the one reconciled checking account, its running balance worked back from Plaid's
  current balance and kept only when the start balance appears in it (else matched by date), and an imported card
  upload for card accounts. Pending transactions are left out. A Plaid posting with the same date and amount as one
  already stored from a file keeps the file's id. Access tokens are sealed with AES-GCM (key from the secret key)
  and are never in backups, exports or the API; a wipe keeps the link and starts the Plaid history over.
- **Why:** the household's bank is on neither SimpleFIN nor a usable export, so Plaid is the only automatic route,
  but the app's promise is that nothing leaves the server; making it a deliberate, two-step choice keeps that
  promise for everyone who does not opt in. Reusing uploads keeps reconciliation, the check-in and every report
  unchanged. No scheduled sync or webhooks: the server is on a home network, and syncing during the weekly
  check-in is enough (the household's choice).

## D-092 An optional daily Plaid sync that only stores; the check-in makes the upload (2026-11-09)
- **Decision:** per household, *Off* (the default) or *Daily*, set by an owner. A server thread looks hourly and
  syncs each bank not synced in the last 20 hours (claimed with one atomic update, so restarts or two processes
  never double up). It stores postings, withdrawals and balances only. The check-in counts what is stored but not
  yet in an upload and offers **Bring them in**, which syncs and makes one bank upload with everything since the
  last reconciliation; a Plaid bank upload still waiting is replaced, not stacked. Card uploads take every stored
  card posting no upload has taken yet.
- **Why:** once real syncs worked, a fresh balance and early sign-in problems were worth a daily call (Plaid bills
  per linked bank, not per call, and refreshes from most banks only a few times a day, so more often adds nothing).
  Keeping the upload a person's action keeps the review in the weekly routine and means days without a check-in
  still end in one upload that reconciles in one pass. Polling, not webhooks, so nothing new is exposed.
  Supersedes D-091's "no scheduled sync".

## D-093 The browser smoke test runs on every release and weekly (2026-11-10)
- **Decision:** the headless-browser test (`tests/e2e`) runs in the release workflow before anything is published,
  in the weekly scheduled CI run, and by hand. It still does not run on pull requests.
- **Why:** it is the only check of the JavaScript. As a manual job it was forgotten and once broke unnoticed;
  a release must not ship pages nobody loaded. On every pull request it would add a browser download and a
  minute or two to each run for changes that mostly do not touch the front end.

## D-094 Starter rules: a rule pack is added, never merged over what is there (2026-11-10)
- **Decision:** the app ships one rule pack (`examples/starter-rules.yaml`, packaged as
  `hbudget/schema/starter-rules.yaml`) and can add it, or a pack file the household supplies, from the Rules page
  and with `hbudget rule-pack`. A pack is its own small format, not a bill-system file: its rules name no
  series. On adding, a `pay` rule is linked to every income series and a `bill` rule to the bills whose name
  contains one of its `bills_named` words; a rule that cannot be linked, or whose bill already has a rule, is
  skipped and reported. Rules whose id exists, and existing report groups and card categories, are never
  changed. The pack's rules go last, and a rule added afterwards goes above the trailing `starter-` rules.
  Packs hold only `pay`, `bill`, `track` and `ignore` rules, with no amounts.
- **Why:** writing a rule per payee was the slowest part of a first week, and a new household has no rules at
  all. A bill-system merge could not do it: its rule targets must be series in the same file, and a merge
  replaces items with the same id, which would undo a household's edits. Adding only, with a dry run first,
  makes the button safe to press at any time. Broad rules ("GROCERY") must not shadow a household's specific
  ones, hence the order.
- **The names in the pack** are large national companies, chosen from general knowledge and not from any
  household's data. The deny-list scan does not report names in that one file (`PUBLIC_LISTS` in
  `scripts/privacy_scan.py`); amounts, numbers and suffixes in it are still checked. Local or regional payees
  do not belong in the pack.


## D-095 Two-factor sign-in: optional TOTP, standard library only, key kept as the database keeps everything else (2026-11-10)
- **Decision:** a user can turn on a second sign-in step under Account: a 6-digit code from any authenticator
  app (TOTP, RFC 6238: SHA-1, 30 seconds, one step either side for clock drift). Off by default;
  `HBUDGET_REQUIRE_2FA=1` makes every user set it up before any other page or session API call works
  (access tokens made earlier keep working; a new or reset account is password-only until its first sign-in
  sets it up).
  - **No new dependency.** The code maths (`core/totp.py`) uses `hmac` and `hashlib`; the QR code on the set-up
    page is drawn by a small encoder in the app (`core/qr.py`, byte mode, error correction M, inline SVG),
    checked in development against a reference library. The page loads nothing from the internet and the key
    is also shown as text.
  - **A code works once.** The last accepted time step is stored per user and a code for that step or an
    earlier one is refused. Ten one-time recovery codes are shown once at set-up; only their SHA-256 hashes
    are stored (each code is 60 random bits, so a slow hash adds nothing). They can be replaced with a code.
  - **Sign-in is two steps.** The right password gives the browser a 5-minute challenge cookie (path `/login`,
    stored hashed), not a session: it opens no page and no API. A right password does not clear the back-off
    for a two-factor user, and wrong codes count towards the same back-off (per user and per address) as wrong
    passwords, so knowing the password does not buy unlimited guesses at the code.
  - **Changing it** asks again: set-up needs the password, turning off needs the password and a code, new
    recovery codes need a code. The key of a set-up in progress is shown to, and can be confirmed by, only
    the session that gave the password, for 10 minutes; another session of the same user sees nothing.
    Turning it on or off signs out the user's other sessions. A new password or a disabled account ends a
    sign-in that is between its two steps.
  - **Access tokens are unchanged**: a bearer token is its own secret and an agent cannot type a code.
  - **A new way in asks for a code.** From a two-factor user these need a current code (header `X-2FA-Code`;
    the pages ask for it): making a token, adding a member or changing a role, and for an admin creating or
    re-enabling a user, changing an admin flag, making themselves owner of a household, and resetting someone's
    password or two-factor sign-in. Each would outlive a borrowed session. Revoking a token needs no code: it
    only takes access away, and a leaked token must be easy to kill. Recovery codes are not accepted here. A user without two-factor
    sign-in is asked nothing, as before.
  - **Lost phone:** a recovery code; or an admin resets another user's on the Admin page; or
    `hbudget disable-2fa USERNAME` on the server. An admin cannot reset their own from the Admin page. All
    are in the audit log.
  - **Guessing codes** is limited per user to 10 wrong codes a day (D-097); the failures are in the audit log
    (`login.code.failed`, `2fa.locked`, and `login.password.ok` with no `login.ok` after it).
  - **The key at rest is not sealed.** It is stored in the database as the authenticator shows it.
- **Why:** the app is meant for a home network or Tailscale, but some households will put it behind a reverse
  proxy, and then a reused or phished password must not be enough. TOTP works offline, with any app, with no
  outside service. **Why the key is not sealed:** the only key the app has is `data/secret_key`, which sits
  next to the database and is in the same backups, so sealing would stop nobody who has the files; and whoever
  has the files has the whole financial history already, with or without signing in (SECURITY.md says the data
  folder is not encrypted at rest). Sealing would also pull `cryptography` into every install; today it is
  imported only when Plaid is on. **Alternatives:** WebAuthn/passkeys (needs HTTPS and a stable name, which a
  LAN install often lacks; possible later next to TOTP); e-mail or SMS codes (the app sends nothing out);
  a QR library dependency (one more package to pin and audit for one picture).

## D-096 The app is called Hearthfund; names inside files and settings do not change (2026-11-10)
- **Decision:** the app, the repository, the container image (`ghcr.io/optimus506/hearthfund`), the Compose
  project, the Python distribution, the MCP server name and download file names are **Hearthfund** /
  `hearthfund`. Unchanged, for good: the Python package and command `hbudget`, every `HBUDGET_*` setting, the
  database file `hbudget.db` and backup file names, and the file format ids `household-budget/bill-system` and
  `household-budget/rule-pack` (and the schema's `$id`). Earlier entries in this file and in CHANGELOG.md keep
  the old name where they describe the past.
- **Why:** "household budget" describes every app of this kind and cannot be searched for or told apart; a
  public project needs its own name. The names that stay are the ones files and deployments already depend
  on: changing a setting name or a format id would break every existing `.env`, backup and exported bill
  system for no benefit to the household, and docs/STABILITY.md promises they stay.
- **Existing deployments:** Compose names containers after the project, so a new project name would start a
  second app beside the running one. `scripts/upgrade.sh` therefore pins `COMPOSE_PROJECT_NAME=household-budget`
  in `.env` when it finds containers of the old project in the folder. It now compares against the image the
  app is running rather than the image tag, because the tag's name changed. Images up to 0.15.0 stay under
  the old name; later releases are published under the new one only.

## D-097 The sign-in back-off counts per name and address; wrong codes are limited per day (2026-11-10)
- **Decision:** the per-username back-off of D-036 is now kept per username **and address**, so a stranger who
  sends wrong passwords for `admin` holds back only their own address. A looser limit per username across all
  addresses (25 failures in 15 minutes) holds back every address that has failed for that name, so spreading
  guesses over many addresses buys one try per address. A right password of a two-factor user clears no
  back-off. Wrong two-factor codes are counted per user in their own table: after 10 in 24 hours, codes from
  the authenticator app are refused for that user until the count drops below 10; recovery codes, the admin
  reset and `hbudget disable-2fa` still work, and the first refusal is audited as `2fa.locked`. A right code
  for a time step that was already used is refused as "just used" without counting as a guess. Migration 0014.
- **Why:** the pre-release security review showed that one wrong password a minute from two addresses kept the
  real user locked out (the back-off was per username only), and that someone who had the password could try
  about 1,100 codes a day (3 valid in a million: about 9% in a month). With 10 a day it is under 0.1% a month.
  The day-long refusal is a denial of service only for someone who already has the password, and recovery
  codes get the real user in. Counting a reused code as a failure punished people for making two changes in
  the same 30 seconds.
- **Alternatives:** a permanent lock after N failures (lets anyone who knows a username shut its owner out);
  CAPTCHA (needs an outside service); per-address limits only (makes distributed guessing free).

## D-098 Only the admin adds an existing user to a household (2026-11-10)
- **Decision:** on a household's Members page an owner can create a new user for the household, but adding
  someone who already has an account on the server is for the admin only (403 `admin_only` otherwise, whatever
  the name, so the answer does not tell whether the account exists).
- **Why:** the pre-release security review found that an owner could add any user of the server to their own
  household without that user agreeing, for example to collect the bank exports the user then uploads there,
  and could probe which usernames exist. On a server shared by several households, joining another household
  should go through the person who runs the server. Decided by the owner of the project on 2026-11-10.
- **Alternatives:** an invitation the user accepts (more pages and state for a rare case; possible later).
