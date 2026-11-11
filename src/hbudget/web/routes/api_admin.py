"""Server-wide administration (admin only): users, households and the System box. Every action is audited
(D-037)."""
from __future__ import annotations

import platform
import shutil
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import Field

from hbudget import __version__, demo
from hbudget.db import backup, migrate, repo, twofactor, users
from hbudget.web.deps import Admin, ApiError, Conn, second_factor, today_of
from hbudget.web.routes.api_household import Body

router = APIRouter(prefix='/api/admin')


@router.get('/overview')
def overview(conn: Conn, auth: Admin) -> dict[str, Any]:
    hh = []
    for h in repo.list_households(conn):
        members = repo.list_members(conn, h.id)
        hh.append({'id': h.id, 'name': h.name, 'members': len(members),
                   'is_member': any(x.user_id == auth.user.id for x in members)})
    return {'users': [{'id': u.id, 'username': u.username, 'is_admin': u.is_admin, 'disabled': u.disabled,
                       'created_at': u.created_at, 'two_factor': u.two_factor} for u in users.list_users(conn)],
            'households': hh,
            'audit': [{'at': a['at'], 'user': a['username'], 'action': a['action'], 'row': a['row'],
                       'imported': a['imported']} for a in repo.list_audit(conn, None, limit=100)]}


BACKUP_STALE_HOURS = 36   # a nightly backup older than this is late
COUNTS = {name: 'SELECT COUNT(*) FROM ' + name   # noqa: S608 - constant table names only
          for name in ('households', 'users', 'ledger_rows', 'bank_txns', 'card_txns', 'uploads')}


def _file_size(p: Any) -> int:
    try:
        return int(p.stat().st_size)
    except OSError:
        return 0


def _backups(folder: Any, now: datetime) -> dict[str, Any]:
    if not folder.is_dir():
        return {'folder_exists': False, 'scheduled': 0, 'before_upgrade': 0, 'newest': None, 'stale': True}
    sched = backup._newest_first(folder, backup.SCHEDULED)
    before = backup._newest_first(folder, backup.BEFORE_UPGRADE)
    newest: dict[str, Any] | None = None
    age = None
    if sched:
        at = datetime.strptime(sched[0].name[len('hbudget-'):-len('.db')], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
        age = round((now - at).total_seconds() / 3600, 1)
        newest = {'name': sched[0].name, 'at': at.isoformat(), 'size': _file_size(sched[0]), 'age_hours': age}
    return {'folder_exists': True, 'scheduled': len(sched), 'before_upgrade': len(before), 'newest': newest,
            'stale': age is None or age > BACKUP_STALE_HOURS}


@router.get('/system')
def system(request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    """What the server runs and how its data is kept: versions, uptime, database, backups and free disk."""
    s = request.app.state.settings
    now = datetime.now(UTC)
    started: datetime = request.app.state.started
    db = s.db_path
    disk = shutil.disk_usage(s.data_dir)
    latest = max((m.version for m in migrate.available()), default=0)
    table_rows = {name: conn.execute(sql).fetchone()[0] for name, sql in COUNTS.items()}
    return {
        'app': {'version': __version__, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                'started': started.isoformat(), 'uptime_seconds': int((now - started).total_seconds())},
        'database': {'file': db.name, 'size': _file_size(db) + _file_size(db.with_name(db.name + '-wal')),
                     'schema': migrate.current_version(conn), 'schema_latest': latest,
                     'journal_mode': conn.execute('PRAGMA journal_mode').fetchone()[0], 'rows': table_rows},
        'backups': _backups(s.data_dir / 'backups', now),
        'disk': {'free': disk.free, 'total': disk.total},
    }


class NewUser(Body):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(max_length=256)]
    is_admin: bool = False


@router.post('/users')
def create_user(body: NewUser, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    second_factor(request, conn, auth.user)     # a new user is a new way in (D-095)
    try:
        uid = users.create_user(conn, body.username, body.password, is_admin=body.is_admin)
    except ValueError as e:
        raise ApiError(400, 'invalid', str(e)) from None
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='user.create', row_id=str(uid),
               detail={'is_admin': body.is_admin})
    return {'id': uid}


def _user(conn: Any, uid: int) -> users.User:
    u = users.get_user(conn, uid)
    if u is None:
        raise ApiError(404, 'not_found', 'Not found.')
    return u


class DisabledBody(Body):
    disabled: bool


@router.post('/users/{user_id}/disabled')
def set_disabled(user_id: int, body: DisabledBody, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    _user(conn, user_id)
    if not body.disabled:
        second_factor(request, conn, auth.user)     # enabling an account opens a way in again (D-095)
    if user_id == auth.user.id and body.disabled:
        raise ApiError(400, 'invalid', 'You cannot disable your own account.')
    users.set_disabled(conn, user_id, body.disabled)
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='user.disable' if body.disabled else 'user.enable',
               row_id=str(user_id))
    return {'ok': True}


class AdminFlag(Body):
    is_admin: bool


@router.post('/users/{user_id}/admin')
def set_admin(user_id: int, body: AdminFlag, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    second_factor(request, conn, auth.user)
    _user(conn, user_id)
    if user_id == auth.user.id and not body.is_admin:
        raise ApiError(400, 'invalid', 'You cannot remove your own admin flag.')
    users.set_admin(conn, user_id, body.is_admin)
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='user.admin', row_id=str(user_id),
               detail={'is_admin': body.is_admin})
    return {'ok': True}


class PasswordBody(Body):
    password: Annotated[str, Field(max_length=256)]


@router.post('/users/{user_id}/password')
def reset_password(user_id: int, body: PasswordBody, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    """An admin reset: every session of that user is signed out."""
    second_factor(request, conn, auth.user)
    _user(conn, user_id)
    try:
        users.set_password(conn, user_id, body.password, keep_token=auth.token if user_id == auth.user.id else None)
    except ValueError as e:
        raise ApiError(400, 'invalid', str(e)) from None
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='password.reset', row_id=str(user_id))
    return {'ok': True}


@router.post('/users/{user_id}/2fa/reset')
def reset_two_factor(user_id: int, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    """A lost phone (D-095): turn that user's two-factor sign-in off. Their sessions are signed out; they sign in
    with the password and can set it up again."""
    _user(conn, user_id)
    second_factor(request, conn, auth.user)
    if user_id == auth.user.id:
        # the admin's own goes through Account, which asks for the password and a code: a borrowed admin session
        # must not be able to drop its own second step
        raise ApiError(400, 'invalid', 'Turn your own two-factor sign-in off under Account.')
    if not twofactor.disable(conn, user_id):
        raise ApiError(400, 'invalid', 'Two-factor sign-in is not on for that user.')
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='2fa.reset', row_id=str(user_id))
    return {'ok': True}


class NewHousehold(Body):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    demo: bool = False


@router.post('/households')
def create_household(body: NewHousehold, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    hid = repo.create_household(conn, body.name, as_of=today_of(request))
    repo.add_member(conn, hid, auth.user.id, 'owner')
    repo.audit(conn, household_id=hid, user_id=auth.user.id, action='household.create')
    if body.demo:
        demo.install(conn, hid, today=today_of(request), user_id=auth.user.id)
    return {'id': hid}


@router.post('/households/{hid}/owner')
def add_self_owner(hid: int, request: Request, conn: Conn, auth: Admin) -> dict[str, Any]:
    if repo.get_household(conn, hid) is None:
        raise ApiError(404, 'not_found', 'Not found.')
    second_factor(request, conn, auth.user)     # a way into another household (D-095)
    repo.add_member(conn, hid, auth.user.id, 'owner')
    repo.audit(conn, household_id=hid, user_id=auth.user.id, action='member.admin_self_owner',
               row_id=str(auth.user.id))
    return {'ok': True}
