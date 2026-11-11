"""Settings.from_env, check() and is_loopback (ARCHITECTURE section 18)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hbudget.web.config import ConfigError, Settings, is_loopback


def test_defaults():
    s = Settings.from_env({})
    assert (s.host, s.port, s.cookie_secure, s.trust_proxy, s.session_hours, s.max_upload_mb) == \
        ('127.0.0.1', 8080, 'auto', False, 12, 5)
    assert s.allowed_hosts == ('127.0.0.1', 'localhost') and s.data_dir == Path('./data')
    assert s.max_upload_bytes == 5 * 1024 * 1024 and s.db_path == Path('./data/hbudget.db')
    assert 'admin_password' not in repr(Settings(admin_password='secret value'))


@pytest.mark.parametrize(('env', 'field', 'want'), [
    ({'HBUDGET_PORT': '9000'}, 'port', 9000),
    ({'HBUDGET_DATA_DIR': '/srv/x'}, 'data_dir', Path('/srv/x')),
    ({'HBUDGET_TRUST_PROXY': 'yes'}, 'trust_proxy', True),
    ({'HBUDGET_TRUST_PROXY': 'off'}, 'trust_proxy', False),
    ({'HBUDGET_TRUST_PROXY': ''}, 'trust_proxy', False),
    ({'HBUDGET_COOKIE_SECURE': 'TRUE'}, 'cookie_secure', 'true'),
    ({'HBUDGET_ALLOWED_HOSTS': 'a.example, b.example ,'}, 'allowed_hosts', ('a.example', 'b.example')),
    ({'HBUDGET_FORWARDED_ALLOW_IPS': '10.0.0.1,10.0.0.2'}, 'forwarded_allow_ips', ('10.0.0.1', '10.0.0.2')),
    ({'HBUDGET_PUBLIC_URL': 'https://budget.example.com'}, 'public_url', 'https://budget.example.com'),
    ({'HBUDGET_SESSION_HOURS': '24'}, 'session_hours', 24),
    ({'HBUDGET_MAX_UPLOAD_MB': '10'}, 'max_upload_mb', 10),
    ({'HBUDGET_DEMO': '1'}, 'demo', True),
    ({'HBUDGET_LOG_LEVEL': 'DEBUG'}, 'log_level', 'debug'),
    ({'HBUDGET_ADMIN_USERNAME': ' boss '}, 'admin_username', 'boss'),
    ({'HBUDGET_DEV': '1'}, 'dev', True),
    ({'HBUDGET_REQUIRE_2FA': '1'}, 'require_2fa', True),
    ({}, 'require_2fa', False),
])
def test_values(env, field, want):
    assert getattr(Settings.from_env(env), field) == want


@pytest.mark.parametrize(('env', 'needle'), [
    ({'HBUDGET_PORT': 'abc'}, 'whole number'),
    ({'HBUDGET_PORT': '70000'}, 'between'),
    ({'HBUDGET_TRUST_PROXY': 'maybe'}, '0 or 1'),
    ({'HBUDGET_COOKIE_SECURE': 'sometimes'}, 'auto, true or false'),
    ({'HBUDGET_DEV': '1', 'HBUDGET_HOST': '0.0.0.0'}, 'loopback'),
    ({'HBUDGET_DEV': '1', 'HBUDGET_HOST': 'budget.example.com'}, 'loopback'),
    ({'HBUDGET_PUBLIC_URL': 'budget.example.com'}, 'http'),
    ({'HBUDGET_ALLOWED_HOSTS': ' , '}, 'at least one'),
    ({'HBUDGET_SESSION_HOURS': '0'}, 'between'),
    ({'HBUDGET_MAX_UPLOAD_MB': '500'}, 'between'),
    ({'HBUDGET_REQUIRE_2FA': 'yes please'}, '0 or 1'),
])
def test_refused(env, needle):
    with pytest.raises(ConfigError, match=needle):
        Settings.from_env(env)


def test_dev_on_loopback_is_fine():
    assert Settings.from_env({'HBUDGET_DEV': '1', 'HBUDGET_HOST': '::1'}).dev


def test_allowed_origins():
    s = Settings(allowed_hosts=('localhost', '*', 'budget.lan'), port=8080, public_url='https://Budget.Example.com/')
    o = s.allowed_origins()
    assert 'https://budget.example.com' in o
    assert {'http://localhost:8080', 'https://localhost:8080', 'http://budget.lan', 'https://budget.lan:8080'} <= o
    assert not any('*' in x for x in o)
    assert Settings(allowed_hosts=('*',)).allowed_origins() == frozenset()


@pytest.mark.parametrize(('host', 'want'), [
    ('127.0.0.1', True), ('127.5.5.5', True), ('::1', True), ('localhost', True), ('', True),
    ('0.0.0.0', False), ('192.168.1.10', False), ('budget.example.com', False), ('::', False),
])
def test_is_loopback(host, want):
    assert is_loopback(host) is want


def test_trusting_every_forwarding_address_is_warned_about():
    s = Settings.from_env({'HBUDGET_TRUST_PROXY': '1', 'HBUDGET_FORWARDED_ALLOW_IPS': '*'})
    assert any('FORWARDED_ALLOW_IPS=*' in w for w in s.warnings())
    assert Settings.from_env({'HBUDGET_TRUST_PROXY': '1', 'HBUDGET_FORWARDED_ALLOW_IPS': '10.0.0.2'}).warnings() == []
    assert Settings.from_env({'HBUDGET_FORWARDED_ALLOW_IPS': '*'}).warnings() == []       # not trusted at all
