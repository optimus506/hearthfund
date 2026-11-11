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
| What each linked account is for | *Checking: reconcile with the budget* (one per household), *Card: add to the Card page*, or *Not used* | the first checking account is reconciled, credit cards go to the Card page, the rest is not used |
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
   `/accounts/get` for the balances. Pending transactions are left out and counted.
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
