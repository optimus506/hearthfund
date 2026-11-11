import pytest

from factories import D, rule, txn
from hbudget.core import rules as R
from hbudget.core.models import Direction
from hbudget.core.rules import classify, compile_safe, matches


def t(desc, amount=-1000):
    return txn('x', D(2029, 3, 1), amount, desc)


def test_contains_case_and_whitespace():
    r = rule('elec', 'bill', ('electric  co',))
    assert matches(r, 'ACH  Electric   Co 123', -100)
    assert not matches(r, 'GAS CO', -100)


def test_regex_and_both():
    r = rule('card', 'card', (), regex=r'TO CARD \S*1234')
    assert matches(r, 'Transfer to card xx1234', -100)
    both = rule('b', 'bill', ('TRANSFER',), regex=r'1234$')
    assert matches(both, 'TRANSFER 1234', -1) and not matches(both, 'TRANSFER 9999', -1)
    assert not matches(rule('none', 'bill', ()), 'ANY', -1)


def test_direction_and_amount_limits():
    pay = rule('pay', 'pay', ('PAYROLL',), direction=Direction.IN, min_amount=100000)
    assert matches(pay, 'EMPLOYER A PAYROLL', 240000)
    assert not matches(pay, 'EMPLOYER A PAYROLL', -240000)
    assert not matches(pay, 'EMPLOYER A PAYROLL', 4000)        # a small item from the same payer is not pay
    out = rule('o', 'track', ('SHOP',), direction=Direction.OUT, max_amount=5000)
    assert matches(out, 'SHOP', -5000) and not matches(out, 'SHOP', -5001) and not matches(out, 'SHOP', 10)


def test_classify_first_match_wins():
    a = rule('a', 'bill', ('LOAN',))
    b = rule('b', 'bill', ('AUTO LOAN',))
    assert classify(t('AUTO LOAN PMT'), [a, b]).id == 'a'
    assert classify(t('AUTO LOAN PMT'), [b, a]).id == 'b'
    assert classify(t('NOTHING'), [a, b]) is None


def test_test_box():
    a, b = rule('a', 'bill', ('LOAN',)), rule('b', 'bill', ('GYM',))
    assert [(r.id, ok) for r, ok in R.test([a, b], 'GYM MEMBERSHIP', -4500)] == [('a', False), ('b', True)]


def test_long_text_is_capped():
    r = rule('x', 'bill', (), regex='END$')
    assert not matches(r, 'A' * 600 + ' END', -1)


@pytest.mark.parametrize('pattern', [
    r'(a+)+$', r'(\w*)*', r'(a|aa)+', r'(x|y)*', r'(a?)+', r'(?:a+){2,}', r'(\d+)+x',
    r'(a)\1', r'(?P<n>a)(?P=n)', r'(?=x)', r'(?!x)', r'(?<=x)', r'(?<!x)', r'(?>a)', r'a++', r'a*+',
    r'a{1,500}', r'a{200}', r'(ab)++', r'a?+', r'a{2}+', r'(?:ab)*+', r'x' * 201, r'(a', r'a)', r'(?(1)a|b)', r'\g<1>',
])
def test_compile_safe_rejects(pattern):
    with pytest.raises(ValueError):
        compile_safe(pattern)


@pytest.mark.parametrize('pattern', [
    r'TO CARD \S*1234', r'^ACH (PAYROLL|DIRECT DEP)', r'\bACME\b', r'[A-Z]{2,10}\d{4}', r'(ab)?c', r'(abc)+',
    r'[()|+*]+', r'a{3}', r'BOUGHT ON (\d{2})/(\d{2})/(\d{4})', r'x{,5}', r'a+?b', r'[^)]+x', r'[]a]+', r'[^]a]+',
])
def test_compile_safe_accepts(pattern):
    assert compile_safe(pattern).pattern == pattern


def test_compile_safe_bad_syntax_and_ignorecase():
    with pytest.raises(ValueError):
        compile_safe('[unclosed')
    assert compile_safe('abc').search('xABCx')


REJECTED_SLOW = [
    # measured slow (seconds to hours on 500 characters) with the earlier counting rule
    r'\w*\w*\w*\w*\w*!', r'a{0,99}a{0,99}a{0,99}a{0,99}!', 'a?' * 28 + 'a' * 28, r'\w{0,20}' * 12 + '!',
    r'\w?' * 60 + '!', r'.*' + r'\w?' * 60 + '!', r'(\w|\d)?' * 20 + '!',
    # two open-ended repeats, or too many optional parts
    r'\d+\s+\d+', r'a*b*', r'.*x.*', r'(ab)+c+', r'x{3,}y{3,}', r'x{0,20}y{0,20}z+', 'a?' * 16, 'a?' * 13,
    r'\S*' + 'a?' * 4,
]
ACCEPTED = [r'TO CARD \S*1234', r'^ACH (PAYROLL|DIRECT DEP)', r'\d{4}-\d{2}-\d{2}', r'ACME .*', r'a{30}b+',
            r'\S{21}\S{21}', r'\S*' + 'a?' * 2, r'ACME .*CO?R?P?', r'Date (\d{1,2})/(\d{1,2})/(\d{2})',
            r'[A-Z]{2,10}\d{4}',
            r'^(EMPLOYER A|EMPLOYER B) PAYROLL']
ADVERSARIAL = ['a' * 500, 'x' * 500, '1' * 500, ' ' * 500, 'TO CARD ' + 'x' * 492, 'ab' * 250, 'A1 ' * 166,
               'ACME ' * 100, 'CORP' * 125, 'Date 1/2/' * 55]


@pytest.mark.parametrize('pattern', REJECTED_SLOW)
def test_compile_safe_rejects_expensive_patterns(pattern):
    with pytest.raises(ValueError, match='too long'):
        compile_safe(pattern)


@pytest.mark.parametrize('pattern', ACCEPTED)
def test_compile_safe_keeps_common_patterns(pattern):
    assert compile_safe(pattern).pattern == pattern
    assert R.pattern_cost(pattern) <= R.MAX_COST


@pytest.mark.parametrize('pattern', ACCEPTED)
def test_accepted_patterns_are_fast_on_adversarial_text(pattern):
    import time
    for text in ADVERSARIAL:
        t0 = time.perf_counter()
        R.safe_search(pattern + '!', text)        # a forced failure at the end: the worst case
        assert time.perf_counter() - t0 < 0.05, (pattern, text[:10])


def test_pattern_cost_rules():
    assert R.pattern_cost('abc') == 1
    assert R.pattern_cost('a?') == 2 and R.pattern_cost('a{2,5}') == 4 and R.pattern_cost('a+') == 501
    assert R.pattern_cost('(a|b|cd)') == 3 and R.pattern_cost('(a|bc)?') == 2 * 2
    assert R.pattern_cost('(a?){3}') == 8 and R.pattern_cost('(ab?){1,2}') == 2 * 4
    assert R.pattern_cost('a?' * 40) == R.MAX_COST + 1 and R.MAX_COST == 5000
    with pytest.raises(ValueError):
        R.pattern_cost('[unclosed')


def test_safe_search_truncates_the_text():
    assert R.safe_search(r'END$', 'A' * 600 + 'END') is None
    assert R.safe_search(r'acme', 'pay ACME co').group() == 'ACME'
    with pytest.raises(ValueError):
        R.safe_search(r'(a+)+', 'aaa')
