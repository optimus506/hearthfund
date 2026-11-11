"""How much to move to a card each pay period (#31, D-085): Home, the ledger, get_paydays and card_transfer_plan."""
from __future__ import annotations

from test_api import bill, pay
from test_api_v1 import anon, get, make_token, tool  # noqa: F401 - anon is a fixture


def test_card_transfers(admin, anon):  # noqa: F811 - the anon fixture
    pay(admin, 300000, 15)
    bill(admin, 'Rent', 100000, 1)
    card = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 50000}).json()['id']
    checking = admin.api('POST', '/accounts', {'name': 'Checking A', 'kind': 'checking'}).json()['id']
    bill(admin, 'Streaming', 1500, 3, card=card)
    bill(admin, 'Phone', 6000, 20, card=card)
    home = admin.api('GET', '/home').json()
    first = home['periods'][0]            # today is 2029-03-01; the first payday is 3/15
    assert first['transfers'] == [{'card': card, 'card_name': 'Card A', 'total': 1500,
                                   'rows': [{'id': first['transfers'][0]['rows'][0]['id'], 'name': 'Streaming',
                                             'due': '2029-03-03', 'amount': 1500}]}]
    assert [t['total'] for t in home['periods'][1]['transfers']] == [7500]   # Phone 3/20 and Streaming 4/3
    assert admin.api('GET', '/ledger').json()['cards'][card] == 'Card A'

    t = make_token(admin)
    periods = tool(anon, t, 'get_paydays')['periods']
    assert periods[0]['card_transfers'][0]['total'] == 1500
    plan = tool(anon, t, 'card_transfer_plan', card_id=card)
    assert plan['through'] == '2029-03-14' and plan['total'] == 1500 and plan['card_name'] == 'Card A'
    plan = get(anon, '/cards/%s/transfer' % card, t, params={'through': '2029-03-31'}).json()
    assert plan['total'] == 7500 and [r['name'] for r in plan['rows']] == ['Streaming', 'Phone']
    assert get(anon, '/cards/%s/transfer' % checking, t).status_code == 404
    assert get(anon, '/cards/nope/transfer', t).status_code == 404


def test_forecast_moves_card_money_on_payday(admin):
    from test_api import settings_body
    pay(admin, 300000, 15)
    card = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 50000}).json()['id']
    bill(admin, 'Phone', 6000, 20, card=card)
    before = admin.api('GET', '/ledger').json()
    phone = next(r for r in before['rows'] if r['name'] == 'Phone')
    assert admin.api('GET', '/rules').json()['settings']['card_transfers_on_payday'] is False
    body = settings_body(admin, card_transfers_on_payday=True)
    assert admin.api('PUT', '/settings', body).status_code == 200
    assert admin.api('GET', '/rules').json()['settings']['card_transfers_on_payday'] is True
    after = admin.api('GET', '/ledger').json()
    pay_row = next(r for r in after['rows'] if r['name'] == 'Pay A' and r['due'] == '2029-03-15')
    phone_after = next(r for r in after['rows'] if r['id'] == phone['id'])
    assert phone_after['balance'] == phone['balance']                 # the same money by 3/20...
    assert phone_after['balance'] == pay_row['balance'] - 6000        # ...moved out right after the pay on 3/15
    assert any(p[2] == 'Move to Card A' and p[0] == '2029-03-15' for p in after['chart'])
    # saving the settings without the field keeps it; it is in the backup and comes back in a restore
    body = settings_body(admin)
    body.pop('card_transfers_on_payday', None)
    assert admin.api('PUT', '/settings', body).status_code == 200
    assert admin.api('GET', '/rules').json()['settings']['card_transfers_on_payday'] is True
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid)
    assert backup.json()['settings']['card_transfers_on_payday'] is True
    assert admin.api('POST', '/wipe', {'confirm': 'Test household'}).status_code == 200
    assert admin.api('GET', '/rules').json()['settings']['card_transfers_on_payday'] is False
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('b.json', backup.content)},
                   data={'mode': 'replace', 'dry_run': '0', 'confirm': 'Test household'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    assert admin.api('GET', '/rules').json()['settings']['card_transfers_on_payday'] is True


def test_changing_amount_and_card_together_moves_hand_edited_rows_too(admin):
    """#60: a save that changes the amount and the card relabels rows edited by hand, not only the generated ones."""
    pay(admin, 300000, 15)
    card = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 50000}).json()['id']
    sid = bill(admin, 'Grocery', 20000, 20)
    rows = [r for r in admin.api('GET', '/ledger').json()['rows'] if r['series_id'] == sid]
    edited = rows[0]['id']
    assert admin.api('POST', '/rows/%s/change' % edited, {'amount': 25000, 'expected_amount': 20000}).status_code == 200
    body = {'name': 'Groceries', 'kind': 'bill', 'amount': 21000, 'card': card,
            'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 20}}
    assert admin.api('PUT', '/series/%s' % sid, body).status_code == 200
    after = {r['id']: r for r in admin.api('GET', '/ledger').json()['rows'] if r['series_id'] == sid}
    assert after[edited]['card'] == card and after[edited]['amount'] == 25000 and after[edited]['name'] == 'Groceries'
    assert all(r['card'] == card for r in after.values() if r['state'] == 'open')
    home = admin.api('GET', '/home').json()
    assert home['periods'][1]['transfers'][0]['total'] == 25000           # the hand-edited 3/20 row counts
