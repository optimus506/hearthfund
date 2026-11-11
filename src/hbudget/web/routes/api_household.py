"""JSON API for one household: /api/h/{hid}/... (ARCHITECTURE 15.2). Every route names its minimum role."""
from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from hbudget import __version__
from hbudget.core import alerts as alerts_mod
from hbudget.core import cards, checkup, flow, ledger, models, payoff, rules, subscriptions
from hbudget.core import forecast as fc
from hbudget.core import goals as goals_mod
from hbudget.core import milestones as milestones_mod
from hbudget.core import recap as recap_mod
from hbudget.core.dates import add_months
from hbudget.core.dates import week_start as ledger_week_start
from hbudget.core.models import (
    Account,
    AccountKind,
    Budget,
    Cadence,
    CapRule,
    CardCategory,
    Direction,
    ExpectedItem,
    FlowGroup,
    Horizon,
    Kind,
    PayeeRule,
    RowState,
    RuleKind,
    Schedule,
    Series,
    Status,
    Weekend,
    WhatIf,
    slug,
)
from hbudget.db import repo, users
from hbudget.db.connection import transaction
from hbudget.io import ImportProblem, billsystem, export, rulepack, tabular, workbook_import
from hbudget.web import billimport, views
from hbudget.web.app import cache_get, cache_put
from hbudget.web.deps import (
    ROLE_ORDER,
    ApiError,
    Contributor,
    Editor,
    Member,
    Owner,
    Viewer,
    second_factor,
)

router = APIRouter(prefix='/api/h/{hid}')
T = TypeVar('T')
MAX_CENTS = 100_000_000_00          # $100 million: anything larger is a typo
Money = Annotated[int, Field(ge=0, le=MAX_CENTS)]
SignedMoney = Annotated[int, Field(ge=-MAX_CENTS, le=MAX_CENTS)]
Name = Annotated[str, Field(min_length=1, max_length=80)]


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


# ---------------------------------------------------------------- helpers

def core[T](fn: Callable[[], T]) -> T:
    """Run a core edit; an unknown id is 404, a refused edit is 400."""
    try:
        return fn()
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    except ValueError as e:
        raise ApiError(400, 'invalid', str(e)) from None


def load(m: Member) -> Budget:
    return repo.load_budget(m.conn, m.hid)


def save(m: Member, budget: Budget, action: str, row_id: str | None = None) -> None:
    repo.save_budget(m.conn, m.hid, budget, user_id=m.user_id, action=action, row_id=row_id)


def edit(m: Member, action: str, fn: Callable[[Budget], Budget], row_id: str | None = None) -> Budget:
    """Load, change and save the budget in one BEGIN IMMEDIATE transaction (repo.edit_budget), so two requests that
    edit the same household at once cannot overwrite each other."""
    b, _ = repo.edit_budget(m.conn, m.hid, fn, user_id=m.user_id, action=action, row_id=row_id)
    return b


def regenerate(budget: Budget) -> Budget:
    try:
        return ledger.generate(budget, ledger.horizon_end(budget))
    except ValueError as e:
        raise ApiError(400, 'invalid', str(e)) from None


def need(found: bool) -> None:
    if not found:
        raise ApiError(404, 'not_found', 'Not found.')


def check_stale(current: int, expected: int | None) -> None:
    if expected is not None and expected != current:
        raise ApiError(409, 'stale', 'This row changed since the page was loaded. The page will reload.')


def check_updated(m: Member, expected: str | None) -> None:
    if expected is not None and expected != m.household.updated_at:
        raise ApiError(409, 'stale', 'Someone else saved changes since the page was loaded. The page will reload.')


def new_id(prefix: str, name: str = '', taken: set[str] | None = None) -> str:
    base = slug(name)[:30] or prefix
    for _ in range(50):
        cand = '%s-%s' % (base, secrets.token_hex(3))
        if not taken or cand not in taken:
            return cand
    raise ApiError(500, 'server', 'Could not make a new id.')


async def time_limited[T](request: Request, fn: Callable[[], T]) -> T:
    """ReDoS backstop: user patterns are cost-checked already; this bounds any single request anyway."""
    limit = request.app.state.settings.time_limit_seconds
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=limit)
    except TimeoutError:
        raise ApiError(503, 'timeout',
                       'This took too long and was stopped. Simplify the payee-rule patterns.') from None


def history(m: Member) -> list[Any]:
    return repo.list_history(m.conn, m.hid)


# ---------------------------------------------------------------- summary and ledger

@router.get('/summary')
def get_summary(m: Viewer) -> dict[str, Any]:
    return views.compute_summary(load(m), history(m), today=m.today)


@router.get('/home')
def get_home(m: Viewer) -> dict[str, Any]:
    return views.home(load(m), history(m), today=m.today)


# ---------------------------------------------------------------- goals (D-081)

class GoalBody(Body):
    id: Annotated[str, Field(max_length=60)] | None = None
    name: Name
    kind: Literal['emergency', 'savings', 'debt_free']
    target: Money | None = None
    months: Annotated[int, Field(ge=1, le=24)] = 3
    saved: Money = 0
    by: date | None = None


class GoalsBody(Body):
    goals: list[GoalBody] = Field(max_length=30)


def goals_json(b: Budget, today: date) -> dict[str, Any]:
    return {'goals': [{'id': p.goal.id, 'name': p.goal.name, 'kind': p.goal.kind, 'target': p.target,
                       'current': p.current, 'pct': p.pct, 'reached': p.reached, 'months': p.goal.months,
                       'saved': p.goal.saved, 'by': views.iso(p.goal.by), 'monthly_needed': p.monthly_needed,
                       'months_left': p.months_left, 'done_on': views.iso(p.done_on)}
                      for p in goals_mod.progress(b, today=today)],
            'monthly_bills': goals_mod.monthly_bills(b, today=today), 'debt': goals_mod.total_debt(b.accounts)}


@router.get('/goals')
def get_goals(m: Viewer) -> dict[str, Any]:
    return goals_json(load(m), m.today)


@router.put('/goals')
def put_goals(body: GoalsBody, m: Editor) -> dict[str, Any]:
    """Replace the goals. A new debt-free goal remembers what is owed today, so its progress is what was paid since."""
    def fn(b: Budget) -> Budget:
        old = {g.id: g for g in b.settings.goals}
        debt = goals_mod.total_debt(b.accounts)
        out = []
        for g in body.goals:
            if g.kind == 'savings' and not g.target:
                raise ApiError(400, 'invalid', 'A savings goal needs a target amount.')
            gid = g.id if g.id in old else new_id('goal', g.name)
            prev = old.get(gid)
            start = (prev.start_debt if prev and prev.kind == 'debt_free' else None) if g.kind == 'debt_free' else None
            out.append(models.Goal(gid, g.name, g.kind, g.target if g.kind == 'savings' else None, g.months,
                                   g.saved if g.kind != 'debt_free' else 0, g.by,
                                   (start if start is not None else debt) if g.kind == 'debt_free' else None))
        if len({g.id for g in out}) < len(out):
            raise ApiError(400, 'invalid', 'A goal is listed twice.')
        return replace(b, settings=replace(b.settings, goals=tuple(out)))
    edit(m, 'goals.save', fn)
    return goals_json(load(m), m.today)


# ---------------------------------------------------------------- milestones and wins (D-082)

def _win(x: models.Milestone) -> dict[str, Any]:
    return {'key': x.key, 'kind': x.kind, 'text': x.text, 'on': views.iso(x.on)}


@router.get('/milestones')
def get_milestones(m: Viewer) -> dict[str, Any]:
    """New wins (not celebrated yet) and the history, newest first."""
    b = load(m)
    _, new = milestones_mod.merge(b.settings.milestones, milestones_mod.detect(b, history(m), today=m.today))
    return {'celebrate': b.settings.celebrate, 'new': [_win(x) for x in new],
            'history': [_win(x) for x in reversed(b.settings.milestones)]}


class SeenBody(Body):
    keys: list[Annotated[str, Field(max_length=120)]] = Field(max_length=50)


@router.post('/milestones/seen')
def milestones_seen(body: SeenBody, m: Contributor) -> dict[str, Any]:
    """Keep these wins in the history, so they are celebrated once."""
    def fn(b: Budget) -> Budget:
        found = [x for x in milestones_mod.detect(b, history(m), today=m.today) if x.key in set(body.keys)]
        kept, _ = milestones_mod.merge(b.settings.milestones, found)
        return replace(b, settings=replace(b.settings, milestones=kept))
    edit(m, 'milestones.seen', fn)
    return {'ok': True}


class CelebrateBody(Body):
    on: bool


@router.put('/celebrate')
def put_celebrate(body: CelebrateBody, m: Editor) -> dict[str, Any]:
    """Show wins on Home, or not (they are still kept)."""
    edit(m, 'settings.celebrate', lambda b: replace(b, settings=replace(b.settings, celebrate=body.on)))
    return {'ok': True}


# ---------------------------------------------------------------- weekly check-in (Home)

CHANGE_WORDS = {
    'row.change': 'changed the amount of', 'row.remove': 'removed', 'row.undo': 'undid a change to',
    'row.status': 'marked', 'row.add': 'added', 'row.move': 'moved', 'row.note': 'wrote a note on',
    'rows.bulk.change': 'changed several rows',
    'rows.bulk.remove': 'removed several rows', 'rows.bulk.status': 'marked several rows',
    'series.add': 'added', 'series.edit': 'changed', 'series.delete': 'deleted',
    'series.status': 'changed the status of',
    'series.seasonal': 'set seasonal amounts for', 'series.amount': 'changed the amount of',
    'reconcile.apply': 'reconciled a bank file', 'reconcile.undo': 'undid a reconciliation',
    'upload.bank': 'uploaded a bank file', 'upload.card': 'uploaded a card file',
    'plaid.sync': 'synced with the bank', 'plaid.link': 'linked a bank', 'plaid.unlink': 'unlinked a bank',
    'plaid.enable': 'turned on the bank connection', 'plaid.disable': 'turned off the bank connection',
    'plaid.role': 'changed a linked account',
    'alert.snooze': 'set aside an alert', 'alert.unsnooze': 'brought back an alert',
    'account.add': 'added the debt', 'account.edit': 'updated the debt', 'account.delete': 'deleted the debt',
    'rule.add': 'added the rule', 'rule.edit': 'changed the rule', 'rule.delete': 'deleted the rule',
    'expected.add': 'added an expected item', 'expected.status': 'marked an expected item',
    'settings.save': 'changed the settings', 'notes.save': 'edited the notes', 'checkin.done': 'checked in',
    'goals.save': 'changed the goals', 'settings.celebrate': 'changed whether wins are shown',
    'expected.edit': 'changed an expected item', 'expected.delete': 'deleted an expected item',
    'rule.pack': 'added rules from a pack',
    'rule.order': 'reordered the rules', 'settings.caps': 'changed the spending caps',
    'settings.groups': 'changed the report groups', 'card.categories': 'changed the card categories',
    'card.store': 'moved a store to another card category', 'card.purchase': 'moved a card purchase',
    'recurring.hide': 'marked a charge as not a subscription', 'recurring.show': 'showed a hidden subscription again',
    'import.replace': 'imported a bill system', 'import.merge': 'imported a bill system',
    'import.workbook': 'imported a workbook', 'import.bills_csv': 'imported bills',
    'household.wipe': 'wiped the budget',
    'member.add': 'added a member', 'member.remove': 'removed a member', 'member.role': 'changed a member\'s role',
    'token.create': 'made an access token', 'token.revoke': 'revoked an access token',
    'upload.delete': 'deleted an upload', 'upload.hide': 'hid an upload',
}
# looking, signing in and housekeeping: not changes anyone needs to hear about
QUIET = frozenset({'milestones.seen', 'generate', 'backup.export', 'export.forecast', 'login.ok', 'login.failed',
                   'logout',
                   'password.change', 'password.change.failed', 'checkin.done', 'plaid.autosync'})
CHECKIN_DUE_DAYS = 7


def _item_name(b: Budget, item: str | None) -> str:
    """A row, series, account or rule id as the name people know it by (ids alone mean nothing on Home)."""
    if not item:
        return ''
    for r in b.rows:
        if r.id == item:
            return r.name
    names = {s.id: s.name for s in b.series} | {a.id: a.name for a in b.accounts} | {r.id: r.name for r in b.rules}
    return names.get(item, '')


@router.get('/checkin')
def get_checkin(request: Request, m: Viewer) -> dict[str, Any]:
    """The weekly check-in on Home: the bank file, bills due soon with no status, what others changed since your
    last check-in, and who checked in last (D-080). Nothing here is a score: no streaks, no "missed" weeks."""
    b = load(m)
    log = repo.list_audit(m.conn, m.hid, limit=500)                 # newest first
    checkins = [a for a in log if a['action'] == 'checkin.done']
    mine = next((a for a in checkins if a['user_id'] == m.user_id and not a['detail'].get('via')), None)
    since = mine['at'] if mine else None
    # check-ins are compared with the audit log's clock (UTC), not the budget's "today"
    week = ledger_week_start(datetime.now(UTC).date(), b.settings.week_starts)
    changes = []
    for a in log:
        if since is not None and a['at'] <= since:
            break
        if a['action'] in QUIET or (a['user_id'] == m.user_id and not a['detail'].get('via')):
            continue
        changes.append({'at': a['at'], 'by': a['detail'].get('via') or a['username'] or 'someone',
                        'what': CHANGE_WORDS.get(a['action'], 'made a change'), 'item': _item_name(b, a['row'])})
        if len(changes) == 20:
            break
    waiting = [u for u in repo.list_uploads(m.conn, m.hid, kind='bank') if u.status == 'waiting']
    linked = request.app.state.settings.plaid != 'off' and plaid_linked(m.conn, m.hid)
    by_id = b.series_by_id()
    end = m.today + timedelta(days=CHECKIN_DUE_DAYS)
    due = [views.row(r, by_id, None, today=m.today) for r in ledger.ordered(b.rows)
           if r.state is RowState.OPEN and r.kind is Kind.BILL and r.amount > 0 and r.due_date <= end
           and models.row_status(r, by_id) is Status.NONE]
    return {
        'week_start': views.iso(week),
        'done_this_week': any(a['at'][:10] >= week.isoformat() for a in checkins),
        'last': {'at': checkins[0]['at'], 'by': checkins[0]['detail'].get('via') or checkins[0]['username']}
        if checkins else None,
        'your_last': since,
        'bank': {'reconciled_through': views.iso(b.last_reconciled_through),
                 'waiting': [{'id': u.id, 'filename': u.filename, 'date_to': views.iso(u.date_to)} for u in waiting]},
        'due': due, 'due_days': CHECKIN_DUE_DAYS, 'changes': changes, 'more_changes': len(changes) == 20,
        # a bank linked through Plaid (docs/PLAID.md): step 1 offers "Sync with the bank" next to the upload
        'plaid': linked,
        **(_plaid_checkin(m, b) if linked else {}),
    }


def _plaid_checkin(m: Member, b: Budget) -> dict[str, Any]:
    """What the daily sync left for the check-in: postings no upload has taken yet, and banks that need attention."""
    from hbudget.db import plaid as pdb
    from hbudget.web.routes import plaid

    return {'plaid_new': plaid.waiting_count(m.conn, m.hid, b.last_reconciled_through),
            'plaid_problems': [{'institution': it.institution, 'message': it.last_error}
                               for it in pdb.items(m.conn, m.hid) if it.last_error]}


def plaid_linked(conn: Any, hid: int) -> bool:
    from hbudget.db import plaid as pdb
    return pdb.enabled(conn, hid) and bool(pdb.items(conn, hid))


class CheckinBody(Body):
    note: str = Field(default='', max_length=200)


@router.post('/checkin')
def post_checkin(body: CheckinBody, m: Contributor) -> dict[str, Any]:
    """Record that this member checked in (an audit entry; the log keeps who and when)."""
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='checkin.done',
               detail={'note_given': bool(body.note.strip())})
    return {'ok': True}


@router.get('/ledger')
def get_ledger(m: Viewer, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    b = load(m)
    return views.ledger(b, fc.compute(b, today=m.today), today=m.today, start=start, end=end)


@router.get('/calendar')
def get_calendar(m: Viewer, month: str | None = Query(default=None, pattern=r'^\d{4}-\d{2}$')) -> dict[str, Any]:
    """The cash-flow calendar for one month (default this month): what moves each day and the balance after it."""
    b = load(m)
    try:
        first = date.fromisoformat(month + '-01') if month else m.today.replace(day=1)
    except ValueError:
        raise ApiError(400, 'invalid', 'month is YYYY-MM.') from None
    return {'calendar': views.calendar(b, fc.compute(b, today=m.today), first, today=m.today)}


class StatusBody(Body):
    status: Literal['autopay', 'manual', 'none'] | None


@router.post('/rows/{row_id}/status')
def row_status(row_id: str, body: StatusBody, m: Contributor) -> dict[str, Any]:
    st = Status(body.status) if body.status else None
    edit(m, 'row.status', lambda b: core(lambda: ledger.mark_status(b, row_id, st)), row_id)
    return {'ok': True}


class RowNoteBody(Body):
    note: str = Field(default='', max_length=300)


@router.post('/rows/{row_id}/note')
def row_note(row_id: str, body: RowNoteBody, m: Contributor) -> dict[str, Any]:
    """A note on one row ("paid by phone", "ask about the late fee"); empty clears it."""
    note = ' '.join(body.note.split())

    def fn(b: Budget) -> Budget:
        need(any(r.id == row_id for r in b.rows))
        return replace(b, rows=tuple(replace(r, note=note) if r.id == row_id else r for r in b.rows))
    edit(m, 'row.note', fn, row_id)
    return {'id': row_id, 'note': note}


class ChangeBody(Body):
    amount: Money
    expected_amount: SignedMoney | None = None


class StaleBody(Body):
    expected_amount: SignedMoney | None = None


def _row(b: Budget, row_id: str) -> Any:
    r = next((x for x in b.rows if x.id == row_id), None)
    if r is None:
        raise ApiError(404, 'not_found', 'Not found.')
    return r


@router.post('/rows/{row_id}/change')
def row_change(row_id: str, body: ChangeBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        check_stale(_row(b, row_id).amount, body.expected_amount)
        return core(lambda: ledger.change_amount(b, row_id, body.amount))
    edit(m, 'row.change', fn, row_id)
    return {'ok': True}


@router.post('/rows/{row_id}/remove')
def row_remove(row_id: str, body: StaleBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        check_stale(_row(b, row_id).amount, body.expected_amount)
        return core(lambda: ledger.remove_row(b, row_id))
    edit(m, 'row.remove', fn, row_id)
    return {'ok': True}


@router.post('/rows/{row_id}/done')
def row_done(row_id: str, body: StaleBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        check_stale(_row(b, row_id).amount, body.expected_amount)
        return core(lambda: ledger.mark_done(b, row_id, m.today))
    edit(m, 'row.done', fn, row_id)
    return {'ok': True}


class BulkRow(Body):
    id: Annotated[str, Field(max_length=200)]
    expected_amount: SignedMoney


class BulkBody(Body):
    rows: list[BulkRow] = Field(min_length=1, max_length=300)
    action: Literal['change', 'remove', 'done', 'status']
    amount: Money | None = None
    status: Literal['autopay', 'manual', 'none'] | None = None


@router.post('/rows/bulk')
def rows_bulk(body: BulkBody, m: Contributor) -> dict[str, Any]:
    """One change to many rows (the bill finder), all or nothing: if any row changed since it was read, nothing is
    saved and the answer names those rows. Marking a status needs a contributor; changing, removing or marking
    already paid an editor."""
    if body.action != 'status' and ROLE_ORDER[m.role] < ROLE_ORDER['editor']:
        raise ApiError(403, 'forbidden', 'Changing, removing or marking rows paid needs an editor.')
    if body.action == 'change' and body.amount is None:
        raise ApiError(400, 'invalid', 'Give the new amount.')
    ids = [r.id for r in body.rows]
    if len(set(ids)) < len(ids):
        raise ApiError(400, 'invalid', 'A row is listed twice.')

    def fn(b: Budget) -> Budget:
        by = {r.id: r for r in b.rows}
        missing = [i for i in ids if i not in by]
        if missing:
            raise ApiError(404, 'not_found', '%d of the rows no longer exist. Reload and try again.' % len(missing))
        stale = sorted({by[x.id].name for x in body.rows if by[x.id].amount != x.expected_amount})
        if stale:
            raise ApiError(409, 'stale', 'Changed since you loaded them: %s. Nothing was saved; reload and try again.'
                           % ', '.join(stale[:10]))
        for i in ids:
            if body.action == 'change':
                assert body.amount is not None
                b = core(lambda b=b, i=i: ledger.change_amount(b, i, body.amount))   # type: ignore[misc]
            elif body.action == 'remove':
                b = core(lambda b=b, i=i: ledger.remove_row(b, i))   # type: ignore[misc]
            elif body.action == 'done':
                b = core(lambda b=b, i=i: ledger.mark_done(b, i, m.today))   # type: ignore[misc]
            else:
                st = Status(body.status) if body.status else None
                b = core(lambda b=b, i=i, st=st: ledger.mark_status(b, i, st))   # type: ignore[misc]
        return b
    edit(m, 'rows.bulk.' + body.action, fn)
    return {'ok': True, 'rows': len(ids)}


@router.post('/rows/{row_id}/undo')
def row_undo(row_id: str, body: StaleBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        check_stale(_row(b, row_id).amount, body.expected_amount)
        return core(lambda: ledger.undo_change(b, row_id))
    edit(m, 'row.undo', fn, row_id)
    return {'ok': True}


class NewRowBody(Body):
    name: Name
    kind: Literal['income', 'bill', 'spending']
    amount: Money
    due: date
    after: str | None = Field(default=None, max_length=120)
    card: str | None = Field(default=None, max_length=40)


@router.post('/rows')
def row_add(body: NewRowBody, m: Editor) -> dict[str, Any]:
    rid = new_id('row', body.name)
    edit(m, 'row.add', lambda b: core(lambda: ledger.add_row(b, rid, body.name, Kind(body.kind), body.amount, body.due,
                                                             card=body.card, order_after=body.after)), rid)
    return {'id': rid}


class MoveBody(Body):
    after: str = Field(max_length=120)


@router.post('/rows/{row_id}/move')
def row_move(row_id: str, body: MoveBody, m: Editor) -> dict[str, Any]:
    edit(m, 'row.move', lambda b: core(lambda: ledger.move_row(b, row_id, body.after)), row_id)
    return {'ok': True}


class WhatIfItem(Body):
    label: str = Field(default='What-if', max_length=60)
    amount: Annotated[int, Field(gt=0, le=MAX_CENTS)]
    direction: Literal['in', 'out']
    date: date


class WhatIfBody(Body):
    items: list[WhatIfItem] = Field(default_factory=list, max_length=50)
    row_amounts: dict[str, Money] = Field(default_factory=dict, max_length=200)


@router.post('/whatif')
def whatif(body: WhatIfBody, m: Viewer) -> dict[str, Any]:
    """The forecast with extra items; nothing is saved (any member may try)."""
    b = load(m)
    base = fc.compute(b, today=m.today)
    wi = fc.compute(b, today=m.today, whatifs=[WhatIf(i.label, i.amount, i.direction, i.date) for i in body.items],
                    row_amounts=body.row_amounts)
    d = fc.compare(base, wi, cushion=b.settings.alerts.cushion)
    verdict = {'ok': 'yes', 'tight': 'tight', 'below_zero': 'no'}[d.verdict]
    return {'base': views.forecast_readout(base, today=m.today), 'whatif': views.forecast_readout(wi, today=m.today),
            'delta': {'low_change': d.low_change, 'end_change': d.end_change, 'verdict': verdict},
            'cushion': b.settings.alerts.cushion}


# ---------------------------------------------------------------- expected items

class ExpectedBody(Body):
    label: Annotated[str, Field(min_length=1, max_length=60)]
    amount: Annotated[int, Field(gt=0, le=MAX_CENTS)]
    direction: Literal['in', 'out']
    date: date
    status: Literal['open', 'done'] = 'open'


@router.get('/expected')
def expected_list(m: Viewer) -> dict[str, Any]:
    return {'items': [views.expected(e) for e in load(m).expected]}


@router.post('/expected')
def expected_add(body: ExpectedBody, m: Editor) -> dict[str, Any]:
    eid = new_id('exp', body.label)
    item = ExpectedItem(eid, body.label, body.amount, body.direction, body.date, body.status)
    edit(m, 'expected.add', lambda b: replace(b, expected=(*b.expected, item)), eid)
    return {'id': eid}


@router.put('/expected/{item_id}')
def expected_edit(item_id: str, body: ExpectedBody, m: Editor) -> dict[str, Any]:
    item = ExpectedItem(item_id, body.label, body.amount, body.direction, body.date, body.status)

    def fn(b: Budget) -> Budget:
        need(any(e.id == item_id for e in b.expected))
        return replace(b, expected=tuple(item if e.id == item_id else e for e in b.expected))
    edit(m, 'expected.edit', fn, item_id)
    return {'ok': True}


@router.delete('/expected/{item_id}')
def expected_delete(item_id: str, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        need(any(e.id == item_id for e in b.expected))
        return replace(b, expected=tuple(e for e in b.expected if e.id != item_id))
    edit(m, 'expected.delete', fn, item_id)
    return {'ok': True}


class ExpectedStatusBody(Body):
    status: Literal['open', 'done']


@router.post('/expected/{item_id}/status')
def expected_status(item_id: str, body: ExpectedStatusBody, m: Contributor) -> dict[str, Any]:
    """The only expected-item change a contributor can make."""
    try:
        repo.set_expected_status(m.conn, m.hid, item_id, body.status, user_id=m.user_id)
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    return {'ok': True}


# ---------------------------------------------------------------- series

class ScheduleBody(Body):
    cadence: Literal['once', 'weekly', 'biweekly', 'semimonthly', 'monthly', 'yearly']
    start: date
    end: date | None = None
    interval: Annotated[int, Field(ge=1, le=52)] = 1
    day: Annotated[int, Field(ge=1, le=31)] | None = None
    days: Annotated[list[Annotated[int, Field(ge=1, le=31)]], Field(min_length=2, max_length=2)] | None = None
    month: Annotated[int, Field(ge=1, le=12)] | None = None
    weekend: Literal['none', 'before', 'after'] = 'none'


class SeriesBody(Body):
    name: Name
    kind: Literal['income', 'bill', 'spending']
    amount: Money
    schedule: ScheduleBody
    funded_by: str | None = Field(default=None, max_length=120)
    status: Literal['autopay', 'manual', 'none'] = 'none'
    card: str | None = Field(default=None, max_length=40)
    debt: str | None = Field(default=None, max_length=40)        # left out keeps the item's link (#97)
    group: str | None = Field(default=None, max_length=40)
    active: bool = True
    notes: str | None = Field(default=None, max_length=1000)   # None keeps the item's notes


def _check_debt(b: Budget, debt: str | None) -> None:
    if debt is not None and not any(a.id == debt and a.kind not in (AccountKind.CHECKING, AccountKind.SAVINGS)
                                    for a in b.accounts):
        raise ApiError(400, 'invalid', 'The "pays this debt" account does not exist.')


def _schedule(s: ScheduleBody) -> Schedule:
    if s.days and s.days[0] == s.days[1]:
        raise ApiError(400, 'invalid', 'The two days of a twice-a-month item must differ.')
    if s.end and s.end < s.start:
        raise ApiError(400, 'invalid', 'The last date is before the first date.')
    return Schedule(Cadence(s.cadence), s.start, s.end, s.interval, s.day,
                    (s.days[0], s.days[1]) if s.days else None, s.month, Weekend(s.weekend))


@router.get('/series')
def series_list(m: Viewer) -> dict[str, Any]:
    return {'series': [views.series(s) for s in load(m).series]}


@router.post('/series')
def series_add(body: SeriesBody, m: Editor) -> dict[str, Any]:
    sid = new_id('series', body.name)
    s = Series(sid, body.name, Kind(body.kind), body.amount, _schedule(body.schedule), funded_by=body.funded_by,
               status=Status(body.status), card=body.card, debt=body.debt, group=body.group, active=body.active,
               notes=body.notes or '')

    def fn(b: Budget) -> Budget:
        if body.funded_by and body.funded_by not in b.series_by_id():
            raise ApiError(400, 'invalid', 'The "paid from" item does not exist.')
        _check_debt(b, body.debt)
        return regenerate(replace(b, series=(*b.series, s)))
    edit(m, 'series.add', fn, sid)
    return {'id': sid}


@router.put('/series/{series_id}')
def series_edit(series_id: str, body: SeriesBody, m: Editor) -> dict[str, Any]:
    sched = _schedule(body.schedule)

    def fn(b: Budget) -> Budget:
        by_id = b.series_by_id()
        old = by_id.get(series_id)
        need(old is not None)
        assert old is not None
        if old.kind != Kind(body.kind):
            raise ApiError(400, 'invalid', 'An item cannot change between income, bill and spending line. Add a new '
                                           'item and delete this one.')
        if body.funded_by and (body.funded_by not in by_id or body.funded_by == series_id):
            raise ApiError(400, 'invalid', 'The "paid from" item does not exist.')
        debt = body.debt if 'debt' in body.model_fields_set else old.debt
        _check_debt(b, debt)
        new = replace(old, name=body.name, kind=Kind(body.kind), amount=body.amount, schedule=sched,
                      funded_by=body.funded_by, status=Status(body.status), card=body.card, debt=debt,
                      group=body.group, active=body.active, notes=old.notes if body.notes is None else body.notes)
        b = replace(b, series=tuple(new if s.id == series_id else s for s in b.series))
        if (new.amount, new.schedule, new.active) == (old.amount, old.schedule, old.active):
            # only the name, card, status, group, notes or "paid from" changed: keep the rows' amounts (#24)
            return core(lambda: ledger.relabel_series(b, series_id, m.today))
        b = core(lambda: ledger.regenerate_series(b, series_id, m.today))
        return regenerate(b)
    edit(m, 'series.edit', fn, series_id)
    return {'ok': True}


@router.delete('/series/{series_id}')
def series_delete(series_id: str, m: Editor) -> dict[str, Any]:
    """The series and its open future rows go; cleared rows and rows edited by hand stay as one-off rows."""
    def fn(b: Budget) -> Budget:
        need(series_id in b.series_by_id())
        rows = []
        for r in b.rows:
            if r.series_id != series_id:
                rows.append(r)
            elif r.state is RowState.OPEN and not r.overridden and r.due_date >= m.today:
                continue
            else:
                rows.append(replace(r, series_id=None))
        series = tuple(replace(s, funded_by=None) if s.funded_by == series_id else s
                       for s in b.series if s.id != series_id)
        return replace(b, series=series, rows=tuple(rows))
    edit(m, 'series.delete', fn, series_id)
    return {'ok': True}


@router.post('/series/{series_id}/status')
def series_status(series_id: str, body: StatusBody, m: Contributor) -> dict[str, Any]:
    """The bill's default status (the Autopay settings list); contributors may set it, like a row mark."""
    st = Status(body.status or 'none')

    def fn(b: Budget) -> Budget:
        need(series_id in b.series_by_id())
        return replace(b, series=tuple(replace(s, status=st) if s.id == series_id else s for s in b.series))
    edit(m, 'series.status', fn, series_id)
    return {'ok': True}


class SeasonalRule(Body):
    months: Annotated[str, Field(min_length=1, max_length=40)]
    amount: Money


class SeasonalBody(Body):
    rules: list[SeasonalRule] = Field(min_length=1, max_length=12)
    from_: date | None = Field(default=None, alias='from')
    dry_run: bool = True


@router.post('/series/{series_id}/seasonal')
def series_seasonal(series_id: str, body: SeasonalBody, m: Editor) -> dict[str, Any]:
    parsed = core(lambda: [(ledger.parse_months(r.months), r.amount) for r in body.rules])
    seen: dict[str, Any] = {}

    def fn(b: Budget) -> Budget:
        new_b, changes = core(lambda: ledger.set_month_amounts(b, series_id, parsed, body.from_ or m.today))
        seen.update(before=fc.compute(b, today=m.today), after=fc.compute(new_b, today=m.today), changes=changes)
        return new_b
    if body.dry_run:
        fn(load(m))
    else:
        edit(m, 'series.seasonal', fn, series_id)
    before, after, changes = seen['before'], seen['after'], seen['changes']
    return {'changes': [{'row_id': c.row_id, 'name': c.name, 'due': views.iso(c.due_date), 'was': c.was, 'now': c.now}
                        for c in changes], 'low': after.low, 'low_before': before.low, 'dry_run': body.dry_run}


@router.post('/generate')
def generate(m: Editor) -> dict[str, Any]:
    b = edit(m, 'generate', regenerate)
    return {'through': views.iso(ledger.horizon_end(b))}


# ---------------------------------------------------------------- debts and payoff

class AccountBody(Body):
    name: Name
    kind: Literal['checking', 'savings', 'credit_card', 'loan', 'mortgage', 'retirement_loan', 'other']
    balance: Money = 0
    apr_bp: Annotated[int, Field(ge=0, le=9999)] = 0
    payment: Money = 0
    minimum: Money = 0
    limit: Money | None = None
    as_of: date | None = None
    promo_ends: date | None = None
    apr_after_bp: Annotated[int, Field(ge=0, le=9999)] | None = None
    deferred_interest: bool = False
    is_mortgage: bool = False
    include_in_payoff: bool = True
    notes: str = Field(default='', max_length=1000)


def _account(aid: str, body: AccountBody, today: date) -> Account:
    import re
    if re.search(r'\d{6,}', body.name):
        raise ApiError(400, 'invalid', 'Leave card and account numbers out of the name.')
    return Account(aid, body.name, AccountKind(body.kind), body.balance, body.apr_bp, body.payment, body.minimum,
                   body.limit, body.as_of or today, body.promo_ends, body.apr_after_bp,
                   body.deferred_interest and body.promo_ends is not None,
                   body.is_mortgage or body.kind == 'mortgage', body.include_in_payoff, body.notes)


@router.get('/accounts')
def accounts_list(m: Viewer) -> dict[str, Any]:
    b = load(m)
    hist = history(m) if any(s.debt for s in b.series) else []
    return {'accounts': [views.account(a, payoff.estimate(a, b, hist, today=m.today)) for a in b.accounts]}


@router.post('/accounts')
def account_add(body: AccountBody, m: Editor) -> dict[str, Any]:
    aid = new_id('acct', body.name)
    acct = payoff.record(None, _account(aid, body, m.today))
    edit(m, 'account.add', lambda b: replace(b, accounts=(*b.accounts, acct)), aid)
    return {'id': aid}


@router.put('/accounts/{account_id}')
def account_edit(account_id: str, body: AccountBody, m: Editor) -> dict[str, Any]:
    acct = _account(account_id, body, m.today)

    def fn(b: Budget) -> Budget:
        old = next((a for a in b.accounts if a.id == account_id), None)
        need(old is not None)
        new = payoff.record(old, acct)
        return replace(b, accounts=tuple(new if a.id == account_id else a for a in b.accounts))
    edit(m, 'account.edit', fn, account_id)
    return {'ok': True}


@router.delete('/accounts/{account_id}')
def account_delete(account_id: str, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        need(any(a.id == account_id for a in b.accounts))
        return replace(b, accounts=tuple(a for a in b.accounts if a.id != account_id))
    edit(m, 'account.delete', fn, account_id)
    return {'ok': True}


class PayoffBody(Body):
    method: Literal['avalanche', 'snowball'] = 'avalanche'
    extra: Money = 0
    once: Money = 0
    once_month: Annotated[str, Field(pattern=r'^\d{4}-\d{2}$')] | None = None
    include_mortgage: bool = False


@router.post('/payoff')
def payoff_plan(body: PayoffBody, m: Viewer) -> dict[str, Any]:
    once_month = None
    if body.once_month:
        y, mo = (int(x) for x in body.once_month.split('-'))
        if not 1 <= mo <= 12:
            raise ApiError(400, 'invalid', 'once_month must be YYYY-MM.')
        once_month = date(y, mo, 1)
    p = payoff.plan(load(m).accounts, method=body.method, extra=body.extra, once=body.once, once_month=once_month,
                    include_mortgage=body.include_mortgage, today=m.today)
    return views.payoff(p)


# ---------------------------------------------------------------- reports

@router.get('/flow')
def get_flow(m: Viewer, view: Literal['actual', 'recent', 'planned'] = 'actual') -> dict[str, Any]:
    """actual: the last 12 full months of bank history; recent: the last 2; planned: the next 12 months."""
    b = load(m)
    f = (flow.planned(b, today=m.today) if view == 'planned'
         else flow.actual(history(m), repo.list_card_txns(m.conn, m.hid), b, today=m.today,
                          months=2 if view == 'recent' else 12))
    return {'flow': views.flow(f)}


@router.get('/checkup')
def get_checkup(m: Viewer) -> dict[str, Any]:
    b = load(m)
    return {'checkup': views.checkup(checkup.compute(history(m), b), b)}


@router.get('/recurring')
def get_recurring(m: Viewer) -> dict[str, Any]:
    """Subscriptions and other recurring charges found in the bank and card history (#34, D-084)."""
    b = load(m)
    s = subscriptions.detect(history(m), repo.list_card_txns(m.conn, m.hid), b, today=m.today)
    return {'recurring': views.recurring(s, b)}


class HideRecurringBody(Body):
    source: Literal['bank', 'card']
    payee: str = Field(min_length=1, max_length=200)
    hidden: bool = True


@router.put('/recurring/hidden')
def hide_recurring(body: HideRecurringBody, m: Editor) -> dict[str, Any]:
    """Mark a payee "not a subscription" (hidden=false shows it again): it is listed apart, out of the totals."""
    key = (body.source, body.payee.strip())

    def fn(b: Budget) -> Budget:
        rest = tuple(x for x in b.settings.not_recurring if x != key)
        if body.hidden:
            if len(rest) >= 500:
                raise ApiError(400, 'invalid', 'At most 500 payees can be hidden.')
            rest = (*rest, key)
        return replace(b, settings=replace(b.settings, not_recurring=rest))
    edit(m, 'recurring.hide' if body.hidden else 'recurring.show', fn)
    return {'source': key[0], 'payee': key[1], 'hidden': body.hidden}


class SnoozeBody(Body):
    alert: Literal['unmatched_recurring', 'unbudgeted']
    payee: str = Field(min_length=1, max_length=200)
    snoozed: bool = True


@router.put('/alerts/snooze')
def snooze_alert(body: SnoozeBody, m: Contributor) -> dict[str, Any]:
    """Set a repeating-payee alert aside until the payee posts in a later month (#104); snoozed=false brings it
    back now. Not a payee rule, so a future payment from the payee is still noticed."""
    key = body.payee.strip()

    def fn(b: Budget) -> Budget:
        rest = tuple(x for x in b.settings.alert_snoozes if (x[0], x[1]) != (body.alert, key))
        if body.snoozed:
            if len(rest) >= 500:
                raise ApiError(400, 'invalid', 'At most 500 alerts can be set aside.')
            rest = (*rest, (body.alert, key, m.today))
        return replace(b, settings=replace(b.settings, alert_snoozes=rest))
    edit(m, 'alert.snooze' if body.snoozed else 'alert.unsnooze', fn)
    return {'alert': body.alert, 'payee': key, 'snoozed': body.snoozed}


@router.get('/recap')
def get_recap(m: Viewer, span: Literal['month', 'year'] = 'month',
              period: str | None = Query(default=None, pattern=r'^\d{4}(-\d{2})?$')) -> dict[str, Any]:
    """The monthly recap or the year in review (D-090). Default: the last full month (or year) with bank history."""
    hist = history(m)
    months = recap_mod.periods_with_history(hist, span)
    if period:
        try:
            start = date.fromisoformat(period + ('-01-01' if len(period) == 4 else '-01'))
        except ValueError:
            raise ApiError(400, 'invalid', 'period is YYYY-MM (or YYYY for a year).') from None
    else:
        this = m.today.replace(month=1, day=1) if span == 'year' else m.today.replace(day=1)
        done = [d for d in months if d < this]
        start = done[-1] if done else this
    b = load(m)
    r = recap_mod.recap(hist, repo.list_card_txns(m.conn, m.hid), b, span=span, start=start, today=m.today)
    return {'recap': views.recap(r, months)}


@router.get('/card')
def get_card(m: Viewer, window: Literal['all', 'recent'] = 'all') -> dict[str, Any]:
    """window=recent: the last 2 months of card purchases only."""
    b = load(m)
    s = cards.weekly(repo.list_card_txns(m.conn, m.hid), b, b.card_categories, today=m.today,
                     months=2 if window == 'recent' else None)
    return {'summary': views.card_summary(s), 'categories': [views.category(c) for c in b.card_categories]}


class CategoryBody(Body):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=list, max_length=50)
    bank_categories: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    weekly_series: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=20)
    has_own_rows: bool = False
    # stores moved here by hand; left out, a category of the same name keeps the ones it had
    merchants: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Field(default=None, max_length=2000)


class CategoriesBody(Body):
    categories: list[CategoryBody] = Field(max_length=50)


def resort_card(m: Member, cats: tuple[CardCategory, ...]) -> int:
    """Re-sort the stored card purchases with the categories as they now are; returns how many changed."""
    moves = cards.recategorise(repo.list_card_txns(m.conn, m.hid), cats)
    with transaction(m.conn):
        for tid, cat in moves:
            m.conn.execute('UPDATE card_txns SET category = ?, by_hand = 0 WHERE household_id = ? AND id = ?',
                           (cat, m.hid, tid))
    return len(moves)


@router.put('/card/categories')
def card_categories(body: CategoriesBody, m: Editor) -> dict[str, Any]:
    names = [c.name for c in body.categories]
    if len(set(names)) != len(names):
        raise ApiError(400, 'invalid', 'Category names must be unique.')

    def fn(b: Budget) -> Budget:
        had = {c.name: c.merchants for c in b.card_categories}
        return replace(b, card_categories=tuple(CardCategory(
            c.name, tuple(k.upper() for k in c.keywords), tuple(c.bank_categories), tuple(c.weekly_series),
            c.has_own_rows, tuple(dict.fromkeys(cards.store(x) for x in c.merchants)) if c.merchants is not None
            else had.get(c.name, ())) for c in body.categories))
    b = edit(m, 'card.categories', fn)
    resort_card(m, b.card_categories)
    return {'ok': True}


CardWindow = Literal['all', 'recent']


def _card_since(m: Member, window: CardWindow, since: date | None) -> date | None:
    return since or (add_months(m.today, -2) if window == 'recent' else None)


@router.get('/card/stores')
def card_stores(m: Viewer, category: str | None = None, window: CardWindow = 'all', since: date | None = None,
                until: date | None = None) -> dict[str, Any]:
    """Card purchases grouped by store (biggest spend first), in one category when given."""
    b = load(m)
    ss = cards.stores(repo.list_card_txns(m.conn, m.hid), b.card_categories, category=category,
                      since=_card_since(m, window, since), until=until)
    return {'stores': [views.card_store(x) for x in ss[:500]], 'more': max(0, len(ss) - 500)}


@router.get('/card/purchases')
def card_purchases(m: Viewer, category: str | None = None, store: str | None = None, window: CardWindow = 'all',
                   since: date | None = None, until: date | None = None,
                   limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
    """Card purchases, newest first, in a category and/or at a store (cards.store names)."""
    start = _card_since(m, window, since)
    shop = cards.store(store) if store else None
    ts = [t for t in reversed(repo.list_card_txns(m.conn, m.hid)) if t.amount < 0
          and (start is None or t.purchased >= start) and (until is None or t.purchased <= until)
          and (category is None or (t.category or cards.OTHER) == category)
          and (shop is None or cards.store(t.description) == shop)]
    return {'purchases': [views.card_purchase(t) for t in ts[:limit]], 'more': max(0, len(ts) - limit)}


class StoreMoveBody(Body):
    store: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(min_length=1, max_length=40)]


def _target(b: Budget, category: str) -> str:
    if category != cards.OTHER and category not in {c.name for c in b.card_categories}:
        raise ApiError(400, 'invalid', 'No card category called "%s".' % category)
    return category


def move_store(body: StoreMoveBody, m: Member) -> dict[str, Any]:
    """Every purchase at the store goes to the category, also the ones moved one by one, and so do later ones.
    "Other" takes the store out of the moves; a keyword may still sort it then."""
    shop = cards.store(body.store)

    def fn(b: Budget) -> Budget:
        to = _target(b, body.category)
        return replace(b, card_categories=tuple(replace(c, merchants=tuple(
            x for x in c.merchants if x != shop) + ((shop,) if c.name == to else ()))
            for c in b.card_categories))
    b = edit(m, 'card.store', fn)
    with transaction(m.conn):    # this store's purchases follow the store move, hand moves included
        for t in repo.list_card_txns(m.conn, m.hid):
            if t.by_hand and cards.store(t.description) == shop:
                m.conn.execute('UPDATE card_txns SET by_hand = 0 WHERE household_id = ? AND id = ?', (m.hid, t.id))
    changed = resort_card(m, b.card_categories)
    now = cards.categorise(shop, '', b.card_categories)
    return {'store': shop, 'category': now, 'changed': changed,
            'note': None if now == body.category else 'A keyword of "%s" still matches this store; remove it there '
                                                      'to send the store to %s.' % (now, body.category)}


@router.put('/card/stores')
def card_store_move(body: StoreMoveBody, m: Editor) -> dict[str, Any]:
    return move_store(body, m)


class PurchaseMoveBody(Body):
    category: Annotated[str, Field(min_length=1, max_length=40)] | None   # null: sorted by the categories again


def move_purchase(txn_id: str, body: PurchaseMoveBody, m: Member) -> dict[str, Any]:
    """One purchase to a category by hand (re-sorting leaves it there), or back to the automatic sorting."""
    b = load(m)
    t = next((x for x in repo.list_card_txns(m.conn, m.hid) if x.id == txn_id), None)
    if t is None:
        raise ApiError(404, 'not_found', 'No card purchase with that id.')
    if t.amount >= 0:
        raise ApiError(400, 'invalid', 'A payment to the card has no category.')
    if body.category is None:
        cat = cards.categorise(t.description, '', b.card_categories, amount=t.amount)
        cat = t.category if cat == cards.OTHER and not t.by_hand and t.category in {
            c.name for c in b.card_categories} else cat
        repo.set_card_category(m.conn, m.hid, txn_id, cat, by_hand=False)
    else:
        cat = _target(b, body.category)
        repo.set_card_category(m.conn, m.hid, txn_id, cat, by_hand=True)
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='card.purchase', row_id=txn_id,
               detail={'by_hand': body.category is not None})
    return {'id': txn_id, 'category': cat, 'by_hand': body.category is not None}


@router.put('/card/purchases/{txn_id}')
def card_purchase_move(txn_id: str, body: PurchaseMoveBody, m: Editor) -> dict[str, Any]:
    return move_purchase(txn_id, body, m)


# ---------------------------------------------------------------- payee rules and settings

class RuleBody(Body):
    name: Name
    kind: Literal['bill', 'pay', 'ask', 'card', 'track', 'ignore', 'savings_draw', 'loan_in', 'allowance']
    contains: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=20)
    regex: Annotated[str, Field(max_length=200)] | None = None
    direction: Literal['in', 'out', 'any'] = 'any'
    min_amount: Money | None = None
    max_amount: Money | None = None
    targets: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=20)
    card: str | None = Field(default=None, max_length=40)
    note: str = Field(default='', max_length=300)


def _rule(rid: str, body: RuleBody, b: Budget) -> PayeeRule:
    if not body.contains and not body.regex:
        raise ApiError(400, 'invalid', 'A rule needs a word to look for or a pattern.')
    if body.regex:
        try:
            rules.compile_safe(body.regex)
        except ValueError as e:
            raise ApiError(400, 'invalid', 'Pattern refused: %s' % e) from None
    if body.kind == 'card' and not body.card:
        raise ApiError(400, 'invalid', 'A card-payment rule needs the card it pays.')
    known = set(b.series_by_id()) | {r.name for r in b.rows if r.series_id is None}
    if any(t not in known for t in body.targets):
        raise ApiError(400, 'invalid', 'A rule clears lines that do not exist.')
    return PayeeRule(rid, body.name, RuleKind(body.kind), tuple(body.contains), body.regex, Direction(body.direction),
                     body.min_amount, body.max_amount, tuple(body.targets), body.card, body.note)


@router.get('/rules')
def rules_list(m: Viewer) -> dict[str, Any]:
    b = load(m)
    s = b.settings
    return {'rules': [views.rule(r) for r in b.rules],
            'series': [{'id': x.id, 'name': x.name, 'kind': str(x.kind)} for x in b.series],
            'row_names': sorted({r.name for r in b.rows if r.series_id is None}),
            'settings': {'week_starts': s.week_starts,
                         'reconcile': {'days_before': s.reconcile.days_before, 'days_after': s.reconcile.days_after,
                                       'tolerance_bp': s.reconcile.tolerance_bp,
                                       'tolerance_abs': s.reconcile.tolerance_abs},
                         'alerts': {'cushion': s.alerts.cushion, 'bill_variance_bp': s.alerts.bill_variance_bp,
                                    'unscheduled_days': s.alerts.unscheduled_days,
                                    'expected_grace_days': s.alerts.expected_grace_days,
                                    'unmatched_recurring': s.alerts.unmatched_recurring},
                         'status_colours': {str(k): v for k, v in s.status_colours}, 'celebrate': s.celebrate,
                         'card_transfers_on_payday': s.card_transfers_on_payday},
            'horizon': {'mode': b.horizon.mode, 'months': b.horizon.months, 'end': views.iso(b.horizon.end)},
            'start_balance': b.start_balance, 'as_of': views.iso(b.as_of),
            'caps': [{'id': c.id, 'name': c.name, 'rule_ids': list(c.rule_ids), 'period': c.period,
                      'amount': c.amount} for c in s.alerts.caps],
            'flow_groups': [views.flow_group(g) for g in b.flow_groups], 'updated_at': m.household.updated_at,
            'version': repo.budget_version(m.conn, m.hid)}


@router.post('/rules')
def rule_add(body: RuleBody, m: Editor) -> dict[str, Any]:
    rid = new_id('rule', body.name)

    def fn(b: Budget) -> Budget:
        at = above_starter(b.rules)
        return replace(b, rules=(*b.rules[:at], _rule(rid, body, b), *b.rules[at:]))
    edit(m, 'rule.add', fn, rid)
    return {'id': rid}


def above_starter(have: tuple[PayeeRule, ...]) -> int:
    """Where a new rule goes: above the starter rules when they end the list (the first match wins, so a
    household's own rule must come before the pack's broad ones), else at the end."""
    at = len(have)
    while at and have[at - 1].id.startswith(rulepack.STARTER_PREFIX):
        at -= 1
    return at


class PackBody(Body):
    text: str | None = Field(default=None, max_length=rulepack.MAX_BYTES)   # a pack file; None = the starter pack
    dry_run: bool = True


@router.get('/rules/starter')
def rules_starter(m: Viewer) -> Response:
    """The starter pack as a file, to change and add back."""
    return Response(rulepack.starter_text(), media_type='application/yaml', headers={
        'Content-Disposition': 'attachment; filename="starter-rules.yaml"'})


@router.post('/rules/pack')
def rules_pack(body: PackBody, m: Editor) -> dict[str, Any]:
    """Add a rule pack (D-094): only rules the household does not have, linked to its paychecks and bills.
    A dry run (the default) says what would be added and what is skipped, and saves nothing."""
    try:
        pack = rulepack.starter() if body.text is None else rulepack.load(body.text.encode(), 'pack.yaml')
    except ImportProblem as e:
        raise ApiError(400, 'invalid', str(e)) from None
    out = rulepack.apply(load(m), pack)
    if not body.dry_run and out.added:
        seen: list[rulepack.Outcome] = []

        def fn(b: Budget) -> Budget:
            seen.append(rulepack.apply(b, pack))
            return seen[-1].budget
        edit(m, 'rule.pack', fn)
        out = seen[-1]
    return {'pack': pack.name, 'dry_run': body.dry_run, 'kept': out.kept, 'groups': list(out.groups),
            'categories': list(out.categories),
            'added': [{'id': r.id, 'name': r.name, 'kind': str(r.kind), 'clears': list(names)}
                      for r, names in out.added],
            'skipped': [{'name': name, 'why': why} for name, why in out.skipped]}


@router.put('/rules/{rule_id}')
def rule_edit(rule_id: str, body: RuleBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        need(any(r.id == rule_id for r in b.rules))
        new = _rule(rule_id, body, b)
        return replace(b, rules=tuple(new if r.id == rule_id else r for r in b.rules))
    edit(m, 'rule.edit', fn, rule_id)
    return {'ok': True}


@router.delete('/rules/{rule_id}')
def rule_delete(rule_id: str, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        need(any(r.id == rule_id for r in b.rules))
        return replace(b, rules=tuple(r for r in b.rules if r.id != rule_id))
    edit(m, 'rule.delete', fn, rule_id)
    return {'ok': True}


class OrderBody(Body):
    ids: list[Annotated[str, Field(max_length=120)]] = Field(max_length=500)


@router.post('/rules/order')
def rules_order(body: OrderBody, m: Editor) -> dict[str, Any]:
    def fn(b: Budget) -> Budget:
        by = {r.id: r for r in b.rules}
        if sorted(body.ids) != sorted(by):
            raise ApiError(400, 'invalid', 'The order must list every rule once.')
        return replace(b, rules=tuple(by[i] for i in body.ids))
    edit(m, 'rule.order', fn)
    return {'ok': True}


class RuleTestBody(Body):
    description: Annotated[str, Field(max_length=500)]
    amount: SignedMoney = 0


@router.post('/rules/test')
async def rules_test(body: RuleTestBody, request: Request, m: Editor) -> dict[str, Any]:
    b = load(m)
    from hbudget.core.textmask import mask
    desc = mask(body.description)
    res = await time_limited(request, lambda: rules.test(b.rules, desc, body.amount))
    first = next((r.name for r, ok in res if ok), None)
    return {'first': first, 'results': [{'id': r.id, 'name': r.name, 'matched': ok} for r, ok in res]}


class CapBody(Body):
    id: Annotated[str, Field(min_length=1, max_length=60)]
    name: Name
    rule_ids: list[Annotated[str, Field(max_length=120)]] = Field(max_length=20)
    period: Literal['week', 'month']
    amount: Money


class AlertsBody(Body):
    cushion: Money
    bill_variance_bp: Annotated[int, Field(ge=0, le=10000)]
    unscheduled_days: Annotated[int, Field(ge=0, le=60)]
    expected_grace_days: Annotated[int, Field(ge=0, le=60)]
    unmatched_recurring: bool
    caps: list[CapBody] | None = Field(default=None, max_length=20)   # None keeps them (the Check-up page sets them)


class ReconcileBody(Body):
    days_before: Annotated[int, Field(ge=0, le=60)]
    days_after: Annotated[int, Field(ge=0, le=90)]
    tolerance_bp: Annotated[int, Field(ge=0, le=10000)]
    tolerance_abs: Money


class HorizonBody(Body):
    mode: Literal['rolling', 'fixed']
    months: Annotated[int, Field(ge=1, le=60)] = 15
    end: date | None = None


class ColoursBody(Body):
    autopay: Annotated[str, Field(pattern=r'^#[0-9a-fA-F]{6}$')]
    manual: Annotated[str, Field(pattern=r'^#[0-9a-fA-F]{6}$')]


class FlowGroupBody(Body):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    colour: Annotated[str, Field(pattern=r'^(#[0-9a-fA-F]{6}|--f-[a-z]+)$')] = '--f-other'
    rule_ids: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=50)
    series_ids: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=100)
    row_names: list[Annotated[str, Field(max_length=80)]] = Field(default_factory=list, max_length=100)


class SettingsBody(Body):
    alerts: AlertsBody
    reconcile: ReconcileBody
    horizon: HorizonBody
    status_colours: ColoursBody
    week_starts: Literal[0, 6] = 0
    start_balance: SignedMoney | None = None
    as_of: date | None = None
    flow_groups: list[FlowGroupBody] | None = Field(default=None, max_length=30)   # None keeps them (Report sets them)
    card_transfers_on_payday: bool | None = None     # None keeps it
    expected_version: int | None = None


@router.put('/settings')
def put_settings(body: SettingsBody, m: Editor) -> dict[str, Any]:
    if body.horizon.mode == 'fixed' and body.horizon.end is None:
        raise ApiError(400, 'invalid', 'A fixed horizon needs an end date.')
    repo.edit_budget(m.conn, m.hid, lambda b: _apply_settings(b, body), user_id=m.user_id, action='settings.save',
                     expected_version=body.expected_version)
    return {'ok': True}


def _caps(b: Budget, body: list[CapBody]) -> tuple[CapRule, ...]:
    known_rules = {r.id for r in b.rules}
    if any(r not in known_rules for c in body for r in c.rule_ids):
        raise ApiError(400, 'invalid', 'A cap counts a rule that does not exist.')
    return tuple(CapRule(c.id, c.name, tuple(c.rule_ids), c.period, c.amount) for c in body)


def _groups(body: list[FlowGroupBody]) -> tuple[FlowGroup, ...]:
    if len({g.name for g in body}) < len(body):
        raise ApiError(400, 'invalid', 'Two report groups have the same name.')
    return tuple(FlowGroup(g.name, g.colour, tuple(g.rule_ids), tuple(g.series_ids), tuple(g.row_names)) for g in body)


class CapsBody(Body):
    caps: list[CapBody] = Field(max_length=20)


class GroupsBody(Body):
    groups: list[FlowGroupBody] = Field(max_length=30)


@router.put('/caps')
def put_caps(body: CapsBody, m: Editor) -> dict[str, Any]:
    """Spending caps (the Check-up page)."""
    def fn(b: Budget) -> Budget:
        s = b.settings
        return replace(b, settings=replace(s, alerts=replace(s.alerts, caps=_caps(b, body.caps))))
    edit(m, 'settings.caps', fn)
    return {'ok': True}


@router.put('/flow-groups')
def put_flow_groups(body: GroupsBody, m: Editor) -> dict[str, Any]:
    """The Report's groups."""
    edit(m, 'settings.groups', lambda b: replace(b, flow_groups=_groups(body.groups)))
    return {'ok': True}


def _apply_settings(b: Budget, body: SettingsBody) -> Budget:
    caps = b.settings.alerts.caps if body.alerts.caps is None else _caps(b, body.alerts.caps)
    s = b.settings
    a = body.alerts
    settings = replace(
        s, week_starts=body.week_starts,
        alerts=replace(s.alerts, cushion=a.cushion, bill_variance_bp=a.bill_variance_bp,
                       unscheduled_days=a.unscheduled_days, expected_grace_days=a.expected_grace_days,
                       unmatched_recurring=a.unmatched_recurring, caps=caps),
        reconcile=replace(s.reconcile, days_before=body.reconcile.days_before, days_after=body.reconcile.days_after,
                          tolerance_bp=body.reconcile.tolerance_bp, tolerance_abs=body.reconcile.tolerance_abs),
        status_colours=((Status.AUTOPAY, body.status_colours.autopay.lower()),
                        (Status.MANUAL, body.status_colours.manual.lower())),
        card_transfers_on_payday=s.card_transfers_on_payday if body.card_transfers_on_payday is None
        else body.card_transfers_on_payday)
    groups = b.flow_groups if body.flow_groups is None else _groups(body.flow_groups)
    nb = replace(b, settings=settings, flow_groups=groups,
                 horizon=Horizon(body.horizon.mode, body.horizon.months, body.horizon.end))
    if body.start_balance is not None:
        nb = replace(nb, start_balance=body.start_balance)
    if body.as_of is not None:
        nb = replace(nb, as_of=body.as_of)
    if nb.horizon != b.horizon or nb.as_of != b.as_of:
        nb = regenerate(nb)
    return nb


class NotesBody(Body):
    notes: Annotated[str, Field(max_length=100_000)]
    expected_updated_at: str | None = None


@router.put('/notes')
def put_notes(body: NotesBody, m: Editor) -> dict[str, Any]:
    check_updated(m, body.expected_updated_at)
    repo.save_notes(m.conn, m.hid, body.notes, user_id=m.user_id)
    h = repo.get_household(m.conn, m.hid)
    return {'ok': True, 'updated_at': h.updated_at if h else None}


# ---------------------------------------------------------------- import and export

async def read_upload(request: Request, f: UploadFile, allowed: tuple[str, ...]) -> tuple[bytes, str]:
    name = f.filename or 'upload'
    check_ext(name, allowed)
    limit = request.app.state.settings.max_upload_bytes
    return check_upload(await f.read(limit + 1), name, limit)


def check_ext(name: str, allowed: tuple[str, ...]) -> None:
    ext = '.' + name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in allowed:
        raise ApiError(415, 'type', 'Use a file ending in %s.' % ', '.join(allowed))


def check_upload(data: bytes, name: str, limit: int) -> tuple[bytes, str]:
    """An uploaded file's size and content checks; returns it with its cleaned name (check_ext first)."""
    from hbudget.io import clean_filename
    ext = '.' + name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if len(data) > limit:
        raise ApiError(413, 'too_large', 'The file is larger than %d MB.' % (limit // (1024 * 1024)))
    if not data:
        raise ApiError(400, 'import', 'The file is empty.')
    zipped = data[:4] == b'PK\x03\x04'
    if (ext in ('.ods', '.xlsx') and not zipped) or (ext not in ('.ods', '.xlsx') and zipped):
        raise ApiError(415, 'type', 'The file content does not match its name.')
    if ext in ('.ofx', '.qfx') and b'OFX' not in data[:4096].upper():
        raise ApiError(415, 'type', 'This does not look like an OFX file.')
    return data, clean_filename(name)


def _history_counts(m: Member) -> dict[str, int]:
    """What a backup restore would delete: the household's current rows and history (hidden uploads counted)."""
    return repo.history_counts(m.conn, m.hid)


def _counts(b: Budget) -> dict[str, int]:
    return {'series': len(b.series), 'rows': len(b.rows), 'rules': len(b.rules), 'accounts': len(b.accounts)}


@router.post('/import/billsystem')
async def import_billsystem(request: Request, m: Owner, file: Annotated[UploadFile, File()],
                            mode: Annotated[Literal['replace', 'merge'], Form()] = 'merge',
                            dry_run: Annotated[str, Form(max_length=5)] = '1',
                            confirm: Annotated[str, Form(max_length=80)] = '',
                            set_balance: Annotated[str, Form(max_length=5)] = '0') -> dict[str, Any]:
    data, name = await read_upload(request, file, ('.yaml', '.yml', '.json', '.csv'))
    dry = dry_run not in ('0', 'false', '')
    if name.lower().endswith('.csv'):
        series_list = billsystem.bills_csv(data, today=m.today)
        b = load(m)

        def merged(b: Budget) -> Budget:
            taken = {s.id for s in b.series}
            add = [s for s in series_list if s.id not in taken] if mode == 'merge' else series_list
            # replace keeps the bank history, uploads and rules: only the bills and their rows are replaced
            return regenerate(replace(b, series=(*b.series, *add) if mode == 'merge' else tuple(add),
                                      rows=b.rows if mode == 'merge' else ()))
        nb = merged(b) if dry else edit(m, 'import.bills_csv', merged)
        f = fc.compute(nb, today=m.today)
        return {'dry_run': dry, 'counts': _counts(nb), 'warnings': [],
                'forecast': views.forecast_readout(f, today=m.today)}
    bs = billsystem.load(data, name, today=m.today)
    set_bal = set_balance not in ('0', 'false', '')
    if bs.extras is not None and mode == 'merge':
        raise ImportProblem('A backup can only replace a household, not be merged into one.')
    if dry:
        out: dict[str, Any] = {'dry_run': True, 'warnings': list(bs.warnings), 'is_backup': bs.extras is not None}
        if bs.extras is not None:
            nb = bs.budget
            out['replaces'] = _history_counts(m)
            out['balance'] = {'taken': True, 'start_balance': nb.start_balance, 'as_of': views.iso(nb.as_of)}
        else:
            old = load(m)
            nb, taken = billimport.combine(old, bs, mode, set_balance=set_bal, today=m.today)
            out['balance'] = {'taken': taken, 'start_balance': nb.start_balance, 'as_of': views.iso(nb.as_of),
                              'file_start_balance': bs.budget.start_balance, 'file_as_of': views.iso(bs.budget.as_of)}
            out['rows'] = billimport.row_changes(old, nb)
            out['forecast_before'] = views.forecast_readout(fc.compute(old, today=m.today), today=m.today)
        out.update(counts=_counts(nb), forecast=views.forecast_readout(fc.compute(nb, today=m.today), today=m.today))
        return out
    if bs.extras is not None and confirm != m.household.name:
        raise ApiError(400, 'confirm', 'Restoring a backup replaces everything in this household, including its bank '
                                       'history. Type the household name to confirm.')
    taken = True
    if bs.extras is not None:
        # a full backup restores everything (history included) into the emptied household
        bs = repo.import_bill_system(m.conn, m.hid, data, name, today=m.today, user_id=m.user_id, mode='replace')
        nb = load(m)
    else:
        # a bill system replaces or merges the budget part only; bank history, uploads and profiles stay
        result: list[bool] = []

        def fn(old: Budget) -> Budget:
            nb, took = billimport.combine(old, bs, mode, set_balance=set_bal, today=m.today)
            result.append(took)
            return nb
        nb = edit(m, 'import.' + mode, fn)
        taken = result[0]
    return {'dry_run': False, 'counts': _counts(nb), 'warnings': list(bs.warnings),
            'balance': {'taken': taken, 'start_balance': nb.start_balance, 'as_of': views.iso(nb.as_of)},
            'forecast': views.forecast_readout(fc.compute(nb, today=m.today), today=m.today)}


def _mapping_json(mp: workbook_import.WorkbookMapping) -> dict[str, Any]:
    return billsystem._mapping_doc(mp)


@router.post('/import/workbook/preview')
async def workbook_preview(request: Request, m: Owner, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    data, name = await read_upload(request, file, ('.ods', '.xlsx', '.csv'))
    sheets = await asyncio.to_thread(tabular.read_table, data, name)
    p = workbook_import.preview(sheets, None)
    token = secrets.token_urlsafe(16)
    cache_put(request.app, 'wb:' + token, sheets, (m.user_id, m.hid))
    from hbudget.core.textmask import mask
    return {'token': token, 'sheets': list(p.sheets), 'sheet': p.sheet, 'columns': list(p.columns),
            'header': list(p.header) if p.header else None,
            'rows': [[mask(c) for c in r] for r in p.rows], 'guess': _mapping_json(p.guess),
            'names': [mask(n) for n in p.names]}


class CommitBody(Body):
    token: Annotated[str, Field(max_length=64)]
    mapping: dict[str, Any]
    save_profile: Annotated[str, Field(max_length=60)] | None = None


@router.post('/import/workbook/commit')
def workbook_commit(body: CommitBody, request: Request, m: Owner) -> dict[str, Any]:
    sheets = cache_get(request.app, 'wb:' + body.token, (m.user_id, m.hid))
    if sheets is None:
        raise ApiError(400, 'expired', 'The file is no longer in memory. Read it again.')
    mp = billsystem._mapping_from('workbook', body.mapping)
    if not isinstance(mp, workbook_import.WorkbookMapping):
        raise ApiError(400, 'invalid', 'Not a workbook mapping.')
    for pat in mp.skip_names:
        core(lambda pat=pat: rules.compile_safe(pat))   # type: ignore[misc]
    imported = workbook_import.import_rows(sheets, mp, today=m.today)

    def take(old: Budget) -> Budget:
        # only the budget part (balance, series and rows) is replaced; bank and card history, uploads,
        # reconciliations, profiles, rules, debts and settings stay
        return replace(imported.budget, rules=old.rules, accounts=old.accounts, expected=old.expected,
                       card_categories=old.card_categories, flow_groups=old.flow_groups,
                       settings=replace(old.settings, status_colours=imported.budget.settings.status_colours))
    nb = edit(m, 'import.workbook', take)
    if body.save_profile:
        from hbudget.io.bank import MappingProfile
        repo.save_profile(m.conn, m.hid, MappingProfile(new_id('wb', body.save_profile), body.save_profile, 'workbook',
                                                         mp, ()))
    f = fc.compute(nb, today=m.today)
    sc = imported.stored_check
    # match / mismatch per stored cell the mapping names (the workbook's own forecast)
    check = sc.matches(f) if sc is not None else None
    return {'dry_run': False, 'counts': _counts(nb), 'warnings': list(imported.warnings), 'stored_check': check,
            'forecast': views.forecast_readout(f, today=m.today)}


def _download(content: bytes | str, ext: str, media: str, household: str = '') -> Response:
    stamp = datetime.now(UTC).strftime('%Y-%m-%d')
    # the household's name as a plain a-z0-9 slug: no user text reaches the header unescaped
    name = slug(household)[:40].strip('-')
    fname = 'hearthfund-%s%s.%s' % (name + '-' if name else '', stamp, ext)
    body = content.encode() if isinstance(content, str) else content
    return Response(body, media_type=media, headers={
        'Content-Disposition': "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fname, fname)})


@router.get('/export/backup.yaml')
def export_yaml(m: Editor) -> Response:
    return _download(repo.backup(m.conn, m.hid, fmt='yaml', app_version=__version__, user_id=m.user_id), 'yaml',
                     'application/yaml', m.household.name)


@router.get('/export/backup.json')
def export_json(m: Editor) -> Response:
    return _download(repo.backup(m.conn, m.hid, fmt='json', app_version=__version__, user_id=m.user_id), 'json',
                     'application/json', m.household.name)


@router.get('/export/forecast.csv')
def export_csv(m: Viewer) -> Response:
    b = load(m)
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='export.forecast', detail={'format': 'csv'})
    return _download(export.forecast_csv(b, fc.compute(b, today=m.today)), 'csv', 'text/csv', m.household.name)


@router.get('/export/forecast.xlsx')
def export_xlsx(m: Viewer) -> Response:
    b = load(m)
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='export.forecast', detail={'format': 'xlsx'})
    return _download(export.forecast_xlsx(b, fc.compute(b, today=m.today)), 'xlsx',
                     'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', m.household.name)


class WipeBody(Body):
    confirm: Annotated[str, Field(max_length=80)]


@router.post('/wipe')
def wipe(body: WipeBody, m: Owner) -> dict[str, Any]:
    if body.confirm != m.household.name:
        raise ApiError(400, 'invalid', 'Type the household name exactly to confirm.')
    repo.wipe_household(m.conn, m.hid, user_id=m.user_id, today=m.today)
    return {'ok': True}


# ---------------------------------------------------------------- members (owner)

class MemberAdd(Body):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    role: Literal['owner', 'editor', 'contributor', 'viewer']
    password: Annotated[str, Field(max_length=256)] | None = None


class MemberRole(Body):
    role: Literal['owner', 'editor', 'contributor', 'viewer']


@router.get('/members')
def members(m: Owner) -> dict[str, Any]:
    return {'members': [{'user_id': x.user_id, 'username': x.username, 'role': x.role}
                        for x in repo.list_members(m.conn, m.hid)]}


@router.post('/members')
def member_add(body: MemberAdd, request: Request, m: Owner) -> dict[str, Any]:
    second_factor(request, m.conn, m.auth.user)     # a new member is a new way in (D-095)
    admin = m.auth.user.is_admin
    if not body.password and not admin:
        # only the admin adds someone who already has an account (D-098): an owner could otherwise pull any user
        # of the server into their household, and learn which usernames exist
        raise ApiError(403, 'admin_only', 'Only the admin can add someone who already has an account. Give a '
                                          'password to create a new user for this household.')
    u = users.get_user_by_name(m.conn, body.username)
    if body.password:
        if u is not None:
            raise ApiError(409, 'taken', 'That username is taken. Leave the password empty to add the existing user.'
                           if admin else 'That username is taken. Choose another one.')
        try:
            uid = users.create_user(m.conn, body.username, body.password)
        except ValueError as e:
            raise ApiError(400, 'invalid', str(e)) from None
        repo.audit(m.conn, household_id=None, user_id=m.user_id, action='user.create', row_id=str(uid))
    else:
        if u is None:
            raise ApiError(404, 'not_found', 'No user with that name. Give a password to create one.')
        uid = u.id
    repo.add_member(m.conn, m.hid, uid, body.role)
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='member.add', row_id=str(uid),
               detail={'role': body.role})
    return {'user_id': uid}


@router.put('/members/{user_id}')
def member_role(user_id: int, body: MemberRole, request: Request, m: Owner) -> dict[str, Any]:
    second_factor(request, m.conn, m.auth.user)
    try:
        repo.set_role(m.conn, m.hid, user_id, body.role)
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='member.role', row_id=str(user_id),
               detail={'role': body.role})
    return {'ok': True}


@router.delete('/members/{user_id}')
def member_remove(user_id: int, m: Owner) -> dict[str, Any]:
    try:
        repo.remove_member(m.conn, m.hid, user_id)
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='member.remove', row_id=str(user_id))
    return {'ok': True}


@router.get('/audit')
def audit_log(m: Owner) -> dict[str, Any]:
    """The household's audit log (ids and field names only; imported entries came from a restored backup)."""
    return {'entries': [{'at': a['at'], 'user': a['username'], 'action': a['action'], 'row': a['row'],
                         'imported': a['imported'], 'via': a['detail'].get('via')}
                        for a in repo.list_audit(m.conn, m.hid, limit=300)]}


# ---------------------------------------------------------------- mapping profiles

@router.get('/profiles')
def profiles(m: Editor, kind: Literal['bank', 'card', 'workbook'] | None = None) -> dict[str, Any]:
    from hbudget.io.bank import BUILTIN_PROFILES
    ps = [p for p in (*BUILTIN_PROFILES, *repo.list_profiles(m.conn, m.hid)) if kind is None or p.kind == kind
          or (kind == 'card' and p.kind == 'bank' and p.builtin)]
    return {'profiles': [{'id': p.id, 'name': p.name, 'kind': p.kind, 'builtin': p.builtin} for p in ps]}


@router.delete('/profiles/{profile_id}')
def profile_delete(profile_id: str, m: Editor) -> dict[str, Any]:
    try:
        repo.delete_profile(m.conn, m.hid, profile_id)
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    return {'ok': True}


__all__ = ['alerts_mod', 'router']
