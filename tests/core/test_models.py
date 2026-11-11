from datetime import date

from hbudget.core.models import (
    Budget,
    Cadence,
    Horizon,
    Kind,
    LedgerRow,
    Schedule,
    Series,
    Settings,
    Status,
    row_key,
    row_status,
    slug,
)


def row(**kw):
    base = dict(id='r1', series_id='electric', name='Electric', kind=Kind.BILL, amount=15000,
                due_date=date(2029, 3, 6), order_date=date(2029, 3, 6), seq=0)
    base.update(kw)
    return LedgerRow(**base)


def test_slug_and_row_key():
    assert slug('Car loan (2nd)') == 'car-loan-2nd'
    assert slug('  Café & More ') == 'cafe-more'
    assert row_key(row()) == 'electric_2029-03-06'


def test_row_status():
    s = Series(id='electric', name='Electric', kind=Kind.BILL, amount=15000,
               schedule=Schedule(Cadence.MONTHLY, date(2029, 1, 6)), status=Status.AUTOPAY)
    by_id = {s.id: s}
    assert row_status(row(), by_id) is Status.AUTOPAY
    assert row_status(row(status_override=Status.MANUAL), by_id) is Status.MANUAL
    assert row_status(row(series_id=None), by_id) is Status.NONE
    assert row_status(row(kind=Kind.INCOME, status_override=Status.MANUAL), by_id) is Status.NONE
    assert row_status(row(kind=Kind.SPENDING), by_id) is Status.NONE


def test_defaults():
    st = Settings()
    assert st.reconcile.days_before == 12 and st.reconcile.days_after == 21
    assert st.reconcile.tolerance_bp == 1000 and st.reconcile.tolerance_abs == 2500
    assert st.alerts.cushion == 30000
    assert dict(st.status_colours) == {Status.AUTOPAY: '#b6d7a8', Status.MANUAL: '#ec9ba4'}
    b = Budget(start_balance=0, as_of=date(2029, 3, 1), horizon=Horizon(), series=(), rows=())
    assert b.horizon.mode == 'rolling' and b.horizon.months == 15
    assert b.series_by_id() == {}
    assert row().state == 'open' and row().nominal_date is None
