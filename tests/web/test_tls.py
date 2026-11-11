"""HTTPS with your own certificate: HBUDGET_TLS_CERT/KEY, the startup check and docker/healthcheck.py."""
from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hbudget import cli
from hbudget.web.config import ConfigError, Settings

HEALTHCHECK = Path(__file__).parents[2] / 'docker' / 'healthcheck.py'

needs_openssl = pytest.mark.skipif(shutil.which('openssl') is None, reason='openssl command not available')


def make_cert(d: Path, name: str, *, passphrase: str | None = None) -> tuple[Path, Path]:
    cert, key = d / f'{name}.pem', d / f'{name}-key.pem'
    cmd = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-days', '2', '-subj', '/CN=budget.test',
           '-addext', 'subjectAltName=DNS:budget.test,IP:127.0.0.1', '-keyout', str(key), '-out', str(cert)]
    cmd += ['-passout', f'pass:{passphrase}'] if passphrase else ['-noenc']
    subprocess.run(cmd, check=True, capture_output=True)
    return cert, key


@pytest.fixture(scope='module')
def certs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    if shutil.which('openssl') is None:
        pytest.skip('openssl command not available')
    d = tmp_path_factory.mktemp('certs')
    return {'a': make_cert(d, 'a'), 'b': make_cert(d, 'b'), 'locked': make_cert(d, 'locked', passphrase='x' * 8)}


def test_tls_settings_from_env():
    s = Settings.from_env({'HBUDGET_TLS_CERT': ' /certs/fullchain.pem ', 'HBUDGET_TLS_KEY': '/certs/privkey.pem'})
    assert (s.tls, s.tls_cert, s.tls_key) == (True, Path('/certs/fullchain.pem'), Path('/certs/privkey.pem'))
    assert not Settings.from_env({'HBUDGET_TLS_CERT': '', 'HBUDGET_TLS_KEY': ' '}).tls
    Settings().check_tls_files()   # no TLS: nothing to check


@pytest.mark.parametrize('env', [{'HBUDGET_TLS_CERT': '/c.pem'}, {'HBUDGET_TLS_KEY': '/k.pem'}])
def test_cert_and_key_go_together(env):
    with pytest.raises(ConfigError, match='set together'):
        Settings.from_env(env)


def test_serve_reports_a_bad_certificate_in_one_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HBUDGET_TLS_CERT', str(tmp_path / 'nope.pem'))
    monkeypatch.setenv('HBUDGET_TLS_KEY', str(tmp_path / 'nope-key.pem'))
    assert cli.main(['serve']) == 2
    assert capsys.readouterr().err.startswith('error: HBUDGET_TLS_CERT: cannot read')


@needs_openssl
def test_check_tls_files(certs, tmp_path):
    (ca, ka), (_, kb), (cl, kl) = certs['a'], certs['b'], certs['locked']
    Settings(tls_cert=ca, tls_key=ka).check_tls_files()
    with pytest.raises(ConfigError, match='HBUDGET_TLS_KEY: cannot read'):
        Settings(tls_cert=ca, tls_key=tmp_path / 'missing.pem').check_tls_files()
    with pytest.raises(ConfigError, match='must match'):
        Settings(tls_cert=ca, tls_key=kb).check_tls_files()
    with pytest.raises(ConfigError, match='passphrase'):
        Settings(tls_cert=cl, tls_key=kl).check_tls_files()
    junk = tmp_path / 'junk.pem'
    junk.write_text('not a certificate')
    with pytest.raises(ConfigError, match='PEM'):
        Settings(tls_cert=junk, tls_key=ka).check_tls_files()


class _Healthz(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == '/healthz' else 404)
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


@contextmanager
def serving(cert_key: tuple[Path, Path] | None = None) -> Iterator[str]:
    """A tiny /healthz server on a free port, plain or with the given certificate; yields the port."""
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Healthz)
    if cert_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*cert_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield str(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()


def use_tls(monkeypatch: pytest.MonkeyPatch, cert_key: tuple[Path, Path] | None) -> None:
    for name, value in zip(('HBUDGET_TLS_CERT', 'HBUDGET_TLS_KEY'), cert_key or (None, None), strict=True):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))


def healthcheck(monkeypatch: pytest.MonkeyPatch, port: str) -> int:
    """Run the container healthcheck script the way the image does, against the given port."""
    import runpy
    monkeypatch.setenv('HBUDGET_PORT', port)
    with pytest.raises(SystemExit) as done:
        runpy.run_path(str(HEALTHCHECK), run_name='__main__')
    return int(done.value.code or 0)


def test_healthcheck_http(monkeypatch, capsys):
    use_tls(monkeypatch, None)
    with serving() as port:
        assert healthcheck(monkeypatch, port) == 0
    # The server is gone: nothing answers on that port any more.
    assert healthcheck(monkeypatch, port) == 1
    assert 'unhealthy' in capsys.readouterr().err


@needs_openssl
def test_healthcheck_https(certs, monkeypatch, capsys):
    with serving(certs['a']) as port:
        use_tls(monkeypatch, certs['a'])
        assert healthcheck(monkeypatch, port) == 0
        # A server presenting some other certificate is not trusted.
        use_tls(monkeypatch, certs['b'])
        assert healthcheck(monkeypatch, port) == 1
    assert 'unhealthy' in capsys.readouterr().err
