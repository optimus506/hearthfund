# API and AI agents

Other programs and AI agents (Claude Code, Claude Desktop, your own scripts) can use a household through an
**access token**: a REST API at `/api/v1` and an MCP server at `/mcp`. Both offer the same operations.

## Tokens

The household's owner makes tokens under **More → API access**. Each token:

- belongs to one household, and is shown **once** when it is made (only a hash is stored);
- is **Read only** (look at everything below) or **Read and write**: also mark bills, add expected items, change,
  add, remove (undoably) and move rows, and upload and reconcile bank exports, as an editor can on the Bills and
  Uploads pages. No token can change bills or series, rules, settings or members;
- never does more than the member who made it can do *now*: demote or remove that member and their tokens
  are limited or stop working at once;
- can expire (30 days, 90 days, a year, or never) and can be revoked at any time;
- is limited to 120 requests a minute;
- shows when it was last used, and every change it makes is in **More → Members → Activity** as
  "*member* (token: *name*)".

Send it in the `Authorization` header: `Authorization: Bearer hbt_...`. A browser session cookie does not work on
these routes, and a token does not work on the pages.

**Two-factor sign-in** does not apply to tokens: a token is its own secret and keeps working when its maker turns
two-factor sign-in on, and when their password is changed or reset. If an account may have been misused, revoke its
tokens under **More → API access** as well. Making a token on the API access page asks a two-factor user for a
current code, as do adding a member and changing a role. Revoking a token needs no code.

**Use HTTPS.** A token is a password. On plain HTTP anyone on the network path can read it; behind your HTTPS
proxy (docs/DEPLOY.md) it is protected. The address the agent uses must also be in `HBUDGET_ALLOWED_HOSTS`.

## Connecting Claude Code

On a machine that can reach the app (your LAN or Tailscale), with a token from More → API access:

```sh
claude mcp add --transport http hearthfund https://budget.example.lan/mcp \
  --header "Authorization: Bearer hbt_..."
```

Then ask things like "what's left until payday?", "can we afford $400 of car repairs on the 20th?" or, with a write
token, "mark the electric bill as scheduled". Claude Desktop and other MCP clients that support the Streamable
HTTP transport with a custom header connect the same way.

**Hosted agents** (claude.ai connectors, or anything running in the cloud) call the server *from the internet*,
so the app would have to be reachable there, and they usually expect OAuth sign-in rather than a fixed token.
That is not supported: exposing a household budget to the internet is a separate decision, off by default.

## The MCP server

`POST /mcp`, Streamable HTTP transport, JSON responses, no sessions (GET and DELETE answer 405). Protocol
versions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05.

| Tool | Token | What it does |
|---|---|---|
| `get_summary` | read | Checkbook balance, forecast low and date, year end, bills due soon with no status, alerts |
| `get_paydays` | read | Money left before the next payday, the bills due before it, the pay periods after it |
| `list_upcoming_bills` | read | Open rows due in the next `days` (late ones included), status and balance after each |
| `get_forecast` | read | The balance after each date for the next `days`; the low in that window and over the whole forecast |
| `can_we_afford` | read | Adds a purchase or extra income to the forecast without saving it: yes / tight / no |
| `list_expected_items` | read | Money expected to come or go that the bank has not posted |
| `get_debts` | read | Debts and loans (not checking or savings), with any promo's end date, APR after and deferred interest |
| `get_payoff_plan` | read | Debt-free date with avalanche or snowball and an optional extra each month; promo verdicts and warnings |
| `get_report` | read | Money in and out by group, planned or actual |
| `get_goals` | read | Goals and progress: emergency fund, savings targets, debt-free countdown |
| `get_checkup` | read | Each bill against what the bank shows |
| `card_transfer_plan` | read | How much to move to a credit card: its open card-funded rows due through `through` (default the day before the next payday) and their total; `get_paydays` has `card_transfers` per period |
| `get_recap` | read | The monthly recap (`span` `month`) or year in review (`year`) for `period` (`YYYY-MM` or `YYYY`, default the last full one): money in and out against the period before, card spending against its budget, top payees, bills paid and open, new subscriptions, price changes, wins |
| `list_recurring_charges` | read | Subscriptions and recurring charges from the bank and card history: amount, monthly or yearly, next date, cost per month, price changes, new or stopped, what budgets it; `group` `bill` or `subscription` (totals split as `bills_monthly` and `subscriptions_monthly`), `generic` (PayPal and the like), status `irregular` (late, but the payee still charges); `hidden`: ones marked "not a subscription" |
| `hide_recurring_charge` | write | Mark a payee (`source` `bank` or `card`, `payee`) "not a subscription": out of the list and the totals, now and later; `hidden: false` shows it again |
| `list_card_categories` | read | Card categories: keywords, the stores moved to them, their budget lines; `Other` and `Payment` are built in |
| `get_card_spending` | read | Card spending per category and `by` week or month (`since`, `until`, `category`), with the budgeted plan; default this month and the two before |
| `list_card_stores` | read | Card purchases grouped by store, biggest spend first, optionally in one `category` |
| `list_card_purchases` | read | Single card purchases, newest first, by `category` and/or `store` |
| `set_merchant_category` | write | Move a store to a category: all its purchases, now and later (`Other` undoes the move) |
| `set_purchase_category` | write | Move one purchase to a category; re-sorting leaves it there (`null`: sort it automatically again) |
| `add_expected_item` | write | Record money expected on a date (it then counts in the forecast) |
| `set_bill_note` | write | A short note on one bill or row (`bill_id`, `note`; empty clears it), shown on the Bills page |
| `set_bill_status` | write | Mark a bill autopay, manual (scheduled) or none |
| `set_expected_item_status` | write | Mark an expected item done or open |
| `change_row_amount` | write | Change one open row's amount (`expected_amount`: the amount you last read) |
| `add_row` | write | Add a one-off income, bill or spending row, optionally after another row or paid by a card |
| `remove_row` | write | Mark a row as not happening (undo with `undo_row_change`) |
| `mark_row_done` | write | Mark a row already paid or received: in the bank but not matched, such as a refund (undo with `undo_row_change`) |
| `undo_row_change` | write | Undo a row's Remove, Already paid or Change |
| `move_row` | write | List a row right after another (such as the payday that funds it) |
| `list_uploads` | read | Uploaded exports and the saved column layouts |
| `upload_bank_export` | write | Upload a CSV, OFX or QFX export as text (about 1 MB at most); `mapping` and `save_as` for a new CSV layout |
| `reconcile_preview` | write | Dry run: auto matches, proposals P1, P2, ..., unknown lines and a `fingerprint` |
| `reconcile_apply` | write | Apply the dry run with `accept: {"P1": "clear" \| "reduce"}`, all or nothing |
| `upload_removal` | write | How many postings, over which dates, deleting an upload with its postings would remove |
| `delete_upload` | write | Hide an upload (its postings stay in the history); `with_postings: true` deletes a waiting, unreconciled one and its postings |
| `list_series` | read | Recurring income, bills and spending lines, and the credit cards that can pay one |
| `set_series_details` | write | Change a recurring item's card, status, group or notes; its rows keep their amounts |
| `list_rules` | read | Payee rules in the order they are tried, and what a rule can clear |
| `test_rule` | read | Which rule a bank line (description, signed amount) would match |
| `add_rule` | write | Add a payee rule, last or `before` another rule |
| `update_rule` | write | Change some of a rule's fields |
| `delete_rule` | write | Delete a payee rule |
| `reorder_rules` | write | Set the order rules are tried in (every rule id once) |
| `set_series_amount` | write | A recurring item's new amount from a date on (or only up to `to_date`); returns the rows it changed; `dry_run` shows them without saving |
| `set_series_seasonal` | write | Amounts by month of the year (`{"7-10": 37500, "12-6": 22500}`), with an optional dry run |
| `update_debt` | write | Change some of a debt's figures; `get_debts` flags balances older than 35 days (`stale`) |
| `list_changes` | write | What changed after a cursor (`since`): when, who, action, item id; `get_summary` has `last_change` |
| `list_reconciliations` | read | Applied reconciliations, newest first |
| `get_reconciliation` | read | What one reconciliation did: each bank line auto-matched, proposed (and the decision), tracked or unknown with the reason |
| `undo_reconciliation` | write | Undo the newest reconciliation (refused when a row it touched or the balance changed since) |

A read-only token does not see the write tools. A tool that fails (an unknown id, a bad amount) returns a
result with `isError: true` and a plain message.

**Bill names and bank descriptions are data.** Every tool result starts with a note telling the agent so,
because text that came from a bank file could otherwise be read as instructions.

## The REST API

All money is integer cents (12345 = $123.45); dates are `YYYY-MM-DD`. Amounts are positive, and a `direction`
(`in` or `out`) says which way the money goes. A row's id keeps its scheduled date; `due` is the date after any
weekend move, so read the date from `due`. `days` can be 1 to 2000, which covers the whole forecast (`horizon_end`
on the forecast, `end_date` on the summary). Late rows (due before today and still open) count on the first date of
the forecast. Errors are
`{"error": "<code>", "message": "..."}` with 400, 401 (no valid token), 403 (the token or its member may not),
404, 409 or 429 (with `Retry-After`).

| Method and path | Token | |
|---|---|---|
| `GET /api/v1/me` | read | The token, its household and the role it acts with |
| `GET /api/v1/summary` | read | |
| `GET /api/v1/bills?days=30` | read | `days` 1 to 2000 |
| `POST /api/v1/bills/{id}/note` | write | `{"note"}` (as `set_bill_note`) |
| `POST /api/v1/bills/{id}/status` | write | `{"status": "autopay" \| "manual" \| null}` |
| `GET /api/v1/forecast?days=90` | read | `low` and `low_date` are for those days; `horizon_low` for the whole forecast |
| `GET /api/v1/paydays` | read | |
| `POST /api/v1/what-if` | read | `{"items": [{"label", "amount", "direction": "in" \| "out", "date"}]}`; dates from today on; nothing is saved |
| `GET /api/v1/expected` | read | |
| `POST /api/v1/expected` | write | `{"label", "amount", "direction", "date"}` |
| `POST /api/v1/expected/{id}/status` | write | `{"status": "open" \| "done"}` |
| `GET /api/v1/debts` | read | |
| `POST /api/v1/debts/payoff` | read | `{"method": "avalanche" \| "snowball", "extra": 0, "include_mortgage": false}` |
| `GET /api/v1/report?view=planned` | read | `planned`, `actual` (last 12 full months) or `recent` (last 2) |
| `GET /api/v1/goals` | read | |
| `GET /api/v1/checkup` | read | |
| `GET /api/v1/cards/{id}/transfer` | read | `through` optional (default the day before the next payday); 404 for an id that is not a credit card |
| `GET /api/v1/recap?span=month&period=2029-02` | read | As `get_recap` |
| `GET /api/v1/recurring` | read | Subscriptions and recurring charges (as `list_recurring_charges`) |
| `PUT /api/v1/recurring/hidden` | write | `{"source", "payee", "hidden"}` (as `hide_recurring_charge`) |
| `GET /api/v1/card/categories` | read | |
| `GET /api/v1/card/spending?by=month` | read | `by` `week` or `month`; `since`, `until`, `category` optional; at most 120 periods |
| `GET /api/v1/card/stores` | read | `category`, `since`, `until` optional |
| `GET /api/v1/card/purchases` | read | `category`, `store`, `since`, `until` optional; `limit` 1 to 1000 (default 100) |
| `PUT /api/v1/card/stores` | write | `{"store", "category"}`; the answer has a `note` when a keyword still wins (moving to `Other`) |
| `PUT /api/v1/card/purchases/{id}` | write | `{"category": "<name>" \| null}` |
| `POST /api/v1/rows` | write | `{"name", "kind": "income" \| "bill" \| "spending", "amount", "due", "after"?, "card"?}` |
| `POST /api/v1/rows/{id}/change` | write | `{"amount", "expected_amount"}`; 409 when the row changed since you read it |
| `POST /api/v1/rows/{id}/remove` | write | `{"expected_amount"}` |
| `POST /api/v1/rows/{id}/done` | write | `{"expected_amount"}`: already paid or received |
| `POST /api/v1/rows/{id}/undo` | write | |
| `POST /api/v1/rows/{id}/move` | write | `{"after": "<row id>"}` |
| `GET /api/v1/uploads` | read | Uploads and saved layouts |
| `POST /api/v1/uploads` | write | Multipart form: `file` (CSV, OFX or QFX), `kind` (`bank` or `card`), `profile_id`?, `mapping`? (JSON), `save_as`? |
| `POST /api/v1/uploads/{id}/preview` | write | Reconcile dry run (nothing is saved) |
| `POST /api/v1/uploads/{id}/apply` | write | `{"fingerprint", "accept": {"P1": "clear"}}`; 409 when anything changed since the preview |
| `GET /api/v1/uploads/{id}/removal` | write | What deleting it with its postings would remove: `can_delete`, `bank_postings`, `card_purchases`, `first`, `last` |
| `DELETE /api/v1/uploads/{id}` | write | Hides it; its postings stay in the history. `?with_postings=true` deletes a waiting, unreconciled one and its postings |
| `GET /api/v1/series` | read | Recurring items and the credit cards that can pay them |
| `PATCH /api/v1/series/{id}` | write | Any of `{"card", "status", "group", "notes"}`; row amounts are kept |
| `GET /api/v1/rules` | read | Payee rules in order |
| `POST /api/v1/rules/test` | read | `{"description", "amount"}` (signed like the bank: money out is negative) |
| `POST /api/v1/rules` | write | `{"name", "kind", "contains"?, "regex"?, "direction"?, "min_amount"?, "max_amount"?, "targets"?, "card"?, "note"?, "before"?}` |
| `PATCH /api/v1/rules/{id}` | write | Any of the rule's fields |
| `DELETE /api/v1/rules/{id}` | write | |
| `POST /api/v1/rules/order` | write | `{"ids": [...]}`, every rule id once |
| `POST /api/v1/series/{id}/amount` | write | `{"amount", "from_date", "to_date"?, "expected_amount"?, "dry_run"?}` |
| `POST /api/v1/series/{id}/seasonal` | write | `{"months": {"7-10": 37500}, "from_date"?, "dry_run"?}` |
| `PATCH /api/v1/debts/{id}` | write | Any of balance, apr_bp, payment, minimum, limit, as_of, promo_ends, apr_after_bp, deferred_interest, include_in_payoff, notes; `expected_balance`? |
| `GET /api/v1/changes?since=0&limit=100` | write | Changes after the audit entry `since`, oldest first, and a `cursor` |
| `GET /api/v1/reconciliations` | read | |
| `GET /api/v1/reconciliations/{id}` | read | Each bank line's outcome (`details` is null for ones applied before 0.14) |
| `POST /api/v1/reconciliations/{id}/undo` | write | The newest one only |
| `GET /api/v1/openapi.json` | read, or signed in | The OpenAPI description of the above |

```sh
curl -H "Authorization: Bearer hbt_..." https://budget.example.lan/api/v1/paydays
curl -H "Authorization: Bearer hbt_..." -F file=@export.csv -F kind=bank https://budget.example.lan/api/v1/uploads
```

**Reconciling from an agent.** Upload the export, run the preview, show the person the proposals (a proposal is a
bank line the rules could not settle alone: two rows from the same payee, an amount off by more than the tolerance,
a card bundle), then apply with the fingerprint and the proposals they accepted. The preview is free to repeat;
apply refuses with 409 if the budget changed in between. A CSV whose columns no saved layout matches is refused
(422) with its columns and a suggested mapping: check the mapping with the person, then upload again with `mapping`
and `save_as` so the bank's later exports are read without it.

**Keeping in sync.** Store `cursor` from `list_changes` after each sync; next time, `get_summary`'s `last_change`
says whether anything is new and `list_changes` with `since` says what (the log keeps ids and actions, not amounts:
read the item to see its value). A reconciliation applied by mistake can be undone while it is the newest one and
nothing it touched has changed.

The `/api/h/{id}/...` routes the pages use are internal: they need a browser session and a CSRF token, and they
change without notice. Build on `/api/v1`.
