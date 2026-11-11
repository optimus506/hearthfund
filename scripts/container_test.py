#!/usr/bin/env python3
"""End-to-end container tests (Phase 5): run with `make container-test`.

  python3 scripts/container_test.py --image hearthfund:latest --previous hearthfund:previous
                                    [--no-upgrade] [--keep] [--port 18080]

Without --previous the upgrade test fails, unless --no-upgrade says to skip it (e.g. under arm64 emulation).
HBUDGET_CT_TIMEOUT (seconds, default 120) is how long to wait for the container to become healthy.

Uses only the standard library and the docker CLI. Each test runs Docker Compose in its own temporary
project folder with a clean ./data, so nothing in this repo's ./data is touched.

  2. compose up on a clean ./data: healthy, / redirects to /setup, setup-token is mode 600 and owned by 10001
  3. setup over HTTP, import the synthetic fixture, then restart and down/up: the summary is unchanged
  4. upgrade: data made with --previous, then the new image on the same volume, then a "next" image with one
     extra migration: data intact, migrations applied, a pre-migration backup written
  5. non-root, read-only root filesystem, no secrets in `docker history` / `docker inspect`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / 'tests' / 'fixtures' / 'synthetic' / 'household.yaml'
PASSWORD = 'container test password'  # noqa: S105 - a throwaway account in a throwaway volume
results: list[tuple[str, bool, str]] = []


def run(*cmd: str, cwd: Path | None = None, check: bool = True, quiet: bool = False) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise RuntimeError('%s failed (%d): %s' % (' '.join(cmd[:4]), p.returncode, (p.stderr or p.stdout)[-800:]))
    return p.stdout if quiet else p.stdout + p.stderr


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print('%-4s %s: %s' % ('PASS' if ok else 'FAIL', name, detail), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: object, **k: object) -> None:
        return None


class Client:
    """A tiny HTTP client with a cookie jar that also keeps cookies set on 127.0.0.1."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookies: dict[str, str] = {}
        self.opener = urllib.request.build_opener(NoRedirect)

    def req(self, method: str, path: str, data: bytes | None = None, headers: dict[str, str] | None = None
            ) -> tuple[int, dict[str, str], bytes]:
        h = {'Origin': self.base, **(headers or {})}
        if self.cookies:
            h['Cookie'] = '; '.join('%s=%s' % kv for kv in self.cookies.items())
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)  # noqa: S310 - always http://127.0.0.1
        try:
            resp = self.opener.open(r, timeout=20)
            status, hdrs, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, hdrs, body = e.code, e.headers, e.read()
        for c in hdrs.get_all('Set-Cookie') or []:
            name, _, rest = c.partition('=')
            value = rest.split(';', 1)[0]
            if 'max-age=0' in c.lower() or value in ('', '""'):
                self.cookies.pop(name.strip(), None)
            else:
                self.cookies[name.strip()] = value
        return status, {k.lower(): v for k, v in hdrs.items()}, body

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return self.req('GET', path)


def hidden(html: bytes, name: str) -> str:
    m = re.search(rb'name="%s" value="([^"]*)"' % name.encode(), html)
    if not m:
        raise RuntimeError('hidden field %s not found' % name)
    return m.group(1).decode()


def form(fields: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(fields).encode()


def multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    b = 'hb' + uuid.uuid4().hex
    out = b''
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode()
    out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (b, file_field, filename)).encode()
    out += content + ('\r\n--%s--\r\n' % b).encode()
    return out, 'multipart/form-data; boundary=' + b


class Project:
    """A compose project in a temp folder: this repo's docker-compose.yml, a .env and a clean ./data."""

    def __init__(self, image: str, port: int, keep: bool) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix='hbudget-ct-'))
        self.name = 'hbct' + uuid.uuid4().hex[:8]
        self.port = port
        self.keep = keep
        shutil.copy(ROOT / 'docker-compose.yml', self.dir / 'docker-compose.yml')
        self.set_image(image)
        self.base = 'http://127.0.0.1:%d' % port

    def set_image(self, image: str) -> None:
        (self.dir / '.env').write_text(
            'HBUDGET_IMAGE=%s\nHBUDGET_HOST_PORT=%d\nHBUDGET_PUBLIC_URL=http://127.0.0.1:%d\n'
            % (image, self.port, self.port))

    def compose(self, *args: str) -> str:
        return run('docker', 'compose', '-p', self.name, '--project-directory', str(self.dir),
                   '-f', str(self.dir / 'docker-compose.yml'), *args)

    def up(self) -> None:
        self.compose('up', '-d', '--no-build', '--pull', 'never')
        self.wait_healthy()

    def container(self) -> str:
        return self.compose('ps', '-q', 'app').strip().splitlines()[0]

    def wait_healthy(self) -> None:
        end = time.time() + float(os.environ.get('HBUDGET_CT_TIMEOUT', '120'))
        while time.time() < end:
            cid = self.compose('ps', '-q', 'app').strip()
            if cid:
                st = run('docker', 'inspect', '--format', '{{.State.Health.Status}}', cid.splitlines()[0], quiet=True)
                if st.strip() == 'healthy':
                    return
            time.sleep(2)
        raise RuntimeError('container did not become healthy:\n' + self.compose('logs', '--tail', '40'))

    def exec(self, *cmd: str) -> str:
        return run('docker', 'exec', self.container(), *cmd, quiet=True)

    def stat(self, path: str) -> tuple[int, int]:
        """(mode, uid) of a path inside the container (./data is mode 700 and not readable by the host user)."""
        out = self.exec('python', '-c', 'import os, sys; s = os.stat(sys.argv[1]); print(s.st_mode & 0o777, s.st_uid)',
                        path).split()
        return int(out[0]), int(out[1])

    def exists(self, path: str) -> bool:
        return self.exec('python', '-c', 'import os, sys; print(os.path.exists(sys.argv[1]))', path).strip() == 'True'

    def data(self) -> Path:
        return self.dir / 'data'

    def close(self) -> None:
        if self.keep:
            print('kept project in', self.dir, 'as', self.name)
            return
        try:
            self.compose('down', '-v', '--remove-orphans')
        finally:
            # files in ./data belong to uid 10001: remove them from a throwaway container
            if self.data().exists():
                run('docker', 'run', '--rm', '--network', 'none', '--user', '0:0', '--cap-drop', 'ALL',
                    '--cap-add', 'DAC_OVERRIDE', '--entrypoint', 'python', '-v', '%s:/d' % self.dir,
                    os.environ.get('HBUDGET_CT_CLEAN_IMAGE', 'python:3.13-slim'),
                    '-c', 'import shutil; shutil.rmtree("/d/data", ignore_errors=True)', check=False)
            shutil.rmtree(self.dir, ignore_errors=True)


def setup_and_import(p: Project) -> tuple[Client, int]:
    c = Client(p.base)
    status, hdrs, body = c.get('/setup')
    assert status == 200, status
    assert b'name="token"' in body, 'the setup page has no token field'
    token = p.exec('cat', '/data/setup-token').strip()
    status, hdrs, body = c.req('POST', '/setup', form({
        'csrf_token': hidden(body, 'csrf_token'), 'token': token, 'username': 'admin', 'password': PASSWORD,
        'household': 'Container test', 'start': 'empty'}), {'Content-Type': 'application/x-www-form-urlencoded'})
    assert status in (302, 303), (status, body[:300])
    m = re.match(r'/h/(\d+)(?:/|$)', hdrs.get('location', ''))
    assert m, hdrs.get('location')
    hid = int(m.group(1))
    status, _, page = c.get('/h/%d/import' % hid)
    csrf = hidden(page, 'csrf_token')
    data, ctype = multipart({'mode': 'replace', 'dry_run': '0'}, 'file', 'household.yaml', FIXTURE.read_bytes())
    status, _, body = c.req('POST', '/api/h/%d/import/billsystem' % hid, data,
                            {'Content-Type': ctype, 'X-CSRF-Token': csrf, 'Accept': 'application/json'})
    assert status == 200, (status, body[:300])
    return c, hid


# the synthetic fixture's hand-checked numbers (tests/core/test_synthetic_regression.py), in cents
EXPECTED = {'balance': 100000, 'low': 69000, 'low_date': '2029-03-14', 'end': 169000}


def summary(c: Client, hid: int) -> dict[str, object]:
    """The forecast readout plus the ledger's row count and ids, to compare before and after."""
    status, _, body = c.get('/api/h/%d/summary' % hid)
    assert status == 200, status
    s = json.loads(body)
    out: dict[str, object] = {k: s.get(k) for k in ('balance', 'low', 'low_date', 'end', 'end_date', 'high')}
    status, _, body = c.get('/api/h/%d/ledger' % hid)
    assert status == 200, status
    ids = [r['id'] for r in json.loads(body)['rows']]
    out['rows'] = len(ids)
    out['row_ids'] = ids
    return out


def matches_fixture(s: dict[str, object]) -> bool:
    return all(s.get(k) == v for k, v in EXPECTED.items()) and int(str(s.get('rows'))) > 0


def short(s: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in s.items() if k != 'row_ids'}


def login(p: Project) -> Client:
    c = Client(p.base)
    status, _, body = c.get('/login')
    status, _, body = c.req('POST', '/login', form({'csrf_token': hidden(body, 'csrf_token'),
                                                       'username': 'admin', 'password': PASSWORD}),
                               {'Content-Type': 'application/x-www-form-urlencoded'})
    assert status in (302, 303), (status, body[:200])
    return c


def migrations(p: Project) -> list[int]:
    out = run('docker', 'exec', p.container(), 'python', '-c',
              'import sqlite3; c = sqlite3.connect("file:/data/hbudget.db?mode=ro", uri=True); '
              'print([r[0] for r in c.execute("select version from schema_migrations order by version")])',
              quiet=True)
    return [int(v) for v in json.loads(out.strip())]


def test_fresh_and_restart(image: str, port: int, keep: bool) -> None:
    p = Project(image, port, keep)
    try:
        p.up()
        c = Client(p.base)
        status, hdrs, _ = c.get('/')
        loc = hdrs.get('location', '')
        ok = status in (302, 303, 307) and loc.endswith('/setup')
        tmode, tuid = p.stat('/data/setup-token')
        dmode, duid = p.stat('/data')
        hst = os.stat(p.data())
        status_s, _, page = c.get('/setup')
        ok2 = (tmode == 0o600 and tuid == 10001 and duid == 10001 and dmode == 0o700
               and status_s == 200 and b'name="token"' in page)
        record('2 fresh start', ok and ok2,
               'healthy; GET / -> %d %s; /setup has the token field: %s; setup-token mode %o uid %d; '
               '/data mode %o uid %d (host ./data uid %d)'
               % (status, loc, b'name="token"' in page, tmode, tuid, dmode, duid, hst.st_uid))

        c, hid = setup_and_import(p)
        before = summary(c, hid)
        p.compose('restart', 'app')
        p.wait_healthy()
        after_restart = summary(login(p), hid)
        p.compose('down')
        p.up()
        after_downup = summary(login(p), hid)
        gone = not p.exists('/data/setup-token')
        record('3 restart keeps data', matches_fixture(before) and before == after_restart == after_downup and gone,
               'summary %s (matches the fixture: %s); same summary and row ids after restart and down/up: %s; '
               'setup-token removed: %s'
               % (short(before), matches_fixture(before), before == after_restart == after_downup, gone))

        cid = p.container()
        uid = run('docker', 'exec', cid, 'id', '-u', quiet=True).strip()
        ro = run('docker', 'exec', cid, 'sh', '-c', 'touch /usr/x 2>&1; echo rc=$?', quiet=True).strip()
        insp = json.loads(run('docker', 'inspect', cid, quiet=True))[0]
        hc = insp['HostConfig']
        ports = [b['HostIp'] for v in (hc.get('PortBindings') or {}).values() for b in v]
        record('5a hardening', uid == '10001' and 'Read-only' in ro and hc.get('ReadonlyRootfs') is True
               and hc.get('CapDrop') == ['ALL'] and ports == ['127.0.0.1']
               and 'no-new-privileges:true' in (hc.get('SecurityOpt') or []),
               'uid %s; write to /usr: %s; ReadonlyRootfs %s; CapDrop %s; ports bound to %s; SecurityOpt %s'
               % (uid, ro.splitlines()[0][:60], hc.get('ReadonlyRootfs'), hc.get('CapDrop'), ports,
                  hc.get('SecurityOpt')))
    finally:
        p.close()


def build_next_image(image: str) -> tuple[str, int]:
    """The current image plus one harmless extra migration, to prove an upgrade applies new migrations."""
    tag = 'hearthfund:ct-next'
    d = Path(tempfile.mkdtemp(prefix='hbudget-next-'))
    try:
        info = run('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c',
                   'import hbudget.db, pathlib; p = pathlib.Path(hbudget.db.__file__).parent / "migrations"; '
                   'print(p, max(int(x.name[:4]) for x in p.glob("*.sql")))', quiet=True).split()
        site, number = info[0], int(info[1]) + 1
        name = '%04d_container_test.sql' % number
        (d / name).write_text('CREATE INDEX IF NOT EXISTS ix_ct_audit ON audit_log(id);\n')
        (d / 'Dockerfile').write_text('FROM %s\nUSER 0\nCOPY %s %s/\nUSER 10001:10001\n' % (image, name, site))
        run('docker', 'build', '-q', '-t', tag, str(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return tag, number


def test_upgrade(image: str, previous: str, port: int, keep: bool) -> None:
    p = Project(previous, port, keep)
    try:
        p.up()
        c, hid = setup_and_import(p)
        before = summary(c, hid)
        m0 = migrations(p)
        p.compose('down')
        p.set_image(image)
        p.up()
        s1 = summary(login(p), hid)
        m1 = migrations(p)
        p.compose('down')
        nxt, number = build_next_image(image)
        p.set_image(nxt)
        p.up()
        s2 = summary(login(p), hid)
        m2 = migrations(p)
        backups = p.exec('ls', '/data/backups').split()
        ok = (matches_fixture(before) and before == s1 == s2 and m1[:len(m0)] == m0
              and m2 == [*m1, number] and any('%04d' % number in b for b in backups))
        record('4 upgrade keeps data', ok,
               'previous -> current -> next: summary and row ids unchanged %s (%d rows, matches the fixture: %s); '
               'migrations %s -> %s -> %s; backups %s'
               % (before == s1 == s2, int(str(before['rows'])), matches_fixture(before), m0, m1, m2, backups))
    finally:
        p.close()
        run('docker', 'image', 'rm', 'hearthfund:ct-next', check=False)


def test_image_metadata(image: str) -> None:
    hist = run('docker', 'history', '--no-trunc', '--format', '{{.CreatedBy}}', image, quiet=True)
    insp = run('docker', 'image', 'inspect', image, quiet=True)
    bad = [w for w in ('PASSWORD=', 'SECRET_KEY=', 'TOKEN=') if w in hist or re.search(w + r'[^"]', insp)]
    cfg = json.loads(insp)[0]['Config']
    record('5b no secrets in image metadata', not bad and cfg.get('User') == '10001:10001',
           'history/inspect checked for %s: %s; image user %s; env %s'
           % ('PASSWORD=/SECRET_KEY=/TOKEN=', bad if bad else 'none', cfg.get('User'),
              sorted(e.split('=')[0] for e in cfg.get('Env', []))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--image', default='hearthfund:latest')
    ap.add_argument('--previous', help='an image of an earlier release, for the upgrade test')
    ap.add_argument('--port', type=int, default=18080)
    ap.add_argument('--no-upgrade', action='store_true', help='skip the upgrade test on purpose (e.g. emulated arm64)')
    ap.add_argument('--keep', action='store_true', help='keep the temp projects for inspection')
    a = ap.parse_args()
    test_image_metadata(a.image)
    try:
        test_fresh_and_restart(a.image, a.port, a.keep)
    except Exception as e:  # report and continue with the next test
        record('2/3/5a', False, repr(e)[:600])
    if a.previous:
        try:
            test_upgrade(a.image, a.previous, a.port + 1, a.keep)
        except Exception as e:
            record('4 upgrade', False, repr(e)[:600])
    elif a.no_upgrade:
        print('SKIP 4 upgrade: --no-upgrade given')
    else:
        record('4 upgrade', False, 'no --previous image given (make container-test builds one; --no-upgrade skips)')
    failed = [r for r in results if not r[1]]
    print('\n%d passed, %d failed' % (len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
