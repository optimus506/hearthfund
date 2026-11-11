"""The hand-written CSS and JS as the app serves them: rules the pages rely on and that are easy to lose."""
from __future__ import annotations

import re

import pytest


@pytest.fixture
def css(admin) -> str:  # type: ignore[no-untyped-def]
    r = admin.get('/static/css/app.css')
    assert r.status_code == 200
    return str(r.text)


def js(client, name: str) -> str:  # type: ignore[no-untyped-def]
    r = client.get('/static/js/%s.js' % name)
    assert r.status_code == 200
    return str(r.text)


def test_hidden_beats_display_rules(css):
    # .card { display: grid } used to show the empty mapping / preview / seasonal boxes
    assert re.search(r'\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}', css)


def test_bills_phone_layout_and_editor(admin, css):
    start = css.index('@media (max-width: 520px)')
    phone = css[start:css.index('\n}\n', start)]             # the first phone block: the plain rows
    assert '.bill .right .seg { display: flex; width: 100%; }' in phone
    assert 'grid-auto-flow: column' not in phone
    # compact rows (#100) put the amount and the pill side by side, but stack them on a phone
    compact = css[css.index('@media (max-width: 520px)', start + 1):]
    compact = compact[:compact.index('\n}\n')]
    assert '.bill.compact .right { grid-column: 3; padding-top: 0; grid-auto-flow: row;' in compact
    assert '.bill.editrow' in css and 'dashed' in css
    b = js(admin, 'bills')
    assert 'editVal' in b and "'bill editrow'" in b
    # a window resize (the phone keyboard opening) redraws only the chart
    assert re.search(r"addEventListener\('resize'.*renderChart\(\)", b)


def _themes(css: str) -> dict[str, dict[str, str]]:
    blocks = {'light': css[css.index(':root {'):css.index('@media (prefers-color-scheme: dark)')],
              'dark': css[css.index(':root[data-theme="dark"]'):css.index('* { box-sizing')]}
    return {k: dict(re.findall(r'(--[a-z0-9-]+):\s*(#[0-9a-f]{6})', v)) for k, v in blocks.items()}


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_contrast_meets_wcag_aa(css):
    themes = _themes(css)
    # the dark values are written twice (system dark and the chosen dark theme): keep them the same
    media = css[css.index('@media (prefers-color-scheme: dark)'):css.index(':root[data-theme="dark"]')]
    assert dict(re.findall(r'(--[a-z0-9-]+):\s*(#[0-9a-f]{6})', media)) == themes['dark']
    for name, t in themes.items():
        text = [('--ink-3', g) for g in ('--bg', '--surface', '--accent-soft')]
        text += [('--accent', '--accent-soft'), ('--good', '--good-soft'), ('--warn', '--warn-soft'),
                 ('--bad', '--bad-soft'), ('--autopay-ink', '--autopay'), ('--manual-ink', '--manual')]
        for fg, bg in text:
            assert _contrast(t[fg], t[bg]) >= 4.5, (name, fg, bg)
        for bg in ('--bg', '--surface'):
            assert _contrast(t['--field-line'], t[bg]) >= 3, (name, bg)
    assert 'border: 1px solid var(--field-line)' in css


def test_touch_targets_safe_areas_and_live_theme(admin, css):
    touch = css[css.index('@media (pointer: coarse)'):]
    assert 'min-height: 44px' in touch and 'font-size: 16px' in touch
    assert 'env(safe-area-inset-left)' in css and 'env(safe-area-inset-bottom)' in css
    assert 'viewport-fit=cover' in admin.get('/h/%d/bills' % admin.hid).text
    assert 'viewport-fit=cover' in admin.get('/no-such-page').text   # error.html extends auth.html
    assert "matchMedia('(prefers-color-scheme: dark)')" in js(admin, 'api')


def test_form_feedback_and_arming_helpers(admin):
    a = js(admin, 'api')
    for name in ('busy', 'arm', 'statusFor', 'announce'):
        assert re.search(r'export (async )?function %s\(' % name, a), name
    assert "'Saving…'" in a and 'aria-live' in a and '4000' in a
    for mod in ('bills', 'plan', 'debts', 'rules', 'series', 'notes', 'members', 'admin', 'importer', 'reconcile',
                'card', 'exportpage'):
        src = js(admin, mod)
        assert 'busy(' in src, mod
        assert "dataset.armed = '1'" not in src, mod     # every confirm-by-second-click goes through arm()


def test_versioned_static_paths_are_cached_and_install_on_phones(admin, app):
    page = admin.get('/h/%d/bills' % admin.hid).text
    tag = app.state.static_tag
    assert tag.startswith('v') and 'src="/static/%s/js/bills.js"' % tag in page
    assert 'rel="manifest" href="/static/%s/manifest.webmanifest"' % tag in page
    assert 'rel="apple-touch-icon"' in page and 'name="theme-color"' in page
    r = admin.get('/static/%s/js/api.js' % tag)
    assert r.status_code == 200 and r.headers['cache-control'] == 'public, max-age=31536000, immutable'
    # the old path still works for anything that kept it, but browsers check it again each time
    assert admin.get('/static/js/api.js').headers['cache-control'] == 'no-cache'
    m = admin.get('/static/%s/manifest.webmanifest' % tag).json()
    assert m['display'] == 'standalone' and m['start_url'] == '/'
    for icon in m['icons']:
        r = admin.get('/static/%s/%s' % (tag, icon['src']))
        assert r.status_code == 200 and r.content[:4] in (b'\x89PNG', b'<svg')
    assert 'no-store' in admin.get('/h/%d/bills' % admin.hid).headers['cache-control']


def test_static_tag_changes_with_the_files(tmp_path, monkeypatch):
    from hbudget.web import app as appmod
    (tmp_path / 'static').mkdir()
    (tmp_path / 'static' / 'a.js').write_text('1')
    monkeypatch.setattr(appmod, 'HERE', tmp_path)
    one = appmod.static_tag()
    (tmp_path / 'static' / 'a.js').write_text('2')
    assert appmod.static_tag() != one


def test_phone_tables_stack_and_charts_take_the_keyboard(admin, css):
    phone = css[css.index('/* phones: rows as stacked cards'):]
    assert '.tablewrap:not(.tall) td::before { content: attr(data-label)' in phone
    assert "setAttribute('data-label'" in js(admin, 'api')
    chart = js(admin, 'chart')
    assert 'tabindex: 0' in chart and "addEventListener('keydown'" in chart and "'aria-live': 'polite'" in chart
    for key in ('ArrowRight', 'ArrowLeft', 'Home', 'End', 'PageDown'):
        assert key in chart
    assert 'svg.kbchart:focus-visible' in css


def test_debts_table_fits_and_report_chart_folds_on_phones(admin, css):
    d = js(admin, 'debts')
    assert "class: 'promo'" in d and 'td .promo { display: block; white-space: normal;' in css      # #47
    assert 'id="flowBox"' in admin.get('/h/%d/report' % admin.hid).text                             # #48
    assert "$('flowBox').open = false" in js(admin, 'report')
