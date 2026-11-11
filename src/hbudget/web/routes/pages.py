"""Server-rendered pages. Each ships only small JSON page data; the JS modules fetch the rest from the API.

Navigation (docs/ROADMAP.md, Phase 2): five sections, Home · Bills · Plan · Money · More. A section with more
than one page shows its pages as a second row of links; on phones the five sections are a bottom bar.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from hbudget.db import repo
from hbudget.web.deps import ROLE_ORDER, Admin, Conn, Member, member, page_context

router = APIRouter()

# (section key, label, [(page key, label in the section's row of links, one line for the More page)])
SECTIONS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ('home', 'Home', [('home', 'Home', '')]),
    # the bills by payday, the list of every recurring item, the debts they pay down and the check-up (D-078)
    ('bills', 'Bills', [('bills', 'By payday', ''), ('series', 'Bill setup', ''), ('debts', 'Debts', ''),
                        ('checkup', 'Check-up', '')]),
    ('plan', 'Plan', [('plan', 'What if', ''), ('goals', 'Goals', ''), ('calendar', 'Calendar', '')]),
    ('money', 'Money', [('reconcile', 'Uploads', ''), ('report', 'Report', ''), ('card', 'Card', ''),
                        ('subscriptions', 'Subscriptions', ''), ('recap', 'Recap', '')]),
    ('more', 'More', [('more', 'More', ''),
                      ('rules', 'Rules', 'Payee rules: how bank lines are matched to the budget'),
                      ('settings', 'Settings', 'Checkbook balance, alerts, matching, forecast length and colours'),
                      ('notes', 'Notes', 'Shared notes for the household'),
                      ('import', 'Import', 'Load a bill system or a workbook'),
                      ('export', 'Export', 'Download your bills, history and a full backup'),
                      ('members', 'Members', 'Who can see and change this household'),
                      ('access', 'API access', 'Tokens for other programs and AI agents')]),
]
TABS = [(key, label) for _, _, pages in SECTIONS for key, label, _ in pages]
SECTION_OF = {key: s for s, _, pages in SECTIONS for key, _, _ in pages} | {'connections': 'more'}
# More > Bank connection has its own route (routes/plaid.py) and shows only when Plaid is turned on
BANK = ('connections', 'Bank connection', 'Bring in bank and card transactions through Plaid (optional)')
TITLES = dict(TABS) | {'bills': 'Bills by payday', 'checkup': 'Bill check-up', 'series': 'Bill setup',
                       'debts': 'Debts and payoff', 'plan': 'What if',
                       'reconcile': 'Bank and card uploads', 'subscriptions': 'Subscriptions and recurring charges',
                       'calendar': 'Cash-flow calendar', 'recap': 'Monthly recap'}
MIN_ROLE = {'import': 'owner', 'members': 'owner', 'access': 'owner'}
# pages that show the summary strip under the navigation (Home draws its own)
STRIP = {'bills', 'checkup', 'series', 'plan', 'goals', 'debts', 'reconcile', 'report', 'card', 'subscriptions',
         'calendar', 'recap'}


def _allowed(role: str, key: str) -> bool:
    return ROLE_ORDER[role] >= ROLE_ORDER[MIN_ROLE.get(key, 'viewer')]


def nav(role: str, tab: str, plaid: bool = False) -> dict[str, Any]:
    """The two navigation rows for `role` on page `tab`: sections (each opens its first page) and sub-pages.
    `plaid` adds Bank connection to More, after Settings."""
    here = SECTION_OF[tab]
    sections = [{'key': s, 'label': label, 'href': pages[0][0], 'current': s == here} for s, label, pages in SECTIONS]
    subs = next(pages for s, _, pages in SECTIONS if s == here)
    if here == 'more' and plaid:
        at = [k for k, _, _ in subs].index('settings') + 1
        subs = [*subs[:at], BANK, *subs[at:]]
    # the More page lists its pages itself; on the pages under it the row leaves the More page out
    sub = [{'key': k, 'label': label} for k, label, _ in subs if k != 'more' and _allowed(role, k)]
    if len(subs) < 2 or tab == 'more':
        sub = []
    more = [{'key': k, 'label': label, 'about': about}
            for k, label, about in (subs if here == 'more' else SECTIONS[-1][2])[1:] if _allowed(role, k)]
    return {'sections': sections, 'sub': sub, 'section': here, 'more': more}


def _page(request: Request, m: Member, tab: str, **extra: Any) -> Response:
    data = {'hid': m.hid, 'role': m.role, 'csrf': m.auth.csrf, 'household_name': m.household.name, **extra}
    plaid = request.app.state.settings.plaid != 'off'
    ctx = page_context(request, m.conn, m.auth, title=TITLES[tab], household=m.household, role=m.role, tab=tab,
                       nav=nav(m.role, tab, plaid), strip=tab in STRIP, page_data=data)
    t = request.app.state.templates
    return t.TemplateResponse(request, tab + '.html', ctx)  # type: ignore[no-any-return]


def _route(tab: str) -> None:
    dep = Depends(member(MIN_ROLE.get(tab, 'viewer')))

    def view(request: Request, m: Member = dep) -> Response:
        extra: dict[str, Any] = {}
        if tab == 'notes':
            extra = {'notes': repo.load_notes(m.conn, m.hid), 'updated_at': m.household.updated_at}
        if tab == 'access':
            extra = {'base_url': str(request.base_url).rstrip('/')}
        return _page(request, m, tab, **extra)
    view.__name__ = 'page_' + tab
    router.add_api_route('/h/{hid}/' + tab, view, methods=['GET'], response_class=HTMLResponse)


for _tab, _ in TABS:
    _route(_tab)


@router.get('/h/{hid}', response_class=HTMLResponse)
def household_home(request: Request, m: Member = Depends(member('viewer'))) -> Response:  # noqa: B008
    return _page(request, m, 'home')


@router.get('/admin', response_class=HTMLResponse)
def admin_page(request: Request, conn: Conn, auth: Admin) -> Response:
    ctx = page_context(request, conn, auth, title='Admin', page_data={'csrf': auth.csrf})
    t = request.app.state.templates
    return t.TemplateResponse(request, 'admin.html', ctx)  # type: ignore[no-any-return]
