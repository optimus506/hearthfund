"""Browser smoke test in headless Chromium: setup, bill-system import, every tab, a full reconciliation, phone width.

Skipped unless playwright is installed and HBUDGET_E2E=1:

    pip install playwright && python -m playwright install chromium-headless-shell
    HBUDGET_E2E=1 python -m pytest tests/e2e

Asserts no page errors, no console errors or warnings, no failed requests, and the hand-checked numbers of the
synthetic fixture (tests/core/test_synthetic_regression.py).
"""
from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

if os.environ.get('HBUDGET_E2E') != '1':
    pytest.skip('browser test: set HBUDGET_E2E=1 (and install playwright)', allow_module_level=True)
playwright = pytest.importorskip('playwright.sync_api')

from hbudget.web.routes.pages import STRIP  # noqa: E402
from hbudget.web.routes.pages import TABS as PAGE_TABS  # noqa: E402

FIX = Path(__file__).parents[1] / 'fixtures' / 'synthetic'
PASSWORD = 'correct horse battery'
TABS = [key for key, _ in PAGE_TABS]          # every page in the navigation, so none is missed


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope='module')
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    import uvicorn

    from hbudget.web.app import create_app
    from hbudget.web.config import Settings
    data = tmp_path_factory.mktemp('data')
    port = free_port()
    app = create_app(Settings(data_dir=data, port=port, allowed_hosts=('127.0.0.1',), cookie_secure='false'))
    app.state.clock = lambda: date(2029, 3, 12)      # the day the synthetic bank export ends
    srv = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield 'http://127.0.0.1:%d' % port, data
    srv.should_exit = True
    app.state.stop.set()
    t.join(timeout=5)


def test_browser_smoke(server: tuple[str, Path]) -> None:
    base, data = server
    problems: list[str] = []

    def watch(page: Any, label: str) -> None:
        page.on('pageerror', lambda e: problems.append('[%s] page error: %s' % (label, e)))
        page.on('console', lambda m: problems.append('[%s] console.%s: %s' % (label, m.type, m.text))
                if m.type in ('error', 'warning') else None)
        page.on('requestfailed', lambda r: problems.append('[%s] failed request %s' % (label, r.url)))
        page.on('response', lambda r: problems.append('[%s] HTTP %d %s %s' % (label, r.status, r.request.method,
                                                                               r.url)) if r.status >= 400 else None)

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        watch(page, 'desktop')

        # first-run setup with the token from the data folder
        page.goto(base + '/')
        assert page.url.endswith('/setup')
        page.fill('#token', (data / 'setup-token').read_text().strip())
        page.fill('#username', 'admin')
        page.fill('#password', PASSWORD)
        page.fill('#household', 'Test household')
        page.click('button[type=submit]')
        page.wait_for_load_state('networkidle')
        assert '/h/' in page.url
        h = page.url.split('/bills')[0]

        # import the synthetic household (replace, not a dry run)
        page.goto(h + '/import')
        page.set_input_files('#bsFile', str(FIX / 'household.yaml'))
        page.select_option('#bsMode', 'replace')
        page.uncheck('#bsDry')
        page.click('#bsForm button[type=submit]')
        page.wait_for_selector('#bsResult >> text=Imported.')

        # every tab renders with its one-line summary (the full strip is on Home only, #99)
        for tab in TABS:
            page.goto(h + '/' + tab)
            page.wait_for_load_state('networkidle')
            assert page.inner_text('h1')
            page.wait_for_function('() => !document.getElementById("main").innerText.includes("Loading…")',
                                   timeout=10000)
            if tab in STRIP:
                page.wait_for_selector('#strip.slim a b')
        page.goto(h + '/bills')
        page.wait_for_selector('#groups .bill .name')
        assert 'Mortgage' in page.inner_text('#groups')
        page.goto(h + '/report')
        page.wait_for_selector('#flowKpis .kpi')
        page.click('text=Budget, next 12 months')
        page.wait_for_selector('#flow svg')

        # reconcile: upload, dry run, accept both proposals, apply
        page.goto(h + '/reconcile')
        page.set_input_files('#upFile', str(FIX / 'bank.csv'))
        page.click('#upForm button[type=submit]')
        page.wait_for_selector('#preview h2:has-text("Dry run")')
        # the dry run is brought into view with focus on its heading
        page.wait_for_function("document.activeElement === document.querySelector('#preview h2')")
        # ?upload=<id> (the link after a bank sync) opens that upload's dry run; an id that is gone says so
        page.goto(h + '/reconcile?upload=nope')
        page.wait_for_selector('#msg >> text=not on this list any more')
        assert page.is_hidden('#preview')
        up_id = page.evaluate("fetch('/api' + location.pathname.replace('/reconcile', '/uploads'))"
                              ".then((r) => r.json()).then((d) => d.uploads.find((u) => u.status === 'waiting').id)")
        page.goto(h + '/reconcile?upload=' + up_id)
        page.wait_for_selector('#preview h2:has-text("Dry run")')
        page.wait_for_function("document.activeElement === document.querySelector('#preview h2')")
        assert 'upload=' not in page.url
        assert page.evaluate("document.querySelector('#preview').getBoundingClientRect().top < innerHeight")
        text = page.inner_text('#preview')
        assert 'null' not in text and 'P1' in text and 'P2' in text
        page.select_option('#acc-P1', 'clear')
        # "Choose the rows it paid…": the proposal's own rows start ticked, with the total against the payment;
        # more than the payment holds Apply until a row comes off or a larger row keeps the rest
        page.select_option('#acc-P2', 'pick')
        page.wait_for_selector('.picker >> text=Chosen')
        assert page.locator('.picker input:checked').count() == 2 and page.is_enabled('#recApply')
        page.locator('.picker input:not(:checked)').first.check()     # 40.00 more than the 320.00 payment
        page.wait_for_selector('.picker >> text=more than the')
        assert page.is_disabled('#recApply') and 'total more than the payment' in page.inner_text('#recApplyNote')
        if os.environ.get('HBUDGET_E2E_SHOTS'):
            page.screenshot(path=os.environ['HBUDGET_E2E_SHOTS'] + '/picker.png', full_page=True)
        page.select_option('.item:has(#acc-P2) .picker select', index=1)     # that row keeps the rest
        page.wait_for_selector('.picker >> text=stays open with $40.00 left')
        assert page.is_enabled('#recApply')
        page.select_option('#acc-P2', 'clear')
        assert page.locator('.item:has(#acc-P2) .picker').count() == 0 and page.is_enabled('#recApply')
        # an unmatched line: "This paid…" a 40.00 row, which keeps the 15.00 the 25.00 payment did not cover
        shop = page.locator('#preview .item:has-text("MYSTERY SHOP")')
        shop.locator('summary').click()
        shop.locator('label:has-text("Lawn care") input').check()
        shop.locator('select').select_option(index=1)
        shop.locator('text=Lawn care stays open with $15.00 left').wait_for()
        assert page.locator('.item:has(#acc-P2) select').first.is_enabled()
        page.click('#preview button:has-text("Apply")')
        page.wait_for_selector('#preview h2:has-text("Applied")')
        page.wait_for_function("document.activeElement === document.querySelector('#preview h2')")
        report = page.inner_text('#preview')
        # 620.00, not 595.00: the 25.00 the shop line paid is no longer counted a second time on Lawn care
        assert '$620.00 on 03/14/2029' in report
        assert 'chosen, part paid' in report and 'MYSTERY SHOP | unmatched' not in report

        # a card export, then the Card tab: move a store to another category (D-083)
        card_csv = data / 'card.csv'
        card_csv.write_text('Date,Description,Amount\n03/02/2029,CORNER MARKET #12,-40.00\n'
                            '03/04/2029,KIOSK,-5.00\n')
        page.goto(h + '/reconcile')
        page.select_option('#upKind', 'card')
        page.set_input_files('#upFile', str(card_csv))
        page.click('#upForm button[type=submit]')
        page.wait_for_selector('#uploads >> text=card.csv')
        # #88: a waiting upload offers Hide, and Delete with postings says what goes before doing it
        extra = data / 'extra.csv'
        extra.write_text('Date,Description,Amount,Balance\n03/13/2029,ODD SHOP,-1.00,1.00\n')
        page.select_option('#upKind', 'bank')
        page.set_input_files('#upFile', str(extra))
        page.click('#upForm button[type=submit]')
        page.wait_for_selector('#uploads .item:has-text("extra.csv") button:has-text("Delete with postings")')
        page.click('#uploads .item:has-text("extra.csv") button:has-text("Delete with postings")')
        page.wait_for_selector('#uploads .confirm >> text=This removes 1 posting')
        page.click('#uploads .confirm button:has-text("Delete file and 1 postings")')
        page.wait_for_selector('#uploads .item:has-text("extra.csv")', state='detached')
        # its automatic dry run answers 400 (no starting point: it only adds history); that one is expected
        expected = [x for x in problems if 'reconcile/preview' in x or 'status of 400' in x]
        assert len(expected) == 2
        problems[:] = [x for x in problems if x not in expected]
        page.goto(h + '/card')
        page.wait_for_selector('#stores td >> text=KIOSK')
        page.fill('#catName', 'Treats')
        page.click('#catAdd button[type=submit]')
        page.click('#catSave')
        page.wait_for_selector('select[aria-label="Move KIOSK to"] option[value="Treats"]', state='attached')
        page.select_option('select[aria-label="Move KIOSK to"]', 'Treats')
        page.wait_for_selector('#stores >> text=moved')
        page.click('#stores tr:has-text("KIOSK") button:has-text("1 purchase")')
        page.wait_for_selector('#stores td[colspan] table td >> text=KIOSK')
        page.goto(h + '/bills')
        page.wait_for_selector('#strip a b')
        strip = page.inner_text('#strip')
        assert '$635.00' in strip and '$620.00' in strip

        # Bills: mark autopay, Change + Undo, Remove + Undo; the summary strip follows every edit. Rows are compact
        # (#100): a status pill opens the status choice and Change under the row.
        def open_row(sel: str) -> None:
            page.locator(sel).first.locator('.pill').click()
            page.wait_for_selector('#groups .bill.more')

        page.goto(h + '/bills')
        page.wait_for_selector('#groups .bill .pill')
        marked = page.locator('#groups .bill[data-status="autopay"]').count()
        open_row('#groups .bill.compact:has(.pill.s-none), #groups .bill.compact:has(.pill.s-manual)')
        page.locator('#groups .bill.more button[data-v="autopay"]').click()
        page.wait_for_function('n => document.querySelectorAll("#groups .bill[data-status=autopay]").length > n',
                               arg=marked)
        # the mark also refreshes the summary (one "not scheduled" alert fewer); let that land before comparing
        page.wait_for_load_state('networkidle')
        assert page.locator('#groups .bill.more').count() == 0        # marking closes the row again
        strip0 = page.inner_text('#strip')
        open_row('#groups .bill.compact:has(.pill.s-autopay)')
        page.locator('#groups .bill.more button:has-text("Change")').click()
        page.locator('#groups input[id^="chg-"]').fill('999.00')
        page.locator('#groups .editrow button.primary:has-text("Save")').click()
        page.wait_for_selector('#groups .was')
        page.wait_for_function('s => document.getElementById("strip").innerText !== s', arg=strip0)
        assert page.locator('#groups .was').count() == 1
        open_row('#groups .bill.compact:has(.was)')
        page.locator('#groups .bill.more button:has-text("Undo")').click()
        page.wait_for_function('s => document.getElementById("strip").innerText === s', arg=strip0)
        open_row('#groups .bill.compact:has(.pill.s-autopay)')
        page.locator('#groups .bill.more button:has-text("Change")').click()
        page.locator('#groups button:has-text("Won’t be paid")').click()
        page.wait_for_selector('#groups .bill.removed')
        page.wait_for_function('s => document.getElementById("strip").innerText !== s', arg=strip0)
        open_row('#groups .bill.compact.removed')
        page.locator('#groups .bill.more button:has-text("Undo")').click()
        page.wait_for_function('s => document.getElementById("strip").innerText === s', arg=strip0)
        assert page.locator('#groups .bill.removed').count() == 0

        # Already paid: the row leaves the forecast, stays listed crossed out, and Undo brings it back
        open_row('#groups .bill.compact:has(.pill.s-autopay)')
        page.locator('#groups .bill.more button:has-text("Change")').click()
        page.locator('#groups button:has-text("Already paid")').click()
        page.wait_for_selector('#groups .bill.removed >> text=already paid')
        page.wait_for_function('s => document.getElementById("strip").innerText !== s', arg=strip0)
        open_row('#groups .bill.compact.removed')
        page.locator('#groups .bill.more button:has-text("Undo")').click()
        page.wait_for_function('s => document.getElementById("strip").innerText === s', arg=strip0)

        # the page search in the tab row: filters the payday list in place and lists every matching open row
        assert page.is_visible('#pageSearch')
        total = page.locator('#groups .bill:visible').count()
        page.fill('#pageSearch', 'mortgage')
        page.wait_for_selector('#findCard:not([hidden]) #findResults td >> text=Mortgage')
        shown = page.locator('#groups .bill:visible')
        assert 0 < shown.count() < total and all('Mortgage' in t for t in shown.all_inner_texts())
        page.press('#pageSearch', 'Escape')
        page.wait_for_selector('#findCard', state='hidden')
        assert page.locator('#groups .bill:visible').count() == total
        page.goto(h + '/rules')
        page.wait_for_selector('#pageSearch', state='visible')
        page.goto(h + '/settings')
        page.wait_for_load_state('networkidle')
        assert not page.is_visible('#pageSearch')     # nothing to search there

        # Plan: a what-if gives a verdict; an expected item is saved
        page.goto(h + '/plan')
        page.fill('#wiWhat', 'Car repair')
        page.fill('#wiAmt', '100000.00')
        page.fill('#wiDate', '2029-03-20')
        page.click('#wiForm button[type=submit]')
        page.wait_for_selector('#wiResult .verdict')
        assert page.inner_text('#wiResult .verdict b').startswith('No.')
        assert page.locator('#wiChart svg').count() == 1
        page.fill('#exWhat', 'Refund')
        page.fill('#exAmt', '40.00')
        page.fill('#exDate', '2029-03-25')
        page.select_option('#exDir', 'in')
        page.click('#exForm button[type=submit]')
        page.wait_for_selector('#exList >> text=Refund')

        # Bill setup: "Every 3 months" is Monthly with every N filled in (D-099); the saved item says so and edits back
        page.goto(h + '/series')
        page.wait_for_selector('#seriesList table')
        page.fill('#sName', 'Trash pickup')
        page.fill('#sAmount', '75.00')
        page.select_option('#sCadence', 'monthly:3')
        assert page.input_value('#sInterval') == '3' and page.is_visible('#sCadenceHint')
        page.fill('#sStart', '2031-12-01')
        page.click('#sSave')
        page.wait_for_selector('#seriesList tr:has-text("Trash pickup") >> text=Every 3 months')
        page.click('#seriesList tr:has-text("Trash pickup") >> text=Edit')
        assert page.input_value('#sCadence') == 'monthly:3'
        page.fill('#sInterval', '4')                         # no choice of its own: plain Monthly, every 4
        assert page.input_value('#sCadence') == 'monthly' and page.is_visible('#sCadenceHint')
        page.click('#sSave')
        page.wait_for_selector('#seriesList tr:has-text("Trash pickup") >> text=Every 4 months')
        page.click('#seriesList tr:has-text("Trash pickup") >> text=Delete')
        page.click('#seriesList tr:has-text("Trash pickup") >> text=Delete future rows too?')
        page.wait_for_selector('#seriesList tr:has-text("Trash pickup")', state='detached')

        # Goals: the list loads and a first goal is saved and shown
        page.goto(h + '/goals')
        page.wait_for_selector('#goalList >> text=No goals yet')
        page.select_option('#gKind', 'savings')
        page.fill('#gName', 'Car fund')
        page.fill('#gTarget', '2000.00')
        page.click('#goalForm button[type=submit]')
        page.wait_for_selector('#goalList .goalrow >> text=Car fund')

        # starter rules (D-094): a preview, then one button adds them to the end of the list
        page.goto(h + '/rules')
        page.click('#packPreview')
        page.wait_for_selector('#packResult >> text=Would add')
        page.click('#packApply')
        page.wait_for_selector('#packResult .msg.ok >> text=Added')
        page.wait_for_selector('#ruleList td >> text=Groceries')
        page.click('#packPreview')
        page.wait_for_selector('#packResult >> text=Nothing to add')

        # theme toggle switches between light and dark
        first = page.evaluate('getComputedStyle(document.body).backgroundColor')
        page.click('.menu > summary')                   # the toggle is in the account menu
        page.click('#theme')
        assert page.evaluate('document.documentElement.dataset.theme') in ('light', 'dark')
        assert page.evaluate('getComputedStyle(document.body).backgroundColor') != first
        page.click('#theme')
        assert page.evaluate('getComputedStyle(document.body).backgroundColor') == first

        # phone width: no horizontal page scroll on any tab
        phone = browser.new_page(viewport={'width': 360, 'height': 780})
        phone.context.add_cookies(page.context.cookies())
        watch(phone, 'phone')
        for tab in TABS:
            phone.goto(h + '/' + tab)
            phone.wait_for_load_state('networkidle')
            wide = phone.evaluate('document.documentElement.scrollWidth - window.innerWidth')
            assert wide <= 1, (tab, wide)

        # two-factor sign-in (D-095): set up from the key the page shows, then sign in with password and code
        from hbudget.core import totp
        other = browser.new_page(viewport={'width': 360, 'height': 780})   # a second sign-in of the same user
        watch(other, 'second session')
        other.goto(base + '/login')
        other.fill('#username', 'admin')
        other.fill('#password', PASSWORD)
        other.click('button[type=submit]')
        other.wait_for_url('**/h/**')
        page.goto(base + '/account/2fa')
        page.fill('#startPass', PASSWORD)
        page.click('button:has-text("Set up two-factor sign-in")')
        page.wait_for_selector('.qr svg')
        secret = page.inner_text('#totpKey').replace(' ', '')
        page.fill('#code', totp.code_at(secret, time.time()))
        page.click('button:has-text("Turn on two-factor sign-in")')
        page.wait_for_selector('#recoveryCodes')
        assert len(page.inner_text('#recoveryCodes').split()) == 10
        # a new member is a new way in: the page asks for a code and tries again (the first answer is a 403)
        page.goto(h + '/members')
        page.fill('#mUser', 'pat')
        page.fill('#mPass', PASSWORD)
        page.once('dialog', lambda d: d.accept(totp.code_at(secret, time.time() + 30)))
        page.click('#addForm button[type=submit]')
        page.wait_for_selector('#memberList >> text=pat')
        asked = [x for x in problems if ('/members' in x and '403' in x) or 'status of 403' in x]
        assert asked, problems
        for x in asked:
            problems.remove(x)
        import sqlite3
        db = sqlite3.connect(data / 'hbudget.db')            # each code works once: forget the step just used
        db.execute('UPDATE users SET totp_last_step = NULL')
        db.commit()
        db.close()
        other.goto(h + '/bills')                         # the other session was signed out
        assert other.url.endswith('/login')
        other.fill('#username', 'admin')
        other.fill('#password', PASSWORD)
        other.click('button[type=submit]')
        other.wait_for_selector('#code')
        assert other.url.endswith('/login/code')
        wide = other.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        assert wide <= 1, ('login/code', wide)
        other.fill('#code', totp.code_at(secret, time.time()))
        other.click('button[type=submit]')
        other.wait_for_load_state('networkidle')
        assert '/h/' in other.url
        other.goto(base + '/account/2fa')
        wide = other.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        assert wide <= 1, ('account/2fa', wide)
        browser.close()
    assert not problems, '\n'.join(problems)
