"""scripts/release.py: the changelog section as release notes, and `prepare` (version, changelog, commit, tag)."""
from __future__ import annotations

import importlib.util
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts' / 'release.py')
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

LOG = """# Changelog

Intro.

## Unreleased

### Added
- A thing.

## 0.1.0 - 2026-11-02

First version.
"""


def test_notes():
    assert release.notes('0.1.0', LOG) == 'First version.\n'
    assert release.notes('Unreleased', LOG) == '### Added\n- A thing.\n'
    with pytest.raises(SystemExit, match=r'no section for 0\.3\.0'):
        release.notes('0.3.0', LOG)
    with pytest.raises(SystemExit, match='empty'):
        release.notes('0.1.0', LOG.replace('First version.\n', ''))


def test_the_repository_changelog_has_the_current_version():
    from hbudget import __version__
    text = (ROOT / 'CHANGELOG.md').read_text()
    assert __version__ in release.sections(text) and 'Unreleased' in release.sections(text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*a):
        return subprocess.run(['git', *a], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'test')
    init = tmp_path / 'src' / 'hbudget' / '__init__.py'
    init.parent.mkdir(parents=True)
    init.write_text("__version__ = '0.1.0'\n")
    (tmp_path / 'CHANGELOG.md').write_text(LOG)
    git('add', '-A')
    git('commit', '-q', '-m', 'start')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'INIT', init)
    monkeypatch.setattr(release, 'CHANGELOG', tmp_path / 'CHANGELOG.md')
    return tmp_path, git


def test_prepare(repo, capsys):
    path, git = repo
    release.prepare('0.2.0', date(2026, 11, 10))
    text = (path / 'CHANGELOG.md').read_text()
    assert '## Unreleased\n\n## 0.2.0 - 2026-11-10\n\n### Added\n- A thing.\n' in text
    assert release.notes('0.2.0', text) == '### Added\n- A thing.\n'
    assert "__version__ = '0.2.0'" in (path / 'src/hbudget/__init__.py').read_text()
    assert git('log', '-1', '--format=%s').strip() == 'Release 0.2.0'
    assert git('tag', '--list').split() == ['v0.2.0']
    assert 'git push origin HEAD v0.2.0' in capsys.readouterr().out
    # nothing new under Unreleased now, and the tag exists
    with pytest.raises(SystemExit, match='already exists'):
        release.prepare('0.2.0', date(2026, 11, 10))
    with pytest.raises(SystemExit, match='Unreleased'):
        release.prepare('0.2.1', date(2026, 11, 10))


def test_prepare_needs_a_clean_tree(repo):
    path, _ = repo
    (path / 'CHANGELOG.md').write_text(LOG + '\nmore\n')
    with pytest.raises(SystemExit, match='commit or stash'):
        release.prepare('0.2.0', date(2026, 11, 10))


def test_main_usage(capsys):
    assert release.main(['prepare', 'v1']) == 2
    assert release.main([]) == 2
