# Importing into Hearthfund

Four ways to get your budget in, and two kinds of bank file to reconcile against it. All of them are on
**More > Import** and **Money > Uploads**; the command line does the same (`hbudget import-bills`, `import-workbook`,
`reconcile`; see the [README](../README.md#command-line)).

Amounts are always written as positive numbers with at most two decimals (`150`, `150.5`, `1,800.00`). Whether a line
is money in or money out comes from its kind (income, bill or spending line).

## 1. A bill-system file (YAML or JSON)

The richest format: income, bills, spending lines, debts, payee rules, alerts, card categories and report groups.

- Start from [examples/bill-system.example.yaml](../examples/bill-system.example.yaml); every field is explained in
  its comments.
- The full format is [schema/bill-system.schema.json](../schema/bill-system.schema.json) (JSON Schema), described in
  [ARCHITECTURE.md section 13](ARCHITECTURE.md#13-the-bill-system-format).
- *Merge* adds or updates items by their `id`; *Replace* replaces the bills and their rows. The household keeps its
  name and its bank history either way.
- The file's `start_balance` and `as_of` are used for a new household (no balance yet, never reconciled). For a
  household that already has a balance, tick **Also set the checkbook balance from the file** (CLI: `--set-balance`).
- A full **backup** is the same format with an extra `backup` section. Restoring it replaces everything, history
  included, and asks you to type the household name.

### Starter payee rules

Rules are what let a bank upload clear your bills by itself. Once your bills are in, **Rules > Starter rules** adds
a ready-made set for common payees in the United States
([examples/starter-rules.yaml](../examples/starter-rules.yaml)); the command line does the same with
`hbudget rule-pack` (a dry run) and `hbudget rule-pack --write`.

- **See what would be added** is a dry run. Pay rules are linked to every paycheck. A bill rule is linked to the
  bills whose name contains one of its `bills_named` words: a bill called "Electric" or "City electric" gets the
  Electric rule. A rule with nothing to link to is skipped; add the bill and add the pack again.
- Nothing you have is changed: a rule you already have (same id), a bill that already has a rule, and your
  report groups and card categories are left as they are. Adding the pack twice adds nothing the second time.
- The rules go to the end of the list and rules you add later go above them, because the first match wins.
- To change the pack first: **Download the starter rules** (or `hbudget rule-pack --show > my-rules.yaml`), edit
  the file, then **add rules from a file** (or `hbudget rule-pack my-rules.yaml --write`). A pack has
  `format: household-budget/rule-pack` (the id keeps the app's earlier name on purpose), `version: 1`, a `name`
  and `rules`; each rule has an `id`, a `name`, a
  `kind` (`pay`, `bill`, `track` or `ignore`), `contains` words or a `regex`, and for a bill `bills_named`. A
  track rule can name a report `group` and a `card_category`. The comments in the file explain each part.

## 2. A simple CSV of bills

For a quick start without a spreadsheet. One bill per line, with this header (columns can be in any order; only
`name`, `kind`, `amount` and `cadence` are required):

```
name,kind,amount,cadence,start,day,days,month,weekend,status,funded_by,card,group
```

| Column | Values |
|---|---|
| `name` | what the row is called, e.g. `Electric` |
| `kind` | `income`, `bill` or `spending` (a budget line such as groceries: no status) |
| `amount` | positive, e.g. `150` or `1,800.00` |
| `cadence` | `once`, `weekly`, `biweekly`, `semimonthly`, `monthly` or `yearly` |
| `start` | `YYYY-MM-DD`. Needed for `weekly` and `biweekly` (see below) and `once`; otherwise optional |
| `day` | monthly / yearly: day of the month, 1-31. Day 31 means the last day (the 30th, or the 28th/29th in February) |
| `days` | semimonthly: the two days, separated by `;`, e.g. `1;15` |
| `month` | yearly: 1-12 |
| `weekend` | `none` (keep the date), `before` (the Friday before) or `after` (the Monday after) |
| `status` | bills only: `autopay`, `manual` (scheduled by hand) or `none` (not scheduled yet) |
| `funded_by` | the id of the income line that pays this bill. It is listed after that payday instead of on its due date. The id is the income's name in lower case with dashes: `Pay A` becomes `pay-a` |
| `card` | the id of the credit card that pays it (for card-payment bundles when reconciling) |
| `group` | the Report group, e.g. `Utilities` |

**Weekly and biweekly start dates set the phase.** A weekly line repeats on the weekday of its `start`; a biweekly
one also needs the right week. Any past date on the right weekday (and, for biweekly, the right week) works: rows are
only made from the household's checkbook date on. Without a start, the line starts on the day you import it. For a
new household, use a recent date, such as your last payday.

A bills CSV has no checkbook balance: set it on **More > Settings** afterwards.
Example: [examples/bills.example.csv](../examples/bills.example.csv).

## 3. A spreadsheet (.ods, .xlsx or .csv)

For a checkbook you already keep as one row per bill or payday, with a name, an amount and a date, and perhaps a
starting balance in a cell. The app reads the file, shows the first rows, and you map the columns:

| Mapping | Meaning |
|---|---|
| Sheet, header row, first data row | where the rows are |
| Name / Amount / Date column | column letters, e.g. `A`, `B`, `C` |
| Amounts | `positive = money out` (bills positive, pay negative) or `positive = money in` |
| Starting balance cell (or value) | e.g. `B1` |
| Skip rows named | patterns for rows that are not bills (e.g. a carry-over line) |
| Spending lines | names to treat as budget lines (no status) |
| Read bill status from the name cell colour | green and pink by default (More > Settings: Autopay colour, Scheduled colour) |
| Check cells | optional: the cells holding the sheet's own lowest balance, its date, and the last balance. After the import the app says whether its forecast matches them |

The rows keep their order from the sheet, so a bill listed after the payday that pays it stays there. Repeating names
become recurring items (monthly, weekly, ...) so the forecast can run on. Save the mapping as a profile to reuse it.
Only the budget part is replaced; bank history, uploads and saved layouts stay. On the command line:
`hbudget import-workbook FILE --profile NAME` (or `--mapping` with the mapping as JSON) and
`hbudget check-workbook FILE --mapping JSON` (match or mismatch against the check cells).

## 4. Bank and card exports

On **Money > Uploads**: CSV, OFX or QFX. The file is read and then thrown away; only the transactions are kept,
with account numbers masked (the last four digits stay).

- **CSV layouts.** Three common layouts are recognised from the header row: Date / Description / Amount / Balance,
  Date / Description / Debit / Credit / Balance, and Date / Description / Amount. Any other layout asks once which
  column is the date, the description, the amount (or debit and credit), the running balance, and the date format;
  tick *money out is positive* for exports that show purchases as positive numbers. Save it as a layout; the next file
  with the same header is recognised. Bank and card layouts are kept apart.
- **OFX / QFX** (the formats many banks offer as "Quicken" or "Money"): read directly, no mapping.
- **The balance column** is what lets the app find where your last reconciliation ended and check that no posting is
  missing. Without one, postings after the last reconciled date are used and duplicates are skipped.
- **Card exports** (kind *Card*) fill the Card tab. Purchases are sorted by the keywords on that page (first match
  wins), or by the export's own category column when you map one.

How a reconciliation works (proposals, *clear* and *reduce*, card-payment bundles):
[README: Reconciling with the bank](../README.md#reconciling-with-the-bank).
