# Changelog

What changed in each release, newest first. Versions follow [semantic versioning](https://semver.org); the image
for each release is `ghcr.io/optimus506/hearthfund:<version>` (0.15.0 and earlier: `.../household-budget`). Upgrading:
docs/DEPLOY.md, "Upgrading".

Issue and pull-request numbers such as #87 that appear before 1.0 refer to the project's earlier private
tracker; those issues are not public, and the public tracker numbers its issues from #1 again.

## Unreleased

### Changed
- **Only the admin adds someone who already has an account** to a household (D-098). Owners can still create
  new users from the Members page.
- **Plaid's script is fetched only when someone starts linking a bank**, not whenever the Bank connection page
  opens, so a household with Plaid off loads nothing from outside the server.
- **A warning at start-up for `HBUDGET_FORWARDED_ALLOW_IPS=*`** with `HBUDGET_TRUST_PROXY=1`: it lets anyone who
  can reach the app fake their address.
- **Sign-in is harder to block and to guess** (D-097). Wrong passwords hold back the address they come from, so
  someone guessing elsewhere no longer locks you out. After 10 wrong two-factor codes in a day, codes from the
  app are refused for that account until the next day; recovery codes still work. Entering the same code twice
  within 30 seconds (for example for two changes in a row) now says "just used" instead of counting as a wrong
  code.
- **Revoking an access token no longer asks for a code**, so a leaked token can be stopped from anywhere.
  Re-enabling a user and an admin making themselves owner of a household now ask a two-factor admin for one.

### Fixed
- **`scripts/upgrade.sh` reports failures honestly.** A failed pull with the app stopped used to end with
  "upgraded"; a start error ended with no message. Now each exits with an error that says what happened. The
  script refuses to start the new version when it cannot tell which backup it just made, so a rollback can
  always restore it, and it says so plainly if putting the backup back fails.

## 0.16.0 - 2026-11-10

### Added
- **Starter rules.** The Rules page can add ready-made payee rules for common payees in the United States:
  paychecks, the usual bills (mortgage or rent, utilities, internet, phone, insurance, loans, childcare) and
  everyday spending to track (groceries, fuel, eating out, shopping, streaming and more). **See what would be
  added** lists them first. Bill rules are linked to your bills by name, pay rules to your paychecks; a rule with
  nothing to link to is skipped, and nothing you already have is changed. To change the pack first, download it,
  edit it and add your copy with **add rules from a file**. Command line: `hbudget rule-pack` (D-094).
- **What stays compatible** is written down: [docs/STABILITY.md](docs/STABILITY.md) says what 1.x releases
  keep working (your data and upgrades, backups and bill-system files, `/api/v1`, the MCP tools, the settings
  in `.env`) and what the known limits are. A security policy ([SECURITY.md](SECURITY.md)) says how to report
  a vulnerability privately.
- **Two-factor sign-in** (optional, off by default; D-095). Under **Account > Two-factor sign-in** scan the
  code with any authenticator app; signing in then asks for the password and a 6-digit code. You get ten
  one-time recovery codes for when the phone is not at hand. Turning it on signs out your other sessions.
  Making or revoking an API access token, adding a member, changing a role and the admin's user changes ask
  for a code too; existing tokens keep working. Lost the phone and the codes: an admin turns it off for you
  on the Admin page, or `hbudget disable-2fa USERNAME` on the server. `HBUDGET_REQUIRE_2FA=1` makes every user
  set it up.
- **Screenshots and an install without cloning.** The README shows the demo household, and
  [docs/DEPLOY.md](docs/DEPLOY.md#install-without-cloning) explains running the app from a downloaded
  `docker-compose.yml` alone.

### Changed
- **The app is now called Hearthfund** (D-096). The image is `ghcr.io/optimus506/hearthfund`; images up to
  0.15.0 keep the old name. To upgrade an existing deployment: `git pull` and `scripts/upgrade.sh` as usual
  (the script keeps your existing containers by adding `COMPOSE_PROJECT_NAME=household-budget` to `.env`); if
  `.env` pins `HBUDGET_IMAGE` to the old image name, change it. Nothing in your data or settings is renamed:
  `HBUDGET_*`, the `hbudget` command, backups and bill-system files work as before. Downloads are now named
  `hearthfund-...`, and the MCP server calls itself `hearthfund` (an agent set up earlier keeps working).
- **A new rule goes above the starter rules**, not to the very end, so your own rule is tried first.
- **An older release refuses to start on newer data.** After a rollback without the pre-upgrade backup, the app
  used to start on a database a newer release had changed. It now stops with a message that says which backup
  to put back.
- **US dollars only, said plainly.** The README says so, and importing a bill-system file that names another
  currency warns that it is kept but changes nothing.
- **Bank connection is in the More row.** When Plaid is on, it sits after Settings with the other More pages
  (it was only on the More page and in Settings).

## 0.15.0 - 2026-11-09

### Added
- **Daily bank sync** (optional, Plaid only; D-092). On the Bank connection page an owner can set **Sync
  automatically** to *Daily*. Once a day the app asks Plaid for new transactions and keeps them; nothing reaches
  the budget until the check-in's **Bring them in**, which makes one bank upload to review however many days it
  covers. A sign-in problem the daily sync finds shows in the check-in. Off by default.

### Changed
- **One waiting Plaid upload at a time.** Syncing again before the last bank upload was reviewed replaces it with
  one that holds everything since the last reconciliation (and nothing the bank withdrew), instead of adding a
  second, overlapping one. Card uploads take every stored card posting not yet brought in.

## 0.14.0 - 2026-11-09

### Changed
- **Bills by payday is compact** (#100). Each bill is one line with its amount and a status pill (Autopay, Scheduled,
  Not yet, in the legend's colours); tapping the row or the pill opens the status choice and Change under it.
  Repeating spending lines of one payday fold into one line, "Grocery ×2 · $320.00", with **Show**.
- **Home has one to-do list** (#98). The weekly check-in gains step 3, **Things to look at**: the alerts and notes,
  each with a button to the page where it is dealt with (alerts about scheduling are step 2 already). **Until
  payday** keeps the big number, the next payday and card moves, and counts the bills instead of listing them
  again; the duplicate "Upload a bank file" shortcut is gone.
- **The full summary strip is on Home only** (#99). Other pages show one line (balance, the low and its date, bills
  to schedule, alerts) that opens Home.
- **Tab names** (#101): Money > Bank is now **Uploads** ("Bank and card uploads"), and Bills > Pay & bills is now
  **Bill setup**.
- **The check-in guides the weekly routine** (#103): a page opened from a check-in step shows "← Back to the
  check-in · Step N of 4".

### Fixed
- **Image:** zlib in the image is upgraded to Debian's fixed version (CVE-2026-76844, CVE-2026-85091); the
  pinned base image does not have it yet.
- **Phones: the second row of tabs shows that it scrolls** (#102). The current page's tab is scrolled into view, the
  edge with more tabs fades and a › button scrolls on. **Paydays ahead** keeps date, in/out and "before next pay"
  in their own columns, with the card-transfer note under the bars.
- The Card page pointed to a "Reconcile tab"; it is Money > Bank (#101).
- **Repeating-payee notes age out and can be set aside** (#104). "Shows up in N months with no payee rule" now
  looks at the last 12 months and drops a payee with nothing posted for 60 days (a card paid off long ago).
  Each note has **Make a rule** (Rules opens with the payee filled in) and **Ignore until it posts again**, which
  brings it back by itself when the payee posts in a later month. The line on other pages now counts these
  notes too, so no page says there is nothing while Home shows one.

### Added
- **Estimated debt balances between your updates** (#97). A recurring bill can name the debt it pays down (**Pays
  this debt** on Bills > Bill setup, filled in when the names match). Debts then shows "est. $9,212.00 · confirmed
  10/24": the typed balance plus a month's interest at the APR on each monthly anniversary, less the reconciled
  payments since. The typed balance stays what the payoff plan uses. Agents: `get_debts` has `estimated_balance`,
  and `set_series_details` takes `debt`.
- **A bill paid in pieces is offered the month's row** (#96). A payment from a known payee with no row in the date
  window (two $50 Zelles for a $75 monthly lawn row due later) is now a proposal on that month's row, or the next
  month's: **Reduce the row by the payment**, or clear it. Before, it was listed as unknown and the row had to be
  edited by hand.
- **A steady payment caught only by a "track" rule is pointed out** (#95): the same payee paid in three months in a
  row at about the same amount (two when the bank calls it a bill payment or autopay) gets a "not budgeted" note
  with **Add it to the bills** and **Ignore until it posts again**.
- **Agents can read what a reconciliation did** (#93): `get_reconciliation` and `GET /api/v1/reconciliations/{id}`
  list each bank line, auto-matched, proposed (with the decision), tracked or unknown with the reason, plus who
  applied it and the balance before and after. Kept from now on; older ones show their report.
- **`set_series_amount` can preview and stop at a date** (#94): `dry_run` returns the rows it would change and the
  forecast low before and after; `to_date` changes only the rows up to it (a one-off month or a promo period).
- **Optional bank connection through Plaid** (D-091, docs/PLAID.md). Off by default, and then the app still makes
  no outbound connections. With `HBUDGET_PLAID` set on the server and turned on by a household owner (More > Bank
  connection), link your bank on Plaid's page, choose which checking account is reconciled and which cards go to
  the Card page, and press **Sync with the bank** in the weekly check-in. The sync arrives as a waiting bank upload,
  reviewed like a file, with the balance checked against Plaid's. Setup: docs/DEPLOY.md.

## 0.13.1 - 2026-11-05

### Fixed
- **Deleting an upload no longer deletes its bank history** (#88). **Hide** takes it off the list and keeps its
  postings in Report, Check-up, Recap and Subscriptions. For a wrong file, **Delete with postings…** first says how
  many postings go and over which dates, then deletes them. Agents: `delete_upload` hides unless `with_postings` is
  set, and `upload_removal` reports what would go.
- **Overlapping uploads no longer store the same bank or card posting twice** (#87). A "Transaction ID" column (and
  "Transaction Number", "Reference Number") is now recognised, the column-mapping form has a Transaction ID picker,
  and a saved layout made without one picks it up. When one file has the bank's ids and an overlapping one does not,
  a posting with the same date, amount and description is matched instead of added. Postings already stored twice
  are merged once when the app starts, which puts Check-up, Card, Recap and Subscriptions right again.

## 0.13.0 - 2026-11-05

### Added
- **AI assistants and the API can mark a row already paid:** the `mark_row_done` tool and `POST /api/v1/rows/{id}/done`,
  the same as Already paid / Already received on the Bills page; `undo_row_change` opens the row again.

### Fixed
- **Plan > Goals** stayed on "Loading…" and showed an error after saving a goal (the goal was saved, but the page
  could not show it). The browser test now opens every page and checks none is left loading.

## 0.12.0 - 2026-11-05

### Added
- **Already paid / Already received** on a Bills row (and **Already done** for ticked rows under Matching bills):
  for a row that is already in the bank but was not matched when reconciling, such as a refund. It leaves the
  forecast without putting money back in the budget, stays listed until the next reconciliation and can be undone.

### Changed
- **Search on every page:** a search box at the end of the tab row (on its own line on phones) filters what is on
  the page as you type: bill rows, list items and table lines. It shows only where there is something to search;
  `/` jumps to it and Escape clears it. On **Bills > By payday** it also lists every matching open row, beyond the
  weeks shown, with the tick boxes and bulk bar of the old Find bills box, which it replaces.
- **Remove (won't be paid)** is now **Won't be paid** (or **Won't come in** for money in), and **Won't happen** for
  ticked rows.

## 0.11.0 - 2026-11-05

### Added
- **Recap** (Money > Recap): the monthly recap and the year in review, from the bank and card history and the
  bills: money in, money out and what was kept next to the period before, card spending by category against its
  budget, where the money went, bills paid and any still open, new subscriptions, price changes and wins. Earlier and
  Later move between the months (or years) that have bank history. `get_recap` for agents, `GET /api/v1/recap`.
- **Offline page**: when the server cannot be reached (a phone away from the home network), the installed app shows
  "You're offline" with a Try again button instead of the browser's error. Nothing from the budget is stored on the
  device. Needs HTTPS (or localhost) for the browser to allow it.
- **System** box on the Admin page: the app, Python and SQLite versions, how long the server has been running, the
  database size and schema version, what is stored, the newest scheduled backup and its age (with a warning when
  there is none or it is more than 36 hours old) and the free disk space. `GET /api/admin/system` (admins only).
- **Cash-flow calendar** (Plan > Calendar): the forecast laid out as a month, with what comes in and goes out each
  day (expected items and "Move to card" included, late rows on today) and the checkbook balance at the end of the
  day; days under the cushion or below zero are marked, with the month's money in, money out, start and end
  balance and lowest day. Weeks start on the day chosen in Settings. On phones it is a list of the busy days.
- **Notes on bills**: a recurring item has a Notes box on Pay & bills (shown under its name there, and when you
  point at the bill on Bills by payday), and one bill row can have its own note ("paid by phone"), written in the
  row's Change box and shown next to its due date. `set_bill_note` for agents, `POST /api/v1/bills/{id}/note`.

### Fixed
- Saving a recurring item on Pay & bills no longer wipes the notes it had (from an import or the API): the form had
  no Notes field and saved an empty one.

## 0.10.0 - 2026-11-04

### Changed
- **Subscriptions** are less noisy (#61, #62): a bill that drops in price or skips a month is no longer "stopped";
  a 1-cent change is not a price change; a restaurant visited twice for the same amount is not a subscription
  (three charges are needed when a store is also paid other amounts); a late charge whose payee still charges is
  "Irregular"; PayPal and similar payees say they may be several services. Bills and loans (bank charges a bill
  rule matches) are marked "Bill" and the monthly total is split into bills and loans and subscriptions.

### Fixed
- Changing a bill's amount (or schedule) and its card in one save left the rows changed by hand on the old card,
  so card transfers and the "not scheduled" alert missed them (#60). They now take the new card and name, also in
  a bill-system merge. Migration 0009 repairs rows already on the wrong card (it runs on start).
- Card purchases are dated by the purchase date when the description carries one ("... Date 10/30/26 ..."), also
  with the built-in layouts, OFX files and uploads through the API, instead of by the posting date (#63). A layout's
  own purchase-date pattern still wins; a date more than 60 days before the posting is not believed. Purchases
  already stored by posting date are re-dated on the next card upload.
- A bill-system merge no longer brings back a bill already paid: when the file's schedule for a bill starts before
  the household's first row of it, the bill starts at that row (#64). The merge preview (Import page and
  `hbudget import-bills --dry-run`) now lists the rows it adds, removes and changes, with the low and end before
  and after, and flags added rows that fall before rows the household already has ("check: may already be paid").

## 0.9.0 - 2026-11-04

### Added
- **"Not a subscription"** on the Subscriptions page: a charge that only looks like a subscription can be hidden;
  it moves to "Not subscriptions", out of the totals, also when it is charged again, and "Show again" undoes it.
  Kept in backups and restores. `hide_recurring_charge` for agents, `PUT /api/v1/recurring/hidden`.
- **Move card money on payday** (Settings, off by default): the forecast takes card-funded bills and spending out
  of checking on the payday that opens their pay period, as one "Move to <card>" per card, instead of each on its
  due date, so the low balance shows when the card money really leaves. The rows, reconcile and the totals are
  unchanged. Kept in backups and restores.

### Fixed
- The image no longer contains apt or libapt-pkg (it already had no apt programs), so it is not flagged for
  CVE-2026-109518 while the base image still ships apt 3.0.3.

## 0.8.0 - 2026-11-04

### Added
- **Card transfers** (#31): Home and Bills > By payday show, per pay period and per card, "Move to Card A: $X" with
  the card-funded bills and spending it covers, so nobody has to add them up by hand. `get_paydays` has
  `card_transfers` per period, and `card_transfer_plan` (`GET /api/v1/cards/{id}/transfer`) gives the total through
  a date. The forecast is unchanged: each card-funded row still counts on its own due date.
- **Subscriptions** (Money > Subscriptions, #34): charges that repeat every month or year at the same amount, found
  in the bank and card history, with the next expected date, the cost per month (yearly ones spread over 12) and
  the monthly and yearly totals. A price change (old → new, and when), a new charge and one that stopped are
  flagged, and each says what it is budgeted as (a payee rule's budget line, or a card category) or "Not in the
  budget". `list_recurring_charges` for agents, `GET /api/v1/recurring`. Nothing is stored; no migration.
- **Card recategorising** (Card tab > Stores): move a store to another category, and all its purchases go there,
  now and in later uploads; open a store to move a single purchase, which then stays put when the keywords change
  ("Sort automatically" undoes it). Click a category under "Where the card money went" to see its stores. Store and
  purchase moves are in backups and restores and go with "Delete all budget data". Needs migration 0008 (two new
  columns; it runs on start).
- **Card tools for agents** (#33): `get_card_spending` (per category and week or month, with the budget),
  `list_card_stores`, `list_card_purchases`, `list_card_categories`, and, with a write token,
  `set_merchant_category` and `set_purchase_category`; the same under `/api/v1/card/...`.

## 0.7.0 - 2026-11-04

### Added
- **Weekly check-in** on Home: bring in the bank file (or review the one waiting), mark the bills due in the next 7
  days as scheduled or autopay, see what others changed since your last check-in, then "Done for this week". It
  shows who checked in last; there are no streaks and nothing about weeks that were skipped.
- **Goals** (Plan > Goals, and a card on Home): an emergency fund of N months of bills, savings targets (with what is
  needed each month to make a date) and a debt-free countdown from the payoff plan, each with a progress bar. Saved
  amounts are typed in; the debt-free goal follows the Debts tab. Included in backups; `get_goals` for agents.
- **Wins** on Home: a debt paid off, a goal reached, 4 / 8 / 13 / 26 / 52 weeks in a row above the cushion, and a
  month that spent less than the budget planned. Each new one gets a small celebration once ("Nice!" keeps it in the
  history); "Hide wins" turns them off, and More > Settings turns them back on.

## 0.6.1 - 2026-11-04

### Changed
- **Pay & bills** (was "All bills") lists pay, bills and spending budgets separately, so paychecks are no longer read
  as bills; it points to Debts for what is owed on cards and loans.

### Fixed
- The Debts table fits a laptop-width page: a promo's terms sit on a second line under the APR and dates are
  short, so Edit and Delete are in view without scrolling sideways (#47).
- On a phone the Report's chart starts folded under "Show the chart", so the breakdown with every figure comes
  first instead of a chart cut off at the money-in side (#48).

## 0.6.0 - 2026-11-03

### Added
- **Debt burn-down** on the Debts page: the total owed month by month with this plan, the other method and payments
  only, each debt's payoff marked on the line, and an "Actual" line from the balances entered over time (with a
  table view). Each debt now keeps its past balances whenever its balance is updated, on the page or through the
  API; backups include them.

### Changed
- The Bills section's tabs are now **By payday**, **All bills** (every recurring item, until now "Income and
  bills"), **Debts** (moved here from Plan, with the payoff plan and burn-down) and **Check-up**. Plan keeps What if.
- On a phone, tables show each row as a small card with the column names as labels (long scrolling tables, such
  as the forecast table, stay tables).
- The balance charts can be read with the keyboard: Tab to a chart, then the arrow keys (Page Up/Down, Home, End)
  move through the dates and the balance on each is read out.

### Fixed
- The Card page's weekly budget for a category no longer reads $0 for past weeks (so every week showed "Over"):
  it comes from the linked spending lines' schedules, and a week with no budget shows the amount alone (#45).

## 0.5.0 - 2026-11-03

### Added
- The app can be installed on a phone's home screen (web app manifest, icons, theme colour).
- **Find bills** on the Bills page: every open row in the forecast whose name matches, not only the next weeks.
  Tick rows to set their amount, mark their status or remove them together; if any of them changed since the page
  loaded, nothing is saved.
- **Last 2 months** views on the Report and Card pages, and weekly bars for "Track only" payees on the Check-up.

### Changed
- **Settings** has its own page under More (checkbook balance, alerts, matching, forecast length, colours); the
  Rules page now holds only payee rules. Spending caps are set on the Check-up page and report groups on the Report
  page, each saved as soon as it is added or removed.
- Pages load their CSS and JavaScript from a path that changes with every release, so a browser never mixes old
  files with a new version after an upgrade; those files are cached for a year.

### Fixed
- The Report's monthly figures no longer divide by 12 when the bank history is shorter: they are averages over the
  full months it covers, and a partial first month is left out.
- Edit and Delete buttons in the Debts, Rules, recurring items and admin tables stay on one line instead of making
  every row very tall.

## 0.4.0 - 2026-11-03

### Added
- API and agent access (asked for by an agent using 0.3.0):
  - a CSV export in a new layout can be read with a column mapping and saved as a layout; the refusal suggests one;
  - list the recurring items and change one's card, status, group or notes;
  - list, test, add, change, delete and reorder payee rules.
- An **Allowance** payee rule for a monthly row used up by several payments (subscriptions billed through one
  processor, a fuel or grocery envelope): each payment takes its amount off that month's row, a payment bigger than
  what is left spills into the next row, and once the bank export passes the end of the month the reconcile offers to
  release the leftover. It is never released on its own.

- Undo the newest reconciliation (Reconcile page history, the API and MCP): its rows reopen with their amounts,
  the checkbook balance goes back and its upload can be reconciled again. Refused when something it touched changed.
- API and agent access: change a recurring item's amount from a date on or by season, update a debt's balance and
  terms (old balances are flagged), and read what changed since the last sync.

### Fixed
- Changing only a recurring item's card, status, group, notes or "paid from" no longer resets its open rows to the
  item's amount (rows imported from a workbook keep theirs).

## 0.3.0 - 2026-11-03

### Added
- API and agent access: write tokens can change, add, remove (undoably) and move rows, and upload and reconcile
  bank exports (dry run, then apply), as an editor can on the Bills and Reconcile pages (docs/API.md).

## 0.2.1 - 2026-11-03

### Fixed
- Bills paid with a credit card no longer show as "not scheduled" in the alerts and the summary strip.
- The balance chart no longer goes back in time when late bills are still open: they count on the balance date.
- API and agent access (found by an agent using 0.2.0):
  - debts leave out checking and savings accounts, and include a promo's APR after and deferred interest;
  - the payoff plan includes the promo verdicts and the debts whose payment does not cover the interest;
  - the forecast's `low` is the low within the requested days (`horizon_low` is the whole forecast's), and `days`
    can reach the end of the forecast (up to 2000);
  - bill rows have a positive `amount` with a `direction`, and show the paying card;
  - a what-if dated before today is refused instead of counted again.

## 0.2.0 - 2026-11-03

### Added
- API access for other programs and AI agents: the owner makes read or write access tokens under More → API
  access; a REST API at `/api/v1` with an OpenAPI description; an MCP server at `/mcp` for Claude Code and other
  agents (docs/API.md). Token changes are in the activity log under the token's name.
- Navigation in five sections (Home, Bills, Plan, Money, More) with a bottom bar on phones, and a Home page:
  money left before the next payday and what is due before it, the next 60 days, the paydays ahead and the
  alerts. Sign-in lands on Home.
- Released images on GitHub's container registry (amd64 and arm64, with an SBOM and build provenance);
  `docker-compose.yml` runs the released image, and `compose.build.yml` builds from source instead.
- `scripts/upgrade.sh`: back up, pull, restart, wait until healthy, and roll back if the new version does not
  start.
- Dependabot updates for the base image and the GitHub Actions; a weekly scheduled CI run.
- `hbudget backup`: an online copy of the database with an integrity check and retention, and a documented
  nightly timer.
- Summary strip: "Not scheduled" (bills due within 7 days with no status) and open expected items under the
  forecast low.
- Report: pay total, the biggest group, how much of the spending pay covers, and what is left over.
- Debts: total without the mortgage; payoff settings are remembered and recalculate while typing; the other
  method's debt-free date.
- Charts: area fill, a dot per series on hover, and the row behind each step in the tooltip; a legend and a
  table view on the what-if chart.

### Changed
- Touch screens get 44px targets and 16px fields; muted text, chips and field borders meet WCAG AA contrast;
  notched phones are padded; the theme follows the system setting live.
- A status message appears next to the form that was used; buttons show "Saving…"; "click again to delete"
  resets after a few seconds.
- Error pages link back to the household.
- Faster history scans (the summary, Report and Check-up) with two years of bank exports.
- Docker logs are capped at 3 × 10 MB per container.

### Fixed
- Empty boxes on Reconcile, Import and Series.
- The Bills change editor lost the typed amount when a phone keyboard opened.
- Bill status buttons overlapped "Change" on narrow phones.
- Spending caps made in the app showed as "cap $0.00" on Check-up.

## 0.1.0 - 2026-11-02

First version: checkbook forecast, bills by payday, expected items and what-if, debts and payoff plans, the
Report flow, Check-up, card spending, bank reconciliation, imports and exports, households with roles, HTTPS
with your own certificate or behind a proxy, and Docker deployment.
