"""The admin page's System box (versions, schema, backups, disk)."""
from __future__ import annotations


def test_system_box(admin, tmp_path):
    """The admin System box: versions, schema, backups (missing, then fresh), disk."""
    from hbudget import __version__
    from hbudget.db import backup
    s = admin.get('/api/admin/system').json()
    assert s['app']['version'] == __version__ and s['app']['uptime_seconds'] >= 0
    assert s['database']['schema'] == s['database']['schema_latest'] and s['database']['size'] > 0
    assert s['database']['rows']['households'] == 1 and s['disk']['free'] > 0
    assert s['backups']['stale'] is True and s['backups']['newest'] is None
    backup.run(tmp_path / 'hbudget.db', tmp_path / 'backups')
    b = admin.get('/api/admin/system').json()['backups']
    assert b['scheduled'] == 1 and b['stale'] is False and b['newest']['age_hours'] < 1
