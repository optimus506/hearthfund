#!/usr/bin/env python3
"""Releases (docs/RELEASING.md). Standard library only.

  release.py prepare X.Y.Z   set __version__, turn CHANGELOG.md's "Unreleased" section into "X.Y.Z - <today>",
                             commit "Release X.Y.Z" and tag vX.Y.Z. Nothing is pushed.
  release.py notes X.Y.Z     print that version's CHANGELOG.md section (the release workflow's release notes)
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / 'src' / 'hbudget' / '__init__.py'
CHANGELOG = ROOT / 'CHANGELOG.md'
SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
HEAD = re.compile(r'^## (\[?[^\]\s]+\]?)(?: - \S+)?\s*$', re.M)


def sections(text: str) -> dict[str, tuple[int, int]]:
    """'Unreleased' / 'X.Y.Z' -> (start of the heading, end of the section)."""
    heads = list(HEAD.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[m.group(1).strip('[]')] = (m.start(), end)
    return out


def notes(version: str, text: str) -> str:
    s = sections(text)
    if version not in s:
        raise SystemExit('CHANGELOG.md has no section for %s' % version)
    a, b = s[version]
    body = text[a:b].split('\n', 1)[1].strip()
    if not body:
        raise SystemExit('the CHANGELOG.md section for %s is empty' % version)
    return body + '\n'


def prepare(version: str, today: date) -> None:
    def git(*args: str) -> str:
        return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('commit or stash your changes first')
    if git('tag', '--list', 'v' + version):
        raise SystemExit('tag v%s already exists' % version)
    text = CHANGELOG.read_text()
    s = sections(text)
    if version in s:
        raise SystemExit('CHANGELOG.md already has a %s section' % version)
    if 'Unreleased' not in s or not text[s['Unreleased'][0]:s['Unreleased'][1]].split('\n', 1)[1].strip():
        raise SystemExit('add what changed under "## Unreleased" in CHANGELOG.md first')
    a = s['Unreleased'][0]
    line_end = text.index('\n', a)
    CHANGELOG.write_text(text[:a] + '## Unreleased\n\n## %s - %s' % (version, today.isoformat()) + text[line_end:])
    INIT.write_text(re.sub(r"__version__ = '[^']+'", "__version__ = '%s'" % version, INIT.read_text()))
    git('add', str(CHANGELOG), str(INIT))
    git('commit', '-q', '-m', 'Release %s' % version)
    git('tag', '-a', 'v' + version, '-m', 'Release %s' % version)
    print('Committed and tagged v%s. Publish it with:\n  git push origin HEAD v%s' % (version, version))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ('prepare', 'notes') or not SEMVER.match(argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == 'notes':
        sys.stdout.write(notes(argv[1], CHANGELOG.read_text()))
    else:
        prepare(argv[1], date.today())
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
