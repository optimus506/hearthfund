# Architecture

Hearthfund is a self-hosted web app for a household checkbook forecast: a starting balance, dated rows of
money in and out, a running balance on every row, bill status, bank reconciliation, debts and reports.
This document is the contract between the builders. Where it says "must", tests should check it.
Decisions and their alternatives are in [../DECISIONS.md](../DECISIONS.md) (D-numbers below).

Contents

1. Principles
2. Repository layout
3. Money, dates and signs
4. Core data model (`hbudget.core.models`)
5. Recurrence and row generation
6. Ledger order
7. Forecast
8. Alerts
9. Reconciliation
10. Debts and the payoff plan
11. Report (Sankey), Check-up and Card
12. Importers and exporters (`hbudget.io`)
13. The bill-system format
14. Database (`hbudget.db`)
15. Web app (`hbudget.web`)
16. Auth, roles and security
17. CLI
18. Configuration
19. Packaging
20. Tests, including the private regression test

---

## 1. Principles

- **Pure core.** `hbudget.core` is standard-library Python only. It must not import `hbudget.db`, `hbudget.web`,
  `hbudget.io`, `fastapi`, `sqlite3`, `jinja2` or any third-party package. Every function takes plain dataclasses
  and returns new values; nothing in the core does I/O or reads the clock (callers pass `today`).
- **Integer cents.** No floats for money anywhere (D-004).
- **Nothing is changed silently.** Automatic bank matches are applied; anything ambiguous is a proposal that the
  user accepts or rejects. Rows are never deleted without either a matching bank transaction or an explicit user
  action, and removed rows are kept (state `removed`) so they can be undone.
- **Order is data.** The ledger is ordered by `(order_date, seq)`, never by due date alone (D-006).
- **No personal data in the repo or the image.** Fixtures and examples are synthetic.
- **No outbound network calls**, no CDNs, no telemetry. The one exception is opt-in: with `HBUDGET_PLAID` on and a
  household owner's consent, `hbudget.io.plaid` talks to Plaid's API host and the Bank connection page loads Plaid
  Link (D-091, docs/PLAID.md). Off, that module is never imported.

## 2. Repository layout

```
hearthfund/
  pyproject.toml  Makefile  Dockerfile  docker-compose.yml  .env.example  .dockerignore
  README.md  CONTRIBUTING.md  LICENSE  DECISIONS.md  SECURITY.md  CHANGELOG.md
  docs/ARCHITECTURE.md  docs/IMPORT.md  docs/DEPLOY.md (install, upgrade, remote access)  docs/API.md
  schema/bill-system.schema.json          canonical schema (see 13.6)
  examples/bill-system.example.yaml       commented fake household
  examples/sample-bank.csv                fake bank export matching the example
  examples/sample-bank.ofx                same, as OFX
  scripts/privacy_scan.py
  src/hbudget/
    __init__.py            __version__
    cli.py
    schema/bill-system.schema.json        packaged copy, byte-identical (test enforces)
    core/
      __init__.py          re-exports the public API listed in this document
      money.py             parse/format cents, percent helpers
      dates.py             month arithmetic, clamping, weekend shifts, week starts
      models.py            all dataclasses and enums (section 4)
      recurrence.py        occurrences of a series (section 5)
      ledger.py            generation, ordering, row edits (sections 5, 6)
      forecast.py          running balance and readout (section 7)
      alerts.py            (section 8)
      rules.py             payee rule matching (section 9.3)
      reconcile.py         (section 9)
      payoff.py            (section 10)
      flow.py              Sankey data (section 11.1)
      checkup.py           budget vs actual (section 11.2)
      cards.py             card categories and weekly summary (section 11.3)
      subscriptions.py     recurring charges found in the history (section 11.3, D-084)
      textmask.py          masking of account-like numbers
    io/
      __init__.py
      limits.py            size / zip-bomb / row-count limits shared by all readers
      tabular.py           Sheet / Cell model shared by the CSV, ODS and XLSX readers
      csvfile.py           CSV reader (dialect sniffing, BOM, encodings)
      ods.py               .ods reader (zipfile + defusedxml), cell values and background colours
      xlsx.py              .xlsx reader/writer (openpyxl, read_only, data_only)
      workbook_import.py   column-mapping step: Sheet + WorkbookMapping -> ImportedBudget
      infer.py             series inference from imported rows
      bank.py              bank/card CSV with mapping profiles -> list[BankTxn]
      ofx.py               OFX 1.x (SGML) and 2.x (XML), QFX
      billsystem.py        bill-system YAML/JSON <-> core objects (+ schema validation)
      export.py            forecast CSV / XLSX, full backup
    db/
      __init__.py
      connection.py        open(), pragmas, transactions
      migrate.py           numbered SQL migration runner
      migrations/0001_init.sql ...
      repo.py              load/save core objects for a household
      users.py             users, memberships, sessions, login throttling
    web/
      __init__.py
      app.py               create_app() factory
      config.py            Settings from HBUDGET_* env vars
      security.py          password hashing, CSRF, headers middleware, role checks
      deps.py              FastAPI dependencies: db connection, current user, household, role guard
      routes/              pages.py auth.py api_ledger.py api_plan.py api_debts.py api_reports.py
                           api_uploads.py api_reconcile.py api_import.py api_settings.py api_admin.py
      templates/           base.html, login.html, setup.html, bills.html, plan.html, ... (Jinja2)
      static/css/app.css
      static/js/           api.js chart.js sankey.js bills.js plan.js debts.js report.js checkup.js
                           card.js uploads.js reconcile.js importer.js settings.js
  tests/
    core/ io/ db/ web/ fixtures/
    private_support.py    locator/skip helper for the private tests (committed, tested; section 20.3)
  private_tests/          git-ignored; private regression tests (section 20.3)
```

## 3. Money, dates and signs

- `Cents = int`. `money.parse(text_or_number) -> int` accepts `1800`, `1800.5`, `"1,800.50"`, `"$1,800.50"`,
  `"(1,800.50)"` (negative), `"-1800.50"`; more than two decimals raises `ValueError`. Floats are converted with
  `Decimal(repr(x))`, never `int(x * 100)`.
- `money.fmt(cents, *, sign="minus"|"parens") -> str` gives `"$1,800.50"` / `"-$1,800.50"` / `"($1,800.50)"`.
- **Ledger sign** (same as the workbook): positive = money out, negative = money in.
  **Bank sign** (as in exports): negative = money out, positive = money in. `BankTxn.amount` keeps the bank sign;
  conversion is `ledger_amount = -bank_amount`. The UI shows money in as `+$x` in green.
- Percentages are stored as basis points (`int`, 1000 = 10%). APR is basis points too (2999 = 29.99%).
  `money.percent_to_bp(x) -> int` converts through `Decimal(repr(x))` and rejects more than two decimals (the
  schema cannot check this reliably for floats, so it is a semantic check, D-031).
- Dates are `datetime.date`, ISO `YYYY-MM-DD` on the wire. The core never calls `date.today()`; callers pass `today`.
- `dates.add_months(d, n)`, `dates.clamp_day(year, month, day)` (day 31 in a 30-day month -> 30; Feb 29/30/31 ->
  28 or 29), `dates.shift_weekend(d, rule)` with rule `none | before | after` (Saturday/Sunday -> previous Friday /
  next Monday), `dates.week_start(d, first_weekday=0)` (0 = Monday).

## 4. Core data model (`hbudget.core.models`)

All dataclasses are `@dataclass(frozen=True, slots=True)` unless noted; collections inside them are tuples.
"Update" means `dataclasses.replace`. IDs are strings: slugs for configuration (`"electric"`) and deterministic
keys for generated rows and transactions. The core never creates random ids: callers that need a new id (a one-off
row, an expected item) generate it outside the core (`uuid4().hex` in the web layer) and pass it in. Enums are
`StrEnum`.

```python
class Kind(StrEnum):            INCOME = "income"; BILL = "bill"; SPENDING = "spending"
class Cadence(StrEnum):         ONCE = "once"; WEEKLY = "weekly"; BIWEEKLY = "biweekly";
                                SEMIMONTHLY = "semimonthly"; MONTHLY = "monthly"; YEARLY = "yearly"
class Weekend(StrEnum):         NONE = "none"; BEFORE = "before"; AFTER = "after"
class Status(StrEnum):          AUTOPAY = "autopay"; MANUAL = "manual"; NONE = "none"
class RowState(StrEnum):        OPEN = "open"; CLEARED = "cleared"; REMOVED = "removed"
class RuleKind(StrEnum):        BILL = "bill"; PAY = "pay"; ASK = "ask"; CARD = "card"; TRACK = "track";
                                IGNORE = "ignore"; SAVINGS_DRAW = "savings_draw"; LOAN_IN = "loan_in"
class Direction(StrEnum):       IN = "in"; OUT = "out"; ANY = "any"
class AccountKind(StrEnum):     CHECKING; SAVINGS; CREDIT_CARD; LOAN; MORTGAGE; RETIREMENT_LOAN; OTHER
class Severity(StrEnum):        INFO = "info"; WARN = "warn"; BAD = "bad"

@dataclass(frozen=True, slots=True)
class Schedule:
    cadence: Cadence
    start: date                      # anchor: the first occurrence (weekly/biweekly/once; any weekday), or the
                                     # first possible date (monthly/semimonthly/yearly: earlier dates are dropped)
    end: date | None = None          # last possible occurrence (inclusive)
    interval: int = 1                # weekly only: every N weeks (biweekly == weekly with interval 2)
    day: int | None = None           # monthly: day of month 1..31 (clamped to month end)
    days: tuple[int, int] | None = None   # semimonthly: two days, e.g. (1, 15) or (15, 31)
    month: int | None = None         # yearly: month 1..12 (day comes from `day`)
    weekend: Weekend = Weekend.NONE

@dataclass(frozen=True, slots=True)
class Series:                        # a recurring (or one-off) income, bill or spending line
    id: str
    name: str                        # shown on rows; payee rules can target names too
    kind: Kind
    amount: Cents                    # magnitude, always >= 0; the row sign comes from kind
    schedule: Schedule
    funded_by: str | None = None     # income series id: rows are ordered after that payday (section 6)
    status: Status = Status.NONE     # default bill status for its rows (spending/income: NONE, not shown)
    card: str | None = None          # credit-card account id that pays it (a "card-funded" line)
    group: str | None = None         # flow group name for the Report tab
    month_amounts: tuple[tuple[int, Cents], ...] = ()   # seasonal: ((5, 12000), (6, 12000), ... ) month -> amount
    cycle_amounts: tuple[Cents, ...] = ()               # occurrence i uses cycle_amounts[i % len]; overrides amount
    active: bool = True
    notes: str = ""
    generated_through: date | None = None   # watermark: rows exist (or were deliberately removed) up to this date

@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: str
    series_id: str | None            # None for one-off rows typed by hand or imported without a series
    name: str
    kind: Kind
    amount: Cents                    # SIGNED, ledger sign (+ out, - in)
    due_date: date                   # the original due date (what the user sees; after any weekend shift)
    nominal_date: date | None = None # the occurrence's date before the weekend shift (series rows only);
                                     # (series_id, nominal_date) identifies an occurrence, D-025
    order_date: date                 # >= due_date for bills funded by a later payday (section 6)
    seq: int                         # tie-break inside an order_date; unique per order_date
    state: RowState = RowState.OPEN
    status_override: Status | None = None   # per-row mark; None = use the series' status
    amount_was: Cents | None = None  # set by Change: the amount before the user changed it (for Undo)
    overridden: bool = False         # amount/date/order edited by hand or imported different from the series
    card: str | None = None          # card-funded by this credit-card account (copied from the series when
                                     # generated; set per row for imported rows without a series)
    paid: Cents = 0                  # partial payments already taken off `amount` by "reduce" (magnitude, >= 0)
    paid_txn_ids: tuple[str, ...] = ()
    paid_at_change: Cents = 0        # `paid` when Change set amount_was (so Undo keeps later partial payments)
    cleared_txn_id: str | None = None
    cleared_on: date | None = None
    note: str = ""

@dataclass(frozen=True, slots=True)
class Horizon:
    mode: Literal["rolling", "fixed"] = "rolling"
    months: int = 15                 # rolling: generate rows until as_of + months
    end: date | None = None          # fixed: last date (imported workbooks keep their own end)

@dataclass(frozen=True, slots=True)
class ReconcileSettings:
    days_before: int = 12            # a row matches postings up to 12 days before its due date
    days_after: int = 21             # ... and up to 21 days after it
    tolerance_bp: int = 1000         # automatic bill match within 10% ...
    tolerance_abs: Cents = 2500      # ... or $25, whichever is larger

@dataclass(frozen=True, slots=True)
class IncomeAlert:
    series_id: str
    min_avg_bp: int = 8500           # rolling average below 85% of budget
    window: int = 4                  # last N deposits
    short_by: Cents | None = None    # a single deposit this far under its row

@dataclass(frozen=True, slots=True)
class CapRule:
    id: str
    name: str                        # e.g. "Online shopping"
    rule_ids: tuple[str, ...]        # payee rules whose postings count (usually track rules)
    period: Literal["week", "month"]
    amount: Cents

@dataclass(frozen=True, slots=True)
class AlertSettings:
    cushion: Cents = 30000
    bill_variance_bp: int = 1000
    unscheduled_days: int = 7        # bills due within N days with status none
    expected_grace_days: int = 3     # expected items this many days past their date
    unmatched_recurring: bool = True # a payee with no rule seen 2+ times in the history
    income: tuple[IncomeAlert, ...] = ()
    caps: tuple[CapRule, ...] = ()

@dataclass(frozen=True, slots=True)
class Settings:
    currency: str = "USD"
    week_starts: int = 0             # 0 = Monday
    reconcile: ReconcileSettings = ReconcileSettings()
    alerts: AlertSettings = AlertSettings()
    status_colours: tuple[tuple[Status, str], ...] = ((Status.AUTOPAY, "#b6d7a8"), (Status.MANUAL, "#ec9ba4"))

@dataclass(frozen=True, slots=True)
class PayeeRule:
    id: str
    name: str
    kind: RuleKind
    contains: tuple[str, ...] = ()   # case-insensitive substrings; any one matches
    regex: str | None = None         # alternative to contains (Python re, IGNORECASE); both = both must match
    direction: Direction = Direction.ANY
    min_amount: Cents | None = None  # on the magnitude of the bank amount
    max_amount: Cents | None = None
    targets: tuple[str, ...] = ()    # series ids or row names the rule clears (bill/pay/ask)
    card: str | None = None          # card kind: the credit-card account it pays; card-funded rows have row.card == this (required for kind card)
    note: str = ""

@dataclass(frozen=True, slots=True)
class Account:                       # debts and other accounts (Debts tab)
    id: str
    name: str
    kind: AccountKind
    balance: Cents = 0               # owed (>= 0) for debts
    apr_bp: int = 0
    payment: Cents = 0               # what is actually paid per month
    minimum: Cents = 0
    limit: Cents | None = None       # credit limit, for utilization
    as_of: date | None = None
    promo_ends: date | None = None
    apr_after_bp: int | None = None
    deferred_interest: bool = False
    is_mortgage: bool = False        # left out of the payoff plan by default
    include_in_payoff: bool = True   # e.g. a retirement-plan loan can be tracked but excluded
    notes: str = ""

@dataclass(frozen=True, slots=True)
class ExpectedItem:
    id: str
    label: str
    amount: Cents                    # magnitude, > 0
    direction: Literal["in", "out"]
    date: date
    status: Literal["open", "done"] = "open"

@dataclass(frozen=True, slots=True)
class WhatIf:                        # never stored; sent with a request
    label: str
    amount: Cents
    direction: Literal["in", "out"]
    date: date

@dataclass(frozen=True, slots=True)
class BankTxn:
    id: str                          # bank's transaction id / OFX FITID, else "h:" + sha256(date|amount|desc|n)[:24]
    posted: date
    amount: Cents                    # BANK sign: - out, + in
    description: str                 # already masked (textmask.mask)
    balance: Cents | None = None     # running balance after this posting, when the export has one
    category: str = ""               # the bank's own category column, if any
    file_index: int = 0              # position in the file after sorting newest-first (0 = newest)
    effective: date | None = None

@dataclass(frozen=True, slots=True)
class CardTxn:
    id: str
    purchased: date                  # purchase date from the text (the profile's regex, else "Date m/d/y"), else posted
    posted: date
    amount: Cents                    # bank sign: purchases negative, payments positive
    description: str                 # masked
    category: str                    # filled by cards.categorise at import; "Payment" for payments

@dataclass(frozen=True, slots=True)
class CardCategory:
    name: str
    keywords: tuple[str, ...]        # case-insensitive substrings; first category that matches wins
    bank_categories: tuple[str, ...] = ()   # fallback: the export's own category column
    weekly_series: tuple[str, ...] = ()     # spending series that budget this category per week
    has_own_rows: bool = False       # a card-paid bill with its own ledger rows: left out of "Other"

@dataclass(frozen=True, slots=True)
class FlowGroup:
    name: str
    colour: str = "--f-other"        # CSS colour token name ("--f-home") or "#rrggbb"
    rule_ids: tuple[str, ...] = ()   # actual view: bank postings matched by these rules
    series_ids: tuple[str, ...] = () # budget view: rows of these series
    row_names: tuple[str, ...] = ()  # budget view: rows by name (imported rows without a series)

@dataclass(frozen=True, slots=True)
class Budget:                        # everything the core needs for one household
    start_balance: Cents
    as_of: date                      # date of the starting balance (last reconciliation)
    horizon: Horizon
    series: tuple[Series, ...]
    rows: tuple[LedgerRow, ...]      # any order; use ledger.ordered()
    rules: tuple[PayeeRule, ...] = ()
    accounts: tuple[Account, ...] = ()
    expected: tuple[ExpectedItem, ...] = ()
    settings: Settings = Settings()
    card_categories: tuple[CardCategory, ...] = ()
    flow_groups: tuple[FlowGroup, ...] = ()
    last_reconciled_through: date | None = None
```

Helpers in `models`: `Budget.series_by_id() -> dict[str, Series]`, `row_status(row, series_by_id) -> Status`
(`status_override` else the series' `status` else `NONE`; spending and income rows always `NONE`),
`row_key(row) -> str` (`slug(name) + "_" + due_date.isoformat()`, used in URLs and the backup), `slug(text)`.

## 5. Recurrence and row generation

`recurrence.occurrences(schedule: Schedule, start: date, end: date) -> list[Occurrence]` returns every occurrence
whose **nominal** date is in `[start, end]` (both inclusive), sorted, where
`Occurrence(nominal: date, actual: date, index: int)` (frozen dataclass): `nominal` is the date before the weekend
shift, `actual` after it, `index` the occurrence number counted from `schedule.start`.
`recurrence.dates(schedule, start, end) -> list[date]` is the list of `actual` dates.

| Cadence | Rule |
|---|---|
| once | `schedule.start` if in range |
| weekly | `schedule.start + 7*interval*k`, k >= 0 |
| biweekly | `schedule.start + 14*k` |
| semimonthly | each month from `schedule.start`'s month: `clamp_day(y, m, days[0])`, `clamp_day(y, m, days[1])` (two distinct days, sorted); nominal dates before `schedule.start` dropped |
| monthly | `clamp_day(y, m, day)` each month from `schedule.start`'s month (`day` defaults to `start.day`); nominal dates before `schedule.start` dropped |
| yearly | `clamp_day(y, month, day)` each year from `schedule.start`'s year (Feb 29 -> Feb 28 in non-leap years); nominal dates before `schedule.start` dropped |

Weekend shifting is applied after clamping. The **nominal** (unshifted) date decides membership in the range and in
the month, so a payday on Saturday the 1st that shifts to Friday the 31st of the previous month still belongs to the
1st's month. If a shift makes two actual dates equal (semimonthly 30th and 31st both moving), keep the first.
`schedule.end` is inclusive and compared with the nominal date. Rows store the **actual** date as `due_date`.

`recurrence.amount_for(series, occ: Occurrence) -> Cents` (magnitude): `cycle_amounts[occ.index % n]` if set,
else the `month_amounts` entry for `occ.nominal.month` if set, else `series.amount`. Using the nominal month means a
weekend shift never flips a seasonal amount; `index` is stable whatever window is generated.

`ledger.generate(budget: Budget, until: date) -> Budget` adds rows for every active series for occurrences with
`nominal > watermark`, `nominal >= schedule.start`, `nominal <= schedule.end` (when set) and `nominal <= until`,
where `watermark = series.generated_through or (budget.as_of - 1 day)` (so the first generation includes a row due
on `as_of` itself), then sets `series.generated_through = max(series.generated_through, until)` (a watermark never
moves back). **Inactive** series get no rows, but their watermark still advances to
`max(generated_through, as_of - 1 day)`, so reactivating a series later does not create past-due rows. The
watermark is what keeps deliberate gaps: an occurrence the user removed, a month skipped in an imported sheet, or
rows of a bill that ended are never re-created by later generations. As a second guard, a row with the same
`(series_id, nominal_date)` in **any** state is never duplicated. New rows get
`id = series.id + ":" + nominal.isoformat()`, `nominal_date = nominal`, `due_date = actual`, sign from the kind,
`card` from the series, `order_date` and `seq` from section 6. Rows are never deleted by generation.

`ledger.regenerate_series(budget, series_id, from_date) -> Budget` is used after a series is edited: open rows of
that series with `due_date >= from_date` and `overridden == False` are recomputed up to the series' current
`generated_through`, matching old and new occurrences by `nominal_date` and never creating an occurrence after the
last existing row of the series when `schedule.end` says it has ended. A row whose occurrence still exists keeps its id, `order_date`, `seq` and status mark (only its
amount and due date are updated); occurrences that no longer exist are removed only if they are open and not
overridden; new occurrences are added as in `generate`. Overridden, cleared and removed rows are never touched.

`ledger.set_month_amounts(budget, series_id, rules: Sequence[tuple[frozenset[int], Cents]], from_date) -> tuple[Budget, list[RowChange]]`
is the seasonal-amount tool: it stores the rules in `series.month_amounts` and changes the amount of every open row
of the series due on or after `from_date` whose nominal month is in a rule. It returns
`RowChange(row_id: str, name: str, due_date: date, was: Cents, now: Cents)` (frozen dataclass) per changed row for
the confirmation screen. Rows are never added, removed or reordered by it.
`ledger.parse_months("10-4") -> frozenset({10, 11, 12, 1, 2, 3, 4})` (ranges may wrap; `ValueError` outside 1-12).

Horizon: `ledger.horizon_end(budget) -> date` = `horizon.end` for fixed, else `add_months(as_of, horizon.months)`.
The web app calls `generate(budget, horizon_end(budget))` after series edits, after each reconciliation and
from the "Extend forecast" button. Fixed horizons are only extended when the user changes the end date.

Row edits (all return a new `Budget`; all keep the row so it can be undone):

| Function | Effect |
|---|---|
| `change_amount(budget, row_id, new_amount)` | `amount_was = old` and `paid_at_change = paid` (only if `amount_was` is not already set), `amount = new`, `overridden = True` |
| `remove_row(budget, row_id)` | `state = REMOVED` (amount kept) |
| `undo_change(budget, row_id)` | removed -> open; changed -> `amount = amount_was - (paid - paid_at_change)`, `amount_was = None`. Partial payments made after the change stay taken off; Undo never gives back money already paid |
| `mark_status(budget, row_id, status \| None)` | `status_override` (None = back to the series default) |
| `add_row(budget, row_id, name, kind, amount, due, *, card=None, order_after: str \| None = None)` | one-off row with a caller-supplied id; `order_after` places it right after that row (same order_date, seq + 1, later rows on the date shift up) |
| `move_row(budget, row_id, after_row_id)` | changes `order_date`/`seq` so the row follows `after_row_id` and sets `overridden = True`; requires the new `order_date >= due_date`, else `ValueError` |
| `clear_row(budget, row_id, txn_id, on)` | `state = CLEARED`, `cleared_txn_id`, `cleared_on` |
| `reduce_row(budget, row_id, by, txn_id)` | `amount -= by` (positive rows; `by < amount`), `paid += by`, `paid_txn_ids += (txn_id,)`; `amount_was` is **not** touched (D-030) |

Every row edit raises `KeyError` for an unknown row id and `ValueError` for an edit that does not apply (e.g.
undo on an untouched row, change on a cleared row).

## 6. Ledger order

The order of rows is part of the budget: a bill is listed after the payday that funds it, even when its due date
is earlier (D-006). Sorting by due date is never correct.

- **Order key:** `ledger.order_key(row) = (row.order_date, row.seq, row.id)`. `ledger.ordered(rows)` sorts by it.
  Removed and cleared rows keep their keys (they are filtered out by the forecast, not reordered).
- **order_date for generated rows:** `due_date`, unless the series has `funded_by`: then find the first occurrence
  of the funding income series whose **nominal** date is on or after the bill's **nominal** due date, looking ahead
  at most 35 days, and use `order_date = max(that occurrence's actual date, due_date)`; if there is none,
  `due_date`. Using nominal dates on both sides means a weekend shift cannot push a bill to the payday after next.
  Example: a bill due on the 10th with `funded_by: pay-a` (paid on the 1st and 15th) gets order_date = the 15th,
  after that pay row; when the 15th is a Saturday and pay moves to Friday the 14th, order_date = the 14th.
- **seq for generated rows:** generated income rows always come before non-income rows of the same order_date:
  income rows get `seq = min(existing seq on that date, 0) - 1, -2, ...` (seq may be negative); bills and spending
  get `seq = max(existing seq on that date, -1) + 1, +2, ...`, sorted by `due_date`, then `name`. Existing rows
  never change seq because of generation.
- **Imported rows (workbook):** the importer walks the sheet top to bottom and sets
  `order_date = max(due_date, previous order_date)` and `seq = sheet row index`. So `(order_date, seq)` reproduces the
  sheet order exactly. When `order_date - due_date > 14 days` the importer adds a warning ("row X is listed long
  after its due date"), because that usually means a typo in the date. Rows dated before the first row keep their own
  date (the running max starts at the first row's due date).
- Running balances, the bills grouping (section 15.3) and the low point all use this order.

## 7. Forecast

`forecast.compute(budget: Budget, *, today: date, include_expected: bool = True, whatifs: Sequence[WhatIf] = (), row_amounts: Mapping[str, Cents] | None = None) -> Forecast`

```python
@dataclass(frozen=True, slots=True)
class BalancePoint:
    row_id: str | None               # None for expected / what-if items
    label: str
    due_date: date
    order_date: date
    amount: Cents                    # ledger sign
    balance: Cents                   # balance after this row

@dataclass(frozen=True, slots=True)
class Forecast:
    start: Cents                     # budget.start_balance
    as_of: date
    points: tuple[BalancePoint, ...] # open rows (and virtual items) in ledger order
    low: Cents                       # minimum balance over points (start excluded); start when no points
    low_date: date                   # due_date of the FIRST point with the minimum (as_of when no points)
    low_row_id: str | None
    high: Cents                      # maximum over start and points
    high_date: date                  # as_of when the start is the maximum, else due_date of the first max point
    end: Cents                       # balance after the last point (start when none)
    end_date: date                   # due_date of the last point (as_of when none)
    year_ends: tuple[tuple[int, Cents], ...]   # (year, balance after the last point with order_date <= Dec 31)
    now: Cents                       # balance after all points with order_date <= today
```

- Only rows with `state == OPEN` count. Zero-amount rows count as points (they can be the first minimum, exactly as
  in a spreadsheet `MIN` + `VLOOKUP`).
- `row_amounts` replaces the amount of listed rows for this computation only (used by the what-if).
- Expected items (`status == "open"`) and what-ifs become virtual points: amount `+a` for out, `-a` for in, placed at
  `order_date = max(item.date, as_of)` after every real row of that date (seq = +inf, then by label). An expected
  item dated in the past therefore hits the balance immediately.
- Year-end entries are produced for each calendar year from `as_of.year` to `end_date.year`; a year with no points
  carries the previous balance.
- `forecast.series_for_chart(f: Forecast) -> list[tuple[date, Cents]]` returns one point per date (the last balance
  of each order_date), starting with `(as_of, start)`, for the balance chart.
- `forecast.compare(base: Forecast, other: Forecast, *, cushion: Cents) -> ForecastDelta` with
  `ForecastDelta(low_change: Cents, end_change: Cents, verdict: Literal["ok", "tight", "below_zero"])` (frozen).
  The verdict judges **`other`** (the what-if) on its own: `other.low < 0` -> below_zero ("No"),
  `other.low < cushion` -> tight ("Tight"), else ok ("Yes"). The changes are `other - base`.

## 8. Alerts

`alerts.evaluate(budget, forecast, *, today, recent: Applied | None = None, history: Sequence[BankTxn] = ()) -> list[Alert]`

`history` is the household's bank history; posted amounts of cleared rows are looked up by `row.cleared_txn_id`
in it (rows whose transaction is not in `history` are skipped for income alerts).

```python
@dataclass(frozen=True, slots=True)
class Alert:
    code: str          # "below_zero" | "cushion" | "bill_variance" | "unmatched" | "unmatched_recurring" |
                       # "income_short" | "income_check_short" | "cap" | "unscheduled" | "expected_late"
    severity: Severity
    message: str       # plain sentence for the UI, amounts formatted, no account numbers
    row_id: str | None = None
    data: tuple[tuple[str, str], ...] = ()
```

| Code | Fires when (defaults) |
|---|---|
| below_zero (bad) | forecast low < 0, with date |
| cushion (warn) | 0 <= low < `alerts.cushion` ($300) |
| bill_variance (warn) | in `recent`: a bill cleared by a single-row match (automatic or an accepted one-row proposal; card bundles excluded) whose posted amount differs from the row by more than `bill_variance_bp` (10%) |
| unmatched (warn) | in `recent`: each unknown transaction, with the reason |
| unmatched_recurring (info) | in `history`: the same masked payee text (digits removed, first 3 words) with no rule appears in 2+ different months |
| income_short (warn) | the **posted** amounts of the last `window` cleared rows of an `IncomeAlert` series (from their `cleared_txn_id`) average below `min_avg_bp` of the budgeted row amounts |
| income_check_short (warn) | in `recent`: an income row cleared by a deposit more than `short_by` under the row |
| cap (warn) | for each `CapRule`: the current period's total of matching postings in `history` exceeds `amount` (also reports the running per-period average) |
| unscheduled (warn) | open bill rows (not spending, not income) with status NONE and `due_date <= today + unscheduled_days` |
| expected_late (info) | open expected items with `date < today - expected_grace_days` |

The summary strip shows balance, low + date, year end and the count of active alerts; the alert list sits under it.

## 9. Reconciliation

A generalised port of the original weekly reconciliation script. Module `hbudget.core.reconcile`.

### 9.1 Inputs

`prepare(txns: Sequence[BankTxn], budget: Budget, *, seen_ids: frozenset[str] = frozenset()) -> Prepared`

`txns` is always the complete content of **one upload**, with `file_index` as assigned when that file was parsed
(the web layer reads it from `upload_txns`, section 14.2, so a transaction that is also in an older upload is still
part of this upload's set and keeps this upload's position). `file_index` is only ever compared within one upload.
`seen_ids` is the set of transaction ids already used by earlier reconciliations (cleared or counted).

```python
@dataclass(frozen=True, slots=True)
class Prepared:
    txns: tuple[BankTxn, ...]        # the upload, newest first
    basis: BankTxn | None            # None in the no-balance fallback
    basis_ambiguous: bool
    new: tuple[BankTxn, ...]         # to reconcile, oldest first
    balance_checked: bool            # False when the export has no balance column
    latest_balance: Cents            # the new start balance
    through: date                    # newest posted date
    ignored: int                     # non-posted rows the importer dropped (for the report)

class ReconcileError(ValueError):
    def __init__(self, code: Literal["chain_break", "no_basis", "nothing_new", "empty", "bad_order"], message: str): ...
```

1. **Order:** sort newest first by `posted`, ties by `file_index` (file order). If the export was oldest first the
   importer reverses it before assigning `file_index` (it detects this from the dates). An empty list raises
   `ReconcileError("empty", ...)`.
2. **Balance chain** (only when every txn has a balance): for each consecutive pair (newer `a`, older `b`):
   `b.balance + a.amount == a.balance`. A break raises `ReconcileError("chain_break", "...between <older> and <newer>:
   transactions are missing from the export")`. A chain that only works in file order is used as file order
   (same-day postings).
3. **Basis:** the newest txn whose `balance == budget.start_balance`. None -> `ReconcileError("no_basis", "The
   starting balance $X does not appear in this export. The export must reach back to the last reconciliation.")`.
   The new transactions are those newer than the basis (oldest first). If several txns match, the newest is used and
   `Prepared.basis_ambiguous = True` (shown as a note).
4. **No balance column:** the basis is `budget.last_reconciled_through`; new = txns with `posted >=` that date and
   `id not in seen_ids` (`>=` so a posting later on the same day is not lost; `seen_ids` stops double counting). The new start balance is `start_balance + sum(bank amounts of new)`.
   `Prepared.balance_checked = False` and the UI says so.
5. Nothing new -> `ReconcileError("nothing_new", ...)` (the UI shows it as information, not failure).
6. `latest_balance` = newest txn's balance (or the computed value), `through` = newest posted date.

Upload status mapping: `chain_break` and `no_basis` mark the upload **rejected** with the message as its note.

### 9.2 Matching

`match(budget: Budget, prepared: Prepared) -> ReconcileResult` — deterministic, oldest transaction first.

```python
@dataclass(frozen=True, slots=True)
class AutoMatch:   txn_id: str; row_id: str; rule_id: str
@dataclass(frozen=True, slots=True)
class Proposal:
    id: str                    # "P1", "P2", ... in creation order
    txn_id: str
    row_ids: tuple[str, ...]   # one row, or a card bundle
    kind: Literal["ask", "tolerance", "card_bundle", "expected"]
    note: str
    total: Cents               # sum of the rows (ledger sign)
    group: str                 # proposals sharing a txn share a group: pick at most one
    can_reduce: bool           # single positive row whose amount > the payment: "reduce instead of clear" allowed
    expected_id: str | None = None
@dataclass(frozen=True, slots=True)
class Unknown:     txn_id: str; reason: str
@dataclass(frozen=True, slots=True)
class Tracked:     txn_id: str; rule_id: str; kind: RuleKind
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    prepared: Prepared
    auto: tuple[AutoMatch, ...]
    proposals: tuple[Proposal, ...]
    tracked: tuple[Tracked, ...]
    unknown: tuple[Unknown, ...]
```

Candidate rows for a transaction `t`: open rows whose `name` or `series_id` is in the rule's `targets`, with
`-days_before <= (t.posted - row.due_date).days <= days_after`, not already claimed in this run, sorted by
`(due_date, order key)` (oldest outstanding first). Tolerance: `| |t.amount| - |row.amount| | <= max(tolerance_bp *
|row.amount| / 10000, tolerance_abs)`.

Pass 1, per new txn (oldest first): classify with `rules.classify(t, budget.rules)` (first matching rule wins).
- no rule -> `Unknown("no rule matches this payee")`.
- `track`, `ignore`, `savings_draw`, `loan_in` -> `Tracked`.
- direction check: `pay` needs money in; `bill`/`ask`/`card` need money out; otherwise
  `Unknown("unexpected direction for <rule> (refund?)")`.
- `allowance` (D-071): collected, then handled oldest first after pass 1. Each payment takes its amount off the
  first open target row due in its calendar month or later (`AutoMatch.amount`, applied with `reduce_row`); a
  payment that uses the row up clears it (`AutoMatch.amount = None`) and the rest goes to the next row. No such row
  -> `Unknown("no open <target> row this month or later")`. After pass 2, every open allowance row whose month ended
  before the export's last date becomes a `leftover` proposal (no bank line; accepting it clears the row).
- `pay`: first candidate is cleared automatically whatever the amount (pay varies; the variance becomes an alert).
  No candidate -> `Unknown("no open <target> row near this date")`.
- `bill`: only the **oldest** candidate is tested: within tolerance -> `AutoMatch`; out of tolerance (or no
  candidate) -> deferred to pass 2. A younger candidate that happens to fit is never matched automatically.
- `ask`, `card`: deferred to pass 2.

Pass 2, deferred txns in order (rows are reserved as proposals are made, so proposals never overlap):
- `card`: `rule.card` is required for this kind (the schema and `billsystem.check` enforce it; a card rule without a
  card matches no rows rather than every row without a card). Candidates are open rows with `row.card == rule.card`
  (and `rule.card is not None`), positive,
  with `due_date <= t.posted`, sorted oldest first. Greedy: keep adding while the total is below the payment, and
  stop before a row that would take the total further from the payment than it already is (always take at least
  one). Proposal kind `card_bundle`, note "card payment covers these card-funded rows ($total of $payment)".
  None -> `Unknown("card payment, but no open card-funded rows dated on or before it")`.
- `bill` out of tolerance / `ask`: one proposal per target name that has a candidate (for `ask` the candidate with
  the closest amount, ties oldest; for `bill` the oldest), each with note "bank amount differs from budget by $d"
  (prefixed by the rule note for `ask`). All proposals of one txn share `group = txn_id`. No candidates: the
  target's open row due in the posting's calendar month (else the next month), nearest by date, becomes a `month`
  proposal (#96, a bill paid in pieces), not claimed, so the month's other pieces are offered the same row; reduce
  is offered when the row is larger than the payment, and on apply a piece that meets or exceeds what is left
  clears the row (a row already cleared by an earlier piece is left alone). No such row ->
  `Unknown("known payee (<rule>) but no open row in the date window")`.

Pass 3, expected items: for each open `ExpectedItem` and each still-unclaimed new txn with the same direction,
`|amount|` within tolerance and `posted` within `[date - days_before, date + days_after]` -> proposal kind
`expected` (accepting marks the item done; the txn is not matched to a row). Unknown txns that get an expected
proposal stay listed as unknown until it is accepted.

### 9.3 Payee rules

`rules.classify(t: BankTxn, rules: Sequence[PayeeRule]) -> PayeeRule | None`: a rule matches when the description
(upper-cased, whitespace collapsed) contains any of `contains` (or `regex` matches), the direction fits, and the
magnitude is within `min_amount..max_amount`. First match wins, so order matters and is editable in the UI
(drag or up/down). `rules.test(rules, description, amount) -> list[tuple[PayeeRule, bool]]` powers the "test a
description" box on the rules page.

User-supplied regexes (payee rules, `skip_names`, `purchase_date_regex`) go through `rules.compile_safe(pattern)`:
at most 200 characters, and a restricted syntax that rejects backreferences, lookarounds, possessive/atomic groups
a quantified group that itself contains a quantifier (e.g. `(a+)+`, `(\w*)*`), a quantified group that contains an
alternation (e.g. `(a|aa)+`, `(x|y)*`), plus `{n,m}` with m > 100. `tests/core/test_rules.py` has a case for each
rejected form, including the alternation one.
Texts matched are capped at 500 characters (descriptions are short). Rejections are validation errors (400), D-032.

### 9.4 Applying

`apply(budget: Budget, result: ReconcileResult, accept: Mapping[str, Literal["clear", "reduce"]], *, today: date) -> Applied`

```python
@dataclass(frozen=True, slots=True)
class ClearedLine:  row_id: str; name: str; due_date: date; amount: Cents; txn_id: str; how: str  # "auto" | "accepted P3" | "reduced P3"
@dataclass(frozen=True, slots=True)
class Applied:
    budget: Budget             # new start_balance = latest balance, as_of = through,
                               # last_reconciled_through = through, rows cleared/reduced, expected items done
    cleared: tuple[ClearedLine, ...]
    still_open: tuple[str, ...]          # open, non-zero rows due on or before `through`
    forecast_before: Forecast
    forecast_after: Forecast
    report_md: str                       # the reconciliation report (section 9.5)
```

- Unknown proposal ids, or two accepted proposals in one group -> `ValueError` (the API turns it into 400).
- `clear`: every row of the proposal -> `state = CLEARED`, `cleared_txn_id`, `cleared_on = t.posted`.
- `reduce` (only when `can_reduce`): `ledger.reduce_row` takes `|t.amount|` off the row, which stays open with the
  remainder; `paid` and `paid_txn_ids` record it (`amount_was` is left for user changes, D-030); listed as
  "reduced P3". This is the partial-payment case (e.g. part of a bill paid separately).
- Automatic matches are always applied. Nothing else changes. Then `generate()` runs for rolling horizons.

### 9.5 Report

`reconcile.report(applied, *, dry_run: bool, today: date) -> str` Markdown with the same sections as the original
weekly report:
summary (export range, starting point, new balance, pending items note), forecast after the update (low, high, end,
change in low), alerts, cleared rows, needs your decision (proposal table), bank items with no budget row
(unknown + tracked), open rows due on or before the export end (with status), cap rule totals. Descriptions are
masked; no full account numbers ever appear (test: no run of 5+ digits in the report).

The web flow is: upload -> **preview** (`prepare` + `match`, nothing saved) -> user ticks proposals, chooses
clear/reduce -> **apply** (saves the new budget, a `reconciliations` record with the report, marks the upload
imported).

## 10. Debts and the payoff plan

Debts are `Account`s with a balance. Display helpers in `hbudget.core.payoff`:

- `utilization(acct) -> int | None` (basis points of `balance / limit`), `monthly_interest(acct) -> Cents`
  (`round(balance * apr / 12)`, using the promo APR until `promo_ends`), totals for the table footer.

`payoff.simulate(debts: Sequence[Account], *, method: Literal["avalanche", "snowball"], extra: Cents = 0, once: Cents = 0, once_month: date | None = None, start: date, max_months: int = 600) -> PayoffResult`

Monthly loop starting at `start` (the first day of next month). Each month `m` (first-of-month date `md`):
1. For each debt whose `promo_ends` is set and `md >= promo_ends` for the first time: record `balance_at_promo`,
   switch to `apr_after_bp` if given.
2. Add interest to every debt with balance > 0: `round_half_up(balance * apr_bp / 120000)`.
3. Money available = sum of every debt's `payment` + `extra` (+ `once` when `md == once_month`). This is a fixed
   budget: payments of paid-off debts roll over to the others (the snowball/avalanche effect).
4. Pay each debt `min(payment, balance, available)`.
5. Spend the rest on open debts in priority order: **deferred-interest promos still running come first**, then
   avalanche (highest APR first, then smallest balance) or snowball (smallest balance first, then highest APR),
   then name. (This is the intended behaviour; the earlier implementation's sort had an operator-precedence bug,
   D-013.)
6. A debt reaching 0 records `paid_month = m + 1`.

```python
@dataclass(frozen=True, slots=True)
class DebtOutcome:  id: str; name: str; paid_month: int | None; paid_on: date | None; interest: Cents;
                    balance_at_promo: Cents | None; promo_ends: date | None; deferred_interest: bool
@dataclass(frozen=True, slots=True)
class PayoffResult: months: int; done: bool; total_interest: Cents; debts: tuple[DebtOutcome, ...]
```

```python
@dataclass(frozen=True, slots=True)
class PromoVerdict:  debt_id: str; name: str; promo_ends: date; paid_before: bool; left_at_promo: Cents;
                     monthly_needed: Cents; payments_left: int; deferred_interest: bool; apr_after_missing: bool
@dataclass(frozen=True, slots=True)
class PayoffPlan:
    method: Literal["avalanche", "snowball"]
    result: PayoffResult             # the chosen plan
    other: PayoffResult              # the other method, same extra money
    baseline: PayoffResult           # chosen method, no extra and no one-time payment
    start: date                      # first month simulated (first day of the month after `today`)
    debt_free_on: date | None
    interest_saved: Cents            # baseline.total_interest - result.total_interest (>= 0)
    months_sooner: int | None        # None when either never finishes
    other_difference: Cents          # other.total_interest - result.total_interest
    promos: tuple[PromoVerdict, ...]
    not_covering: tuple[str, ...]    # names whose payment <= monthly interest
    excluded: tuple[str, ...]        # names left out (mortgage switch, include_in_payoff False)
```

`payoff.plan(accounts, *, method, extra, once, once_month, include_mortgage=False, today) -> PayoffPlan` runs the
chosen plan, the other method and the payments-only baseline, and returns the KPIs the Debts tab shows: debt-free
month, interest paid, interest saved by the extra money (and months sooner), the other method's difference, the
order table, promo verdicts (paid before the promo ends, or the balance left and the monthly amount needed to clear
it in time; for deferred interest the warning that the whole promo period's interest is added if any balance is
left), and "payment does not cover the interest" warnings. Accounts with `is_mortgage` are excluded unless
`include_mortgage`; accounts with `include_in_payoff == False` are always excluded but still listed.

## 11. Report (Sankey), Check-up and Card

### 11.1 Flow (`hbudget.core.flow`)

`flow.actual(history: Sequence[BankTxn], card_txns: Sequence[CardTxn], budget: Budget, *, today: date) -> Flow`
and `flow.planned(budget: Budget, *, today: date) -> Flow`.

```python
@dataclass(frozen=True, slots=True)
class FlowItem:  name: str; amount: Cents
@dataclass(frozen=True, slots=True)
class FlowGroupTotal: name: str; colour: str; items: tuple[FlowItem, ...]; total: Cents
@dataclass(frozen=True, slots=True)
class Flow:
    start: date; end: date; months: int        # end exclusive
    sources: tuple[FlowItem, ...]              # sorted by amount, largest first
    groups: tuple[FlowGroupTotal, ...]         # sorted by total; plus "Left over" when sources > groups
    note: str
    complete: bool                             # False when history does not cover 12 full months
```

- `flow.months_back(last: date, n: int = 12) -> tuple[date, date]` (start inclusive, end exclusive): the n full
  calendar months ending with the month **before** `last + 1 day`'s month. So when the newest posting is the last
  day of its month, that month is included; otherwise the window ends before the newest posting's month. Works
  across January (year rolls back).
- Actual: the window is `months_back(newest posting)`. `ignore` postings are left out entirely. Money in by rule:
  `pay` -> source named after the rule's first target's series name; `savings_draw` -> "From savings"; `loan_in` ->
  "Loans"; anything else -> "Other money in". Money out by rule -> the flow group listing that rule id (else
  "Other"); `card` rule payments are split across the card's categories in proportion to card spending in the same
  months; the split items go to the flow group that lists the card rule's id, or to a group named "Card" when no
  group lists it (if there is no card data, one item "Card payments"). Postings with no rule go to the group named
  "Food & everyday" if present, by card category (`cards.categorise`), else "Other". A rule listed in two groups
  counts in the first one.
- Planned: open rows with `due_date` in `[today + 1, same day next year)`; when `today + 1` is Feb 29 the end is
  Mar 1 of the next year. Income rows are sources by name, other rows go to the first group listing their series id
  or name, else "Other".
- Default groups and colours (configurable): Money in green `--f-in`, Debt payments red `--f-debt`, Home blue
  `--f-home`, Food & everyday yellow `--f-food`, Card orange `--f-card`, Savings drawn brown `--f-savings`,
  Utilities `--f-util`, Insurance `--f-ins`, Family `--f-family`, Other grey `--f-other`.
- The page shows totals per month (divide by `months`).

### 11.2 Check-up (`hbudget.core.checkup`)

`checkup.compute(history: Sequence[BankTxn], budget: Budget) -> CheckUp`: for each payee rule of kind bill, ask or
pay that matched at least one posting: the budgeted amount (for each target, the most common magnitude among its
remaining open rows; the budget is that amount when **exactly one** target has remaining rows, else None, e.g. one
payee for two different bills), actuals per calendar month (bills; full months only when any exist) or per deposit
(pay), the average, the difference in % (tenths of a percent as int), and a verdict `ok | over | under` against
`bill_variance_bp` (for pay, under is the problem). Also weekly totals for each cap rule (last 12 weeks).

```python
@dataclass(frozen=True, slots=True)
class BillCheck:  rule_id: str; name: str; targets: tuple[str, ...]; kind: RuleKind; budget: Cents | None;
                  per: Literal["month", "deposit"]; actuals: tuple[tuple[date, Cents], ...]  # last 6
                  count: int; average: Cents; diff_tenths_pct: int | None; verdict: Literal["ok", "over", "under"]
@dataclass(frozen=True, slots=True)
class CheckUp:    since: date | None; through: date | None; transactions: int; bills: tuple[BillCheck, ...];
                  cap_weeks: tuple[tuple[str, tuple[tuple[date, Cents], ...]], ...]   # cap id -> (week, total)
```

### 11.3 Card (`hbudget.core.cards`)

Recurring charges (`hbudget.core.subscriptions.detect`, D-084): monthly and yearly same-amount charges per payee in
the bank and card history, with price changes and new / stopped status; computed on demand.

- `cards.store(description) -> str`: the store name (D-083): upper case, cut at the first `*` or `#`, words with a
  digit dropped.
- `cards.categorise(description, bank_category, categories) -> str`: the category whose `merchants` holds the
  store (moved by hand, D-083); else the first category with a keyword in the description; else the first whose
  `bank_categories` contains the export's category; else "Other". Payments (positive amounts) are "Payment".
- `cards.recategorise(txns, categories)`: the (id, category) changes after the categories changed; purchases with
  `by_hand` keep theirs while that category exists.
- `cards.stores(...)` groups purchases by store; `cards.spending(..., by='week'|'month')` totals them per period
  with the plan of the budgeted categories.
- `cards.weekly(card_txns, budget, categories, *, today, weeks=12) -> CardSummary`: per week (week start from
  settings) actual by category vs the weekly budget (sum of rows of the category's `weekly_series` that week),
  averages over finished weeks, the number of weeks a category ran more than 10% over, monthly spend and spending by
  category for the period. Categories with `has_own_rows` are left out of "Other".

```python
@dataclass(frozen=True, slots=True)
class CardWeek:    week: date; actual: tuple[tuple[str, Cents], ...]; budget: tuple[tuple[str, Cents], ...]
@dataclass(frozen=True, slots=True)
class CardSummary: categories: tuple[str, ...]; weeks: tuple[CardWeek, ...]; since: date | None;
                   through: date | None; full_weeks: int; avg_per_week: tuple[tuple[str, Cents], ...];
                   budget_per_week: tuple[tuple[str, Cents], ...]; weeks_over: tuple[tuple[str, int], ...];
                   monthly_spend: Cents; by_category: tuple[tuple[str, Cents], ...]
```
- Default categories (generic keywords only, D-016): Grocery (GROCERY, SUPERMARKET, MARKET, FOODS), Fuel (FUEL,
  GAS STATION, PETROL), Eating out (RESTAURANT, CAFE, COFFEE, PIZZA, GRILL, DINER, TAKEOUT; bank category
  "Food & Dining"), Pharmacy (PHARMACY, DRUG), Other.

### 11.4 Masking (`hbudget.core.textmask`)

`mask(text) -> str`: collapse whitespace; then
1. **grouped numbers**: digit groups joined by single spaces, dashes or dots that hold 8 or more digits in total
   (`"4000 1234 5678 9010"`, `"4000-1234-5678-9010"`) are treated as one run;
2. every run (plain or grouped) of **5 or more** digits has all digits but its last 4 replaced by `#`, separators
   kept (`"CARD 123456789012"` -> `"CARD ########9012"`, `"4000-1234-5678-9010"` -> `"####-####-####-9010"`).
Runs of 4 or fewer digits that are not part of a group stay, so payee rules can still match a "last 4" suffix
(D-015). Dates such as `10/22/29` are not grouped (slash is not a separator here). Applied by every importer before a
description is stored or shown, to OFX `NAME`/`MEMO`, and to upload filenames shown in the UI.
`mask_amount_safe` is not needed: amounts are numbers, never text.

## 12. Importers and exporters (`hbudget.io`)

All readers take `bytes` plus a filename and raise `ImportProblem(message, *, line: int | None = None)` (defined in
`io/__init__.py`, a subclass of `ValueError`) with a user-readable message; messages never include account numbers.

### 12.1 Limits (`io.limits`)

Max upload 5 MB (configurable); ODS/XLSX: max 50 MB uncompressed total, max 2,000 zip members, compression ratio
limit 100; max 20,000 rows per sheet / file; CSV fields max 10,000 chars; ODS `number-columns-repeated` capped at
1,024 and `number-rows-repeated` at 20,000 per element, and 1,000,000 cells per sheet. Zip limits are enforced on the
**actual** decompressed bytes (each member is read through a counting stream that stops at the limit), not on the
sizes declared in the zip header, and the same pre-check runs on an XLSX before it is handed to openpyxl. XML is
parsed only through `defusedxml` (no entities, no DTDs, no network): ODS `content.xml` and `styles.xml`, and OFX 2.x.
`io.xlsx` asserts at import time that `openpyxl.DEFUSEDXML` is true. XLSX opened with
`openpyxl.load_workbook(read_only=True, data_only=True)`;
formulas are never evaluated by us (cached values are used; a formula cell with no cached value is an error for
that row).

### 12.2 Tabular model (`io.tabular`)

```python
@dataclass(frozen=True, slots=True)
class Cell:  value: str | int | float | date | None; text: str; background: str | None  # "#rrggbb" lower-case
@dataclass(frozen=True, slots=True)
class Sheet: name: str; rows: tuple[tuple[Cell, ...], ...]
def read_table(data: bytes, filename: str) -> list[Sheet]   # dispatches on extension + magic bytes
```

ODS: `content.xml` via defusedxml; expands `number-columns-repeated` / `number-rows-repeated` (capped); reads
`office:value`, `office:date-value`, `office:string-value`, text; resolves the cell style's
`fo:background-color` from `office:automatic-styles` (and `styles.xml` named styles). Trailing empty rows/columns
are trimmed. XLSX: fill colour from the cell style (`fgColor.rgb`, theme colours ignored). CSV: one sheet, all text.

### 12.3 Workbook import with column mapping (`io.workbook_import`)

```python
@dataclass(frozen=True, slots=True)
class WorkbookMapping:
    sheet: str
    header_row: int | None           # 1-based; None = no header
    first_row: int                   # 1-based first data row
    name_col: str                    # column letter, e.g. "A"
    amount_col: str
    date_col: str
    sign: Literal["positive_out", "positive_in"] = "positive_out"
    income_col: str | None = None    # optional column that flags income (non-empty = income)
    start_balance_cell: str | None = None    # e.g. "B1"; or start_balance_value
    start_balance_value: Cents | None = None
    stop_at_blank_name: bool = True
    skip_names: tuple[str, ...] = () # regexes; matching rows are skipped (e.g. a carry-over line)
    read_status_colours: bool = True
    status_colours: tuple[tuple[str, Status], ...] = (("#b6d7a8", AUTOPAY), ("#ec9ba4", MANUAL))
    date_format: str | None = None   # for text dates, e.g. "%m/%d/%Y"; None = auto
    expected_low: tuple[str, str] | None = None   # optional cells (value, date) to cross-check, e.g. ("C2", "C4")
    expected_end_cell: str | None = None
    spending_names: tuple[str, ...] = ()   # names the user ticked as spending lines (D-008)
    card_names: tuple[tuple[str, str], ...] = ()   # (row name, card account id): rows paid with that card

@dataclass(frozen=True, slots=True)
class Preview:
    sheets: tuple[str, ...]
    sheet: str                       # the sheet shown
    columns: tuple[str, ...]         # letters A, B, ...
    header: tuple[str, ...] | None
    rows: tuple[tuple[str, ...], ...]   # first 20 data rows as display text
    guess: WorkbookMapping
    names: tuple[str, ...]           # distinct names in the name column (for the spending/card tick lists)

@dataclass(frozen=True, slots=True)
class StoredCheck:                   # what the workbook itself says, read from the mapped cells
    start: Cents | None; low: Cents | None; low_date: date | None; end: Cents | None
    def matches(self, f: Forecast) -> dict[str, bool]: ...   # {"start": True, "low": ..., "low_date": ..., "end": ...}

def preview(sheets: list[Sheet], mapping: WorkbookMapping | None) -> Preview
def guess_mapping(sheets: list[Sheet]) -> WorkbookMapping                       # header-name heuristics
def import_rows(sheets: list[Sheet], mapping: WorkbookMapping, *, today: date) -> ImportedBudget

@dataclass(frozen=True, slots=True)
class ImportedBudget:
    budget: Budget                   # fixed horizon end = the latest due date of any row; as_of = today unless given
    warnings: tuple[str, ...]
    stored_check: StoredCheck | None # None when no check cells were mapped
```

The end balance cell is the balance after the last sheet row; the private test compares both `Forecast.end` and the
last entry of `Forecast.year_ends` with it (they are the same cell when the sheet ends on Dec 31).

- Rows: walk from `first_row` until the first blank name (if `stop_at_blank_name`). Amount parsed with
  `money.parse`; empty = 0. Dates: real date cells, else `date_format`, else ISO / `m/d/Y` / `d-Mon-Y`.
  A row with a bad date is reported as a warning and skipped (never silently).
- Kind: amount sign (after `sign`) < 0 -> income; names matching a spending heuristic are **not** guessed — every
  positive row is a bill unless the user flags spending lines in the mapping step (`spending_names`), D-008.
- Order: section 6 (running max of due dates, seq = sheet order).
- Status: the Name cell background mapped through `status_colours` -> `status_override` per row. After series
  inference, a series whose rows are all autopay gets `status=AUTOPAY` and the per-row overrides are dropped (so the
  data stays compact); mixed series keep overrides.
- Ids: imported rows get `id = "r" + str(sheet_row_number)` unless a series is inferred, in which case they get the
  same scheme as generated rows: `nominal_date` = the occurrence's date before the detected weekend shift, and
  `id = series.id + ":" + nominal.isoformat()` (sheet row number appended on a collision, e.g. two rows of one name
  on one date). Inferred series ids are `slug(name)`, with `-2`, `-3` ... on collisions.
- Watermark and end: every inferred series gets `generated_through` = the import's horizon end (the latest due date
  in the **whole sheet**), not its own last row, so a bill that stopped part-way through the sheet is never refilled
  by a series edit, an Extend or a switch to a rolling horizon. When a series' last row is more than one period (its
  cadence's interval; for once: not applicable) before the sheet's latest due date, inference also sets
  `schedule.end` = that last row's nominal date, so the series stays ended after the horizon is extended.
- `io.infer.infer_series(rows) -> tuple[list[Series], list[LedgerRow]]` groups rows by exact name and detects the
  cadence from the due dates: all gaps 7 -> weekly; 14 -> biweekly; same day of month (allowing clamping) ->
  monthly; two stable days a month -> semimonthly; one per year -> yearly; one row -> once; anything else ->
  rows stay standalone (series_id None) with a warning-free note. Weekend shifts are detected for income (dates that
  only miss the pattern on Fridays before a weekend). The series amount is the most common amount; rows with another
  amount get `overridden=True` and keep their exact amount. Inference never changes a row's amount, date or order.

### 12.4 Bank and card files (`io.bank`, `io.ofx`)

```python
@dataclass(frozen=True, slots=True)
class BankMapping:                   # saved as a mapping profile (kind bank or card)
    date_col: str                    # header names, not letters
    amount_col: str | None = None    # signed amount, or:
    debit_col: str | None = None
    credit_col: str | None = None
    description_cols: tuple[str, ...] = ("Description",)
    balance_col: str | None = None
    id_col: str | None = None
    status_col: str | None = None
    posted_values: tuple[str, ...] = ("Posted",)    # rows with another status are ignored and counted
    category_col: str | None = None
    date_format: str | None = None
    negate: bool = False             # for exports where purchases are positive
    ignore_cols: tuple[str, ...] = ()   # columns never read (account / card numbers)
    purchase_date_regex: str | None = None   # card exports: purchase date inside the text, groups m, d, y

def detect_profile(header: Sequence[str], profiles: Sequence[MappingProfile]) -> MappingProfile | None
def read_bank_csv(data: bytes, mapping: BankMapping) -> BankFile
def read_ofx(data: bytes) -> BankFile          # OFX 1.x SGML (unclosed tags), OFX 2.x XML, QFX (same + INTU tags)
def read_card(data: bytes, filename: str, mapping: BankMapping | None, categories) -> list[CardTxn]

@dataclass(frozen=True, slots=True)
class BankFile:
    txns: tuple[BankTxn, ...]        # newest first, file_index assigned, descriptions masked
    ignored: int                     # non-posted rows
    duplicates: int                  # dropped duplicates inside the file
    first: date; last: date
    latest_balance: Cents | None
    has_balance: bool
```

- Built-in profiles (generic, shipped in `io.bank.BUILTIN_PROFILES`, read-only, not stored in the DB):
  "Date / Description / Amount", "Date / Description / Amount / Balance" (+ optional Id and Status columns) and
  "Date / Description / Debit / Credit / Balance". `detect_profile` matches a header case-insensitively against the
  household's saved profiles first, then the built-ins. Any other layout goes through the mapping step and can be
  saved as a household profile.

```python
@dataclass(frozen=True, slots=True)
class MappingProfile:
    id: str
    name: str
    kind: Literal["bank", "card", "workbook"]
    mapping: BankMapping | WorkbookMapping
    header: tuple[str, ...] = ()     # the header this profile was saved from (bank/card), used by detect_profile
    builtin: bool = False
```
- IDs: the id column, else OFX `FITID`, else `"h:" + sha256(posted|amount|description|n)` where `n` counts identical
  rows in the file (so two genuine identical purchases stay two transactions).
- OFX: `STMTTRN` (`TRNTYPE`, `DTPOSTED`, `TRNAMT`, `FITID`, `NAME`, `MEMO`), `LEDGERBAL` (`BALAMT`, `DTASOF`).
  When `DTASOF` is on or after the newest posting, running balances are computed backwards from `BALAMT` so the
  chain/basis logic works; otherwise `has_balance = False`. Account ids (`ACCTID`) are never stored.
- Dedup across uploads happens in the DB: `bank_txns` has one row per (household, id); `upload_txns` links every
  upload to all of its transactions with that upload's own `file_index` (section 14.2).
- Empty input: every reader raises `ImportProblem("The file has no transactions.")` (or "no rows" for workbooks)
  rather than returning an empty result; a file with only non-posted rows raises "no posted transactions".

### 12.5 Bill system and backup (`io.billsystem`)

```python
@dataclass(frozen=True, slots=True)
class BackupExtras:
    bank_txns: tuple[tuple[BankTxn, str | None], ...] = ()   # (txn, first upload id)
    upload_links: tuple[tuple[str, str, int], ...] = ()      # (upload id, txn id, file_index)
    card_txns: tuple[tuple[CardTxn, str | None], ...] = ()
    uploads: tuple[dict[str, object], ...] = ()              # schema "upload" objects
    reconciliations: tuple[dict[str, object], ...] = ()
    mapping_profiles: tuple[MappingProfile, ...] = ()
    notes: str = ""
    audit: tuple[dict[str, object], ...] = ()
    members: tuple[tuple[str, str], ...] = ()                # (username, role)

@dataclass(frozen=True, slots=True)
class BillSystem:
    doc: dict[str, object]           # the validated document (dates as strings)
    budget: Budget                   # resolved with to_budget
    extras: BackupExtras | None      # present when the document has a `backup` section
    warnings: tuple[str, ...]

def load(data: bytes, filename: str, *, today: date) -> BillSystem   # YAML or JSON, validated, resolved
def to_budget(doc: dict[str, object], *, today: date) -> Budget
def from_budget(budget: Budget, extras: BackupExtras | None, *, exported_at: datetime, app_version: str) -> dict[str, object]
def dump_yaml(doc: dict[str, object]) -> str; def dump_json(doc: dict[str, object]) -> str
def validate(doc: dict[str, object]) -> list[str]          # schema errors with JSON paths
def check(doc: dict[str, object]) -> list[str]             # semantic checks (13.5)
def bills_csv(data: bytes) -> list[Series]                 # simple CSV: name,kind,amount,cadence,start,day,...
```

Import modes (web and CLI):
- **replace**: without a `backup` section, only the budget part (series, rows, rules, accounts, categories,
  groups, settings) is replaced and the bank/card history, uploads, reconciliations and profiles are kept (D-050).
  With a `backup` section everything is restored as-is, after the household-name confirmation. The web form and
  the CLI default to merge (the CLI needs `--replace` for a backup).
- **merge** (setup only, no `backup` section allowed): series, rules, accounts, categories and groups from the
  document are added; an entry whose id already exists is **updated** (series are then regenerated from today with
  `regenerate_series`); nothing is deleted; the household's start balance and ledger rows outside the merged series
  are kept. The dry run lists added / updated ids.

YAML is read with a `yaml.SafeLoader` subclass whose implicit timestamp resolver is removed, so dates stay
strings (the schema checks them as ISO dates) and the same validation applies to YAML and JSON. The loader's
`compose_node` raises on any alias event (anchors/aliases are rejected before anything is built, so alias bombs
cannot expand), and both YAML and JSON documents are rejected above 5 MB or a nesting depth of 32; all of these are
validation errors (HTTP 400, never 500).

`BackupExtras` carries what is outside the core `Budget`: bank and card transactions, uploads metadata,
reconciliation history (with reports), notes, mapping profiles, audit log and member list (usernames + roles, never
password hashes). Import of a backup into an empty household restores all of it; `test_roundtrip` checks
`export -> wipe -> import -> export` gives an identical document (except `exported_at`) and an identical forecast.

### 12.6 Exports (`io.export`)

`forecast_csv(budget, forecast) -> bytes` (columns: Order, Name, Kind, Due date, Weekday, Amount, Balance, Status,
State), `forecast_xlsx(budget, forecast) -> bytes` (sheet "Forecast" with a summary block on top: balance, low +
date, high, end, year ends; then the rows, amounts as numbers with currency format, status colours as fills; sheet
"Series"), `backup(...) -> str` (YAML or JSON of the full bill system + backup extension).

**Formula injection:** every cell of a **text column** written to CSV or XLSX (names, labels, notes, descriptions,
status) that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is prefixed with `'`; in XLSX text cells are
written with the string data type (`cell.data_type = "s"`), never as formulas. **Numeric and date columns** (Order,
Amount, Balance, Due date) are never prefixed: in CSV they are written as plain numbers (`-150.00` stays `-150.00`),
in XLSX as typed numbers/dates. Tests cover both formats, including a negative amount that must stay numeric and a
name starting with `=` and `-` that must be prefixed (D-033).

## 13. The bill-system format

A YAML or JSON document validated by `schema/bill-system.schema.json` (JSON Schema 2020-12). The commented example
is `examples/bill-system.example.yaml`. Money is written in dollars (a number or a string with at most two decimals);
dates are ISO strings; amounts in the file are always magnitudes except in `backup.ledger` and transactions, which
are signed (ledger sign and bank sign respectively, as in section 3).

### 13.1 Top level

| Key | Contents |
|---|---|
| `format` | `"household-budget/bill-system"` (required) |
| `version` | `1` (required). Future versions are migrated by `billsystem.upgrade(doc)` |
| `household` | `name`, `currency`, `start_balance` (default 0), `as_of` (default: import day), `last_reconciled_through`, `horizon` (`mode`, `months`, `end`) |
| `settings` | `week_starts`, `reconcile` (`days_before`, `days_after`, `tolerance_pct`, `tolerance_abs`), `status_colours` |
| `accounts` | checking/savings/cards/loans/mortgage (section 4 `Account`) |
| `income`, `bills`, `spending` | series (section 4 `Series`), with `schedule` |
| `payee_rules` | ordered list (first match wins) |
| `alerts` | `cushion`, `bill_variance_pct`, `unscheduled_days`, `expected_grace_days`, `unmatched_recurring`, `income[]`, `caps[]` |
| `card_categories` | ordered list |
| `flow_groups` | ordered list |
| `backup` | optional: the full-backup extension (13.3) |

### 13.2 Series in the file

```yaml
- id: water
  name: Water
  amount: 60
  schedule: {cadence: monthly, start: 2029-01-08, day: 8}
  status: autopay            # autopay | manual | none
  funded_by: pay-a           # optional income id: listed after that payday
  card: rewards-card         # optional: paid with this credit card (card-funded)
  group: Utilities           # optional flow group
  seasonal: {"5-9": 80, "10-4": 60}     # optional month ranges -> amount (month_amounts in the core)
  cycle: [60, 70]            # optional repeating amounts by occurrence
  notes: ""
```

In the file `schedule.start` is optional when the pattern is fully given by `day` (monthly), `days` (semimonthly),
`month` + `day` (yearly) or `weekday` (weekly/biweekly: `monday` ... `sunday`). `billsystem.to_budget` then resolves
it to the first matching date on or after `household.as_of` (itself defaulting to the import day), so a shared file
needs no real dates at all. The core `Schedule.start` is always a concrete date. `settings.week_starts` is
`monday`/`sunday` in the file and 0/6 in the core. A `biweekly` schedule given only by `weekday` has an arbitrary
phase (which of the two weeks); `to_budget` resolves it to the first such weekday on or after `as_of` and adds the
warning "set a start date for <name> so the paydays fall in the right weeks" (D-034). If `start` and `weekday` are
both given, the first occurrence is the first such weekday on or after `start`.

### 13.3 Backup extension (`backup`)

`exported_at`, `app_version`, `ledger[]` (every row in every state: `id`, `series`, `name`, `kind`, `amount`
(signed), `due`, `nominal`, `order_date`, `seq`, `state`, `status` (override or null), `amount_was`, `overridden`, `card`,
`paid`, `paid_txns`, `paid_at_change`, `cleared_txn`, `cleared_on`, `note`), `series_state[]` (`id`,
`generated_through` for each series), `expected[]`, `notes` (text), `bank_transactions[]`, `upload_links[]`
(`upload`, `txn`, `file_index`), `card_transactions[]`,
`uploads[]` (`id`, `kind`, `filename`, `uploaded_at`, `uploaded_by`, `rows`, `from`, `to`, `latest_balance`,
`status` waiting/imported/rejected, `note`), `mapping_profiles[]` (`id`, `name`, `kind` bank/card/workbook,
`mapping` object), `reconciliations[]` (`id`, `at`, `by`, `upload`, `through`, `new_balance`, `cleared[]`,
`accepted[]`, `report`), `audit[]` (`at`, `by`, `action`, `row`, `detail`, `imported`), `members[]`
(`username`, `role`). With `backup` present, `household.start_balance` / `as_of` are the live values and the ledger
is restored as-is (no regeneration on import). Restoring never creates or changes logins or memberships: the
`members` list is shown after the import so the owner can re-add people by hand (D-026), and imported audit entries
are stored with `imported: true`.

### 13.4 Simple CSV for bills

For people without a spreadsheet: `name,kind,amount,cadence,start,day,days,month,weekend,status,funded_by,card,group`
with one series per line (`docs/IMPORT.md` documents it; `examples/bills.example.csv`).

### 13.5 Validation

Schema validation first (errors with paths, e.g. `bills[3].schedule.day: 32 is greater than the maximum of 31`), then
semantic checks in `billsystem.check(doc)`: unique ids, `funded_by` points at an income id, `card` at a credit-card
account, rule targets exist, cap rule ids exist, money has at most two decimals, percentages have at most two
decimals (JSON Schema `multipleOf` is unreliable for floats, so this is checked here, D-031), regexes pass
`rules.compile_safe`, the two semimonthly days differ, and every `card` rule names a card.

### 13.6 Where the schema lives

`schema/bill-system.schema.json` at the repo root is canonical (people read it there). The package ships a
byte-identical copy at `src/hbudget/schema/bill-system.schema.json` (loaded with `importlib.resources`);
Edit `schema/` and copy it to the package by hand; `tests/io/test_schema_copy.py` fails if they differ (D-017).

## 14. Database (`hbudget.db`)

SQLite file `$HBUDGET_DATA_DIR/hbudget.db`, WAL mode, `foreign_keys=ON`, `busy_timeout=5000`. Stdlib `sqlite3`,
parameters only (no string-built SQL with user values). One connection per request (FastAPI dependency), with
`with conn:` transactions around every write.

### 14.1 Migrations (`db.migrate`)

`migrations/NNNN_name.sql`, applied in order inside a transaction each; table `schema_migrations(version INTEGER
PRIMARY KEY, name TEXT, applied_at TEXT)`. `migrate(conn, db_path) -> list[int]`. Before applying any pending
migration to a database that already has data, the runner copies the file to
`$HBUDGET_DATA_DIR/backups/hbudget-before-<version>-<timestamp>.db` (SQLite backup API). Migrations are
forward-only and never drop a column holding user data; a test applies all migrations to a database made by the
first one with sample data and checks nothing is lost. The app runs `migrate` at start-up.

### 14.2 Tables (0001_init.sql)

```
users(id PK, username UNIQUE, password_hash, is_admin, created_at, disabled, password_changed_at)
sessions(token_hash PK, user_id FK, csrf_token, created_at, last_seen, expires_at, user_agent)
login_attempts(id PK, username, ip, at, ok)          -- rows older than 30 days are purged at start-up and daily
login_backoff(kind CHECK IN (user, ip), key, reject_until, failures, PK(kind, key))   -- "reject until T" (D-036)
households(id PK, name, currency, start_balance, as_of, last_reconciled_through,
           horizon_mode, horizon_months, horizon_end, settings_json, notes, created_at, updated_at)
memberships(user_id FK, household_id FK, role CHECK IN (owner, editor, contributor, viewer), PK(user_id, household_id))
series(household_id FK, id, name, kind, amount, schedule_json, funded_by, status, card, grp, month_amounts_json,
       cycle_amounts_json, active, notes, position, generated_through, PK(household_id, id))
ledger_rows(household_id FK, id, series_id, name, kind, amount, due_date, nominal_date, order_date, seq, state, status_override,
            amount_was, overridden, card, paid, paid_txn_ids_json, paid_at_change, cleared_txn_id, cleared_on, note,
            PK(household_id, id))
            INDEX(household_id, order_date, seq)
payee_rules(household_id, id, position, name, kind, contains_json, regex, direction, min_amount, max_amount,
            targets_json, card, note, PK(household_id, id))
accounts(household_id, id, name, kind, balance, apr_bp, payment, minimum, credit_limit, as_of, promo_ends,
         apr_after_bp, deferred_interest, is_mortgage, include_in_payoff, notes, PK(household_id, id))
expected_items(household_id, id, label, amount, direction, date, status, PK(household_id, id))
card_categories(household_id, position, name, keywords_json, bank_categories_json, weekly_series_json, has_own_rows,
                PK(household_id, name))
flow_groups(household_id, position, name, colour, rule_ids_json, series_ids_json, row_names_json, PK(household_id, name))
uploads(household_id, id, kind, filename, uploaded_at, uploaded_by, rows, date_from, date_to, latest_balance,
        has_balance, status CHECK IN (waiting, imported, rejected), note, PK(household_id, id))
bank_txns(household_id, id, first_upload_id, posted, effective, amount, balance, description, category,
          PK(household_id, id))   -- one row per transaction: dedup across uploads (history, reports)
upload_txns(household_id, upload_id, txn_id, file_index, PK(household_id, upload_id, txn_id),
            FK(household_id, txn_id) -> bank_txns)   -- every upload's full set in its own file order (prepare())
card_txns(household_id, id, first_upload_id, purchased, posted, amount, description, category, PK(household_id, id))
mapping_profiles(household_id NOT NULL, id, name, kind, header_json, mapping_json, PK(household_id, id))
            -- built-in profiles live in code only (read-only); there are no global DB profiles
reconciliations(household_id, id, at, by_user, upload_id, through, new_balance, result_json, report_md, PK(household_id, id))
audit_log(id PK, household_id NULL, at, user_id, action, row_id, detail_json, imported)
            -- household_id NULL for server-wide events (logins, user admin); kept by wipe
app_settings(key PK, value)      -- e.g. the generated secret key fingerprint, first-run flag
```

Money columns are INTEGER cents; dates TEXT ISO; booleans INTEGER 0/1; JSON columns TEXT. Upload raw files are
**not** stored: they are parsed in memory and discarded; only masked transactions and the upload metadata remain
(D-015).

Every query on a household table filters `WHERE household_id = ?` with the household id taken from the authorised
URL (`/h/{hid}`), never from the request body; a sub-resource id (row, rule, profile, upload) that is not in that
household is a 404. The process sets `umask 077` before creating the database, its WAL/SHM files, the secret key
and migration backups.

### 14.3 Repository (`db.repo`)

`load_budget(conn, household_id) -> Budget`, `save_budget(conn, household_id, budget, *, user_id, action)`
(diff-based: upserts changed rows/series/rules, records an `audit_log` entry), `load_extras` / `save_extras` for
backup parts, `list_history(conn, hid) -> list[BankTxn]`, `insert_txns(conn, hid, upload_id, txns) -> (new, dup)` (inserts new `bank_txns` rows and writes an
`upload_txns` link with this upload's `file_index` for **every** transaction of the file, duplicates included, so
the upload's full set is available to `prepare()`),
`wipe_household(conn, hid)` (keeps the household and memberships), CRUD helpers per table.
`db.users`: `create_user`, `verify_login`, `create_session`, `get_session`, `delete_session`, `record_attempt`,
`too_many_attempts(username, ip)`.

## 15. Web app (`hbudget.web`)

FastAPI app factory `create_app(settings: Settings | None = None) -> FastAPI`, served by uvicorn. Pages are
server-rendered Jinja2 (autoescape on) and each page ships its data as JSON in
`<script type="application/json" id="page-data">` (not executed, so CSP stays strict); small vanilla ES modules in
`static/js/` render the interactive parts and call the JSON API. Charts are hand-written SVG (`chart.js` line chart,
`sankey.js`). No CDN, no build step, no external fonts. Light and dark themes via `prefers-color-scheme` plus a
toggle; layout works at 360 px wide.

Page data is written only with Jinja's `|tojson` filter (which escapes `<`, `>`, `&` and quotes), and the JS renders
every user-supplied string with `textContent` / `createElementNS` + `setAttribute`, never `innerHTML`. Colours from
the database are validated against `^#[0-9a-f]{6}$` or a known `--f-*` token before they reach a style attribute.

### 15.1 Pages

| Path | Content |
|---|---|
| `/setup` | first run only (no users yet): create the admin account and the first household (empty, demo, or import) |
| `/login`, `/logout` | `/logout` is POST only (with CSRF); GET shows a "Sign out" button |
| `/` | redirect to the user's first household `/h/{hid}/bills` |
| `/h/{hid}/bills` | summary strip + alerts; bills grouped by funding payday; status segment control; Change / Remove / Undo; balance chart; "show as table"; horizon setting (next N weeks) |
| `/h/{hid}/plan` | what-if (in-page only; verdict Yes / Tight / No with low and year end; dashed line on the chart; "Add to expected") and expected items (add, mark posted, delete) |
| `/h/{hid}/debts` | debt table (balance, APR, payment, minimum, limit, utilization, monthly interest, as-of, promo end, APR after, deferred), add/edit form, payoff plan controls (method, extra per month, one-time extra + month, mortgage include switch) and results |
| `/h/{hid}/report` | Sankey, Actual / Budget toggle, per-month figures, group legend |
| `/h/{hid}/checkup` | budget vs actual per bill, verdict colours, cap rule weekly totals |
| `/h/{hid}/card` | card spending by week and category vs budget, category totals, merchant-keyword rules editor link |
| `/h/{hid}/reconcile` | uploads list (status, note), upload form (bank/card, profile picker or "map columns"), preview with automatic matches, proposals (tick + clear/reduce), unknown/tracked items, apply; past reconciliations with their reports |
| `/h/{hid}/series` | income, bills and spending lines: add/edit (schedule, funded by, status, card, group, seasonal amounts, cycle), extend forecast |
| `/h/{hid}/rules` | payee rules (ordered), test box; card categories; flow groups; alert settings and caps |
| `/h/{hid}/notes` | free-text notes (plain text, rendered escaped with line breaks) |
| `/h/{hid}/import` | bill system YAML/JSON/CSV, workbook (.ods/.xlsx/.csv) with the column-mapping step and saved profiles |
| `/h/{hid}/export` | backup YAML/JSON, forecast CSV/XLSX |
| `/h/{hid}/members` | owner only: add users, set roles |
| `/admin` | admin only: users, households, create household |
| `/healthz` | `{"status": "ok"}` after a `SELECT 1`; no auth, no data |

### 15.2 JSON API (all under `/api/h/{hid}`; JSON bodies; cents integers)

| Method + path | Role | Purpose |
|---|---|---|
| GET `/summary` | viewer | forecast readout + alerts |
| GET `/ledger?from=&to=` | viewer | ordered rows with balance, status, state, key |
| POST `/rows/{row_id}/status` `{status}` | contributor | mark autopay / manual / none / null |
| POST `/rows/{row_id}/change` `{amount}` | editor | change the amount (keeps `amount_was`) |
| POST `/rows/{row_id}/remove` · `/undo` | editor | remove (kept as removed) / undo change or removal |
| POST `/rows` `{name, kind, amount, due, after?}` · POST `/rows/{id}/move` `{after}` | editor | one-off row, reorder |
| POST `/whatif` `{items[], row_amounts{}}` | viewer | forecast with the what-ifs, nothing saved |
| GET/POST/PUT/DELETE `/expected[/id]` | editor | expected items |
| POST `/expected/{id}/status` `{status: open\|done}` | contributor | the only expected-item change a contributor can make |
| GET/POST/PUT/DELETE `/series[/id]` · POST `/series/{id}/seasonal` `{rules, from, dry_run}` · POST `/generate` | editor | series, seasonal amounts (dry run lists RowChanges), extend horizon |
| GET/POST/PUT/DELETE `/accounts[/id]` · POST `/payoff` | editor / viewer | debts, payoff plan |
| GET `/flow?view=actual\|planned` · GET `/checkup` · GET `/card` | viewer | reports |
| GET `/uploads` | viewer | uploads with status and note |
| POST `/uploads` (multipart `file`, `kind`, `profile_id?`) · DELETE `/uploads/{id}` | editor | upload a bank/card export (parsed, raw discarded); delete removes its transactions only while waiting (D-028) |
| POST `/uploads/{id}/mapping` `{mapping, save_as?}` | editor | map columns for an unknown CSV layout |
| POST `/reconcile/preview` `{upload_id}` · POST `/reconcile/apply` `{upload_id, accept{P1: clear\|reduce}}` | editor | reconciliation (a preview is computed, never stored; viewers see past reports only) |
| GET `/reconciliations[/id]` | viewer | history and reports |
| GET/POST/PUT/DELETE `/rules[/id]` · POST `/rules/order` · POST `/rules/test` | editor | payee rules |
| GET `/rules/starter` (viewer) · POST `/rules/pack` `{text?, dry_run}` | editor | the starter rule pack as a file; add a pack (D-094) |
| PUT `/settings` · PUT `/notes` | editor | settings, notes |
| POST `/import/billsystem` (multipart, `mode=replace\|merge`, `dry_run`) | owner | |
| POST `/import/workbook/preview` (multipart) -> `{token, sheets, guess}` · POST `/import/workbook/commit` `{token, mapping, save_profile?}` (spending lines and card names are part of `mapping`) | owner | the parsed sheets are kept in memory (server-side cache, 15 min, per session) between the two calls |
| GET `/export/backup.yaml` · `/export/backup.json` · `/export/forecast.csv` · `/export/forecast.xlsx` | viewer (backup: editor) | |
| POST `/wipe` `{confirm: household name}` | owner | delete all budget data of the household |
| GET/POST/PUT/DELETE `/members` | owner | list members, create a new user for this household (adding an existing user: admin only), change a role, remove; an owner cannot remove the last owner |

Server-wide routes under `/api/admin` (admin only): users (create, disable, reset password), households (create,
add self or another user as owner). Admin actions are audited.

Request bodies: JSON bodies are limited to 1 MB (413 above), multipart bodies to the upload limit. Downloads set
`Content-Disposition: attachment; filename="hearthfund-<date>.<ext>"; filename*=UTF-8''<percent-encoded>`
with a generated name (no user text in the header).

Errors: `{"error": code, "message": text}` with 400 (validation), 401, 403, 404, 409 (stale: every write that
edits a row sends `expected_amount` or the household `updated_at`; mismatch -> 409 and the UI reloads), 413, 415.

### 15.3 Bills grouping

The Bills tab walks the ordered open rows: an income row starts a new group headed "<name> · <date> · +$amount";
the rows before the first income form "From the checkbook now". Each group shows "Balance after". Spending lines
show as "budget line" with no status control. Rows past their due date show "past due date, not posted yet".
Open expected items appear in the group whose date range contains them. Groups beyond the chosen horizon (default
next 8 weeks) are hidden behind "Show more".

## 16. Auth, roles and security

- **First run (D-035):** while the `users` table is empty, every page redirects to `/setup`. At start-up with no
  users the server generates a one-time **setup token** (32 random bytes, URL-safe), writes it to the file
  `$HBUDGET_DATA_DIR/setup-token` (mode 600) and keeps only its hash in `app_settings`. The token is **never**
  logged and never put in a URL: the log only says "First run: open /setup and enter the setup token from
  <data dir>/setup-token" (the README shows `cat ./data/setup-token` / `docker compose exec app cat
  /data/setup-token`). `/setup` takes the token in a form field (POST body) together with the pre-session CSRF
  token; a query-string token is ignored. The file is deleted when setup completes. The create-admin transaction runs as
  `BEGIN IMMEDIATE`, re-checks that `users` is still empty, inserts the admin and the first household, and deletes
  the token; a concurrent second request gets "setup is already done". `/setup` returns 404 forever once a user
  exists. Alternatives that skip the token: `hbudget create-admin` in the container, or `HBUDGET_ADMIN_USERNAME` /
  `HBUDGET_ADMIN_PASSWORD`, which are read **only** while no user exists (ignored with a warning afterwards; the docs
  say to remove them from `.env` after the first start).
- **Host and origin:** `HBUDGET_ALLOWED_HOSTS` (default `127.0.0.1,localhost`) is enforced by Starlette's
  `TrustedHostMiddleware` (blocks DNS rebinding). `HBUDGET_PUBLIC_URL` (optional, e.g. `https://budget.example.com`)
  gives the expected origin. The CSRF origin check compares `Origin` (or `Referer`) with the configured allowed
  origins (`HBUDGET_PUBLIC_URL`, plus `http(s)://<allowed host>:<port>`), never with the request's own `Host`.
- **Passwords:** `hashlib.scrypt(n=2**15, r=8, p=1, dklen=32)` with a 16-byte random salt, stored as
  `scrypt$32768$8$1$<salt b64>$<hash b64>`; compared with `hmac.compare_digest`. Minimum 10 characters. At most 2
  scrypt computations run at once (an `asyncio.Semaphore`, the work in a thread); a login waits at most about 2 s
  for a free slot and otherwise gets **503** ("busy, try again") without hashing. An unknown username is checked
  against a fixed dummy hash so timing does not reveal which usernames exist.
- **Sessions:** 32-byte random token in cookie `__Host-hb_session` when Secure (else `hb_session`): HttpOnly,
  SameSite=Lax, Path=/, no Domain. Secure per config: `auto` = on when the request is HTTPS; with
  `HBUDGET_TRUST_PROXY=1` the scheme and client IP come from the proxy headers **only** for proxies listed in
  `HBUDGET_FORWARDED_ALLOW_IPS` (passed to uvicorn's `forwarded_allow_ips`; default `127.0.0.1`). The DB stores
  `sha256(token)`. Idle expiry 12 h (configurable), absolute 7 days; expired rows are purged at start-up and daily.
  The token rotates at login; logout deletes the row; a password change or admin reset deletes all of that user's
  sessions (except the current one on a self-service change). Every request re-reads the user (disabled users are
  logged out) and the membership role (a role change applies at once).
- **CSRF (deny by default):** a middleware rejects every POST/PUT/PATCH/DELETE without a valid token; there is no
  exempt route. With a session, the token is the session's synchronizer token. Before login (`/login`,
  `/setup`) a double-submit token is used: a random `hb_csrf_pre` cookie (HttpOnly, SameSite=Strict) plus the same
  value in the form, compared in constant time. JS sends `X-CSRF-Token`; HTML forms send `csrf_token`. Multipart
  requests must use the header, and the header is checked **before** the body is parsed. The origin check above
  applies to all of them. No CORS headers are ever sent.
- **Throttling (D-036, D-097):** failed logins are counted in `login_attempts` by username and by client IP (the
  real peer address unless a trusted proxy sets it). Per username **and address**: after 5 failures in 15 minutes
  the server stores a **"reject until T"** time for that pair (T grows exponentially: now + 2 s, 4 s, ... up to
  60 s with each further failure), so someone guessing from elsewhere does not hold back the real user. Per
  username across all addresses: after 25 failures the same delay applies to every address that failed for that
  name in the window (a fresh address still gets its try). Attempts before T are answered at once with 429 and a
  `Retry-After` header, without hashing; the server never sleeps on a request. This is a short back-off, not a
  lockout. Per IP across usernames: after 20 failures in 15 minutes that IP gets "too many attempts" (429) for 15
  minutes. The message for unknown user and wrong password is the same. A right password of a two-factor user
  clears nothing (`record_attempt(clears=False)`). Wrong two-factor codes are also counted per user in
  `code_failures`: after 10 in 24 hours, codes from the app are refused for that user until the count drops
  (recovery codes still work; a right code clears the count; `2fa.locked` in the audit log). A right code for a
  step already used (`twofactor.REUSED`) is refused without counting as a guess.
- **Roles** (per household; the admin is not implicitly a member, but can add themselves as owner through
  `/api/admin`, which is audited):

| Action | owner | editor | contributor | viewer |
|---|---|---|---|---|
| view everything, what-if, forecast exports, past reconciliation reports | yes | yes | yes | yes |
| mark bill status, mark an expected item posted/open | yes | yes | yes | no |
| change/remove/undo rows, series, rules, debts, expected items, notes, uploads, reconcile preview/apply, backup export | yes | yes | no | no |
| import (replace/merge), wipe, members | yes | no | no | no |

  Enforced server-side by a `require(role)` dependency on every route; the UI only hides what the server refuses.
  A user who is not a member of a household gets **404** for everything under `/h/{hid}` and `/api/h/{hid}` (no
  hint that it exists). Creating users is open to the admin (any) and to owners (users created from a household's
  members page start as members of that household only). Only the admin can add an **existing** user to a
  household (D-098), so an owner can neither pull another household's member into theirs nor probe which
  usernames exist (beyond "taken" when creating one).
- **Audit (D-037):** `audit_log` records logins (ok and failed, without the password), logouts, user and membership
  changes, role changes, password changes/resets, every row edit and status mark, series/rule/settings changes,
  uploads and their deletion, reconciliation applies, imports, exports and backups, and wipes. Wipe deletes budget
  data but keeps the audit log (and adds an entry). Entries hold ids and field names, not descriptions or amounts.
- **Headers:** `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `Cache-Control: no-store` on API and pages,
  HSTS only when Secure cookies are on.
- **Uploads:** max size (default 5 MB) enforced while streaming; allowed extensions `.csv .ofx .qfx .ods .xlsx
  .yaml .yml .json`; content sniffed (zip magic for ods/xlsx, `OFXHEADER`/`<OFX>` for OFX, UTF-8/UTF-8-BOM/latin-1
  text for CSV); the original filename is masked (`textmask.mask`) and reduced to `[A-Za-z0-9._-]{1,80}` for display
  and is never used as a path.
- **Untrusted text:** user regexes go through `rules.compile_safe` (9.3); YAML/JSON limits in 12.5; ODS/XLSX/OFX
  limits in 12.1.
- **No outbound calls**: no CDN, fonts, update checks or telemetry. The only listener is the HTTP port. Opt-in
  exception (D-091): the Plaid connection posts to `sandbox.plaid.com` or `production.plaid.com` only (no
  redirects), and only `/h/{hid}/connections` and `/plaid/oauth` widen the CSP (`script-src` Plaid Link's script,
  `frame-src https://cdn.plaid.com`, `connect-src` the API host) and send `Cross-Origin-Opener-Policy:
  same-origin-allow-popups`. Tests run with a socket guard that fails any connection off the machine.
- **Two-factor sign-in (D-095, optional per user):** migration 0013 adds `users.totp_secret`,
  `totp_enabled_at`, `totp_last_step` and the tables `recovery_codes` (hashed, one use) and `login_challenges`
  (hashed token, 5 minutes). `core/totp.py` is the pure code maths, `core/qr.py` the QR encoder,
  `db/twofactor.py` the state. `POST /login` with the right password for a two-factor user sets the challenge
  cookie `hb_2fa` (HttpOnly, SameSite=Strict, path `/login`) and redirects to `/login/code`; only
  `POST /login/code` with a current code or an unused recovery code creates the session. The challenge is not a
  session: `current_auth` never reads it. Wrong codes use the login back-off; a right password alone does not
  clear it. Pages: `/account/2fa` (start with the password, confirm with a code, new recovery codes, turn off
  with password and code). A pending key is tied to the session that started it (`totp_pending_session`,
  `totp_pending_at`; 10 minutes): `twofactor.state` and `confirm` give nothing to any other session. Turning
  it on or off deletes the user's other sessions. `deps.second_factor` asks a two-factor user for the header
  `X-2FA-Code` (one check at a time, under the login back-off) on: `POST /api/h/{hid}/tokens` (revoking needs none),
  `POST /api/h/{hid}/members`, `PUT /api/h/{hid}/members/{id}`, and under `/api/admin/users`: create,
  `/admin`, `/password`, `/2fa/reset`, `/disabled` when enabling, and
  `POST /api/admin/households/{id}/owner`. The pages retry with a prompted code on `403 two_factor`
  (`apiCode` in `api.js`). Reset for a lost phone:
  `POST /api/admin/users/{id}/2fa/reset` (not for the admin's own account) and `hbudget disable-2fa`. With
  `HBUDGET_REQUIRE_2FA=1` a signed-in user without it is sent to `/account/2fa` from every page and gets
  `403 two_factor_required` from the session API. Bearer tokens and `/mcp` are not affected.
- **Dev mode:** `HBUDGET_DEV=1` refuses to start unless the bind host is a loopback address.
- Errors never show stack traces outside dev mode; logs never contain descriptions or amounts at INFO level.

## 17. CLI (`hbudget`)

```
hbudget serve [--host 127.0.0.1 --port 8080]          run the server (the container uses this)
hbudget migrate                                       apply migrations (also done at start-up)
hbudget create-admin USERNAME                         prompts for the password; works only while no user exists, unless --reset-password
hbudget disable-2fa USERNAME                          turn off a user's two-factor sign-in (lost phone); signs them out
hbudget demo [--household NAME]                       create a household with the fake demo data
hbudget import-bills FILE [--household ID] [--replace] [--dry-run]
hbudget import-workbook FILE --profile NAME|--mapping JSON [--household ID] [--dry-run]
hbudget reconcile FILE [--household ID] [--accept P1:clear,P2:reduce] [--write]   dry run unless --write
hbudget export [--household ID] [--format yaml|json|csv|xlsx] [-o FILE]           never overwrites -o
hbudget wipe --household ID --yes
hbudget check-workbook FILE --mapping JSON            print B1/low/low date/end as the app computes them vs the stored cells (match / mismatch only; with --show it prints values)
```

The CLI uses the same `db` and `core` code as the web app and the same data dir.

## 18. Configuration (environment, `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `HBUDGET_DATA_DIR` | `/data` (container), `./data` (`make dev`) | database, secret key, migration backups |
| `HBUDGET_HOST` / `HBUDGET_PORT` | `127.0.0.1` / `8080` (the container listens on `0.0.0.0`; compose publishes `127.0.0.1:8080`) | |
| `HBUDGET_SECRET_KEY` | generated into `$DATA_DIR/secret_key` (mode 600) on first start | signs nothing user-visible today; reserved for tokens |
| `HBUDGET_COOKIE_SECURE` | `auto` | `auto`, `true`, `false` |
| `HBUDGET_TRUST_PROXY` | `0` | trust `X-Forwarded-Proto/For` from a reverse proxy ... |
| `HBUDGET_FORWARDED_ALLOW_IPS` | `127.0.0.1` | ... but only from these proxy addresses |
| `HBUDGET_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host header allow-list (add your proxy/tailnet name) |
| `HBUDGET_PUBLIC_URL` | unset | the public origin, e.g. `https://budget.example.com` (CSRF origin check) |
| `HBUDGET_TLS_CERT` / `HBUDGET_TLS_KEY` | unset | PEM certificate (with chain) and unencrypted key; set both to serve HTTPS directly (D-062). Compose mounts `${HBUDGET_CERTS_DIR:-./certs}` read-only at `/certs` |
| `HBUDGET_SESSION_HOURS` | `12` | idle timeout |
| `HBUDGET_REQUIRE_2FA` | `0` | `1` = every user must set up two-factor sign-in before using the app (D-095) |
| `HBUDGET_MAX_UPLOAD_MB` | `5` | |
| `HBUDGET_DEMO` | `0` | `1` = create the demo household at first run |
| `HBUDGET_DEV` | `0` | reload, debug pages; refuses to run on a non-loopback host |
| `HBUDGET_LOG_LEVEL` | `info` | |
| `HBUDGET_ADMIN_USERNAME` / `HBUDGET_ADMIN_PASSWORD` | unset | optional non-interactive first-run admin; read only while there are no users (remove them after the first start) |
| `HBUDGET_PLAID` | `off` | `off`, `sandbox` or `production`: the optional Plaid connection (D-091) |
| `HBUDGET_PLAID_CLIENT_ID` | unset | required when Plaid is on |
| `HBUDGET_PLAID_SECRET` / `HBUDGET_PLAID_SECRET_FILE` | unset | the secret for that environment, directly or in a file (one of them, required when on) |

## 19. Packaging

- `Dockerfile`: both stages use `python:3.13-slim` pinned by digest (`python:3.13-slim@sha256:...`, updated by a
  documented `make bump-base`); stage 1 builds a wheel and installs it into `/opt/venv`; stage 2
  `python:3.13-slim` copies `/opt/venv` only, creates user `app` (uid 10001), `WORKDIR /app`, `VOLUME /data`,
  `ENV HBUDGET_DATA_DIR=/data HBUDGET_HOST=0.0.0.0`, `EXPOSE 8080`, `USER app`,
  `HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"`,
  `CMD ["hbudget", "serve"]`. No compilers, no git, no data in the image. Labels with version and source.
- `docker-compose.yml`: one service, `ports: ["127.0.0.1:8080:8080"]`, `volumes: ["./data:/data"]`, `env_file: .env`
  (optional), `read_only: true`, `tmpfs: [/tmp]`, `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`,
  `restart: unless-stopped`. The data folder must be writable by uid 10001 (documented; the entrypoint prints a
  clear message when it is not).
- `.dockerignore` excludes everything except `pyproject.toml`, `src/`, `README.md`, `LICENSE`, `schema/`.
- Multi-arch: `docker buildx build --platform linux/amd64,linux/arm64` (CI with QEMU). Pure-Python dependencies
  plus wheels available for both architectures (lxml is not used).
- Upgrade = pull/build the new image and `docker compose up -d`; migrations run at start with an automatic DB backup
  (14.1). Tested: start old image, add data, start new image on the same volume, data intact.
- Remote access (docs/DEPLOY.md, "Remote access"): keep the port on localhost; use Tailscale (`tailscale serve`, which stays
  inside your tailnet — **not** `tailscale funnel`, which makes the app public) or a reverse proxy with HTTPS (Caddy
  example) and `HBUDGET_TRUST_PROXY=1`, `HBUDGET_FORWARDED_ALLOW_IPS`, `HBUDGET_ALLOWED_HOSTS`, `HBUDGET_PUBLIC_URL`,
  `HBUDGET_COOKIE_SECURE=true`. Never forward the port on a router. The docs warn that `docker run -p 8080:8080`
  (without `127.0.0.1:`) publishes on every interface and that Docker's port publishing bypasses ufw/firewalld rules.
- CI runs `pip-audit` on the locked dependencies and the image build on every push.

## 20. Tests

### 20.1 Layout and gates

`tests/core` (pure functions), `tests/io` (readers, bill system, exports), `tests/db` (migrations, repo, the round
trip), `tests/web` (TestClient: auth, CSRF, roles, every route; shared helpers in `tests/webhelpers.py`, imported by
name, so the folders can run in any order), `tests/fixtures` (synthetic files only, years
2028-2031). `make test` runs everything (private tests skip without their files). `make cov` runs the suite with a
per-module coverage report and fails when `hbudget.core` is under 90%; CI runs `make lint cov` on every push.

### 20.2 Every public function has a direct test

Rule: every public function and class in `hbudget.core` and every reader/writer in `hbudget.io` is called directly
by at least one test in its mapped file (not only through another function). The test-auditor checks this map:

| Module | Test file |
|---|---|
| core/money.py, core/dates.py | tests/core/test_money.py, tests/core/test_dates.py |
| core/models.py | tests/core/test_models.py (row_status, row_key, slug, defaults) |
| core/recurrence.py | tests/core/test_recurrence.py |
| core/ledger.py | tests/core/test_ledger_generate.py, test_ledger_order.py, test_ledger_edits.py |
| core/forecast.py | tests/core/test_forecast.py |
| core/alerts.py | tests/core/test_alerts.py |
| core/rules.py | tests/core/test_rules.py (classify, test, compile_safe) |
| core/reconcile.py | tests/core/test_reconcile_prepare.py, test_reconcile_match.py, test_reconcile_apply.py |
| core/payoff.py | tests/core/test_payoff.py |
| core/flow.py | tests/core/test_flow.py |
| core/checkup.py | tests/core/test_checkup.py |
| core/cards.py | tests/core/test_cards.py |
| core/textmask.py | tests/core/test_textmask.py |
| core/goals.py, core/milestones.py, core/recap.py | tests/core/test_goals.py, test_milestones.py, test_recap.py |
| core/subscriptions.py, core/paydays.py | tests/core/test_subscriptions.py, test_paydays.py |
| core/totp.py, core/qr.py | tests/core/test_totp.py (RFC vectors), test_qr.py |
| (purity) | tests/core/test_purity.py |
| (synthetic regression) | tests/core/test_synthetic_regression.py |
| io/limits.py, io/tabular.py | tests/io/test_limits.py, tests/io/test_tabular.py |
| io/csvfile.py, io/bank.py | tests/io/test_csvfile.py, tests/io/test_bank.py |
| io/ods.py, io/xlsx.py | tests/io/test_ods.py, tests/io/test_xlsx.py |
| io/workbook_import.py, io/infer.py | tests/io/test_workbook_import.py, tests/io/test_infer.py |
| io/ofx.py | tests/io/test_ofx.py |
| io/billsystem.py | tests/io/test_billsystem.py, tests/io/test_example_file.py, tests/io/test_schema_copy.py |
| io/export.py | tests/io/test_export.py (incl. formula injection) |
| io/rulepack.py, io/plaid.py | tests/io/test_rulepack.py, tests/io/test_plaid.py |
| db/* | tests/db/test_backup.py, test_migrate.py, test_repo.py, test_roundtrip.py, test_tokens.py, test_twofactor.py, test_users.py |
| web/* | tests/web/test_*.py; tests/web/test_access.py fails when a route is missing from its role and CSRF tables |
| tests/private_support.py | tests/test_private_support.py |

### 20.3 Required edge cases

Each must have a named test:
- Dates: month ends; `add_months(Jan 31, 1)` in 2028 (leap: Feb 29) and 2029 (Feb 28); monthly on the 29th, 30th
  and 31st in February and 30-day months; yearly Feb 29 2028 -> Feb 28 2029; semimonthly days on weekends with
  `before`/`after`, including a shift across a month and a year boundary and the 30th/31st collapse; weekend shift
  at the window edges (nominal inside, actual outside and the reverse); `week_start` with Monday and Sunday weeks.
- Generation: first occurrence when `schedule.start > as_of`; a row due on `as_of`; watermark keeps a removed
  occurrence and an imported gap; `funded_by` look-ahead miss (no payday within 35 days); funded_by with the payday
  shifted by a weekend; seasonal month by nominal date; cycle amounts with index stable across windows; an imported
  bill that ended part-way through the sheet is not refilled by a series edit, Extend, or a switch to rolling (and
  gets `schedule.end`); a watermark never moves back; an inactive series reactivated later creates no past-due rows;
  imported and generated rows of one series share the nominal-date id scheme.
- Forecast: negative start balance; low below zero (and the alerts); minimum occurring twice (first wins); zero rows;
  no rows; expected item in the past; year_ends over several years.
- Reconciliation: chain break (a row dropped in memory); basis not found; basis ambiguous; nothing new; empty upload;
  oldest-first export; same-day postings in file order; overlapping uploads (the newer upload contains transactions
  of an older one and still finds its basis); no-balance dedup with a later posting on the same day; oldest bill
  candidate out of tolerance while a younger one fits (-> proposal); card bundle (greedy stop rule, rule without card
  matches nothing); ask with two targets; reduce and then Undo of an earlier change; expected item match; refunds
  (wrong direction); duplicate transactions within one file, across uploads, and two genuine identical purchases.
- Payoff: promo with deferred interest gets extra first (the D-013 case), mortgage excluded and included, payment
  larger than the balance, payment below interest, one-time extra.
- Flow: with and without card data; `months_back` when the newest posting is a month end, mid-month, and in January;
  planned window from Feb 29.
- Cards: purchase date in the text across a year end; bank-category fallback.
- Importers: empty file for each reader (named `ImportProblem` message); malformed CSVs (missing columns, bad dates,
  bad amounts, ragged rows, oversize fields); OFX 1.x without closing tags, OFX 2.x XML, QFX header, LEDGERBAL before
  the newest posting (no balances), ACCTID never stored; limits as tests (zip bomb by real size, too many members,
  repeated columns cap, rows cap, oversize upload, YAML alias, nesting depth); grouped card numbers masked.
- Export: formula injection in CSV and XLSX (text cells prefixed, numeric columns including negative amounts left
  numeric); export -> wipe -> import round trip.
- Auth: setup token only accepted from the form, never logged; login back-off answers 429 with Retry-After without
  sleeping; a login that cannot get a scrypt slot within about 2 s gets 503.

### 20.4 Core purity

`tests/core/test_purity.py` parses every module in `src/hbudget/core` with `ast` and fails on any import that is not
the standard library or `hbudget.core` itself, and on any use of `date.today`, `datetime.now`, `datetime.utcnow`,
`time.time`, `uuid`, `random` or `secrets`.

### 20.5 Synthetic regression fixtures (ship in the repo)

1. `tests/fixtures/synthetic/household.yaml`: a fake household with a fixed horizon of about two months (2029-03-01
   to 2029-04-30), round amounts, two earners, a bill ordered after a later payday through `funded_by`, a seasonal
   amount change, a zero-amount row, and a minimum that occurs twice (the first must win).
   `tests/core/test_synthetic_regression.py` holds the expected start, low + date, high + date, end and year-end
   values computed by hand and written in a comment table row by row, plus a synthetic bank export
   (`synthetic/bank.csv`) whose reconciliation gives a hand-checked set of automatic matches, proposals, tracked and
   unknown items and the new balance.
2. `tests/fixtures/synthetic/multiyear.yaml`: a fixed horizon from 2029-11-01 to 2031-02-28 whose low goes below
   zero, to check `year_ends` over three years and the below-zero alert.
3. The first household as `.ods` and `.xlsx` (generated by `tests/fixtures/make_fixtures.py`, committed with its
   output), with check cells (start balance, a MIN cell, the date of the minimum, the last balance), so the public
   suite also covers the importer's order reproduction, status colours and the `StoredCheck` path.

### 20.6 Round trip

`tests/db/test_roundtrip.py` uses the real `db` layer: load the synthetic household plus a reconciliation and some
history into a database, export a backup, wipe the household, import the backup, export again, and compare the two
documents (except `exported_at`) and the two forecasts. It runs for YAML and JSON.

### 20.7 Private regression tests (never committed)

**Locator (committed):** `tests/private_support.py` holds the only code that finds private files:
`private_file(name: str) -> Path` reads `BUDGET_PRIVATE_DIR` and calls `pytest.skip(reason)` when the variable is
unset, the folder does not exist, or the file is missing. Reasons name the variable or the file pattern only, never
a value ("BUDGET_PRIVATE_DIR is not set", "no *.ods workbook in BUDGET_PRIVATE_DIR"). It also provides
`report(field, ok)` which formats a failure as "<field>: mismatch" (values only when `PRIVATE_SHOW=1`).
`tests/test_private_support.py` tests it with the variable unset, set to an empty folder, and with one file missing.

**Private tests:** `private_tests/` is git-ignored. A maintainer can keep regression tests there that run the
importers, the forecast and a reconciliation against their own household's files (a real workbook and bank export),
and compare the results with the figures those files hold. Everything private (the files, a mapping profile, payee
rules, and any expected values) stays in that folder or in `BUDGET_PRIVATE_DIR`; nothing about them is written into
the repository. Failure messages come from `private_support.report` and print only the field name and "mismatch".
