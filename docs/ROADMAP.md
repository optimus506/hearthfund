# Roadmap

The plan after the November 2026 review: the app compared with the single-page dashboard it grew out of, a UI and
information-architecture review, and a web platform and operations review. Decisions are recorded in
[DECISIONS.md](../DECISIONS.md) as each item lands; this file says what is planned and in what order. Issue and
pull-request numbers such as #87 that appear before 1.0 refer to the project's earlier private tracker; those
issues are not public, and the public tracker numbers its issues from #1 again.

**Decided (2026-11-03):**
- Navigation becomes five items: **Home · Bills · Plan · Money · More**, with a bottom bar on phones.
- Releases are published as images on GHCR; deployments run `docker compose pull` (building locally stays
  possible for development).
- Engagement work starts with the weekly check-in, goals, the debt burn-down chart and milestones that
  celebrate wins. Gentle and optional: no streak-loss or guilt messages.
- Phase 2 adds API access for other programs and AI agents: scoped access tokens, a versioned REST API and
  an MCP endpoint.

Sizes: S = under a day, M = a few days, L = a week or more.

## Phase 1: fixes and quick wins (no design decisions)

- [x] **Bugs**
  - [x] `[hidden]` is overridden by `.card { display: grid }`: empty boxes on Reconcile, Import and Series (S)
  - [x] Bills: the change editor loses the typed amount when the page re-renders (phone keyboard resize) (S)
  - [x] Bills on phones: the status buttons overlap "Change" at 520px and below (S)
- [x] **Touch and readability**
  - [x] 44px targets on touch screens; 16px inputs (stops iOS zooming into fields) (S)
  - [x] Contrast: muted text, chips, group-header text and input borders to WCAG AA (S)
  - [x] Safe-area insets for notched phones; follow the system theme live (S)
- [x] **Feedback**
  - [x] Status message next to the form that was used; buttons show "Saving…" and are disabled meanwhile (S)
  - [x] "Click again to delete" resets after a few seconds and is announced to screen readers (S)
  - [x] Error pages link back to the household home (S)
- [x] **Dashboard polish**
  - [x] Summary strip: "Not scheduled" (count and total due within 7 days); "with N expected" under the low (S)
  - [x] Charts: area fill, hover dots, the row name in the tooltip; legend and table view on the what-if chart;
        a height-limited scrolling table under the balance chart (S)
  - [x] Debts: "Total without mortgage"; payoff settings remembered; recalculate while typing (S)
  - [x] Verdicts: date of today's low (what-if), payoff month (promo), the other method's debt-free date (S)
  - [x] Card: "over budget in N of M weeks" (already computed, not shown) (S)
  - [x] Report: pay total, biggest group, "pay covers N%", percentages and a left-over line in the breakdown (S)
  - [x] Check-up: month names for monthly bills, pay-row wording (S)
- [x] **Operations**
  - [x] Index `upload_txns(household_id, txn_id)` and `card_upload_txns(household_id, txn_id)` (history scans) (S)
  - [x] Cap Docker's log size in `docker-compose.yml` (S)
  - [x] nginx example: `X-Forwarded-For $remote_addr` (S)
  - [x] `hbudget backup --keep N` (online backup, integrity check, retention) and a documented nightly timer (M)

## Phase 2: organisation, Home and releases

- [x] **Five-item navigation** with a phone bottom bar; role-aware; household switcher in the header (M)
- [x] **Home dashboard**: balance, forecast low, money left until next payday, a sparkline, a payday timeline,
      alerts shown here instead of on every page; `/h/{id}` lands on Home (M)
- [x] **Split Rules into Rules and Settings**; caps move to Check-up, report groups to Report (M)
- [x] **Bill finder** across all weeks, then the **bulk change bar** (one transaction, stale-check) (M + M)
- [x] **"Last 2 months"** views on Report and Card; weekly bars for tracked payees on Check-up (M)
  - [x] Report per-month figures divide by 12 even when the bank history is shorter (decide partial-month
        counting together with the "last 2 months" window) (S)
- [x] **Small layout follow-ups from Phase 1**: the Debts action column wraps (very tall rows) (S)
  - [x] Chart "low" labels near the start no longer run into the axis labels
- [x] **Phone tables as stacked cards**; keyboard-accessible charts with table fallbacks everywhere (M)
- [x] **Releases on GHCR**: one version source, tags `vX.Y.Z`, `CHANGELOG.md`, a release workflow building
      amd64 + arm64 with SBOM and provenance; `docker-compose.yml` uses the published image, with `build: .`
      moved to a development override; upgrade docs become `docker compose pull && docker compose up -d` (M)
- [x] **API and agent access** (requested 2026-11-03). Today only a signed-in browser can use the API
      (session cookie + CSRF token, OpenAPI turned off).
  - [x] **Access tokens**: created per household by the owner (Settings → API access), named, scoped
        (`read`, `write`), optional expiry, shown once and stored hashed, revocable, "last used" shown. Sent as
        `Authorization: Bearer`; never a cookie, so CSRF does not apply. A token can never do more than its
        creator's role; every write is in the audit log under the token's name; its own rate limit (M)
  - [x] **Versioned REST API** `/api/v1`: a stable, documented contract separate from the pages' internal
        endpoints (summary, ledger/upcoming bills, forecast, what-if, expected items, bill status, debts and
        payoff, uploads with dry-run reconcile, report data). OpenAPI spec and docs served to signed-in users and
        token holders only (M)
  - [x] **MCP endpoint for AI agents** `/mcp` (Streamable HTTP, bearer token): tools such as get summary, list
        upcoming bills, forecast, "can we afford it?", add an expected item, set a bill's status, upload a bank
        export as a dry run. Read-only tokens get read tools only; no delete tools at first; writes are small,
        reversible and audited. Text from bank files is returned as data, labelled as such, because an agent may
        treat it as instructions (M)
  - [x] **Docs**: connecting Claude Code or another agent on the LAN with a token; what is needed for hosted
        agents (claude.ai connectors call from the internet, so the app must be reachable there and usually needs
        OAuth: a separate decision, not on by default) (S)
  - [x] Later: bank-file upload with a dry-run reconcile through the API and an MCP tool (left out of the first
        version: it stores an upload and needs multipart or base64 input)
- [x] **Safe upgrades**: `scripts/upgrade.sh` (backup, keep the previous image, pull, wait for healthy, roll back
      otherwise) (S)
- [x] **Fresh assets after upgrades**: versioned static paths with long-lived caching (S–M)
- [x] **Install on phones**: web app manifest, icons, theme colour (S)
- [x] **Dependency updates**: Dependabot (base image digest, actions), weekly scheduled CI scan (S)

## Phase 3: engagement and reports

- [x] **Weekly check-in** on Home: upload the bank file → confirm matches → schedule what is due → done; records
      who checked in and when; shows "since your last visit" from the audit log (M)
- [x] **Goals**: emergency fund in months of bills, savings targets, debt-free countdown, with progress bars (M)
- [x] **Debt burn-down chart**: balances by month with paid-off milestones, comparing the chosen method, the
      other method and payments only (M)
- [x] **Milestones and wins**: a debt paid off, a goal reached, weeks above the cushion, a month under budget;
      shown on Home with a small celebration, kept in a history; can be turned off (M)
- [x] **Card recategorising**: move a store, or one purchase, to another category; included in backup,
      restore and wipe (L, migration)
- [x] **Subscriptions** (#34): recurring charges found in the bank and card history, with price changes, new and
      stopped ones, and what each is budgeted as (M)
- [ ] Later candidates (2026-11-05: the ones that needed no decisions were done first)
  - [x] Offline page; the service worker stores no budget data (D-089)
  - [x] Admin System page: versions, uptime, database, backups, disk
  - [x] Cash-flow calendar (Plan > Calendar)
  - [x] Monthly recap and year in review (Money > Recap, D-090)
  - [x] Notes on bills: item notes and a note per row
  - [x] Starter rules: ready-made payee rules for common payees, added in one step (D-094)

## Optional bank connection through Plaid

- [x] **Plaid, off by default** (D-091, [PLAID.md](PLAID.md)): the app stays offline unless the server enables
      Plaid and a household owner turns it on; a sync becomes an ordinary upload, so the check-in and
      reconciliation are unchanged. Manual sync, checking account and card (L)
- [x] Daily sync that only stores; the check-in brings the postings in (D-092)

## Going public: 1.0

- [x] Personal data out of tests, examples and docs; the deny-list scan reports nothing on the tree
- [x] GitHub Actions pinned by commit; the browser test runs on every release (D-055, D-093)
- [x] What 1.x keeps compatible ([STABILITY.md](STABILITY.md)); an older release refuses newer data
- [x] Security policy, issue templates, contributor conduct
- [x] Two-factor sign-in (TOTP with recovery codes), optional per user (D-095)
- [x] Screenshots of the demo household in the README; install without cloning the repository
- [ ] A second household installs from the public README with its own bank's export

## Next, after 1.0

In rough order. Open an issue to argue for a different one.

- [ ] **Budget vs actual per bill**: what each bill was planned at against what the bank paid, month by month
      (Check-up compares the latest payment today)
- [ ] **Category trends with caps**: spending per category over the months, against the caps
- [ ] **Other currencies and formats**: a currency symbol and number and date formats per household (1.0 is US
      dollars and US formats only)
- [ ] **More starter rule packs**: other countries, and packs shared by households
- [ ] **Sign-in with passkeys**
- [ ] **Bill-due reminders** by e-mail or a push service the household configures (off by default: the app
      makes no outbound calls)

## Not carried over from the earlier dashboard

Hard-coded creditor, store and person names (private data); web fonts from a third-party CDN (the app makes
no outbound calls); partial bulk saves; detecting the mortgage by its name; detecting read-only users from a
failed write.
