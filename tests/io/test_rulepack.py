"""Rule packs (D-094): the shipped starter rules, and adding a pack to a household without changing what it has."""
from __future__ import annotations

from pathlib import Path

import pytest

from factories import D, budget, rule, schedule, series, txn
from hbudget.core.models import CardCategory, FlowGroup
from hbudget.core.rules import classify
from hbudget.io import ImportProblem, rulepack

ROOT = Path(__file__).resolve().parents[2]
MONTHLY = schedule('monthly', D(2029, 3, 5), day=5)


def household(**kw):  # type: ignore[no-untyped-def]
    return budget(series=(series('pay-a', 'income', -200000, MONTHLY, name='Pay A'),
                          series('pay-b', 'income', -150000, MONTHLY, name='Pay B'),
                          series('elec', 'bill', 12000, MONTHLY, name='City electric'),
                          series('net', 'bill', 6000, MONTHLY, name='Internet'),
                          series('gas', 'spending', 4000, MONTHLY, name='Fuel')), **kw)


def test_the_packaged_starter_pack_is_the_example_file_and_loads() -> None:
    canonical = (ROOT / 'examples' / 'starter-rules.yaml').read_bytes()
    packaged = (ROOT / 'src' / 'hbudget' / 'schema' / 'starter-rules.yaml').read_bytes()
    assert canonical == packaged, 'copy examples/starter-rules.yaml to src/hbudget/schema/ (`make schema`)'
    pack = rulepack.starter()
    assert len(pack.rules) >= 20 and all(p.rule.id.startswith(rulepack.STARTER_PREFIX) for p in pack.rules)
    assert all(not p.rule.targets for p in pack.rules)


def test_apply_links_pay_and_bill_rules_and_skips_what_it_cannot_link() -> None:
    out = rulepack.apply(household(), rulepack.starter())
    added = {r.id: (r, names) for r, names in out.added}
    assert added['starter-paycheck'][0].targets == ('pay-a', 'pay-b')
    assert added['starter-electric'][1] == ('City electric',) and added['starter-internet'][0].targets == ('net',)
    skipped = dict(out.skipped)
    assert skipped['Mortgage'] == 'no bill with a name like "mortgage"'
    assert 'starter-mortgage' not in added and 'starter-groceries' in added and out.kept == 0
    # the rules work on bank lines: the specific rule first
    rules = out.budget.rules
    assert classify(txn('a', D(2029, 3, 5), -11800, 'CITY ELECTRIC CO ACH'), rules).id == 'starter-electric'  # type: ignore[union-attr]
    assert classify(txn('b', D(2029, 3, 5), 200000, 'EMPLOYER DIRECT DEP'), rules).id == 'starter-paycheck'  # type: ignore[union-attr]
    assert classify(txn('c', D(2029, 3, 6), -1500, 'UBER EATS ORDER'), rules).id == 'starter-eating-out'  # type: ignore[union-attr]
    assert classify(txn('d', D(2029, 3, 6), -1500, 'UBER TRIP'), rules).id == 'starter-getting-around'  # type: ignore[union-attr]
    assert classify(txn('e', D(2029, 3, 6), -1500, 'AMAZON PRIME'), rules).id == 'starter-streaming'  # type: ignore[union-attr]
    assert classify(txn('f', D(2029, 3, 6), -1500, 'CURRENT PAYMENT'), rules) is None       # not rent
    # groups and card categories come with the track rules
    groups = {g.name: g for g in out.budget.flow_groups}
    assert groups['Food & everyday'].colour == '--f-food' and 'starter-groceries' in groups['Food & everyday'].rule_ids
    assert groups['Cash'].colour == '--f-other' and 'Cash' in out.groups
    cats = {c.name: c for c in out.budget.card_categories}
    assert 'SUPERMARKET' in cats['Grocery'].keywords and 'Grocery' in out.categories


def test_apply_never_changes_what_the_household_has() -> None:
    mine = rule('starter-groceries', 'track', ('MY SHOP',), name='My groceries')
    own = rule('power', 'bill', ('POWER',), targets=('elec',))
    b = household(rules=(own, mine), flow_groups=(FlowGroup('food & EVERYDAY', '#112233', ('x',)),),
                  card_categories=(CardCategory('grocery', ('MY SHOP',)),))
    out = rulepack.apply(b, rulepack.starter())
    assert out.budget.rules[:2] == (own, mine) and out.kept == 1
    assert ('Electric', 'the bill already has a rule') in out.skipped
    g = out.budget.flow_groups[0]
    assert g.name == 'food & EVERYDAY' and g.colour == '#112233' and g.rule_ids[0] == 'x'
    assert 'starter-fuel' in g.rule_ids and 'Food & everyday' not in out.groups
    assert out.budget.card_categories[0] == CardCategory('grocery', ('MY SHOP',)) and 'Grocery' not in out.categories
    # a second time adds nothing
    again = rulepack.apply(out.budget, rulepack.starter())
    assert again.added == () and again.budget == out.budget and again.kept == len(out.added) + 1
    # with no paychecks or bills only the track rules are added
    empty = rulepack.apply(budget(), rulepack.starter())
    assert {r.kind for r, _ in empty.added} == {'track'}
    assert ('Paycheck', 'no paycheck to link it to yet') in empty.skipped


PACK = b'''format: household-budget/rule-pack
version: 1
name: Mine
rules:
  - {id: lawn, name: Lawn, kind: bill, contains: [GREEN LAWN], bills_named: [lawn]}
'''


@pytest.mark.parametrize('bad,says', [
    (b'format: other\nversion: 1\n', 'not a rule pack'),
    (PACK.replace(b'kind: bill', b'kind: card'), 'rules[0].kind'),
    (PACK.replace(b'id: lawn', b'id: Lawn Care'), 'rules[0].id'),
    (PACK.replace(b', bills_named: [lawn]', b''), 'bills_named'),
    (PACK.replace(b'contains: [GREEN LAWN]', b'regex: "(a+)+b"'), 'rules[0].regex'),
    (PACK.replace(b'contains: [GREEN LAWN], ', b''), 'needs `contains` words'),
    (PACK.replace(b'kind: bill', b'kind: bill, targets: [x]'), 'not a field'),
    (PACK + b'  - {id: lawn, name: Again, kind: track, contains: [X]}\n', 'used twice'),
    (PACK + b'groups: {Home: red}\n', 'needs a colour'),
    (PACK + b'# ' + b'x' * rulepack.MAX_BYTES, 'at most'),
])
def test_a_bad_pack_is_refused_with_the_reason(bad: bytes, says: str) -> None:
    with pytest.raises(ImportProblem) as e:
        rulepack.load(bad, 'pack.yaml')
    assert says in str(e.value)


def test_a_custom_pack_loads() -> None:
    pack = rulepack.load(PACK, 'pack.yaml')
    assert pack.name == 'Mine' and pack.rules[0].bills_named == ('lawn',)


def test_starter_text_is_the_shipped_pack() -> None:
    text = rulepack.starter_text()
    assert text.startswith('#') or 'format: household-budget/rule-pack' in text
    assert rulepack.load(text.encode(), 'starter-rules.yaml') == rulepack.starter()


@pytest.mark.parametrize('body,why', [
    ('rules: []', 'rules'),
    ('rules: [x]', 'rule'),
    ('name: [1]\nrules: [{id: a, name: A, kind: track, contains: [X]}]', 'name'),
    ('rules: [{id: a, name: A, kind: track, contains: X}]', 'contains'),
    ('rules: [{id: a, name: A, kind: track, contains: [%s]}]' % ', '.join('W%d' % i for i in range(21)), 'contains'),
    ('rules: [{id: a, name: A, kind: track, contains: [X], direction: sideways}]', 'direction'),
    ('rules: [{id: a, name: A, kind: ignore, contains: [X], group: Fun}]', 'group'),
    ('rules: [{id: a, name: A, kind: track, regex: X, card_category: Fun}]', 'card_category'),
    ('rules: [{id: a, name: A, kind: track, contains: [X]}]\nsurprise: 1', 'surprise'),
    ('groups: [1]\nrules: [{id: a, name: A, kind: track, contains: [X]}]', 'groups'),
])
def test_each_bad_part_of_a_pack_is_named(body: str, why: str) -> None:
    text = 'format: household-budget/rule-pack\nversion: 1\nname: P\n' + body + '\n'
    with pytest.raises(ImportProblem) as e:
        rulepack.load(text.encode(), 'pack.yaml')
    assert why in str(e.value).lower(), str(e.value)


def test_a_list_or_broken_yaml_is_not_a_pack() -> None:
    for text in ('- a\n- b\n', 'format: [unclosed\n'):
        with pytest.raises(ImportProblem):
            rulepack.load(text.encode(), 'pack.yaml')
