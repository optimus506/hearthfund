"""Settings from HBUDGET_* environment variables (ARCHITECTURE section 18)."""
from __future__ import annotations

import ipaddress
import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

CookieSecure = Literal['auto', 'true', 'false']
PlaidMode = Literal['off', 'sandbox', 'production']


class ConfigError(ValueError):
    """A setting has a value the app refuses to start with."""


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == '':
        return default
    v = raw.strip().lower()
    if v in ('1', 'true', 'yes', 'on'):
        return True
    if v in ('0', 'false', 'no', 'off'):
        return False
    raise ConfigError(f'{name} must be 0 or 1')


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f'{name} must be a whole number') from None
    if not lo <= v <= hi:
        raise ConfigError(f'{name} must be between {lo} and {hi}')
    return v


def _list(env: Mapping[str, str], name: str, default: str) -> tuple[str, ...]:
    raw = env.get(name)
    if raw is None or raw.strip() == '':
        raw = default
    return tuple(x.strip() for x in raw.split(',') if x.strip())


def _path(env: Mapping[str, str], name: str) -> Path | None:
    raw = (env.get(name) or '').strip()
    return Path(raw) if raw else None


def _secret(env: Mapping[str, str], name: str) -> str | None:
    """A secret from NAME, or read from the file NAME_FILE (keeps it out of `docker inspect`)."""
    direct = (env.get(name) or '').strip()
    path = (env.get(name + '_FILE') or '').strip()
    if direct and path:
        raise ConfigError(f'Set {name} or {name}_FILE, not both')
    if path:
        try:
            return Path(path).read_text(encoding='utf-8').strip() or None
        except OSError as e:
            raise ConfigError(f'{name}_FILE: cannot read {path} ({e.strerror})') from None
    return direct or None


def is_loopback(host: str) -> bool:
    if host in ('localhost', ''):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path('./data')
    host: str = '127.0.0.1'
    port: int = 8080
    secret_key: str | None = None
    cookie_secure: CookieSecure = 'auto'
    trust_proxy: bool = False
    forwarded_allow_ips: tuple[str, ...] = ('127.0.0.1',)
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost')
    public_url: str | None = None
    session_hours: int = 12
    session_max_days: int = 7
    max_upload_mb: int = 5
    demo: bool = False
    require_2fa: bool = False     # every user must turn on two-factor sign-in (D-095)
    dev: bool = False
    log_level: str = 'info'
    admin_username: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # Plaid bank connection (docs/PLAID.md): off unless the server enables it; offline is the default
    plaid: PlaidMode = 'off'
    plaid_client_id: str | None = None
    plaid_secret: str | None = field(default=None, repr=False)
    # tests lower these; production keeps the documented values
    scrypt_n: int = 2 ** 15
    scrypt_slots: int = 2
    scrypt_wait_seconds: float = 2.0
    time_limit_seconds: float = 10.0

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'hbudget.db'

    def allowed_origins(self) -> frozenset[str]:
        """Origins a state-changing request may come from (the CSRF origin check)."""
        out = set()
        if self.public_url:
            out.add(self.public_url.rstrip('/').lower())
        for h in self.allowed_hosts:
            if h == '*':
                continue
            for scheme in ('http', 'https'):
                out.add(f'{scheme}://{h}:{self.port}'.lower())
                out.add(f'{scheme}://{h}'.lower())
        return frozenset(out)

    def check(self) -> None:
        if self.dev and not is_loopback(self.host):
            raise ConfigError('HBUDGET_DEV=1 only runs on a loopback address (127.0.0.1 or localhost)')
        if self.public_url and not self.public_url.startswith(('http://', 'https://')):
            raise ConfigError('HBUDGET_PUBLIC_URL must start with http:// or https://')
        if not self.allowed_hosts:
            raise ConfigError('HBUDGET_ALLOWED_HOSTS must list at least one host')
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ConfigError('HBUDGET_TLS_CERT and HBUDGET_TLS_KEY must be set together')
        if self.plaid != 'off':
            if not self.plaid_client_id:
                raise ConfigError('HBUDGET_PLAID is on: set HBUDGET_PLAID_CLIENT_ID')
            if not self.plaid_secret:
                raise ConfigError('HBUDGET_PLAID is on: set HBUDGET_PLAID_SECRET or HBUDGET_PLAID_SECRET_FILE')

    def warnings(self) -> list[str]:
        """Settings that work but weaken something; logged at start-up."""
        out = []
        if self.trust_proxy and '*' in self.forwarded_allow_ips:
            out.append('HBUDGET_FORWARDED_ALLOW_IPS=* trusts X-Forwarded-For from any address, so anyone who can '
                       'reach the app can pretend to come from any address and get around the sign-in back-off. '
                       'List the proxy address instead.')
        return out

    @property
    def plaid_redirect_uri(self) -> str | None:
        """Where a bank's own sign-in page sends the user back (OAuth banks): HTTPS public address only."""
        if self.public_url and self.public_url.startswith('https://'):
            return self.public_url.rstrip('/') + '/plaid/oauth'
        return None

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None and self.tls_key is not None

    def check_tls_files(self) -> None:
        """Load the certificate and key once, so a bad pair stops the server with a clear message."""
        if self.tls_cert is None or self.tls_key is None:
            return
        for name, p in (('HBUDGET_TLS_CERT', self.tls_cert), ('HBUDGET_TLS_KEY', self.tls_key)):
            try:
                with open(p, 'rb'):
                    pass
            except OSError as e:
                raise ConfigError(f'{name}: cannot read {p} ({e.strerror})') from None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            # A passphrase-protected key gets an empty password and fails here instead of prompting.
            ctx.load_cert_chain(self.tls_cert, self.tls_key, password=lambda: b'')
        except (ssl.SSLError, OSError):
            raise ConfigError('HBUDGET_TLS_CERT / HBUDGET_TLS_KEY could not be loaded: they must be PEM files, '
                              'the key must match the certificate and must not have a passphrase') from None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        secure = (e.get('HBUDGET_COOKIE_SECURE') or 'auto').strip().lower()
        if secure not in ('auto', 'true', 'false'):
            raise ConfigError('HBUDGET_COOKIE_SECURE must be auto, true or false')
        plaid = (e.get('HBUDGET_PLAID') or 'off').strip().lower()
        if plaid not in ('off', 'sandbox', 'production'):
            raise ConfigError('HBUDGET_PLAID must be off, sandbox or production')
        s = cls(
            data_dir=Path(e.get('HBUDGET_DATA_DIR') or './data'),
            host=(e.get('HBUDGET_HOST') or '127.0.0.1').strip(),
            port=_int(e, 'HBUDGET_PORT', 8080, 1, 65535),
            secret_key=e.get('HBUDGET_SECRET_KEY') or None,
            cookie_secure=secure,  # type: ignore[arg-type]
            trust_proxy=_bool(e, 'HBUDGET_TRUST_PROXY', False),
            forwarded_allow_ips=_list(e, 'HBUDGET_FORWARDED_ALLOW_IPS', '127.0.0.1'),
            allowed_hosts=_list(e, 'HBUDGET_ALLOWED_HOSTS', '127.0.0.1,localhost'),
            public_url=(e.get('HBUDGET_PUBLIC_URL') or '').strip() or None,
            session_hours=_int(e, 'HBUDGET_SESSION_HOURS', 12, 1, 24 * 7),
            max_upload_mb=_int(e, 'HBUDGET_MAX_UPLOAD_MB', 5, 1, 50),
            demo=_bool(e, 'HBUDGET_DEMO', False),
            require_2fa=_bool(e, 'HBUDGET_REQUIRE_2FA', False),
            dev=_bool(e, 'HBUDGET_DEV', False),
            log_level=(e.get('HBUDGET_LOG_LEVEL') or 'info').strip().lower(),
            admin_username=(e.get('HBUDGET_ADMIN_USERNAME') or '').strip() or None,
            admin_password=e.get('HBUDGET_ADMIN_PASSWORD') or None,
            tls_cert=_path(e, 'HBUDGET_TLS_CERT'),
            tls_key=_path(e, 'HBUDGET_TLS_KEY'),
            plaid=plaid,  # type: ignore[arg-type]
            plaid_client_id=(e.get('HBUDGET_PLAID_CLIENT_ID') or '').strip() or None,
            plaid_secret=_secret(e, 'HBUDGET_PLAID_SECRET') if plaid != 'off' else None,
        )
        s.check()
        return s
