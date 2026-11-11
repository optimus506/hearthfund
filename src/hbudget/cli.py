"""Command line (ARCHITECTURE section 17). Uses the same db and core code and the same data dir as the web app."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

from hbudget import __version__


def _settings() -> Any:
    from hbudget.web.config import Settings
    return Settings.from_env()


_OPEN: list[Any] = []   # connections of the running command, closed by main()


def _conn() -> Any:
    from hbudget.db import connection, migrate
    s = _settings()
    s.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = connection.connect(s.db_path)
    _OPEN.append(conn)
    migrate.migrate(conn, s.db_path)
    return conn


def _household(conn: Any, which: str | None) -> int:
    """--household by id or by name; without it, the only household (when there is exactly one)."""
    from hbudget.db import repo
    hs = repo.list_households(conn)
    if which is None:
        if len(hs) != 1:
            raise SystemExit('There are %d households; pick one with --household ID or NAME '
                             '(see `hbudget households`).' % len(hs))
        return int(hs[0].id)
    if which.isdigit() and any(h.id == int(which) for h in hs):
        return int(which)
    named = [h for h in hs if h.name.casefold() == which.strip().casefold()]
    if len(named) == 1:
        return int(named[0].id)
    if len(named) > 1:
        raise SystemExit('Several households are called %r; use the id (see `hbudget households`).' % which)
    raise SystemExit('No household %r (see `hbudget households`).' % which)


def _read(path: str) -> bytes:
    """A file argument; "-" reads standard input (e.g. `docker compose exec -T app hbudget import-bills - < f`)."""
    return sys.stdin.buffer.read() if path == '-' else Path(path).read_bytes()


def _name(path: str, data: bytes) -> str:
    """A file name for format detection when the data came from standard input."""
    if path not in ('-', '/dev/stdin'):
        return path
    head = data.lstrip()[:200]
    if head.startswith(b'{'):
        return 'stdin.json'
    if head.upper().startswith((b'OFXHEADER', b'<OFX', b'<?XML')):
        return 'stdin.ofx'
    if data[:4] == b'PK\x03\x04':
        return 'stdin.xlsx'
    # YAML (comments and "key: value" lines) unless the first real line looks like a CSV header
    first = next((ln.strip() for ln in data.splitlines() if ln.strip() and not ln.lstrip().startswith(b'#')), b'')
    return 'stdin.csv' if b',' in first and b': ' not in first else 'stdin.yaml'


def cmd_households(a: argparse.Namespace) -> int:
    from hbudget.db import repo
    conn = _conn()
    hs = repo.list_households(conn)
    if not hs:
        print('no households yet')
    for h in hs:
        print('%d\t%s' % (h.id, h.name))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from hbudget.web.app import create_app
    s = _settings()
    host, port = a.host or s.host, a.port or s.port
    from dataclasses import replace
    s = replace(s, host=host, port=port)
    s.check()
    s.check_tls_files()
    if s.db_path.exists():     # a rollback onto newer data: say so in one line, not as a start-up traceback
        from hbudget.db import connection, migrate
        conn = connection.connect(s.db_path)
        try:
            migrate.check_not_newer(conn)
        finally:
            conn.close()
    uvicorn.run(create_app(s), host=host, port=port, proxy_headers=s.trust_proxy,
                forwarded_allow_ips=','.join(s.forwarded_allow_ips) if s.trust_proxy else '127.0.0.1',
                server_header=False, log_level=s.log_level,
                ssl_certfile=str(s.tls_cert) if s.tls_cert else None,
                ssl_keyfile=str(s.tls_key) if s.tls_key else None)
    return 0


def cmd_migrate(a: argparse.Namespace) -> int:
    from hbudget.db import connection, migrate
    s = _settings()
    s.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = connection.connect(s.db_path)
    _OPEN.append(conn)
    done = migrate.migrate(conn, s.db_path)
    print('applied: %s' % (', '.join(str(v) for v in done) or 'nothing (up to date)'))
    return 0


def cmd_backup(a: argparse.Namespace) -> int:
    from hbudget.db import backup
    s = _settings()
    folder = Path(a.dir) if a.dir else s.data_dir / 'backups'
    res = backup.run(s.db_path, folder, keep=a.keep, keep_upgrade=a.keep_upgrade)
    print('backup: %s (%d bytes, integrity ok)' % (res.path, res.size))
    for p in res.removed:
        print('removed old backup: %s' % p.name)
    return 0


def cmd_create_admin(a: argparse.Namespace) -> int:
    from hbudget.db import repo, users
    conn = _conn()
    prompt = 'Password (10+ characters): '
    # a line from a file or a pipe may end in \n, \r\n (Windows) or \r: none of it belongs to the password
    password = sys.stdin.readline().rstrip('\r\n') if a.password_stdin else getpass.getpass(prompt)
    if a.reset_password:
        u = users.get_user_by_name(conn, a.username)
        if u is None:
            raise SystemExit('No user %r.' % a.username)
        users.set_password(conn, u.id, password)
        repo.audit(conn, household_id=None, user_id=u.id, action='password.reset.cli')
        print('Password reset; that user\'s sessions were signed out.')
        return 0
    try:
        uid, hid = users.create_first_admin(conn, a.username, password, a.household)
    except users.SetupDone:
        raise SystemExit('Users already exist. Use --reset-password to set a new password.') from None
    except ValueError as e:
        raise SystemExit(str(e)) from None
    repo.audit(conn, household_id=None, user_id=uid, action='setup.done.cli')
    (_settings().data_dir / 'setup-token').unlink(missing_ok=True)
    repo.delete_setting(conn, 'setup_token_hash')
    print('Created admin %r and household %d.' % (a.username, hid))
    return 0


def cmd_disable_2fa(a: argparse.Namespace) -> int:
    from hbudget.db import repo, twofactor, users
    conn = _conn()
    u = users.get_user_by_name(conn, a.username)
    if u is None:
        raise SystemExit('No user %r.' % a.username)
    if not twofactor.disable(conn, u.id):
        print('Two-factor sign-in was not on for %s.' % u.username)
        return 0
    repo.audit(conn, household_id=None, user_id=None, action='2fa.reset.cli', row_id=str(u.id))
    print('Two-factor sign-in is off for %s; their sessions were signed out. They sign in with the password and '
          'can set it up again under Account.' % u.username)
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from hbudget import demo
    from hbudget.db import repo
    conn = _conn()
    hid = repo.create_household(conn, a.household)
    demo.install(conn, hid, today=date.today(), user_id=None)
    print('Created demo household %d (made-up data). The admin can add themselves as owner on the Admin page.'
          % hid)
    return 0


def cmd_rule_pack(a: argparse.Namespace) -> int:
    from hbudget.db import repo
    from hbudget.io import rulepack
    if a.show:
        sys.stdout.write(rulepack.starter_text())
        return 0
    data = _read(a.file) if a.file else b''
    pack = rulepack.load(data, _name(a.file, data)) if a.file else rulepack.starter()
    conn = _conn()
    hid = _household(conn, a.household)
    out = rulepack.apply(repo.load_budget(conn, hid), pack)
    if a.write and out.added:
        seen = []

        def fn(b: Any) -> Any:
            seen.append(rulepack.apply(b, pack))
            return seen[-1].budget
        repo.edit_budget(conn, hid, fn, user_id=None, action='rule.pack')
        out = seen[-1]
    for r, names in out.added:
        print('%s %s (%s)%s' % ('added' if a.write else 'would add', r.name, r.kind,
                                ': clears ' + ', '.join(names) if names else ''))
    for name, why in out.skipped:
        print('skipped %s: %s' % (name, why))
    print('%d rule(s) %s, %d skipped, %d already there%s' % (
        len(out.added), 'added' if a.write else 'to add', len(out.skipped), out.kept,
        '' if a.write or not out.added else ' (dry run: add --write)'))
    return 0


def cmd_import_bills(a: argparse.Namespace) -> int:
    from hbudget.core import forecast
    from hbudget.db import repo
    from hbudget.io import billsystem
    from hbudget.web import billimport
    conn = _conn()
    hid = _household(conn, a.household)
    data = _read(a.file)
    name = _name(a.file, data)
    today = date.today()
    mode: Literal['replace', 'merge'] = 'replace' if a.replace else 'merge'
    if name.lower().endswith('.csv'):
        return _import_bills_csv(conn, hid, data, mode, a.dry_run, today)
    bs = billsystem.load(data, name, today=today)
    for w in bs.warnings:
        print('warning:', w)
    if bs.extras is not None:
        if not a.replace:
            raise SystemExit('This is a full backup: it can only replace a household (--replace). It replaces '
                             'everything, including the bank history.')
        if a.dry_run:
            print('dry run: a full backup; --replace would replace everything in household %d' % hid)
            return 0
        repo.import_bill_system(conn, hid, data, name, today=today, user_id=None, mode='replace')
        print('restored the backup into household %d' % hid)
        return 0
    old = repo.load_budget(conn, hid)
    nb, taken = billimport.combine(old, bs, mode, set_balance=a.set_balance, today=today)
    f = forecast.compute(nb, today=today)
    balance = 'checkbook balance %.2f as of %s %s' % (nb.start_balance / 100, nb.as_of,
                                                      'taken from the file' if taken else 'kept (add --set-balance)')
    if a.dry_run:
        before = forecast.compute(old, today=today)
        print('dry run: %d series, %d rows, low %.2f on %s; %s' % (len(nb.series), len(nb.rows), f.low / 100,
                                                                   f.low_date, balance))
        print('low %.2f -> %.2f, end %.2f -> %.2f' % (before.low / 100, f.low / 100, before.end / 100, f.end / 100))
        ch = billimport.row_changes(old, nb)
        for kind in ('added', 'removed', 'changed'):
            for r in ch[kind]:
                extra = ' (was %.2f on %s)' % (r['amount_was'] / 100, r['due_was']) if kind == 'changed' else ''
                flag = '  CHECK: may already be paid' if r.get('check') else ''
                print('%-8s %s  %-30s %10.2f%s%s' % (kind, r['due'], r['name'][:30], r['amount'] / 100, extra, flag))
            if ch['counts'][kind] > len(ch[kind]):
                print('%-8s ... and %d more' % (kind, ch['counts'][kind] - len(ch[kind])))
        return 0
    repo.edit_budget(conn, hid, lambda _old: billimport.combine(_old, bs, mode, set_balance=a.set_balance,
                                                                 today=today)[0],
                     user_id=None, action='import.' + mode + '.cli')
    print('imported into household %d; %s' % (hid, balance))
    return 0


def _import_bills_csv(conn: Any, hid: int, data: bytes, mode: str, dry_run: bool, today: date) -> int:
    """The simple bills CSV (name,kind,amount,cadence,...): adds the bills (merge) or replaces them (replace)."""
    from dataclasses import replace

    from hbudget.core import ledger
    from hbudget.db import repo
    from hbudget.io import billsystem
    new = billsystem.bills_csv(data, today=today)

    def fn(b: Any) -> Any:
        taken = {s.id for s in b.series}
        add = [s for s in new if s.id not in taken] if mode == 'merge' else new
        nb = replace(b, series=(*b.series, *add) if mode == 'merge' else tuple(add),
                     rows=b.rows if mode == 'merge' else ())
        return ledger.generate(nb, ledger.horizon_end(nb))
    if dry_run:
        print('dry run: %d bills in the file' % len(new))
        return 0
    repo.edit_budget(conn, hid, fn, user_id=None, action='import.bills_csv.cli')
    print('imported %d bills into household %d' % (len(new), hid))
    return 0


def _mapping_json(text: str) -> Any:
    """--mapping is JSON text or the path of a JSON file. JSON text is never handed to the file system: on
    Python 3.12/3.13 Path.exists() raises for a name longer than the file system allows."""
    if text.lstrip().startswith(('{', '[')):
        return json.loads(text)
    return json.loads(Path(text).read_text())


def _mapping(a: argparse.Namespace, conn: Any, hid: int) -> Any:
    from hbudget.db import repo
    from hbudget.io import billsystem
    if a.mapping:
        raw = _mapping_json(a.mapping)
        return billsystem._mapping_from('workbook', raw)
    p = next((x for x in repo.list_profiles(conn, hid, kind='workbook') if x.name == a.profile), None)
    if p is None:
        raise SystemExit('No workbook profile named %r.' % a.profile)
    return p.mapping


def cmd_import_workbook(a: argparse.Namespace) -> int:
    from hbudget.core import forecast
    from hbudget.db import repo
    from hbudget.io import tabular, workbook_import
    conn = _conn()
    hid = _household(conn, a.household)
    data = _read(a.file)
    sheets = tabular.read_table(data, _name(a.file, data))
    imp = workbook_import.import_rows(sheets, _mapping(a, conn, hid), today=date.today())
    f = forecast.compute(imp.budget, today=date.today())
    print('%d rows, %d series; low %.2f on %s; end %.2f' % (len(imp.budget.rows), len(imp.budget.series), f.low / 100,
                                                          f.low_date, f.end / 100))
    if not a.dry_run:
        repo.wipe_household(conn, hid)
        repo.save_budget(conn, hid, imp.budget, user_id=None, action='import.workbook.cli')
        print('saved into household %d' % hid)
    return 0


def cmd_check_workbook(a: argparse.Namespace) -> int:
    from hbudget.core import forecast
    from hbudget.io import billsystem, tabular, workbook_import
    raw = _mapping_json(a.mapping)
    mapping = billsystem._mapping_from('workbook', raw)
    data = _read(a.file)
    imp = workbook_import.import_rows(tabular.read_table(data, _name(a.file, data)), mapping,  # type: ignore[arg-type]
                                      today=date.today())
    f = forecast.compute(imp.budget, today=date.today())
    if imp.stored_check is None:
        print('the mapping names no check cells')
        return 1
    res = imp.stored_check.matches(f)
    for k, ok in res.items():
        line = '%-9s %s' % (k, 'match' if ok else 'MISMATCH')
        if a.show:
            line += '  app=%s sheet=%s' % (getattr(f, k), getattr(imp.stored_check, k))
        print(line)
    return 0 if all(res.values()) else 1


def cmd_reconcile(a: argparse.Namespace) -> int:
    from hbudget.core import reconcile
    from hbudget.db import repo
    from hbudget.io import bank as bankio
    from hbudget.io import clean_filename, csvfile
    from hbudget.io.ofx import read_ofx
    conn = _conn()
    hid = _household(conn, a.household)
    data = _read(a.file)
    if _name(a.file, data).lower().endswith(('.ofx', '.qfx')):
        bf = read_ofx(data)
    else:
        header = [c.strip() for c in csvfile.read_rows(data)[0]]
        prof = bankio.detect_profile(header, [*repo.list_profiles(conn, hid, kind='bank'), *bankio.BUILTIN_PROFILES])
        if prof is None:
            raise SystemExit('Unknown CSV layout: upload it once on the web page to map the columns.')
        bf = bankio.read_bank_csv(data, prof.mapping)  # type: ignore[arg-type]
    version = repo.budget_version(conn, hid)
    budget = repo.load_budget(conn, hid)
    prepared = reconcile.prepare(bf.txns, budget, seen_ids=repo.seen_txn_ids(conn, hid))
    result = reconcile.match(budget, prepared)
    accept: dict[str, Any] = {}
    for part in filter(None, (a.accept or '').split(',')):
        pid, _, how = part.partition(':')
        accept[pid.strip().upper()] = how.strip() or 'clear'
    try:
        applied = reconcile.apply(budget, result, accept, today=date.today())
    except ValueError as e:
        raise SystemExit(str(e)) from None
    print(reconcile.report(applied, dry_run=not a.write, today=date.today()))
    if a.write:
        import secrets

        from hbudget.db.connection import transaction
        uid = 'up-' + secrets.token_hex(8)
        # one transaction, and refused (Conflict) when the budget changed since it was read above
        with transaction(conn, immediate=True):
            repo.create_upload(conn, hid, upload_id=uid, kind='bank', filename=clean_filename(a.file),
                               uploaded_by='cli', rows=len(bf.txns), date_from=bf.first, date_to=bf.last,
                               latest_balance=bf.latest_balance, has_balance=bf.has_balance)
            repo.insert_txns(conn, hid, uid, bf.txns)
            repo.edit_budget(conn, hid, lambda _b: applied.budget, user_id=None, action='reconcile.apply.cli',
                             row_id=uid, expected_version=version)
            repo.save_reconciliation(conn, hid, rec_id='rec-' + secrets.token_hex(6), by_user='cli', upload_id=uid,
                                     through=prepared.through, new_balance=applied.budget.start_balance,
                                     result={'cleared': [{'row': c.row_id, 'txn': c.txn_id, 'how': c.how}
                                                         for c in applied.cleared], 'accepted': sorted(accept)},
                                     report_md=applied.report_md)
            repo.set_upload_status(conn, hid, uid, 'imported', 'reconciled from the command line')
    else:
        print('\n(dry run: nothing saved; add --write)')
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    from hbudget.core import forecast
    from hbudget.db import repo
    from hbudget.io import export
    conn = _conn()
    hid = _household(conn, a.household)
    if a.format in ('yaml', 'json'):
        content: bytes = repo.backup(conn, hid, fmt=a.format, app_version=__version__).encode()
    else:
        b = repo.load_budget(conn, hid)
        f = forecast.compute(b, today=date.today())
        content = export.forecast_csv(b, f) if a.format == 'csv' else export.forecast_xlsx(b, f)
    if a.output:
        with open(a.output, 'xb') as fh:      # never overwrites
            fh.write(content)
        print('wrote', a.output)
    else:
        sys.stdout.buffer.write(content)
    return 0


def cmd_wipe(a: argparse.Namespace) -> int:
    from hbudget.db import repo
    if not a.yes:
        raise SystemExit('Add --yes to delete all budget data of the household.')
    conn = _conn()
    repo.wipe_household(conn, _household(conn, a.household))
    print('wiped')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='hbudget', description='Hearthfund %s' % __version__)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve', help='run the web server')
    s.add_argument('--host')
    s.add_argument('--port', type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser('migrate', help='apply database migrations').set_defaults(fn=cmd_migrate)
    s = sub.add_parser('backup', help='copy the database (safe while running), check it, keep the newest copies')
    s.add_argument('--keep', type=int, default=14, help='scheduled backups to keep (default 14)')
    s.add_argument('--keep-upgrade', type=int, default=5,
                   help='pre-upgrade copies (hbudget-before-*) to keep (default 5)')
    s.add_argument('--dir', help='folder for the backups (default: <data dir>/backups)')
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser('create-admin', help='create the first admin (or reset a password)')
    s.add_argument('username')
    s.add_argument('--household', default='Household')
    s.add_argument('--reset-password', action='store_true')
    s.add_argument('--password-stdin', action='store_true', help='read the password from standard input')
    s.set_defaults(fn=cmd_create_admin)
    s = sub.add_parser('disable-2fa', help='turn off two-factor sign-in for a user who lost their phone')
    s.add_argument('username')
    s.set_defaults(fn=cmd_disable_2fa)
    s = sub.add_parser('demo', help='create a household with fake demo data')
    s.add_argument('--household', default='Demo household')
    s.set_defaults(fn=cmd_demo)
    sub.add_parser('households', help='list the households (id and name)').set_defaults(fn=cmd_households)
    s = sub.add_parser('import-bills', help='import a bill-system YAML/JSON file or a backup ("-" = stdin)')
    s.add_argument('file')
    s.add_argument('--household', help='id or name (default: the only household)')
    s.add_argument('--replace', action='store_true')
    s.add_argument('--set-balance', action='store_true',
                   help="also take the file's checkbook balance and as-of date (always taken for a new household)")
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=cmd_import_bills)
    s = sub.add_parser('rule-pack', help='add ready-made payee rules: the starter pack, or a pack file')
    s.add_argument('file', nargs='?', help='a rule-pack file ("-" = stdin); default: the starter pack')
    s.add_argument('--household', help='id or name (default: the only household)')
    s.add_argument('--show', action='store_true', help='print the starter pack, to save and change')
    s.add_argument('--write', action='store_true', help='add the rules (without it: a dry run)')
    s.set_defaults(fn=cmd_rule_pack)
    s = sub.add_parser('import-workbook', help='import a spreadsheet with a saved profile or a mapping')
    s.add_argument('file')
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--profile')
    g.add_argument('--mapping', help='JSON text or a JSON file')
    s.add_argument('--household', help='id or name (default: the only household)')
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=cmd_import_workbook)
    s = sub.add_parser('check-workbook', help='compare the app forecast with the workbook\'s own cells')
    s.add_argument('file')
    s.add_argument('--mapping', required=True)
    s.add_argument('--show', action='store_true', help='also print the values')
    s.set_defaults(fn=cmd_check_workbook)
    s = sub.add_parser('reconcile', help='reconcile a bank export (dry run unless --write)')
    s.add_argument('file')
    s.add_argument('--household', help='id or name (default: the only household)')
    s.add_argument('--accept', help='e.g. P1:clear,P2:reduce')
    s.add_argument('--write', action='store_true')
    s.set_defaults(fn=cmd_reconcile)
    s = sub.add_parser('export', help='backup or forecast export')
    s.add_argument('--household', help='id or name (default: the only household)')
    s.add_argument('--format', choices=['yaml', 'json', 'csv', 'xlsx'], default='yaml')
    s.add_argument('-o', '--output')
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser('wipe', help='delete all budget data of a household')
    s.add_argument('--household', required=True, help='id or name')
    s.add_argument('--yes', action='store_true')
    s.set_defaults(fn=cmd_wipe)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return int(a.fn(a))
    except Exception as e:  # a readable message instead of a traceback for expected problems
        from hbudget.core.reconcile import ReconcileError
        from hbudget.db.backup import BackupError
        from hbudget.db.migrate import NewerDatabase
        from hbudget.io import ImportProblem
        from hbudget.web.config import ConfigError
        if isinstance(e, (ImportProblem, ReconcileError, ConfigError, BackupError, NewerDatabase,
                          FileNotFoundError, FileExistsError)):
            print('error: %s' % e, file=sys.stderr)
            return 2
        raise
    finally:
        while _OPEN:
            _OPEN.pop().close()


if __name__ == '__main__':
    sys.exit(main())
