"""Recurring charges from the bank and card history (issue #34, D-084)."""
from factories import D, budget, rule, txn
from hbudget.core.dates import add_months
from hbudget.core.models import CardTxn
from hbudget.core.subscriptions import detect

TODAY = D(2029, 10, 15)


def monthly(prefix, desc, amount, first, n, day_shift=0):
    out = []
    for i in range(n):
        d = add_months(first, i)
        out.append(txn('%s%d' % (prefix, i), d.replace(day=min(28, d.day + (day_shift if i % 2 else 0))),
                       -amount, desc))
    return out


def test_the_household_example():
    hist = [
        *monthly('a', 'PROCESSOR *VIDEO', 8000, D(2029, 1, 3), 10),
        *monthly('b', 'PROCESSOR *MUSIC', 2200, D(2029, 1, 8), 5),           # 22.00 Jan to May
        *monthly('c', 'PROCESSOR *MUSIC', 2600, D(2029, 6, 8), 5),           # 26.00 from June
        *monthly('d', 'PROCESSOR *GAMES', 1999, D(2029, 1, 20), 10, day_shift=2),
        *monthly('e', 'PROCESSOR *CLOUD', 999, D(2029, 1, 12), 7),           # stopped after July
        *monthly('f', 'PROCESSOR *NEWS', 1099, D(2029, 1, 25), 9),
        txn('g1', D(2028, 3, 1), -9900, 'ANNUAL DUES'), txn('g2', D(2029, 3, 1), -9900, 'ANNUAL DUES'),
        txn('h1', D(2029, 9, 1), -500, 'NEW APP'), txn('h2', D(2029, 10, 1), -500, 'NEW APP'),
        # not subscriptions: varying amounts, pay, a card payment, an old stopped one, a single charge
        *[txn('m%d' % i, D(2029, i, 4), -(5000 + 137 * i), 'CORNER MARKET') for i in range(1, 10)],
        *[txn('p%d' % i, D(2029, i, 1), 300000, 'EMPLOYER PAY') for i in range(1, 10)],
        *[txn('k%d' % i, D(2029, i, 9), -20000, 'CARD CO PAYMENT') for i in range(1, 10)],
        txn('o1', D(2028, 1, 5), -700, 'OLD GYM'), txn('o2', D(2028, 2, 5), -700, 'OLD GYM'),
        txn('s1', D(2029, 5, 5), -4500, 'ONE OFF SHOP'),
    ]
    b = budget(rules=(rule('cardpay', 'card', ('CARD CO',)), rule('vid', 'bill', ('VIDEO',), name='Video',
                                                                    targets=('video',))))
    s = detect(hist, [], b, today=TODAY)
    got = {(i.payee, i.status): i for i in s.items}
    assert sorted(got) == [('ANNUAL DUES', 'active'), ('NEW APP', 'new'), ('PROCESSOR *CLOUD', 'stopped'),
                           ('PROCESSOR *GAMES', 'active'), ('PROCESSOR *MUSIC', 'active'),
                           ('PROCESSOR *NEWS', 'active'), ('PROCESSOR *VIDEO', 'active')]
    music = got[('PROCESSOR *MUSIC', 'active')]
    assert music.amount == 2600 and music.count == 10 and music.price_changes == ((D(2029, 6, 8), 2200, 2600),)
    assert music.next == D(2029, 11, 8) and music.cadence == 'monthly'
    cloud = got[('PROCESSOR *CLOUD', 'stopped')]
    assert cloud.last == D(2029, 7, 12) and cloud.next == D(2029, 8, 12)
    video = got[('PROCESSOR *VIDEO', 'active')]
    assert (video.rule_id, video.rule_name, video.targets) == ('vid', 'Video', ('video',))
    club = got[('ANNUAL DUES', 'active')]
    assert club.cadence == 'yearly' and club.monthly == 825 and club.next == D(2030, 3, 1)
    assert got[('PROCESSOR *GAMES', 'active')].count == 9   # a day or two late is still monthly (10/22 is ahead)
    # the order: live ones by monthly cost, then the stopped ones
    assert [i.payee for i in s.items] == ['PROCESSOR *VIDEO', 'PROCESSOR *MUSIC', 'PROCESSOR *GAMES', 'PROCESSOR *NEWS',
                                          'ANNUAL DUES', 'NEW APP', 'PROCESSOR *CLOUD']
    assert s.monthly_total == 8000 + 2600 + 1999 + 1099 + 825 + 500
    assert s.yearly_total == 12 * (8000 + 2600 + 1999 + 1099 + 500) + 9900
    assert s.price_rises == 1
    assert len({i.id for i in s.items}) == len(s.items)


def test_card_subscriptions_and_edges():
    cards = [CardTxn('c%d' % i, add_months(D(2029, 6, 2), i), add_months(D(2029, 6, 2), i), -1599,
                     'STREAMBOX*AB%d' % i, 'Fun') for i in range(5)]
    cards.append(CardTxn('pay', D(2029, 7, 1), D(2029, 7, 1), 50000, 'PAYMENT', 'Payment'))
    s = detect([], cards, budget(), today=TODAY)
    assert [(i.payee, i.source, i.category, i.count) for i in s.items] == [('STREAMBOX', 'card', 'Fun', 5)]
    assert detect([], [], budget(), today=TODAY).items == ()
    # a price that doubles more than twice over is a different charge; a cut in price is followed too
    hist = [*monthly('a', 'SVC', 1000, D(2029, 1, 5), 3), *monthly('b', 'SVC', 3000, D(2029, 4, 5), 3),
            *monthly('c', 'TV', 2000, D(2029, 4, 6), 3), txn('c9', D(2029, 7, 6), -1500, 'TV')]
    got = {(i.payee, i.amount): i for i in detect(hist, [], budget(), today=D(2029, 7, 20)).items}
    assert set(got) == {('SVC', 1000), ('SVC', 3000), ('TV', 1500)}
    assert got[('SVC', 1000)].status == 'stopped'
    assert got[('TV', 1500)].price_changes == ((D(2029, 7, 6), 2000, 1500),)   # the latest single charge counts
    # future-dated lines are ignored
    assert detect([txn('f1', D(2030, 1, 1), -100, 'X'), txn('f2', D(2030, 2, 1), -100, 'X')], [], budget(),
                  today=TODAY).items == ()


def test_payees_marked_not_a_subscription_are_hidden():
    from dataclasses import replace

    hist = [*monthly('a', 'PROCESSOR *VIDEO', 8000, D(2029, 1, 3), 10), *monthly('b', 'GYM', 3000, D(2029, 1, 9), 10)]
    cards = [CardTxn('c%d' % i, add_months(D(2029, 6, 2), i), add_months(D(2029, 6, 2), i), -1599, 'GYM', 'Fun')
             for i in range(5)]
    b = budget()
    b = replace(b, settings=replace(b.settings, not_recurring=(('bank', 'GYM'),)))
    s = detect(hist, cards, b, today=TODAY)
    assert [(i.source, i.payee) for i in s.items] == [('bank', 'PROCESSOR *VIDEO'), ('card', 'GYM')]
    assert [(i.source, i.payee) for i in s.hidden] == [('bank', 'GYM')]   # by source: the card's GYM stays
    assert s.monthly_total == 8000 + 1599


def test_bills_that_drop_in_price_or_skip_a_month_are_not_stopped():
    """#61: a big price drop and a skipped month continue a bill a bill rule matches."""
    phone = [*monthly('a', 'PHONE CO', 9000, D(2029, 5, 8), 5), txn('a9', D(2029, 10, 6), -3300, 'PHONE CO')]
    loan = [*monthly('l', 'LOAN CO', 50000, D(2029, 3, 3), 5), txn('l7', D(2029, 8, 3), -60000, 'LOAN CO'),
            txn('l8', D(2029, 10, 2), -35000, 'LOAN CO')]                  # nothing in September
    b = budget(rules=(rule('ph', 'bill', ('PHONE CO',)), rule('ln', 'bill', ('LOAN CO',))))
    got = {i.payee: i for i in detect([*phone, *loan], [], b, today=D(2029, 10, 20)).items}
    assert got['PHONE CO'].status == 'active' and got['PHONE CO'].amount == 3300
    assert got['PHONE CO'].price_changes == ((D(2029, 10, 6), 9000, 3300),)
    assert got['LOAN CO'].status == 'active' and got['LOAN CO'].amount == 35000 and got['LOAN CO'].count == 7
    assert [c[1:] for c in got['LOAN CO'].price_changes] == [(50000, 60000), (60000, 35000)]
    assert {i.group for i in got.values()} == {'bill'}
    # without a bill rule the cheaper plan is a different charge: the old one is irregular (still charged)
    got = {(i.payee, i.amount): i for i in detect(phone, [], budget(), today=D(2029, 10, 20)).items}
    assert got[('PHONE CO', 9000)].status == 'irregular' and got[('PHONE CO', 9000)].latest == (D(2029, 10, 6), 3300)


def test_noisy_signals():
    """#62: a 1-cent change is no price change; a store also paid other amounts needs three charges; generic
    payees are not joined; the totals say what is a bill."""
    ins = [txn('i1', D(2029, 7, 1), -25001, 'INSURER'), txn('i2', D(2029, 8, 1), -25001, 'INSURER'),
           txn('i3', D(2029, 9, 1), -25000, 'INSURER'), txn('i4', D(2029, 10, 1), -25000, 'INSURER')]
    pp = [*monthly('p', 'PAYPAL TYPE: PURCHASE', 1099, D(2029, 6, 4), 3),
          *monthly('q', 'PAYPAL TYPE: PURCHASE', 1599, D(2029, 9, 4), 2)]
    cards = [CardTxn('f1', D(2029, 8, 9), D(2029, 8, 9), -1500, 'SANDWICH PLACE', 'Eating out'),
             CardTxn('f2', D(2029, 9, 9), D(2029, 9, 9), -1500, 'SANDWICH PLACE', 'Eating out'),
             CardTxn('f3', D(2029, 9, 20), D(2029, 9, 20), -2200, 'SANDWICH PLACE', 'Eating out'),
             *[CardTxn('m%d' % i, add_months(D(2029, 8, 2), i), add_months(D(2029, 8, 2), i), -999, 'MUSICBOX', 'Fun')
               for i in range(3)]]
    b = budget(rules=(rule('ins', 'bill', ('INSURER',)),))
    s = detect([*ins, *pp], cards, b, today=D(2029, 10, 15))
    got = {(i.payee, i.amount): i for i in s.items}
    assert set(got) == {('INSURER', 25000), ('PAYPAL TYPE: PURCHASE', 1099), ('PAYPAL TYPE: PURCHASE', 1599),
                        ('MUSICBOX', 999)}                                   # no sandwich subscription
    assert got[('INSURER', 25000)].price_changes == () and got[('INSURER', 25000)].count == 4
    assert got[('PAYPAL TYPE: PURCHASE', 1599)].generic and got[('PAYPAL TYPE: PURCHASE', 1599)].price_changes == ()
    assert got[('PAYPAL TYPE: PURCHASE', 1099)].status == 'stopped'
    assert s.bills_monthly == 25000 and s.subscriptions_monthly == 1599 + 999 == s.monthly_total - s.bills_monthly


def test_is_generic_names_payment_processors_not_shops() -> None:
    from hbudget.core.subscriptions import is_generic
    assert is_generic('PAYPAL *SOMESHOP') and is_generic('SQ *COFFEE CART') and is_generic('AMZN MKTP')
    assert not is_generic('CITY WATER') and not is_generic('XPAYPAL')            # a prefix, not anywhere
