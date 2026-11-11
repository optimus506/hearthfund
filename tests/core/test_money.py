from decimal import Decimal

import pytest

from hbudget.core import money


@pytest.mark.parametrize('value, cents', [
    (1800, 180000), (1800.5, 180050), ('1,800.50', 180050), ('$1,800.50', 180050), ('(1,800.50)', -180050),
    ('-1800.50', -180050), ('$-12.00', -1200), ('-$12', -1200), (' 7 ', 700), ('+3.1', 310), (0, 0),
    (Decimal('2.25'), 225), (0.07, 7), (1234.56, 123456), ('.5', 50),
])
def test_parse(value, cents):
    assert money.parse(value) == cents


@pytest.mark.parametrize('bad', ['1.234', 0.1 + 0.2, '12a', '', '$', '1.2.3', 'nan', float('inf')])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        money.parse(bad)


def test_parse_rejects_bool_and_other_types():
    with pytest.raises(TypeError):
        money.parse(True)
    with pytest.raises(TypeError):
        money.parse([1])  # type: ignore[arg-type]


def test_fmt():
    assert money.fmt(180050) == '$1,800.50'
    assert money.fmt(-180050) == '-$1,800.50'
    assert money.fmt(-180050, sign='parens') == '($1,800.50)'
    assert money.fmt(5) == '$0.05'
    assert money.fmt(0) == '$0.00'
    assert money.fmt(123456789) == '$1,234,567.89'


@pytest.mark.parametrize('value, bp', [(24.99, 2499), (10, 1000), ('29.95%', 2995), (0, 0), (Decimal('0.5'), 50)])
def test_percent_to_bp(value, bp):
    assert money.percent_to_bp(value) == bp


def test_percent_to_bp_rejects_three_decimals():
    with pytest.raises(ValueError):
        money.percent_to_bp(29.995)
    with pytest.raises(ValueError):
        money.percent_to_bp('abc')


def test_bp_to_percent():
    assert money.bp_to_percent(2499) == '24.99'
    assert money.bp_to_percent(1000) == '10'
    assert money.bp_to_percent(50) == '0.5'
