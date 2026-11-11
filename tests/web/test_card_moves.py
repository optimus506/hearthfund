"""Card recategorising (D-083): store and purchase moves on the Card page, kept through re-sorting, backup and
restore, wiped with the budget; the card tools for agents (#33)."""
from __future__ import annotations

from typing import Any

from test_api_v1 import anon, get, make_token, rpc, tool  # noqa: F401 - anon is a fixture
from test_reconcile_flow import upload

from webhelpers import Client

CARD = (b'Date,Description,Amount\n'
        b'03/02/2029,CORNER MARKET #12,-40.00\n'
        b'03/03/2029,CORNER MARKET #34,-10.00\n'
        b'03/04/2029,BISTRO*AB12,-25.00\n'
        b'03/06/2029,KIOSK,-5.00\n'
        b'03/05/2029,PAYMENT THANK YOU,100.00\n')


def setup(admin: Client) -> None:
    r = admin.api('PUT', '/card/categories', {'categories': [{'name': 'Grocery', 'keywords': ['MARKET']},
                                                               {'name': 'Fun', 'keywords': []}]})
    assert r.status_code == 200, r.text
    assert upload(admin, 'card.csv', CARD, kind='card').status_code == 200


def purchases(admin: Client, **q: str) -> list[dict[str, Any]]:
    qs = '&'.join('%s=%s' % kv for kv in q.items())
    return admin.api('GET', '/card/purchases?' + qs).json()['purchases']


def by_category(admin: Client) -> dict[str, int]:
    return dict(admin.api('GET', '/card').json()['summary']['by_category'])


def test_stores_and_purchases(admin):
    setup(admin)
    ss = admin.api('GET', '/card/stores').json()
    assert [(s['store'], s['category'], s['spent'], s['purchases']) for s in ss['stores']] == [
        ('CORNER MARKET', 'Grocery', 5000, 2), ('BISTRO', 'Other', 2500, 1), ('KIOSK', 'Other', 500, 1)]
    assert [s['store'] for s in admin.api('GET', '/card/stores?category=Other').json()['stores']] == ['BISTRO', 'KIOSK']
    ps = purchases(admin, store='CORNER MARKET')
    assert [(p['purchased'], p['amount'], p['by_hand']) for p in ps] == [('2029-03-03', 1000, False),
                                                                          ('2029-03-02', 4000, False)]
    assert len(purchases(admin)) == 4 and len(purchases(admin, limit='1')) == 1   # the payment is not a purchase


def test_store_move_wins_over_keywords_and_hand_moves(admin):
    setup(admin)
    one = purchases(admin, store='CORNER MARKET')[0]['id']
    assert admin.api('PUT', '/card/purchases/' + one, {'category': 'Fun'}).json()['by_hand'] is True
    r = admin.api('PUT', '/card/stores', {'store': 'corner market #99', 'category': 'Fun'}).json()
    assert r['store'] == 'CORNER MARKET' and r['category'] == 'Fun' and r['note'] is None
    assert by_category(admin) == {'Fun': 5000, 'Other': 3000}
    assert all(not p['by_hand'] for p in purchases(admin, category='Fun'))     # the store move took them over
    cats = admin.api('GET', '/card').json()['categories']
    assert next(c for c in cats if c['name'] == 'Fun')['merchants'] == ['CORNER MARKET']
    # saving the categories from the page (no merchants sent) keeps the store move
    admin.api('PUT', '/card/categories', {'categories': [{'name': 'Grocery', 'keywords': ['MARKET']},
                                                          {'name': 'Fun', 'keywords': ['KIOSK']}]})
    assert by_category(admin) == {'Fun': 5500, 'Other': 2500}
    # back to Other: the keyword sorts it again, and the answer says so
    r = admin.api('PUT', '/card/stores', {'store': 'CORNER MARKET', 'category': 'Other'}).json()
    assert r['category'] == 'Grocery' and 'MARKET' not in r['note'] and 'Grocery' in r['note']
    assert by_category(admin)['Grocery'] == 5000
    assert admin.api('PUT', '/card/stores', {'store': 'X', 'category': 'Nope'}).status_code == 400


def test_purchase_move_survives_resorting_and_can_be_undone(admin):
    setup(admin)
    kiosk = purchases(admin, store='KIOSK')[0]['id']
    assert admin.api('PUT', '/card/purchases/' + kiosk, {'category': 'Grocery'}).status_code == 200
    admin.api('PUT', '/card/categories', {'categories': [{'name': 'Grocery', 'keywords': ['MARKET']},
                                                          {'name': 'Fun', 'keywords': ['KIOSK']}]})
    assert purchases(admin, store='KIOSK')[0]['category'] == 'Grocery'          # by hand: kept
    r = admin.api('PUT', '/card/purchases/' + kiosk, {'category': None}).json()
    assert r == {'id': kiosk, 'category': 'Fun', 'by_hand': False}
    assert admin.api('PUT', '/card/purchases/nope', {'category': 'Fun'}).status_code == 404
    assert admin.api('PUT', '/card/purchases/' + kiosk, {'category': 'Nope'}).status_code == 400
    actions = [a['action'] for a in admin.api('GET', '/audit').json()['entries']]
    assert 'card.purchase' in actions


def test_moves_in_backup_restore_and_wipe(admin):
    setup(admin)
    admin.api('PUT', '/card/stores', {'store': 'BISTRO', 'category': 'Fun'})
    kiosk = purchases(admin, store='KIOSK')[0]['id']
    admin.api('PUT', '/card/purchases/' + kiosk, {'category': 'Grocery'})
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid)
    doc = backup.json()
    assert next(c for c in doc['card_categories'] if c['name'] == 'Fun')['merchants'] == ['BISTRO']
    assert [t['by_hand'] for t in doc['backup']['card_transactions'] if t.get('by_hand')] == [True]
    r = admin.api('POST', '/wipe', {'confirm': 'Test household'})
    assert r.status_code == 200, r.text
    assert purchases(admin) == [] and admin.api('GET', '/card').json()['categories'] == []
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('b.json', backup.content)},
                   data={'mode': 'replace', 'dry_run': '0', 'confirm': 'Test household'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    assert by_category(admin) == {'Grocery': 5500, 'Fun': 2500}
    assert purchases(admin, store='KIOSK')[0]['by_hand'] is True
    assert next(c for c in admin.api('GET', '/card').json()['categories'] if c['name'] == 'Fun')['merchants'] == [
        'BISTRO']


def test_read_token_cannot_move(admin, anon):  # noqa: F811 - the anon fixture
    setup(admin)
    ro = make_token(admin, 'read')
    h = {'Authorization': 'Bearer ' + ro}
    assert get(anon, '/card/stores', ro).status_code == 200
    assert anon.put('/api/v1/card/stores', headers=h, json={'store': 'KIOSK', 'category': 'Fun'}).status_code == 403
    assert anon.put('/api/v1/card/purchases/x', headers=h, json={'category': 'Fun'}).status_code == 403
    assert rpc(anon, ro, 'tools/call', {'name': 'set_merchant_category',
                                        'arguments': {'merchant': 'KIOSK', 'category': 'Fun'}}).json()['error']


def test_card_tools(admin, anon):  # noqa: F811 - the anon fixture
    setup(admin)
    ro, rw = make_token(admin, 'read'), make_token(admin, 'write', name='Agent')
    cats = tool(anon, ro, 'list_card_categories')
    assert [c['name'] for c in cats['categories']] == ['Grocery', 'Fun'] and cats['built_in'] == ['Other', 'Payment']
    sp = tool(anon, ro, 'get_card_spending', since='2029-03-01', until='2029-03-31')
    assert sp['by'] == 'month' and sp['periods'] == [{'start': '2029-03-01', 'total': 8000, 'budget': {},
                                                      'spent': {'Grocery': 5000, 'Other': 3000}}]
    weeks = tool(anon, ro, 'get_card_spending', since='2029-03-01', until='2029-03-14', by='week', category='Other')
    assert sum(p['total'] for p in weeks['periods']) == 3000 and all(set(p['spent']) <= {'Other'}
                                                                     for p in weeks['periods'])
    assert [s['store'] for s in tool(anon, ro, 'list_card_stores', category='Other')['stores']] == ['BISTRO', 'KIOSK']
    kiosk = tool(anon, ro, 'list_card_purchases', store='KIOSK')['purchases'][0]
    assert tool(anon, rw, 'set_merchant_category', merchant='BISTRO', category='Fun')['category'] == 'Fun'
    assert tool(anon, rw, 'set_purchase_category', purchase_id=kiosk['id'], category='Fun')['by_hand'] is True
    assert by_category(admin) == {'Grocery': 5000, 'Fun': 3000}
    assert tool(anon, rw, 'set_purchase_category', purchase_id=kiosk['id'], category=None)['category'] == 'Other'
    assert by_category(admin) == {'Grocery': 5000, 'Fun': 2500, 'Other': 500}
    r = get(anon, '/card/spending?by=week&since=2029-03-01&until=2029-03-07', ro)
    assert r.status_code == 200 and r.json()['periods'][0]['spent'] == {'Fun': 2500, 'Grocery': 5000}
    assert get(anon, '/card/spending?since=2029-03-09&until=2029-03-01', ro).status_code == 400
    assert get(anon, '/card/spending?by=week&since=2020-01-01&until=2029-03-01', ro).status_code == 400


def test_a_card_upload_re_dates_purchases_stored_by_posting_date(admin, tmp_path):
    """#63: purchases stored before the default pattern get their purchase date on the next card upload."""
    from datetime import date

    from hbudget.core.models import CardTxn
    from hbudget.db import connection, repo
    old = [CardTxn('old1', date(2029, 2, 1), date(2029, 2, 1), -18772, 'GROCER #12 Date 01/31/29', 'Other'),
           CardTxn('old2', date(2029, 2, 1), date(2029, 2, 1), -500, 'KIOSK', 'Other')]
    conn = connection.connect(tmp_path / 'hbudget.db')
    repo.insert_card_txns(conn, admin.hid, None, old)
    conn.commit()
    conn.close()
    assert upload(admin, 'card.csv', CARD, kind='card').status_code == 200
    got = {p['id']: p['purchased'] for p in purchases(admin)}
    assert got['old1'] == '2029-01-31' and got['old2'] == '2029-02-01'
