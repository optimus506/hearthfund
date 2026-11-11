// Home: the summary, money left until the next payday and what is due before it, the next 60 days of the
// balance, the paydays after that, and the alerts (the only page that lists them).
import { api, $, el, money, fmtD, page, can, say, busy } from './api.js';
import { renderSummary } from './summary.js';
import { lineChart } from './chart.js';
import { goalBar, goalLine } from './goalbits.js';

let data = null;

function tone(cents, cushion) { return cents < 0 ? 'bad' : (cents < cushion ? 'warn' : 'good'); }

function renderPay() {
  const box = $('pay'), s = data.summary, p = data.periods[0];
  if (!p) {
    box.replaceChildren(el('p', { class: 'empty' }, 'No payday is scheduled. Add your pay on ',
      el('a', { href: '/h/' + page.hid + '/series', text: 'Bills → Bill setup' }), ' to see what is left until each payday.'));
    return;
  }
  const next = data.periods[1];
  const t = tone(p.balance_end, s.cushion);
  const head = el('div', { class: 'payhead' },
    el('div', { class: 'big ' + t },
      el('span', { class: 'eyebrow', text: 'Left before payday' }),
      el('b', { class: 'num', text: money(p.balance_end) }),
      el('small', { text: p.low < p.balance_end ? 'lowest ' + money(p.low) + ' before then' : 'after ' + money(p.out) + ' going out' })),
    el('div', { class: 'next' },
      el('span', { class: 'eyebrow', text: 'Next payday' }),
      el('b', { text: fmtD(p.end, { weekday: 'long', month: 'short', day: 'numeric' }) }),
      el('small', { class: 'muted', text: daysTo(p.end) + (next && next.pay_in ? ' · ' + money(next.pay_in, true) + ' coming in' : '') })));
  // the bills themselves are in the check-in and on Bills (#98): here just how many, with a link
  const owed = p.pay_in ? el('p', { class: 'muted small', text: 'Counts ' + money(p.pay_in, true) + ' of earlier pay or expected money not in the bank yet.' }) : null;
  const bills = p.rows.filter((r) => r.amount > 0);
  const count = el('p', { class: 'small' }, bills.length
    ? [(bills.length === 1 ? '1 bill' : bills.length + ' bills') + ' (' + money(bills.reduce((a, r) => a + r.amount, 0)) + ') due before payday. ',
      el('a', { href: '/h/' + page.hid + '/bills', text: 'See them on Bills' })]
    : ['Nothing else is due before payday.']);
  box.replaceChildren(head, owed || '', moves(p, 'before payday'), count);
}

// card-funded rows in the period, per card: what to move from checking to that card (#31)
function moves(p, when) {
  if (!p.transfers || !p.transfers.length) return '';
  return el('ul', { class: 'list plain' }, ...p.transfers.map((t) => el('li', { class: 'item' },
    el('span', { class: 'what' }, el('b', { text: 'Move to ' + t.card_name }),
      el('span', { class: 'small muted', text: 'for ' + t.rows.map((r) => r.name).join(', ') + ' ' + when })),
    el('b', { class: 'num', text: money(t.total) }))));
}

function daysTo(iso) {
  const [y, m, d] = iso.split('-').map(Number), [ty, tm, td] = data.today.split('-').map(Number);
  const n = Math.round((Date.UTC(y, m - 1, d) - Date.UTC(ty, tm - 1, td)) / 86400000);
  return n <= 0 ? 'today' : n === 1 ? 'tomorrow' : 'in ' + n + ' days';
}

function renderAhead() {
  const box = $('ahead'), s = data.summary, rest = data.periods.slice(1);
  box.closest('section').hidden = !rest.length;
  const most = Math.max(1, ...rest.map((p) => Math.max(p.pay_in, p.out)));
  box.replaceChildren(...rest.map((p) => {
    const t = tone(p.low, s.cushion);
    const bar = (cls, v, label) => el('div', { class: 'meter ' + cls, role: 'img', 'aria-label': label + ' ' + money(v) },
      el('i', { 'data-w': String(Math.round(v / most * 100)) }));
    const li = el('li', { class: 'period ' + t },
      el('div', { class: 'when' }, el('b', { text: fmtD(p.start) }),
        el('small', { class: 'muted', text: p.end ? 'until ' + fmtD(p.end) : 'and after' })),
      el('div', { class: 'flows' },
        el('div', { class: 'flow' }, el('span', { class: 'small', text: 'In' }), bar('in', p.pay_in, 'Money in'), el('b', { class: 'num', text: money(p.pay_in) })),
        el('div', { class: 'flow' }, el('span', { class: 'small', text: 'Out' }), bar('out', p.out, 'Money out'), el('b', { class: 'num', text: money(p.out) }))),
      p.transfers && p.transfers.length ? el('div', { class: 'note small muted', text: p.transfers.map((t) => 'Move ' + money(t.total) + ' to ' + t.card_name).join(' · ') }) : '',
      el('div', { class: 'ends' }, el('span', { class: 'eyebrow', text: p.end ? 'Before next pay' : 'Then' }),
        el('b', { class: 'num', text: money(p.balance_end) }),
        p.low < Math.min(p.balance_end, s.cushion) ? el('small', { class: t, text: 'low ' + money(p.low) }) : ''));
    return li;
  }));
  // widths are set from script (the CSP blocks inline style attributes in markup, not CSSOM writes)
  for (const i of box.querySelectorAll('.meter i')) i.style.width = i.dataset.w + '%';
}

function renderSpark() {
  lineChart($('spark'), [{ name: 'Balance', points: data.spark }], { h: 140, cushion: data.summary.cushion,
    label: 'Balance over the next 60 days' });
}

// alerts are a check-in step on Home (#98); the ones about scheduling are step 2 already
const LOOK_SKIP = ['unscheduled'];
const lookCount = () => (data ? data.summary.alerts.filter((a) => !LOOK_SKIP.includes(a.code)).length : 0);
function renderAlerts() {
  $('noalerts').hidden = lookCount() > 0;
}

// weekly check-in (D-080): the bank file, bills due soon with no status, what others changed since your last
// check-in, then "Done". Gentle on purpose: no streaks and nothing about weeks that were skipped.
const when = (iso) => fmtD(iso.slice(0, 10), { weekday: 'short', month: 'short', day: 'numeric' });
async function renderCheckin() {
  let c;
  try { c = await api('GET', '/checkin'); } catch (e) { $('checkSteps').replaceChildren(el('li', { class: 'msg err', text: e.message })); return; }
  const h = '/h/' + page.hid + '/';
  $('checkLast').textContent = c.last ? 'Last check-in: ' + c.last.by + ', ' + when(c.last.at) : '';
  const step = (n, title, done, ...body) => el('li', { class: 'step' + (done ? ' done' : '') },
    el('span', { class: 'n', 'aria-hidden': 'true', text: done ? '✓' : String(n) }), el('div', {}, el('b', { text: title }), ...body));
  // 1. the bank file
  const w = c.bank.waiting[0];
  const fresh = c.plaid_new || 0;     // postings the daily bank sync stored that no upload has taken yet
  const plaidBad = (c.plaid_problems || []).map((p) => el('p', { class: 'msg err small' }, (p.institution || 'Bank') + ': ' + p.message + ' ',
    el('a', { href: h + 'connections', text: 'Bank connection' })));
  const bank = c.plaid && fresh && can('editor')
    ? step(1, 'Bring in the new bank transactions', false, ...plaidBad,
      el('p', { class: 'small', text: fresh + ' new transaction' + (fresh === 1 ? '' : 's') + ' from the daily bank sync. They come in as one upload to review.' }),
      el('button', { class: 'btn primary', type: 'button', text: 'Bring them in', onclick: (ev) => syncBank(ev.currentTarget) }))
    : w
    ? step(1, 'Confirm the bank matches', false, ...plaidBad, el('p', { class: 'small', text: 'A bank file is waiting' + (w.date_to ? ' (through ' + fmtD(w.date_to) + ')' : '') + '.' }),
      el('a', { class: 'btn', href: h + 'reconcile?checkin=1', text: 'Review the matches' }))
    : step(1, c.plaid ? 'Bring in this week\'s bank transactions' : 'Bring in this week\'s bank file', !!c.bank.reconciled_through && c.done_this_week,
      ...plaidBad, el('p', { class: 'small muted', text: c.bank.reconciled_through ? 'Reconciled through ' + fmtD(c.bank.reconciled_through) + '.' : 'Nothing reconciled yet.' }),
      el('span', { class: 'row-actions' },
        c.plaid && can('editor') ? el('button', { class: 'btn primary', type: 'button', text: 'Sync with the bank', onclick: (ev) => syncBank(ev.currentTarget) }) : null,
        el('a', { class: 'btn', href: h + 'reconcile?checkin=1', text: 'Upload a bank file' })));
  // 2. bills due soon that are neither scheduled nor on autopay
  const mark = async (r, status, btn) => {
    try { await busy(btn, () => api('POST', '/rows/' + encodeURIComponent(r.id) + '/status', { status }), '…'); await renderCheckin(); }
    catch (e) { say(btn, e.message, 'err'); }
  };
  const due = step(2, 'Schedule what is due in the next ' + c.due_days + ' days', c.due.length === 0,
    c.due.length ? el('ul', { class: 'list plain' }, ...c.due.map((r) => el('li', { class: 'item' },
      el('span', { class: 'what' }, el('b', { text: r.name }), el('span', { class: 'small ' + (r.late ? 'late' : 'muted'), text: (r.late ? 'Late · was due ' : 'Due ') + fmtD(r.due) + ' · ' + money(r.amount) })),
      can('contributor') ? el('span', { class: 'row-actions' },
        el('button', { class: 'btn', type: 'button', text: 'Scheduled', onclick: (ev) => mark(r, 'manual', ev.currentTarget) }),
        el('button', { class: 'btn', type: 'button', text: 'Autopay', onclick: (ev) => mark(r, 'autopay', ev.currentTarget) })) : null)))
      : el('p', { class: 'small muted', text: 'Every bill due in the next ' + c.due_days + ' days is scheduled or on autopay.' }),
    el('a', { class: 'small', href: h + 'bills?checkin=2', text: 'All bills by payday' }));
  // 3. alerts and notes, each with its own link or action (the box is filled by summary.js)
  const look = step(3, 'Things to look at', lookCount() === 0, $('alerts'), $('noalerts'));
  // 4. what others changed
  const seen = step(4, c.your_last ? 'Changes since your last check-in' : 'Recent changes by others', true,
    c.changes.length ? el('ul', { class: 'list plain small' }, ...c.changes.map((x) => el('li', { class: 'item' },
      el('span', { text: x.by + ' ' + x.what + (x.item ? ' ' + x.item : '') }), el('span', { class: 'muted', text: when(x.at) }))))
      : el('p', { class: 'small muted', text: 'Nobody else changed anything.' }),
    c.more_changes ? el('p', { class: 'small muted', text: 'Showing the latest 20.' }) : null);
  $('checkSteps').replaceChildren(bank, due, look, seen);
  const btn = $('checkDone');
  btn.hidden = !can('contributor');
  btn.textContent = c.done_this_week ? 'Check in again' : 'Done for this week';
}
// a bank linked through Plaid (docs/PLAID.md): the sync becomes a waiting bank upload, reviewed like a file
async function syncBank(btn) {
  try {
    const r = await busy(btn, () => api('POST', '/plaid/sync', {}), 'Syncing…');
    const bad = r.problems.map((p) => (p.institution || 'Bank') + ': ' + p.message + (p.relink ? ' (sign in again on the Bank connection page)' : ''));
    if (r.bank || r.cards.length) { await renderCheckin(); if (!r.bank) say($('checkSteps'), 'New card purchases are on the Card page.', 'ok'); return; }
    const text = bad.length ? bad.join(' ') : r.not_ready ? 'Plaid is still fetching your history; try again in a few minutes.'
      : 'Nothing new from the bank' + (r.pending ? ' (' + r.pending + ' pending until they post)' : '') + '.'
        + (r.cards.length ? ' New card purchases are on the Card page.' : '');
    say(btn, text, bad.length ? 'err' : 'ok');
  } catch (e) { say(btn, e.message, 'err'); }
}
$('checkDone').addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  try { await busy(btn, () => api('POST', '/checkin', {}), 'Saving…'); say(btn, 'Checked in. Thanks!', 'ok'); await renderCheckin(); }
  catch (e) { say(btn, e.message, 'err'); }
});

// wins (D-082): new ones get a small celebration once, then join the list; can be hidden (Settings turns them back on)
const WIN_ICON = { debt_paid: '🎉', goal_reached: '🏁', above_cushion: '🛟', under_budget: '👏' };
async function renderWins() {
  let w;
  try { w = await api('GET', '/milestones'); } catch (e) { return; }
  $('winsCard').hidden = !w.celebrate || (!w.new.length && !w.history.length);
  if ($('winsCard').hidden) return;
  $('winsOff').hidden = !can('editor');
  $('winsNew').replaceChildren(...(w.new.length ? [el('div', { class: 'celebrate' },
    ...w.new.map((x) => el('p', {}, el('span', { class: 'burst', 'aria-hidden': 'true', text: WIN_ICON[x.kind] || '🎉' }), ' ', el('b', { text: x.text }))),
    can('contributor') ? el('button', { class: 'btn primary', type: 'button', text: 'Nice!', onclick: async (ev) => {
      try { await busy(ev.currentTarget, () => api('POST', '/milestones/seen', { keys: w.new.map((x) => x.key) }), '…'); await renderWins(); }
      catch (e) { say(ev.currentTarget, e.message, 'err'); }
    } }) : null)] : []));
  $('winsPast').replaceChildren(...w.history.slice(0, 5).map((x) => el('li', { class: 'item' },
    el('span', {}, (WIN_ICON[x.kind] || '') + ' ' + x.text), el('span', { class: 'muted', text: fmtD(x.on) }))));
}
$('winsOff').addEventListener('click', async (ev) => {
  try { await busy(ev.currentTarget, () => api('PUT', '/celebrate', { on: false }), '…'); $('winsCard').hidden = true; say($('msg'), 'Wins are hidden. Turn them back on under More → Settings.', 'ok'); }
  catch (e) { say(ev.currentTarget, e.message, 'err'); }
});

async function renderGoals() {
  let g;
  try { g = await api('GET', '/goals'); } catch (e) { return; }      // goals are extra: Home works without them
  $('goalsCard').hidden = !g.goals.length;
  $('homeGoals').replaceChildren(...g.goals.slice(0, 4).map((x) => el('div', { class: 'goalrow' },
    el('div', { class: 'thead' }, el('b', { text: x.name }), el('span', { class: 'muted small num', text: x.pct + '%' })), goalBar(x),
    el('p', { class: 'small muted', text: goalLine(x) }))));
}

async function load() {
  renderGoals(); renderWins();
  try { data = await api('GET', '/home'); }
  catch (e) { $('pay').replaceChildren(el('p', { class: 'msg err', text: e.message })); renderCheckin(); return; }
  renderSummary(data.summary);
  renderPay(); renderSpark(); renderAhead(); renderAlerts();
  await renderCheckin();
  if (location.hash === '#alerts') $('alerts').scrollIntoView({ block: 'center' });
}
// an alert set aside (or a rule made) reloads the summary: keep step 3 in step with it
document.addEventListener('hb:summary', (ev) => { if (data) { data.summary = ev.detail; renderAlerts(); renderCheckin(); } });

let w = 0;
window.addEventListener('resize', () => { const nw = $('spark').clientWidth; if (data && nw !== w) { w = nw; renderSpark(); } });
window.addEventListener('themechange', () => { if (data) renderSpark(); });
load();
