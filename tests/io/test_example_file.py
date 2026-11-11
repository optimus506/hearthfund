from __future__ import annotations

from datetime import date
from pathlib import Path

from hbudget.io import billsystem as bs

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'bill-system.example.yaml'


def test_example_validates_checks_and_loads() -> None:
    doc = bs.parse(EXAMPLE.read_bytes(), EXAMPLE.name)
    assert bs.validate(doc) == []
    assert bs.check(doc) == []
    b = bs.load(EXAMPLE.read_bytes(), EXAMPLE.name, today=date(2029, 1, 5))
    assert b.warnings == ()
    assert {s.id for s in b.budget.series} >= {'pay-a', 'pay-b', 'mortgage', 'grocery', 'fuel', 'lawn-care'}
    assert len(b.budget.rows) > 100
