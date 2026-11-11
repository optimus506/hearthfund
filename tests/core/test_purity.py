"""The core must stay pure: standard library only, no clock, no randomness (ARCHITECTURE 20.4)."""
import ast
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / 'src' / 'hbudget' / 'core'
FORBIDDEN_MODULES = {'uuid', 'random', 'secrets', 'sqlite3'}
FORBIDDEN_CALLS = {('date', 'today'), ('datetime', 'now'), ('datetime', 'utcnow'), ('datetime', 'today'),
                   ('time', 'time'), ('time', 'monotonic')}
CLOCK_ATTRS = {'today', 'now', 'utcnow'}
FORBIDDEN_FROM = {('time', 'time'), ('time', 'monotonic'), ('time', 'perf_counter')}


def _base_names(node: ast.expr) -> list[str]:
    """Names along an attribute chain: datetime.datetime.now -> ['datetime', 'datetime']."""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
    return out


def problems(source: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import inside hbudget.core
                continue
            names = [node.module or '']
        else:
            names = []
        if isinstance(node, ast.ImportFrom) and not node.level:
            out += ['from %s import %s' % (node.module, a.name) for a in node.names
                    if (node.module, a.name) in FORBIDDEN_FROM]
        for name in names:
            top = name.split('.')[0]
            if name.startswith('hbudget.core'):
                continue
            if top in FORBIDDEN_MODULES or top not in sys.stdlib_module_names:
                out.append('import %s' % name)
        if isinstance(node, ast.Attribute):
            chain = _base_names(node.value)
            if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in FORBIDDEN_CALLS:
                out.append('%s.%s' % (node.value.id, node.attr))
            elif node.attr in CLOCK_ATTRS and {'date', 'datetime'} & set(chain):
                out.append('%s.%s' % ('.'.join(reversed(chain)), node.attr))
    return out


@pytest.mark.parametrize('path', sorted(CORE.glob('*.py')), ids=lambda p: p.name)
def test_core_module_is_pure(path):
    assert problems(path.read_text()) == []


def test_checker_catches_violations():
    bad = 'import uuid\nimport fastapi\nfrom hbudget.db import repo\nfrom datetime import date\nx = date.today()\n'
    assert problems(bad) == ['import uuid', 'import fastapi', 'import hbudget.db', 'date.today']
    assert problems('from . import money\nimport datetime\n') == []
    assert problems('import datetime\nx = datetime.datetime.now()\ny = datetime.date.today()\n') == [
        'datetime.datetime.now', 'datetime.date.today']
    assert problems('from time import time, sleep\nfrom time import monotonic\n') == [
        'from time import time', 'from time import monotonic']
