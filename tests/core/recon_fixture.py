"""A small synthetic household and bank export for the reconciliation tests (round amounts, 2029)."""
from factories import D, budget, chain, row, rule
from hbudget.core.models import Direction, ExpectedItem

RULES = (
    rule('payroll', 'pay', ('EMPLOYER A PAYROLL',), direction=Direction.IN, targets=('pay-a',)),
    rule('mortgage', 'bill', ('HOME LOAN SERVICER',), targets=('mortgage',)),
    rule('electric', 'bill', ('ELECTRIC CO',), targets=('electric',)),
    rule('cards', 'ask', ('CARD SERVICES',), targets=('card-a', 'card-b'), note='two cards from one bank'),
    rule('rewards', 'card', ('TRANSFER TO CARD 1234',), card='rewards'),
    rule('nocard', 'card', ('TRANSFER TO OTHER CARD',)),
    rule('shop', 'track', ('ONLINE MARKETPLACE',), direction=Direction.OUT),
    rule('ignore', 'ignore', ('INTERNAL MOVE',)),
)


def rows():
    return (
        row('m', 'Mortgage', 180000, D(2029, 3, 3), series_id='mortgage'),
        row('e1', 'Electric', 15000, D(2029, 3, 6), series_id='electric'),
        row('e2', 'Electric', 15000, D(2029, 3, 20), series_id='electric', seq=1),
        row('g1', 'Grocery', 16000, D(2029, 3, 3), seq=1, card='rewards', kind='spending'),
        row('f1', 'Fuel', 5000, D(2029, 3, 3), seq=2, card='rewards', kind='spending'),
        row('g2', 'Grocery', 16000, D(2029, 3, 10), card='rewards', kind='spending'),
        row('g3', 'Grocery', 16000, D(2029, 3, 17), card='rewards', kind='spending'),
        row('pay', 'Pay A', -240000, D(2029, 3, 15), series_id='pay-a'),
        row('ca', 'Card A', 30000, D(2029, 3, 18), series_id='card-a', seq=1),
        row('cb', 'Card B', 70000, D(2029, 3, 7), series_id='card-b'),
    )


def household(**kw):
    kw.setdefault('rules', RULES)
    kw.setdefault('expected', (ExpectedItem('refund', 'Store refund', 4000, 'in', D(2029, 3, 12)),))
    return budget(start=300000, as_of=D(2029, 2, 28), rows=rows(), **kw)


def export():
    """Oldest first in the list; chain() returns newest first with running balances."""
    return chain(303000, basis=False, items=[
        ('t0', D(2029, 2, 27), -1000, 'OLD COFFEE'),               # before the basis
        ('t1', D(2029, 2, 28), -2000, 'OLD SHOP'),                 # basis: balance 300000 after it
        ('t2', D(2029, 3, 2), -180000, 'HOME LOAN SERVICER PMT'),  # auto: mortgage
        ('t3', D(2029, 3, 5), -19000, 'ELECTRIC CO ACH'),          # e1 is 150.00: 190 is out of tolerance
        ('t4', D(2029, 3, 8), -31000, 'CARD SERVICES PAYMENT'),    # ask: Card A or Card B
        ('t5', D(2029, 3, 12), -35000, 'TRANSFER TO CARD 1234'),   # card bundle
        ('t6', D(2029, 3, 12), 4000, 'STORE REFUND'),              # no rule; expected item matches
        ('t7', D(2029, 3, 14), 230000, 'EMPLOYER A PAYROLL'),      # auto: pay, short by 100.00
        ('t8', D(2029, 3, 14), -3000, 'ONLINE MARKETPLACE'),       # tracked
        ('t9', D(2029, 3, 15), 2500, 'ELECTRIC CO REFUND'),        # refund: wrong direction
        ('t10', D(2029, 3, 16), -5000, 'TRANSFER TO OTHER CARD'),  # card rule without a card
        ('t11', D(2029, 3, 16), -100, 'INTERNAL MOVE'),            # ignored -> tracked
    ])
