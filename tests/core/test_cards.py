from factories import D, budget, row, schedule, series
from hbudget.core.cards import DEFAULT_CATEGORIES, categorise, recategorise, spending, store, stores, weekly
from hbudget.core.models import CardCategory, CardTxn, RowState, Settings

CATS = (*DEFAULT_CATEGORIES, CardCategory('Lawn care', ('LAWN',), has_own_rows=True))
GROCERY = CardCategory('Grocery', ('GROCERY', 'MARKET'), weekly_series=('grocery',))


def test_categorise():
    assert categorise('CORNER  supermarket 12', '', CATS) == 'Grocery'
    assert categorise('MAIN ST GRILL', '', CATS) == 'Eating out'
    assert categorise('SOMEWHERE', 'Food & Dining', CATS) == 'Eating out'
    assert categorise('SOMEWHERE', 'Travel', CATS) == 'Other'
    assert categorise('ANY', '', CATS, amount=5000) == 'Payment'
    assert categorise('GAS STATION 9', '', CATS, amount=-5000) == 'Fuel'


def card(i, d, amount, cat, desc='X'):
    return CardTxn('k%d' % i, d, d, amount, desc, cat)


def test_weekly_summary():
    cats = (GROCERY, CardCategory('Fuel', ('FUEL',)), CardCategory('Lawn care', ('LAWN',), has_own_rows=True))
    weekly_rows = [row('g%d' % i, 'Grocery', 16000, D(2029, 3, 4 + 7 * i), series_id='grocery', kind='spending')
                   for i in range(3)]
    rows = (*weekly_rows, row('gx', 'Grocery', 999, D(2029, 3, 5), series_id='grocery', state=RowState.REMOVED))
    txns = [card(1, D(2029, 3, 5), -10000, 'Grocery'), card(2, D(2029, 3, 6), -8000, 'Grocery'),      # week 3/5
            card(3, D(2029, 3, 13), -12000, 'Grocery'), card(4, D(2029, 3, 13), -4000, 'Fuel'),         # week 3/12
            card(5, D(2029, 3, 14), -7000, 'Lawn care'), card(6, D(2029, 3, 14), -1500, 'Mystery'),
            card(7, D(2029, 3, 20), -20000, 'Grocery'),                                                   # this week
            card(8, D(2029, 3, 21), 30000, 'Payment')]
    grocery = series('grocery', 'spending', 16000, schedule('weekly', D(2029, 3, 4)), name='Grocery')
    b = budget(rows=rows, series=(grocery,), settings=Settings(week_starts=0))
    s = weekly(txns, b, cats, today=D(2029, 3, 21))
    assert s.categories == ('Grocery', 'Fuel', 'Other')
    assert [w.week for w in s.weeks] == [D(2029, 2, 26) + __import__('datetime').timedelta(days=7 * i)
                                         for i in (1, 2, 3)]
    assert s.weeks[0].actual == (('Grocery', 18000), ('Fuel', 0), ('Other', 0))
    assert s.weeks[0].budget == (('Grocery', 16000),)
    assert s.weeks[1].actual == (('Grocery', 12000), ('Fuel', 4000), ('Other', 1500))   # lawn care left out
    assert s.full_weeks == 2
    assert dict(s.avg_per_week) == {'Grocery': 15000, 'Fuel': 2000, 'Other': 750}
    assert dict(s.budget_per_week) == {'Grocery': 16000}
    assert dict(s.weeks_over) == {'Grocery': 1}          # 180 > 176 in week 1; 120 is under
    assert (s.since, s.through) == (D(2029, 3, 5), D(2029, 3, 20))
    assert dict(s.by_category)['Lawn care'] == 7000 and s.by_category[0] == ('Grocery', 50000)
    assert s.monthly_spend == 62500                     # under a month of history counts as one month
    assert weekly(txns, b, cats, today=D(2029, 3, 21), weeks=1).weeks[0].week == D(2029, 3, 19)


def test_weekly_empty_and_long_history():
    s = weekly([], budget(), (GROCERY,), today=D(2029, 3, 21))
    assert s.weeks == () and s.monthly_spend == 0 and s.since is None and dict(s.avg_per_week) == {'Grocery': 0,
                                                                                                    'Other': 0}
    txns = [card(1, D(2029, 1, 1), -30440, 'Grocery'), card(2, D(2029, 3, 2), -30440, 'Grocery')]
    # 608.80 over 60 days = 1.971 months
    assert weekly(txns, budget(), (GROCERY,), today=D(2029, 3, 21)).monthly_spend == 30886


def test_weekly_last_two_months_only():
    txns = [card(1, D(2029, 1, 2), -9000, 'Grocery'), card(2, D(2029, 3, 6), -1000, 'Grocery')]
    b = budget(settings=Settings(week_starts=0))
    s = weekly(txns, b, (GROCERY,), today=D(2029, 3, 21), months=2)
    assert s.since == D(2029, 3, 6) and dict(s.by_category) == {'Grocery': 1000}
    assert dict(weekly(txns, b, (GROCERY,), today=D(2029, 3, 21)).by_category) == {'Grocery': 10000}


def test_weekly_plan_comes_from_the_series_not_the_open_rows():
    # #45: past rows have left the forecast (cleared), yet those weeks still have their budget
    grocery = series('grocery', 'spending', 16000, schedule('weekly', D(2029, 2, 4)), name='Grocery',
                     month_amounts=((2, 12000),))
    changed = row('g-mar11', 'Grocery', 20000, D(2029, 3, 11), series_id='grocery', overridden=True,
                  nominal_date=D(2029, 3, 11), state=RowState.CLEARED)
    txns = [card(1, D(2029, 2, 6), -9000, 'Grocery'), card(2, D(2029, 3, 6), -9000, 'Grocery')]
    b = budget(series=(grocery,), rows=(changed,), settings=Settings(week_starts=0))
    s = weekly(txns, b, (GROCERY,), today=D(2029, 3, 21))
    plan = {w.week: dict(w.budget)['Grocery'] for w in s.weeks}
    assert plan[D(2029, 2, 5)] == 12000           # February's seasonal amount (row due Sunday 2/11)
    assert plan[D(2029, 3, 5)] == 20000           # the row changed by hand that week, though cleared
    assert dict(s.budget_per_week)['Grocery'] > 0
    no_series = weekly(txns, budget(settings=Settings(week_starts=0)), (GROCERY,), today=D(2029, 3, 21))
    assert dict(no_series.budget_per_week)['Grocery'] == 0


def test_weekly_plan_reaches_back_before_the_series_was_set_up():
    # a weekly line entered on 3/16 still budgets the card weeks before it (same weekday)
    grocery = series('grocery', 'spending', 16000, schedule('weekly', D(2029, 3, 16)), name='Grocery')
    txns = [card(1, D(2029, 2, 20), -15000, 'Grocery'), card(2, D(2029, 3, 6), -17000, 'Grocery')]
    s = weekly(txns, budget(series=(grocery,), settings=Settings(week_starts=0)), (GROCERY,), today=D(2029, 3, 21))
    assert [dict(w.budget)['Grocery'] for w in s.weeks] == [16000, 16000]
    assert dict(s.budget_per_week)['Grocery'] == 16000
    monthly = series('grocery', 'spending', 60000, schedule('monthly', D(2029, 4, 10), day=10), name='Grocery')
    m = weekly(txns, budget(series=(monthly,), settings=Settings(week_starts=0)), (GROCERY,), today=D(2029, 3, 21))
    assert dict(m.weeks[1].budget)['Grocery'] == 60000          # the week of 3/5 holds the 10th


def test_store_name():
    assert store('CORNER MARKET #1234 SPRINGFIELD') == store('corner  market #5678') == 'CORNER MARKET'
    assert store('SHOP*AB12CD') == 'SHOP'
    assert store('MAIN ST GRILL 0042 CITY') == 'MAIN ST GRILL CITY'
    assert store("JOE'S & SONS, LLC.") == "JOE'S & SONS LLC"
    assert store('12345') == '12345'                     # nothing left: the whole text
    assert store('X' * 60) == 'X' * 40
    assert store(store('A-B STORE #9')) == store('A-B STORE #9') == 'A B STORE'


def test_store_moves_come_before_keywords():
    cats = (CardCategory('Grocery', ('MARKET',)), CardCategory('Treats', (), merchants=('CORNER MARKET',)))
    assert categorise('CORNER MARKET #12', '', cats) == 'Treats'
    assert categorise('OTHER MARKET', '', cats) == 'Grocery'
    assert categorise('CORNER MARKET', '', cats, amount=100) == 'Payment'


def test_recategorise_keeps_hand_moves():
    cats = (CardCategory('Grocery', ('MARKET',)), CardCategory('Fun', ()))
    txns = [CardTxn('a', D(2029, 3, 1), D(2029, 3, 1), -100, 'CORNER MARKET', 'Other'),          # keyword now
            CardTxn('b', D(2029, 3, 1), D(2029, 3, 1), -100, 'CORNER MARKET', 'Fun', by_hand=True),  # kept
            CardTxn('c', D(2029, 3, 1), D(2029, 3, 1), -100, 'CORNER MARKET', 'Gone', by_hand=True),  # deleted
            CardTxn('d', D(2029, 3, 1), D(2029, 3, 1), -100, 'NOWHERE', 'Fun'),                    # bank category
            CardTxn('e', D(2029, 3, 1), D(2029, 3, 1), -100, 'NOWHERE', 'Gone'),
            CardTxn('f', D(2029, 3, 1), D(2029, 3, 1), -100, 'MARKET', 'Other', by_hand=True)]
    assert recategorise(txns, cats) == [('a', 'Grocery'), ('c', 'Grocery'), ('e', 'Other')]


def test_stores():
    cats = (CardCategory('Fun', (), merchants=('BISTRO',)),)
    txns = [card(1, D(2029, 3, 1), -500, 'Fun', 'BISTRO #1'), card(2, D(2029, 3, 9), -700, 'Grocery', 'BISTRO #2'),
            card(3, D(2029, 3, 5), -300, '', 'KIOSK'), card(4, D(2029, 3, 6), 900, 'Payment', 'THANKS'),
            card(5, D(2029, 2, 1), -9999, 'Fun', 'OLD PLACE')]
    got = stores(txns, cats, since=D(2029, 3, 1))
    assert [(s.name, s.category, s.total, s.count, s.last, s.moved) for s in got] == [
        ('BISTRO', 'Grocery', 1200, 2, D(2029, 3, 9), True), ('KIOSK', 'Other', 300, 1, D(2029, 3, 5), False)]
    assert [s.name for s in stores(txns, cats, category='Fun')] == ['OLD PLACE', 'BISTRO']
    assert [s.total for s in stores(txns, cats, category='Fun', until=D(2029, 2, 28))] == [9999]


def test_spending_by_month_and_week():
    grocery = series('grocery', 'spending', 10000, schedule('weekly', D(2029, 1, 1)), name='Grocery')
    b = budget(series=(grocery,), settings=Settings(week_starts=0))
    cats = (GROCERY, CardCategory('Fuel', ('FUEL',)))
    txns = [card(1, D(2029, 3, 5), -4000, 'Grocery'), card(2, D(2029, 3, 20), -6000, 'Fuel'),
            card(3, D(2029, 4, 2), -1000, ''), card(4, D(2029, 4, 3), 5000, 'Payment'),
            card(5, D(2029, 2, 27), -800, 'Grocery')]
    months = spending(txns, b, cats, since=D(2029, 3, 1), until=D(2029, 4, 30))
    assert [p.start for p in months] == [D(2029, 3, 1), D(2029, 4, 1)]
    assert months[0].spent == (('Fuel', 6000), ('Grocery', 4000)) and months[1].spent == (('Other', 1000),)
    assert months[0].budget == (('Grocery', 40000),)      # Mondays 3/5, 12, 19, 26
    assert months[1].budget == (('Grocery', 50000),)      # Mondays 4/2 .. 4/30
    weeks = spending(txns, b, cats, since=D(2029, 3, 5), until=D(2029, 3, 18), by='week', category='Grocery')
    assert [(p.start, p.spent, p.budget) for p in weeks] == [
        (D(2029, 3, 5), (('Grocery', 4000),), (('Grocery', 10000),)), (D(2029, 3, 12), (), (('Grocery', 10000),))]
    assert spending(txns, b, cats, since=D(2029, 3, 5), until=D(2029, 3, 1)) == []
