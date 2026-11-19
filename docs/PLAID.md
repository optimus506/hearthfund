# Plaid bank connection

Status: **built** (D-091). Setup steps are in [DEPLOY.md](DEPLOY.md), "Bank connection through Plaid".

**Before you start: most households do not need this.** Uploading the bank's own export (CSV, OFX or QFX) is
the normal way to use the app and needs nothing from anyone. Plaid is for a bank with no usable export, and it
asks a lot of you:

- **Your own Plaid developer account.** The app ships with no Plaid credentials and the maintainers never share
  theirs. You sign up with Plaid yourself and put your own client ID and secret on your server.
- **Production approval for real banks.** A new account can only reach Plaid's test banks. Access to real banks
  means applying to Plaid, answering their questions about how the data is used, and waiting: days at best,
  sometimes weeks, and some banks need a further approval of their own.
- **It may cost money.** Plaid bills per linked bank. Check their current pricing before you apply.
- **Your data then passes through Plaid.** That is the trade for not downloading files by hand.

## Why

Some banks are not on SimpleFIN and have no usable CSV or OFX export. For those households, Plaid can pull
transactions straight from the bank. The app's rule is **no outbound network calls** (ARCHITECTURE.md sections 1
and 16). Offline stays the default; Plaid is something a household **chooses to turn on**.

## Two modes

| | **Offline** (default) | **Plaid** (opt-in) |
|---|---|---|
| Outbound connections | none | Plaid's API host for the chosen environment only, plus Plaid Link's script on the Bank connection page |
| How bank data arrives | file upload (CSV, OFX, QFX) | file upload **or** "Sync with the bank" |
| Content-Security-Policy | unchanged | unchanged everywhere except `/h/{hid}/connections` and `/plaid/oauth` |
| Secrets stored | none beyond the session key | Plaid access tokens, sealed |

Offline is exactly what it was before. With Plaid off, `hbudget.io.plaid` and `cryptography` are never imported
and no Plaid route exists (a test checks this in a fresh process). The whole test suite runs with a socket guard
that fails any connection off the machine.

### Turning it on (two switches)

1. **Server:** `HBUDGET_PLAID=sandbox|production`, `HBUDGET_PLAID_CLIENT_ID` and `HBUDGET_PLAID_SECRET` (or
   `HBUDGET_PLAID_SECRET_FILE`). With Plaid on and a credential missing, the app refuses to start.
2. **Household:** an **owner** opens More > **Bank connection**, reads what it means and types the household name.
   Turning it off unlinks every bank at Plaid (`/item/remove`) and deletes the tokens; the transactions already
   brought in stay in the history.

A server with Plaid on can still have households that stay offline.

## Choices the household makes

| Choice | Options | Default |
|---|---|---|
| What each linked account is for | *Checking: reconcile with the budget* (one per household), *Card: add to the Card page*, *Investment: keep its balance on Money > Investments*, or *Not used* | the first checking account is reconciled, credit cards go to the Card page, the rest is not used |
| How far back the first sync reaches | 90, 180, 365 or 730 days (chosen when linking) | 90 |
| Who can sync | editors and owners; linking, roles, the daily sync and turning it off are owner-only | |
| Sync automatically | *Off: only when someone syncs* or *Daily* (D-092) | Off |

Decided with the household (2026-11-09): sync by hand in the weekly check-in or on the Bank connection page, with
an optional **daily sync** that only stores what Plaid sends (below). Only the checking account and the credit card
come through Plaid.
Banks that sign you in on their own website (OAuth) need the HTTPS public address.

## How a sync fits what already exists

A sync **creates uploads**, so matching, proposals, the check-in, Subscriptions, Recap and the audit log are
unchanged:

1. `/transactions/sync` from the saved cursor (all pages; restarted if Plaid changes during paging), then
   `/accounts/get` for the balances. Pending transactions are left out of the budget and counted; their amount
   and date (no description) are kept in `plaid_pending` until Plaid withdraws them, only to explain a balance
   gap (below).
2. Each posted transaction is kept in `plaid_txns` in the bank's sign, with the description masked. Plaid's
   original description is used because payee rules were written against the bank statement's text.
3. **Checking account:** a waiting bank upload with the account's postings from a week before the last
   reconciliation. Plaid sends no running balance, so it is worked back from Plaid's current balance. It is kept
   only when the checkbook's start balance appears in it, so the normal **balance-chain check and basis search**
   run, and the note says "Balance checked". Otherwise the upload has no balances and reconcile matches by date and
   the transactions already seen, as for a CSV without a balance column. The note says why. No upload is made when
   nothing is new.
4. **Card accounts:** the new postings become an imported card upload (purchase date = Plaid's authorized date).
5. **Already stored from a file:** a Plaid posting with the same date and amount as one stored from a file keeps
   the file's id (once, and remembered), so moving from files to Plaid counts nothing twice.
6. **Changed or withdrawn by the bank:** counted and reported. Nothing already stored is changed silently.
7. **One waiting bank upload at a time:** if a sync's bank upload is still waiting (not reconciled) when the next
   one runs, it is kept when it holds exactly the same postings, and otherwise deleted and replaced by one that
   holds them all (and none the bank withdrew). Reviewing late never means stacked or overlapping uploads.

## Investment balances

An owner can set any linked account to **Investment: keep its balance on Money > Investments** (D-102). The app
then makes an investment account named after the bank and the account, and every sync, the daily one included,
notes the balance Plaid reports for that day (`/accounts/get`, which a sync calls anyway and Plaid does not
charge for). Only the balance is used: no holdings, and none of the account's transactions are brought in.

- Plaid refreshes that balance about once a day, so it can be a day behind the institution's own site.
- Switching the account back to *Not used* stops the updates and keeps the history; switching it on again
  continues the same investment account. Deleting the investment account on Money > Investments ends it.
- **Which accounts can be linked:** the app links with Plaid's Transactions product, so Plaid Link offers the
  institutions and accounts that support it. An investment account at a bank that is already linked usually
  shows up there; a broker or plan administrator that only supports Plaid's Investments product (a separate,
  paid product) cannot be linked this way. Type those balances in on Money > Investments instead.

## Debt balances

On More > Bank connection, a linked card or loan has a second choice: **Keeps the balance of (a debt)** (D-105).
Every sync, the daily one included, then writes what the bank says is owed to that debt on Bills >
Debts, with the day as its date; the earlier balance goes to the debt's history. It uses the same free
`/accounts/get` balance as above and no new Plaid product.

- It does not depend on what the account is used for: a card linked for its purchases (*Card*) can keep its
  debt's balance too.
- A credit on a card (a negative balance) is stored as nothing owed.
- Choosing *Debt balance: not kept* stops it; the debt keeps its last balance and is yours to update again.
  Deleting the debt ends it as well.
- Debts kept this way never come up in the reminder to update balances while the link works. If the bank stops
  answering, the balance's date ages like a typed one and the reminder covers it.

## Rates, minimum payments and due dates (optional, paid)

Plaid's **Liabilities** product reports, for a bank's cards and loans, the interest rate, the minimum payment,
the next due date and the last statement balance. It is off unless two people turn it on (D-106):

1. **The server:** `HBUDGET_PLAID_LIABILITIES=true`, after requesting Liabilities in the Plaid dashboard.
2. **The household's owner, per bank:** More > Bank connection > *Also read rates, minimum payments and due
   dates* > *Allow it*. The bank's sign-in page opens (Plaid Link, update mode) and asks to allow it.

From then on every sync, the daily one included, asks Plaid for them (`/liabilities/get`). What comes back shows
beside each account on the Bank connection page. A debt whose balance the account keeps (see above) takes the
rate and the minimum payment, and the Debts page shows its next due date. While a debt's promo rate runs, its
rate is left alone: the bank reports the card's standard rate.

- **Cost:** Plaid bills Liabilities per linked bank and month, from the first read until that bank is
  unlinked. Stopping it in the app stops the reads and forgets what was read; it does not end Plaid's billing.
  Unlinking the bank does.
- **Not every bank offers it.** When one does not, or the Plaid account lacks the product, the bank's card on
  the page says so in plain words and the rest of the sync works as before.
- Only the four figures are read. Account numbers, addresses and the other fields Plaid sends are not stored.
- This part is tested against a stand-in for Plaid, not yet against a real bank.

## When the bank's balance and the checkbook differ

Plaid's balance (`/accounts/get`) can run ahead of the postings it has delivered (`/transactions/sync`): a
deposit shows in the balance in the morning and arrives as a posting later that day or the next. A bare "Nothing
new" would then leave two different balances with no reason. So after every sync, on **More > Bank connection**
and in the check-in, the app compares the linked checking account's balance with the checkbook's and says which
case it is (D-100):

| Case | What it says |
|---|---|
| The balances agree | nothing more |
| The newest postings lead from the checkbook's balance to the bank's | they make up the difference: review the upload |
| The bank already counts pending transactions | the pending transactions make up the difference; they are counted when they post |
| Neither | the postings sent so far do not explain it; one has most likely not been sent yet: sync again later before looking for a mistake |

A pending transaction more than 14 days old is ignored. An agent can read the same through the `get_bank_connection`
tool or `GET /api/v1/bank-connection` (docs/API.md): the linked banks, the last sync, any problem, and the balance
with its reason. Neither returns a token or a Plaid id.

## Refresh from the bank now (optional, paid)

Plaid fetches from each bank on its own schedule, a few times a day; a sync only reads what Plaid already has. With
`HBUDGET_PLAID_REFRESH=true` on the server, **More > Bank connection** gets a **Refresh from the bank now** button,
and the check-in offers it when the bank's balance is ahead and nothing sent so far explains it. It asks Plaid to
fetch from the bank at once (`/transactions/refresh`, usually under ten seconds), then runs the normal sync (D-101).

- **It is a Plaid add-on with its own fee, charged per request.** Request it in the Plaid dashboard (Products) and
  check the price on your Plaid account before turning the setting on. The setting is off by default, so nothing
  is ever charged without the person who runs the server deciding so.
- **One request an hour per bank.** A second press within the hour only syncs, and says so.
- **When Plaid refuses,** the app says why in plain words (the add-on is not on this Plaid account; this bank does
  not support it; asked too often) and still syncs. A refused request does not use up the hour; one that got no
  answer does, because Plaid may have carried it out.
- An editor can use it; every refresh is in the audit log. Over MCP: `sync_bank` with `refresh: true`.

## The daily sync

Off unless an owner chooses **Daily** on the Bank connection page. About once a day (looked for hourly, so a
restart never skips a day; a bank synced by hand in the last 20 hours is left alone) the server does steps 1, 2
and 6 above for each linked bank: it **only stores** what Plaid sent. No upload is made and nothing in the budget
changes. The check-in then shows "Bring in the new bank transactions" with the count, and **Bring them in** syncs
once more and makes one bank upload with everything since the last reconciliation (steps 3 to 5, 7), however many
days it covers, plus a card upload with every card posting not yet brought in. A sign-in problem found by the daily
sync shows in the check-in with a link to the Bank connection page. The daily sync's own audit entries are not
listed as "changes by others".

## Security and privacy

- **Tokens:** in `plaid_items`, sealed with AES-GCM. The key is derived from the server's secret key, and the
  household and item ids are the associated data. They are never in backups, exports or API answers. A database
  moved without its secret key needs the bank linked again.
- **One host:** the client posts only to `sandbox.plaid.com` or `production.plaid.com` over TLS with certificate
  checks, and refuses redirects. It uses the standard library (`urllib`), so there is no new HTTP dependency.
- **Link pages:** only `/h/{hid}/connections` and `/plaid/oauth` allow Plaid Link's script, its frame and the API
  host, and send `Cross-Origin-Opener-Policy: same-origin-allow-popups`.
- **OAuth return:** `/plaid/oauth` needs a signed-in session (the cookie is SameSite=Lax, so it survives the
  bank's redirect). It finishes Link with the token kept in `sessionStorage` before leaving.
- **No webhooks:** they would need the server to be reachable from the internet.
- **Audit:** turning on or off, linking, unlinking, role changes and every sync (counts only) go to the audit log.
- **New dependency:** `cryptography`, with wheels for amd64 and arm64.

## Not built (yet)

- Updating a posting the bank changed after it was stored. It is reported, not applied.
