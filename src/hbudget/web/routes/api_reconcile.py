"""Uploads (bank and card exports) and reconciliation: /api/h/{hid}/uploads, /reconcile, /reconciliations."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import replace
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import Field

from hbudget.core import reconcile
from hbudget.core.models import Budget
from hbudget.core.textmask import mask
from hbudget.db import repo
from hbudget.db.connection import transaction
from hbudget.io import ImportProblem, billsystem, csvfile
from hbudget.io.bank import (
    BUILTIN_PROFILES,
    BankFile,
    BankMapping,
    MappingProfile,
    card_date_pattern,
    detect_profile,
    id_column,
    purchase_date,
    read_bank_csv,
    read_card,
)
from hbudget.io.ofx import read_ofx
from hbudget.web import views
from hbudget.web.app import cache_get, cache_put
from hbudget.web.deps import ApiError, Editor, Member, Viewer
from hbudget.web.routes.api_household import Body, core, load, new_id, read_upload, time_limited

router = APIRouter(prefix='/api/h/{hid}')
BANK_EXT = ('.csv', '.ofx', '.qfx')


def _upload_json(u: repo.Upload) -> dict[str, Any]:
    return {'id': u.id, 'kind': u.kind, 'filename': u.filename, 'uploaded_at': u.uploaded_at, 'rows': u.rows,
            'date_from': views.iso(u.date_from), 'date_to': views.iso(u.date_to), 'latest_balance': u.latest_balance,
            'has_balance': u.has_balance, 'status': u.status, 'note': u.note}


@router.get('/uploads')
def uploads(m: Viewer) -> dict[str, Any]:
    return {'uploads': [_upload_json(u) for u in repo.list_uploads(m.conn, m.hid)]}


def _profiles(m: Member, kind: str = 'bank') -> list[MappingProfile]:
    """Layouts for bank or card CSV files (the household's own first; the built-in ones read both)."""
    return [*repo.list_profiles(m.conn, m.hid, kind=kind), *BUILTIN_PROFILES]


def _store_bank(m: Member, upload_id: str, name: str, bf: BankFile) -> repo.Upload:
    with transaction(m.conn):
        repo.create_upload(m.conn, m.hid, upload_id=upload_id, kind='bank', filename=name,
                           uploaded_by=m.auth.user.username, rows=len(bf.txns), date_from=bf.first, date_to=bf.last,
                           latest_balance=bf.latest_balance, has_balance=bf.has_balance)
        new, dup = repo.insert_txns(m.conn, m.hid, upload_id, bf.txns)
        repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='upload.bank', row_id=upload_id,
                   detail={'rows': len(bf.txns), 'new': new, 'duplicates': dup})
    u = repo.get_upload(m.conn, m.hid, upload_id)
    assert u is not None
    return u


def _store_card(m: Member, upload_id: str, name: str, data: bytes, mapping: BankMapping | None) -> repo.Upload:
    b = load(m)
    txns = read_card(data, name, mapping, b.card_categories)
    first = min((t.purchased for t in txns), default=None)
    last = max((t.posted for t in txns), default=None)
    with transaction(m.conn):
        repo.create_upload(m.conn, m.hid, upload_id=upload_id, kind='card', filename=name,
                           uploaded_by=m.auth.user.username, rows=len(txns), date_from=first, date_to=last,
                           latest_balance=None, has_balance=False, status='imported',
                           note='card purchases added to the Card tab')
        new, dup = repo.insert_card_txns(m.conn, m.hid, upload_id, txns)
        pattern = card_date_pattern(mapping)
        redated = repo.redate_card_txns(m.conn, m.hid, lambda d, p: purchase_date(d, p, pattern))
        repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='upload.card', row_id=upload_id,
                   detail={'rows': len(txns), 'new': new, 'duplicates': dup, 'redated': redated})
    u = repo.get_upload(m.conn, m.hid, upload_id)
    assert u is not None
    return u


def _guess(header: list[str]) -> dict[str, Any]:
    low = [h.lower() for h in header]

    def find(*words: str) -> str | None:
        for w in words:
            for h, lh in zip(header, low, strict=True):
                if w in lh:
                    return h
        return None
    return {'date_col': find('date', 'posted'), 'description_col': find('description', 'memo', 'payee', 'name'),
            'amount_col': find('amount'), 'debit_col': find('debit', 'withdrawal'),
            'credit_col': find('credit', 'deposit'),
            'balance_col': find('balance'), 'id_col': id_column(header)}


def suggest_mapping(header: list[str]) -> dict[str, Any]:
    """A first guess at a column mapping (BankMapping field names) for a caller to check and send back."""
    g = _guess(header)

    def find(word: str) -> str | None:
        return next((h for h in header if word in h.lower()), None)
    out: dict[str, Any] = {'date_col': g['date_col'], 'description_cols': [g['description_col']] if g['description_col']
                           else [], 'balance_col': g['balance_col'],
                           'id_col': id_column(header),
                           'status_col': find('status'), 'category_col': find('category')}
    if g['amount_col']:
        out['amount_col'] = g['amount_col']
    else:
        out.update(debit_col=g['debit_col'], credit_col=g['credit_col'])
    if out['status_col']:
        out['posted_values'] = ['Posted']
    return {k: v for k, v in out.items() if v not in (None, [])}


def ingest(m: Member, upload_id: str, data: bytes, name: str, kind: str, profile_id: str = '') -> dict[str, Any]:
    """Read and store a bank or card export (the page and /api/v1 share it). When no layout fits, nothing is stored
    and the answer has needs_mapping, the masked header and a sample, so the caller can ask for a mapping."""
    profile: MappingProfile | None = None
    if profile_id:
        every = [*repo.list_profiles(m.conn, m.hid), *BUILTIN_PROFILES]
        profile = next((p for p in every if p.id == profile_id), None)
        if profile is None:
            raise ApiError(404, 'not_found', 'No such layout profile.')
        if profile.kind == 'workbook' or (profile.kind != kind and not profile.builtin):
            raise ApiError(400, 'invalid', 'That layout is for %s files, not %s exports.' % (profile.kind, kind))
    if name.lower().endswith(('.ofx', '.qfx')):
        if kind == 'card':
            raise ApiError(415, 'type', 'Card exports are read from CSV files.')
        return _upload_json(_store_bank(m, upload_id, name, read_ofx(data)))
    rows = csvfile.read_rows(data)
    if not rows:
        raise ImportProblem('The file has no transactions.')
    header = [c.strip() for c in rows[0]]
    if profile is None:
        profile = detect_profile(header, _profiles(m, kind))
    if profile is None:
        return {'id': upload_id, 'kind': kind, 'needs_mapping': True, 'header': [mask(h) for h in header],
                'sample': [[mask(c) for c in r] for r in rows[1:6]], 'guess': _guess(header)}
    mapping = profile.mapping
    assert isinstance(mapping, BankMapping)
    if kind == 'card':
        return _upload_json(_store_card(m, upload_id, name, data, mapping))
    return _upload_json(_store_bank(m, upload_id, name, read_bank_csv(data, mapping)))


@router.post('/uploads')
async def upload(request: Request, m: Editor, file: Annotated[UploadFile, File()],
                 kind: Annotated[Literal['bank', 'card'], Form()] = 'bank',
                 profile_id: Annotated[str, Form(max_length=120)] = '') -> dict[str, Any]:
    data, name = await read_upload(request, file, BANK_EXT)
    upload_id = 'up-' + secrets.token_hex(8)
    out = await time_limited(request, lambda: ingest(m, upload_id, data, name, kind, profile_id))
    if out.get('needs_mapping'):
        cache_put(request.app, 'up:' + upload_id, (data, name, kind), (m.user_id, m.hid))
    return out


class MappingBody(Body):
    mapping: dict[str, Any]
    save_as: Annotated[str, Field(max_length=60)] | None = None


@router.post('/uploads/{upload_id}/mapping')
async def upload_mapping(upload_id: str, body: MappingBody, request: Request, m: Editor) -> dict[str, Any]:
    hit = cache_get(request.app, 'up:' + upload_id, (m.user_id, m.hid))
    if hit is None:
        raise ApiError(400, 'expired', 'The file is no longer in memory. Upload it again.')
    data, name, kind = hit
    return await time_limited(request, lambda: ingest_mapped(m, upload_id, data, name, kind, body.mapping,
                                                             body.save_as))


def parse_mapping(raw_in: dict[str, Any]) -> BankMapping:
    raw = {k: v for k, v in raw_in.items() if v is not None}
    if isinstance(raw.get('description_cols'), list):
        raw['description_cols'] = [x for x in raw['description_cols'] if x]
    mapping = billsystem._mapping_from('bank', raw)
    if not isinstance(mapping, BankMapping):
        raise ApiError(400, 'invalid', 'Not a bank mapping.')
    return mapping


def ingest_mapped(m: Member, upload_id: str, data: bytes, name: str, kind: str, raw: dict[str, Any],
                  save_as: str | None = None) -> dict[str, Any]:
    """Read a CSV export with a column mapping the person (or agent) gave; save_as keeps it as a layout, so the
    next export with the same columns is read without asking."""
    mapping = parse_mapping(raw)
    rows = csvfile.read_rows(data)
    if not rows:
        raise ImportProblem('The file has no transactions.')
    header = tuple(c.strip() for c in rows[0])
    if kind == 'card':
        u = _store_card(m, upload_id, name, data, mapping)
    else:
        u = _store_bank(m, upload_id, name, read_bank_csv(data, mapping))
    if save_as:
        repo.save_profile(m.conn, m.hid, MappingProfile(new_id('profile', save_as), save_as,
                                                        'card' if kind == 'card' else 'bank', mapping, header))
    return _upload_json(u)


@router.get('/uploads/{upload_id}/removal')
def upload_removal(upload_id: str, m: Editor) -> dict[str, Any]:
    return removal(m, upload_id)


def removal(m: Member, upload_id: str) -> dict[str, Any]:
    """What deleting the upload with its postings would remove, for the confirmation (#88)."""
    try:
        r = repo.upload_removal(m.conn, m.hid, upload_id)
    except KeyError:
        raise ApiError(404, 'not_found', 'Not found.') from None
    return {'can_delete': r.can_delete, 'bank_postings': r.bank, 'card_purchases': r.card,
            'first': views.iso(r.first), 'last': views.iso(r.last)}


@router.delete('/uploads/{upload_id}')
def upload_delete(upload_id: str, m: Editor, postings: bool = False) -> dict[str, Any]:
    return delete_upload(m, upload_id, postings=postings)


def delete_upload(m: Member, upload_id: str, *, postings: bool = False) -> dict[str, Any]:
    """Hide an upload, keeping its postings in the history; with `postings`, delete a waiting, unreconciled one
    together with the postings nothing else holds (#88)."""
    u = repo.get_upload(m.conn, m.hid, upload_id)
    if u is None:
        raise ApiError(404, 'not_found', 'Not found.')
    outcome = repo.delete_upload(m.conn, m.hid, upload_id, postings=postings)
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='upload.' + ('delete' if outcome == 'deleted'
                                                                                  else 'hide'), row_id=upload_id)
    return {'ok': True, 'outcome': outcome}


class PreviewBody(Body):
    upload_id: Annotated[str, Field(max_length=60)]


class ApplyBody(Body):
    upload_id: Annotated[str, Field(max_length=60)]
    fingerprint: Annotated[str, Field(max_length=64)] | None = None
    accept: dict[Annotated[str, Field(max_length=20)], Literal['clear', 'reduce']] = Field(default_factory=dict,
                                                                                          max_length=200)


FILE_PROBLEMS = frozenset({'chain_break', 'empty', 'bad_order'})
WAITING_NOTES = {
    'no_basis': 'Waiting: the checkbook balance was not found in this export. Set the checkbook balance (More > '
                'Settings) to a balance shown in the export, or upload an export that reaches back to it.',
    'nothing_new': 'Waiting: nothing in this export is newer than the last reconciliation.',
}


def fingerprint(version: int, result: reconcile.ReconcileResult) -> str:
    """What the user decided on: the budget version and every proposal's id and targets. Apply refuses (409) when
    it no longer matches, so a decision is never applied to proposals that changed in between."""
    doc = [version, [(p.id, p.txn_id, list(p.row_ids), p.kind) for p in result.proposals],
           [(a.txn_id, a.row_id, a.amount) for a in result.auto]]
    return hashlib.sha256(json.dumps(doc).encode()).hexdigest()[:32]


def _match(m: Member, upload_id: str) -> tuple[Budget, reconcile.Prepared, reconcile.ReconcileResult]:
    u = repo.get_upload(m.conn, m.hid, upload_id)
    if u is None or u.kind != 'bank':
        raise ApiError(404, 'not_found', 'Not found.')
    if u.status != 'waiting':
        raise ApiError(409, 'used', 'This upload was already %s.' % u.status)
    b = load(m)
    txns = repo.upload_txns(m.conn, m.hid, upload_id)
    try:
        prepared = reconcile.prepare(txns, b, seen_ids=repo.seen_txn_ids(m.conn, m.hid))
    except reconcile.ReconcileError as e:
        if e.code in FILE_PROBLEMS:
            # the file itself is wrong (a gap in the balance chain, no postings, a broken order): it cannot be used
            repo.set_upload_status(m.conn, m.hid, upload_id, 'rejected', e.message)
        else:
            # the budget's state (no starting point found, nothing new yet): keep the upload waiting and say what to do
            repo.set_upload_status(m.conn, m.hid, upload_id, 'waiting', WAITING_NOTES.get(e.code, e.message))
        raise
    return b, prepared, reconcile.match(b, prepared)


def preview(m: Member, upload_id: str) -> dict[str, Any]:
    """A dry run: computed, never stored. The fingerprint goes back with apply."""
    b, prepared, result = _match(m, upload_id)
    out = views.preview(b, prepared, result)
    out['fingerprint'] = fingerprint(repo.budget_version(m.conn, m.hid), result)
    return out


def apply(m: Member, upload_id: str, fp: str | None, accept: dict[str, Any]) -> dict[str, Any]:
    """Apply a dry run, all or nothing: 409 when the budget or the proposals changed since the preview."""
    with transaction(m.conn, immediate=True):
        version = repo.budget_version(m.conn, m.hid)
        b, prepared, result = _match(m, upload_id)
        if fp is not None and fp != fingerprint(version, result):
            raise ApiError(409, 'stale', 'The budget or the proposals changed since the dry run. Preview again.')
        ids = {p.id for p in result.proposals}
        unknown = [k for k in accept if k not in ids]
        if unknown:
            raise ApiError(400, 'unknown_proposal', 'Unknown proposal id(s): %s.' % ', '.join(sorted(unknown)))
        applied = core(lambda: reconcile.apply(b, result, accept, today=m.today))
        report = reconcile.report(applied, dry_run=False, today=m.today)
        repo.edit_budget(m.conn, m.hid, lambda _b: applied.budget, user_id=m.user_id, action='reconcile.apply',
                         row_id=upload_id, expected_version=version)
        rec_id = 'rec-' + secrets.token_hex(6)
        repo.save_reconciliation(
            m.conn, m.hid, rec_id=rec_id, by_user=m.auth.user.username, upload_id=upload_id,
            through=prepared.through, new_balance=applied.budget.start_balance,
            result={'cleared': [{'row': c.row_id, 'txn': c.txn_id, 'how': c.how} for c in applied.cleared],
                    'accepted': sorted(accept), 'undo': _undo_data(b, applied.budget),
                    'details': _details(b, prepared, result, accept)}, report_md=report)
        repo.set_upload_status(m.conn, m.hid, upload_id, 'imported',
                               'reconciled through %s' % prepared.through.isoformat())
    return {'id': rec_id, 'report_md': report, 'balance': applied.budget.start_balance,
            'cleared': len(applied.cleared)}


def _details(b: Budget, prepared: reconcile.Prepared, result: reconcile.ReconcileResult,
             accept: dict[str, Any]) -> dict[str, Any]:
    """What the reconciliation saw and decided, kept for get_reconciliation (#93): every new bank line with what
    happened to it (auto-matched, a proposal and its decision, tracked by a rule, or unknown and why)."""
    txns = {t.id: t for t in prepared.new}

    def line(tid: str) -> dict[str, Any]:
        t = txns.get(tid)
        return {'txn': tid, 'posted': views.iso(t.posted) if t else None, 'amount': t.amount if t else None,
                'description': t.description if t else ''}
    rows = {r.id: r for r in b.rows}

    def row(rid: str) -> dict[str, Any]:
        r = rows.get(rid)
        return {'row': rid, 'name': r.name if r else '', 'due': views.iso(r.due_date) if r else None,
                'amount': r.amount if r else None}
    return {
        'balance_before': b.start_balance, 'balance_checked': prepared.balance_checked,
        'auto': [{**line(a.txn_id), **row(a.row_id), 'rule': a.rule_id, 'took': a.amount} for a in result.auto],
        'proposals': [{'id': p.id, **line(p.txn_id), 'rows': [row(r) for r in p.row_ids], 'kind': p.kind,
                       'note': p.note, 'decision': accept.get(p.id) or 'skipped'} for p in result.proposals],
        'tracked': [{**line(t.txn_id), 'rule': t.rule_id, 'kind': str(t.kind)} for t in result.tracked],
        'unknown': [{**line(u.txn_id), 'reason': u.reason} for u in result.unknown],
    }


def _undo_data(before: Budget, after: Budget) -> dict[str, Any]:
    """What undoing a reconciliation puts back: the rows it changed (as they were and as it left them), the
    checkbook balance and dates, and the expected items it marked done."""
    old = {r.id: repo.row_rec(r) for r in before.rows}
    new = {r.id: repo.row_rec(r) for r in after.rows}
    changed = sorted(i for i in old if i in new and old[i] != new[i])
    done = sorted(e.id for e in after.expected if e.status == 'done'
                  and any(x.id == e.id and x.status != 'done' for x in before.expected))
    return {'rows_before': [old[i] for i in changed], 'rows_after': [new[i] for i in changed],
            'start_balance': before.start_balance, 'as_of': views.iso(before.as_of),
            'last_reconciled_through': views.iso(before.last_reconciled_through), 'expected_done': done,
            'balance_after': after.start_balance, 'as_of_after': views.iso(after.as_of)}


def undo(m: Member, rec_id: str) -> dict[str, Any]:
    """Undo the newest reconciliation: reopen and restore the rows it changed, put back the checkbook balance and
    dates, and make its upload waiting again. Refused when anything it touched changed since."""
    with transaction(m.conn, immediate=True):
        recs = repo.list_reconciliations(m.conn, m.hid)
        rec = next((r for r in recs if r.id == rec_id), None)
        if rec is None:
            raise ApiError(404, 'not_found', 'Not found.')
        if recs[0].id != rec_id:
            raise ApiError(409, 'not_newest', 'Only the newest reconciliation can be undone. Undo the later ones '
                                              'first.')
        data = rec.result.get('undo')
        if not isinstance(data, dict):
            raise ApiError(409, 'no_undo', 'This reconciliation was made before undo existed (or came from a backup), '
                                           'so it cannot be undone. Fix the rows by hand.')
        b = load(m)
        now = {r.id: repo.row_rec(r) for r in b.rows}
        moved = [x['name'] for x in data['rows_after'] if now.get(x['id']) != x]
        if moved:
            raise ApiError(409, 'changed', 'These rows changed after the reconciliation: %s. Undo those changes '
                                           'first.' % ', '.join(sorted(set(moved))[:10]))
        if b.start_balance != data['balance_after'] or views.iso(b.as_of) != data['as_of_after']:
            raise ApiError(409, 'changed', 'The checkbook balance changed after the reconciliation, so it cannot be '
                                           'undone.')
        put = {x['id']: repo.row_from(x) for x in data['rows_before']}
        done = set(data['expected_done'])

        def fn(cur: Budget) -> Budget:
            return replace(cur, rows=tuple(put.get(r.id, r) for r in cur.rows), start_balance=data['start_balance'],
                           as_of=date.fromisoformat(data['as_of']),
                           last_reconciled_through=date.fromisoformat(data['last_reconciled_through'])
                           if data['last_reconciled_through'] else None,
                           expected=tuple(replace(e, status='open') if e.id in done else e for e in cur.expected))
        repo.edit_budget(m.conn, m.hid, fn, user_id=m.user_id, action='reconcile.undo', row_id=rec_id)
        repo.delete_reconciliation(m.conn, m.hid, rec_id)
        if rec.upload_id and repo.get_upload(m.conn, m.hid, rec.upload_id) is not None:
            repo.set_upload_status(m.conn, m.hid, rec.upload_id, 'waiting', 'reconciliation undone')
    return {'ok': True, 'rows_restored': len(put), 'balance': data['start_balance'], 'upload_id': rec.upload_id}


@router.post('/reconciliations/{rec_id}/undo')
def reconciliation_undo(rec_id: str, m: Editor) -> dict[str, Any]:
    return undo(m, rec_id)


@router.post('/reconcile/preview')
async def reconcile_preview(body: PreviewBody, request: Request, m: Editor) -> dict[str, Any]:
    return await time_limited(request, lambda: preview(m, body.upload_id))


@router.post('/reconcile/apply')
async def reconcile_apply(body: ApplyBody, request: Request, m: Editor) -> dict[str, Any]:
    return await time_limited(request, lambda: apply(m, body.upload_id, body.fingerprint, body.accept))


@router.get('/reconciliations')
def reconciliations(m: Viewer) -> dict[str, Any]:
    return {'reconciliations': [{'id': r.id, 'at': r.at, 'by': r.by_user, 'through': views.iso(r.through),
                                 'new_balance': r.new_balance} for r in repo.list_reconciliations(m.conn, m.hid)]}


@router.get('/reconciliations/{rec_id}')
def reconciliation(rec_id: str, m: Viewer) -> dict[str, Any]:
    r = repo.get_reconciliation(m.conn, m.hid, rec_id)
    if r is None:
        raise ApiError(404, 'not_found', 'Not found.')
    return {'id': r.id, 'at': r.at, 'by': r.by_user, 'through': views.iso(r.through), 'new_balance': r.new_balance,
            'report_md': r.report_md, 'result': r.result}


def reconciliation_details(m: Member, rec_id: str) -> dict[str, Any]:
    """One applied reconciliation for agents (#93): who applied it and when, the balance before and after, and
    each bank line's outcome. Reconciliations applied before 0.14 kept only the cleared rows, the accepted proposal
    ids and the report; `details` is then null and `report_md` has the rest."""
    r = repo.get_reconciliation(m.conn, m.hid, rec_id)
    if r is None:
        raise ApiError(404, 'not_found', 'Not found.')
    return {'id': r.id, 'applied_at': r.at, 'applied_by': r.by_user, 'upload_id': r.upload_id,
            'through': views.iso(r.through), 'new_balance': r.new_balance,
            'cleared': r.result.get('cleared', []), 'accepted': r.result.get('accepted', []),
            'details': r.result.get('details'), 'report_md': r.report_md}
