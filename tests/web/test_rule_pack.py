"""Starter rules on the Rules page and the command line (D-094)."""
from __future__ import annotations

from test_api import bill, pay
from test_cli import data, run  # noqa: F401 - `data` is a fixture

from hbudget.io import rulepack


def test_preview_then_add_and_new_rules_go_above_the_starter_rules(admin) -> None:  # type: ignore[no-untyped-def]
    pay_id, electric = pay(admin), bill(admin, 'Electric', 12000, 5)
    r = admin.api('POST', '/rules/pack', {})
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry['dry_run'] and {'id': 'starter-electric', 'name': 'Electric', 'kind': 'bill',
                               'clears': ['Electric']} in dry['added']
    assert {'name': 'Mortgage', 'why': 'no bill with a name like "mortgage"'} in dry['skipped']
    assert admin.api('GET', '/rules').json()['rules'] == []                 # a dry run saves nothing
    done = admin.api('POST', '/rules/pack', {'dry_run': False}).json()
    assert done['added'] == dry['added'] and 'Grocery' in done['categories']
    rules = admin.api('GET', '/rules').json()['rules']
    by = {x['id']: x for x in rules}
    assert by['starter-paycheck']['targets'] == [pay_id] and by['starter-electric']['targets'] == [electric]
    again = admin.api('POST', '/rules/pack', {'dry_run': False}).json()
    assert again['added'] == [] and again['kept'] == len(rules)
    # a household's own rule is tried before the starter rules
    body = {'name': 'Farm stand', 'kind': 'track', 'contains': ['GROCERY CO-OP']}
    new = admin.api('POST', '/rules', body).json()['id']
    ids = [x['id'] for x in admin.api('GET', '/rules').json()['rules']]
    assert ids[0] == new and all(i.startswith('starter-') for i in ids[1:])
    assert admin.api('POST', '/rules/test', {'description': 'GROCERY CO-OP 12', 'amount': -500}).json()[
        'first'] == 'Farm stand'


def test_a_pack_file_the_download_and_who_may_add(admin) -> None:  # type: ignore[no-untyped-def]
    bill(admin, 'Lawn care', 5000, 9)
    text = ('format: household-budget/rule-pack\nversion: 1\nname: Mine\nrules:\n'
            '  - {id: lawn, name: Lawn, kind: bill, contains: [GREEN LAWN], bills_named: [lawn]}\n')
    got = admin.api('POST', '/rules/pack', {'text': text, 'dry_run': False}).json()
    assert got['pack'] == 'Mine' and [a['clears'] for a in got['added']] == [['Lawn care']]
    bad = admin.api('POST', '/rules/pack', {'text': 'format: nope\n'})
    assert bad.status_code == 400 and 'not a rule pack' in bad.json()['message']
    assert admin.api('POST', '/rules/pack', {'text': 'x' * (rulepack.MAX_BYTES + 1)}).status_code == 400
    down = admin.api('GET', '/rules/starter')
    assert down.status_code == 200 and down.text == rulepack.starter_text()
    assert 'attachment' in down.headers['content-disposition']


def test_command_line(data, capsys) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    assert run('migrate') == 0 and run('demo') == 0
    capsys.readouterr()
    assert run('rule-pack', '--show') == 0
    assert capsys.readouterr().out == rulepack.starter_text()
    assert run('rule-pack') == 0
    out = capsys.readouterr().out
    assert 'would add Groceries (track)' in out and 'dry run: add --write' in out
    assert run('rule-pack', '--write') == 0
    assert 'added Groceries (track)' in capsys.readouterr().out
    assert run('rule-pack', '--write') == 0
    assert '\n0 rule(s) added, ' in capsys.readouterr().out
