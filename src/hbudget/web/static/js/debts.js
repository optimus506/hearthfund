// Debts tab: balances (typed in by the user) and the payoff plan.
import { api, $, el, money, money0, longD, shortD, isoD, toCents, centsInput, can, say, busy, submitter, arm, table, page } from './api.js';
import { loadSummary } from './summary.js';
import { lineChart } from './chart.js';

const msg = $('msg');
let accounts = [];
let reminder = { days: 0, stale: [], snoozed_until: null };
let bills = [];              // recurring bills, for "Paid by this bill"
let quick = new URLSearchParams(location.search).get('update') === '1';
const F = ['dId', 'dName', 'dKind', 'dBal', 'dApr', 'dPay', 'dMin', 'dLimit', 'dPromo', 'dAfter', 'dDeferred', 'dMortgage', 'dInclude'];

const pct = (bp) => (bp == null ? '' : (bp / 100).toFixed(2) + '%');
const toBp = (t) => { const s = String(t || '').trim(); if (!s) return null; if (!/^\d{1,2}(\.\d{1,2})?$/.test(s)) return NaN; const [w, f = ''] = s.split('.'); return Number(w) * 100 + Number((f + '00').slice(0, 2)); };

function renderTable() {
  const box = $('debtTable');
  const ds = accounts.filter((a) => !['checking', 'savings', 'investment'].includes(a.kind));
  if (!ds.length) { box.replaceChildren(el('div', { class: 'empty', text: 'No debts yet. Add the first one below. Do not enter card or account numbers.' })); return; }
  const rows = ds.map((d) => el('tr', {},
    el('td', {}, d.name + (d.is_mortgage ? ' (mortgage)' : '') + (d.include_in_payoff ? '' : ' (not in plan)'),
      d.paid_by && d.paid_by.length ? el('small', { class: 'sub', text: 'paid by ' + d.paid_by.join(', ') }) : null,
      // from the linked bank, where the owner turned Plaid Liabilities on (D-106)
      d.bank && d.bank.terms && d.bank.terms.next_due ? el('small', { class: 'sub', text: 'next payment due ' + longD(d.bank.terms.next_due) }) : null),
    // between hand updates: the estimate from reconciled payments of the linked bill (#97); the typed balance stays
    el('td', { class: 'r num' }, money(d.balance), d.estimate ? el('small', { class: 'promo', title: money(d.estimate.paid) + ' paid in ' +
      d.estimate.payments + (d.estimate.payments === 1 ? ' payment' : ' payments') + ', about ' + money(d.estimate.interest) + ' interest, since ' + shortD(d.estimate.since),
    text: 'est. ' + money(d.estimate.balance) + ' · confirmed ' + shortD(d.estimate.since) }) : null),
    // a promo goes on its own line, which may wrap, so the APR column stays narrow (#47)
    el('td', { class: 'r num' }, pct(d.apr_bp), d.promo_ends ? el('small', { class: 'promo', text: 'until ' + shortD(d.promo_ends) + '/' + d.promo_ends.slice(2, 4) +
      (d.apr_after_bp != null ? ', then ' + pct(d.apr_after_bp) : '') + (d.deferred_interest ? ' (deferred)' : '') }) : null),
    el('td', { class: 'r num', text: money(d.payment) }),
    el('td', { class: 'r num', text: d.minimum ? money(d.minimum) : '' }),
    el('td', { class: 'r num', text: d.utilization_bp == null ? '' : pct(d.utilization_bp) }),
    el('td', { class: 'r num', text: money(d.monthly_interest) }),
    el('td', { class: 'num' }, d.as_of ? shortD(d.as_of) + '/' + d.as_of.slice(2, 4) : '', el('small', { class: 'sub' }, keptBy(d))),
    el('td', { class: 'actions' }, can('editor') ? el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: 'Edit', onclick: () => edit(d) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Delete', onclick: (ev) => {
        if (!arm(ev.currentTarget, 'Delete for good?')) return;
        api('DELETE', '/accounts/' + encodeURIComponent(d.id)).then(load, (e) => say(msg, e.message, 'err')); } })) : null)));
  const total = (label, list) => {
    const tot = (f) => list.reduce((a, d) => a + (d[f] || 0), 0);
    return el('tr', { class: 'total' }, el('td', { text: label }), el('td', { class: 'r num', text: money(tot('balance')) }), el('td', {}),
      el('td', { class: 'r num', text: money(tot('payment')) }), el('td', {}), el('td', {}), el('td', { class: 'r num', text: money(tot('monthly_interest')) }), el('td', {}), el('td', {}));
  };
  rows.push(total('Total', ds));
  if (ds.some((d) => d.is_mortgage)) rows.push(total('Total without mortgage', ds.filter((d) => !d.is_mortgage)));
  box.replaceChildren(table(['Debt', { t: 'Balance', r: true }, { t: 'APR', r: true }, { t: 'Monthly', r: true }, { t: 'Minimum', r: true },
    { t: 'Used', r: true }, { t: 'Interest/mo', r: true }, 'As of', ''], rows));
}

// who keeps the balance: a linked bank account, or you (marked when the reminder says it is time, D-105)
function keptBy(d) {
  if (d.source === 'bank') return el('span', { class: d.bank.problem ? 'chip warn' : 'chip good', title: d.bank.problem || 'Kept by the linked account at every sync', text: (d.bank.institution || 'bank') + (d.bank.problem ? ' · needs attention' : '') });
  const old = reminder.stale.includes(d.id);
  return el('span', { class: old ? 'chip warn' : 'chip', title: old ? 'Typed in; older than the reminder allows' : 'Typed in', text: old ? 'update' : 'you' });
}
const every = (days) => { const m = Math.max(1, Math.round(days / 30.5)); return m === 1 ? 'a month' : m + ' months'; };

// the reminder (D-105): typed balances older than the household's interval, with the quick form and "later"
function renderRemind() {
  const box = $('debtRemind'), ds = accounts.filter((a) => reminder.stale.includes(a.id));
  $('dQuickOpen').hidden = !can('editor') || !typed().length;
  $('dQuickOpen').setAttribute('aria-expanded', String(quick));
  if (!ds.length || quick) { box.replaceChildren(); return; }
  if (reminder.snoozed_until) {
    box.replaceChildren(el('p', { class: 'muted small', text: (ds.length === 1 ? '1 balance is' : ds.length + ' balances are') + ' older than ' + every(reminder.days) + '. The reminder is put off until ' + longD(reminder.snoozed_until) + '.' }));
    return;
  }
  box.replaceChildren(el('div', { class: 'verdict warn' },
    el('b', { text: (ds.length === 1 ? '1 balance has' : ds.length + ' balances have') + ' not been updated for more than ' + every(reminder.days) + ': ' + ds.map((d) => d.name).join(', ') + '.' }),
    el('span', { text: 'Look at the statements and enter what is owed now.' }),
    el('span', { class: 'row-actions' },
      can('editor') ? el('button', { class: 'btn primary', type: 'button', text: 'Enter balances', onclick: () => { quick = true; render(); } }) : null,
      can('contributor') ? el('button', { class: 'btn', type: 'button', text: 'Remind me in 2 weeks', onclick: async (ev) => {
        try { await busy(ev.currentTarget, () => api('PUT', '/accounts/reminder/later', { days: 14 })); await load(); } catch (e) { say(msg, e.message, 'err'); }
      } }) : null)));
}

// every typed debt on one screen: the balance now and its date; rows left empty are not changed
const typed = () => accounts.filter((a) => !['checking', 'savings', 'investment'].includes(a.kind) && a.source !== 'bank');
function renderQuick() {
  const box = $('debtQuick'), ds = typed();
  box.hidden = !quick || !can('editor') || !ds.length;
  if (box.hidden) { box.replaceChildren(); return; }
  const today = isoD(new Date());
  ds.sort((a, b) => (reminder.stale.includes(b.id) - reminder.stale.includes(a.id)) || (a.as_of || '').localeCompare(b.as_of || ''));
  const form = el('form', { class: 'card inner', 'aria-label': 'Update balances' },
    el('h3', { text: 'Update balances' }),
    el('p', { class: 'muted small', text: 'Enter what each statement says is owed now. Leave a row empty to keep its balance.' }),
    table(['Debt', { t: 'Balance now', r: true }, 'New balance', 'As of'], ds.map((d) => el('tr', { 'data-id': d.id },
      el('td', {}, d.name, reminder.stale.includes(d.id) ? el('span', { class: 'chip warn label', text: 'time to update' }) : null),
      el('td', { class: 'r num' }, money(d.balance), el('small', { class: 'sub', text: d.as_of ? 'as of ' + shortD(d.as_of) + '/' + d.as_of.slice(2, 4) : 'no date' })),
      el('td', {}, el('input', { type: 'text', inputmode: 'decimal', 'aria-label': 'New balance of ' + d.name, 'data-f': 'bal' })),
      el('td', {}, el('input', { type: 'date', 'aria-label': 'As of, for ' + d.name, 'data-f': 'day', value: today, max: today }))))),
    el('div', { class: 'row-actions' }, el('button', { class: 'btn primary', type: 'submit', text: 'Save balances' }),
      el('button', { class: 'btn', type: 'button', text: 'Close', onclick: () => { quick = false; render(); } })));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const items = [];
    for (const tr of form.querySelectorAll('tr[data-id]')) {
      const text = tr.querySelector('[data-f=bal]').value.trim();
      if (!text) continue;
      const balance = toCents(text);
      if (Number.isNaN(balance) || balance < 0) { say(form, 'A balance could not be read. Enter it like 1250.00.', 'err'); tr.querySelector('[data-f=bal]').focus(); return; }
      items.push({ id: tr.dataset.id, balance, as_of: tr.querySelector('[data-f=day]').value || null });
    }
    if (!items.length) { say(form, 'Enter at least one balance.', 'err'); return; }
    try {
      await busy(submitter(e), () => api('POST', '/accounts/balances', { items }));
      quick = false; await load(); say(msg, items.length + (items.length === 1 ? ' balance' : ' balances') + ' updated.', 'ok');
    } catch (err) { say(form, err.message, 'err'); }
  });
  box.replaceChildren(form);
}
$('dQuickOpen').addEventListener('click', () => { quick = !quick; render(); if (quick) setTimeout(() => { const i = $('debtQuick').querySelector('input'); if (i) i.focus(); }, 0); });
function render() { renderRemind(); renderQuick(); renderTable(); }

function edit(d) {
  $('dPaidByField').hidden = false;
  $('dPaidBy').replaceChildren(el('option', { value: '', text: 'None' }), ...bills.map((s) => el('option', { value: s.id, text: s.name })));
  $('dPaidBy').value = (bills.find((s) => s.debt === d.id) || { id: '' }).id; $('dPaidBy').dataset.was = $('dPaidBy').value;
  $('dId').value = d.id; $('dName').value = d.name; $('dKind').value = d.kind; $('dBal').value = centsInput(d.balance);
  $('dApr').value = d.apr_bp == null ? '' : (d.apr_bp / 100).toFixed(2); $('dPay').value = centsInput(d.payment);
  $('dMin').value = d.minimum ? centsInput(d.minimum) : ''; $('dLimit').value = d.limit == null ? '' : centsInput(d.limit);
  $('dPromo').value = d.promo_ends || ''; $('dAfter').value = d.apr_after_bp == null ? '' : (d.apr_after_bp / 100).toFixed(2);
  $('dDeferred').checked = d.deferred_interest; $('dMortgage').checked = d.is_mortgage; $('dInclude').checked = d.include_in_payoff;
  $('dSave').textContent = 'Save changes'; $('dCancel').hidden = false; $('dName').focus();
}
function reset() {
  for (const f of F) { const n = $(f); if (n.type === 'checkbox') n.checked = f === 'dInclude'; else if (n.tagName !== 'SELECT') n.value = ''; }
  $('dKind').value = 'credit_card'; $('dSave').textContent = 'Add debt'; $('dCancel').hidden = true; $('dPaidByField').hidden = true;
}
$('dCancel').addEventListener('click', reset);
$('debtForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = { name: $('dName').value.trim(), kind: $('dKind').value, balance: toCents($('dBal').value), apr_bp: toBp($('dApr').value) ?? 0,
    payment: toCents($('dPay').value || '0'), minimum: toCents($('dMin').value || '0'), limit: $('dLimit').value ? toCents($('dLimit').value) : null,
    promo_ends: $('dPromo').value || null, apr_after_bp: $('dPromo').value ? toBp($('dAfter').value) : null,
    deferred_interest: $('dDeferred').checked, is_mortgage: $('dMortgage').checked, include_in_payoff: $('dInclude').checked };
  if (!body.name || [body.balance, body.payment, body.minimum, body.apr_bp].some((v) => Number.isNaN(v)) || body.limit !== null && Number.isNaN(body.limit)) {
    say(form, 'Enter a name, the balance, the APR (e.g. 18.5) and the monthly payment.', 'err'); return;
  }
  if (/\d{6,}/.test(body.name)) { say(form, 'Leave card and account numbers out of the name.', 'err'); return; }
  try {
    const id = $('dId').value;
    const paidBy = $('dPaidBy').value, relink = id && paidBy !== $('dPaidBy').dataset.was;
    await busy(submitter(e), async () => {
      await (id ? api('PUT', '/accounts/' + encodeURIComponent(id), body) : api('POST', '/accounts', body));
      if (relink) await api('PUT', '/accounts/' + encodeURIComponent(id) + '/paid-by', { series_id: paidBy || null });
    });
    reset(); say(form, 'Saved.', 'ok'); await load();
  } catch (err) { say(form, err.message, 'err'); }
});
if (!can('editor')) $('debtForm').hidden = true;

// burn-down: the total owed month by month for this plan, the other method and payments only, with each debt's
// payoff on the line (hover or arrow keys), and what was actually owed from the balances typed in over time
const iso = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
function months(p, r, cap) {
  const s = new Date(p.start + 'T00:00'), paid = {};
  for (const d of r.debts) if (d.paid_month) (paid[d.paid_month] = paid[d.paid_month] || []).push(d.name);
  return r.balances.slice(0, cap + 1).map((b, i) => [iso(new Date(s.getFullYear(), s.getMonth() + i, 1)), b,
    paid[i] ? paid[i].join(', ') + ' paid off' : null, paid[i] ? paid[i].length : 0]);
}
function actual(p) {
  const ids = new Set(p.result.debts.map((d) => d.id));
  const hs = accounts.filter((a) => ids.has(a.id) && a.history && a.history.length);
  if (!hs.some((a) => a.history.length > 1)) return null;
  const dates = [...new Set(hs.flatMap((a) => a.history.map(([d]) => d)))].sort();
  return dates.map((d) => [d, hs.reduce((sum, a) => { let v = 0; for (const [hd, hv] of a.history) if (hd <= d) v = hv; return sum + v; }, 0)]);
}
let burnSeries = null;
function burnDown(p) {
  const done = [p.result, p.other, p.baseline].filter((r) => r.done).map((r) => r.months);
  const cap = Math.min(360, Math.max(p.result.months, ...(done.length ? done : [p.result.months])));
  const series = [{ name: p.method === 'avalanche' ? 'Highest rate first' : 'Smallest first', colour: '--accent', points: months(p, p.result, cap) },
    { name: p.method === 'avalanche' ? 'Smallest first' : 'Highest rate first', colour: '--whatif', dash: '6 3', points: months(p, p.other, cap) },
    { name: 'Payments only', colour: '--ink-3', dash: '2 4', points: months(p, p.baseline, cap) }];
  const real = actual(p);
  if (real) series.push({ name: 'Actual', colour: '--good', points: real });
  burnSeries = series;
  const host = el('div', { class: 'chart', id: 'burn' });
  const paid = p.result.debts.filter((d) => d.paid_on).sort((a, b) => a.paid_month - b.paid_month);
  const box = el('div', {}, el('h3', { text: 'Burn-down' }), host,
    el('p', { class: 'muted small', text: 'Total owed each month. ' + (paid.length ? 'Paid off along the way: ' + paid.map((d) => d.name + ' ' +
      new Date(d.paid_on + 'T00:00').toLocaleDateString('en-US', { month: 'short', year: 'numeric' })).join(', ') + '.' : '') +
      (real ? ' Actual: the balances entered over time.' : ' Update the balances from each statement to see the actual line.') }),
    el('details', {}, el('summary', { text: 'Show as a table' }), table(['Month', ...series.slice(0, 3).map((s) => ({ t: s.name, r: true })), 'Paid off'],
      series[0].points.filter((_, i) => i % 3 === 0 || series[0].points[i][2]).map(([d, b, row]) => {
        const k = series[0].points.findIndex((x) => x[0] === d);
        return el('tr', {}, el('td', { text: new Date(d + 'T00:00').toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) }),
          el('td', { class: 'r num', text: money0(b) }), el('td', { class: 'r num', text: series[1].points[k] ? money0(series[1].points[k][1]) : '' }),
          el('td', { class: 'r num', text: series[2].points[k] ? money0(series[2].points[k][1]) : '' }), el('td', { text: row || '' }));
      }))));
  // drawn once the box is in the page (the chart needs its width)
  setTimeout(() => lineChart(host, series, { label: 'Debt burn-down: total owed by month', h: 240, lows: false }), 0);
  return box;
}
window.addEventListener('themechange', () => { if (burnSeries && $('burn')) lineChart($('burn'), burnSeries, { label: 'Debt burn-down: total owed by month', h: 240, lows: false }); });

let planSeq = 0;
async function renderPlan() {
  const box = $('payoff'), seq = ++planSeq;
  const p = await api('POST', '/payoff', { method: $('pMethod').value, extra: toCents($('pExtra').value || '0') || 0,
    once: toCents($('pOnce').value || '0') || 0, once_month: $('pOnceM').value || null, include_mortgage: $('pMortgage').value === 'in' });
  if (seq !== planSeq) return;     // a newer request (typing) is on its way
  if (!p.result.debts.length) { box.replaceChildren(el('div', { class: 'empty', text: 'The plan appears once balances are entered.' })); return; }
  const mo = (d) => (d ? new Date(d + 'T00:00').toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) : 'not paid off');
  const kids = [];
  const paidOn = Object.fromEntries(p.result.debts.map((d) => [d.id, d.paid_on]));
  for (const v of p.promos) {
    if (v.paid_before) kids.push(el('div', { class: 'verdict good' }, el('b', { text: v.name + ': paid off in ' + mo(paidOn[v.debt_id]) + ', before the promo rate ends ' + longD(v.promo_ends) + '.' })));
    else kids.push(el('div', { class: 'verdict warn' }, el('b', { text: v.name + ': ' + money0(v.left_at_promo) + ' left when the promo rate ends ' + longD(v.promo_ends) + '.' }),
      el('span', { text: 'Clearing it in time takes about ' + money0(v.monthly_needed) + ' a month (' + v.payments_left + ' payments).' +
        (v.deferred_interest ? ' Deferred interest: if any balance is left, the interest for the whole promo period is added (not counted here).' : '') +
        (v.apr_after_missing ? ' Add the APR after the promo so the plan charges the right interest.' : '') +
        (paidOn[v.debt_id] ? ' With this plan it is paid off in ' + mo(paidOn[v.debt_id]) + '.' : '') })));
  }
  if (p.not_covering.length) kids.push(el('div', { class: 'verdict bad' }, el('b', { text: 'Some payments do not cover the interest.' }),
    el('span', { text: p.not_covering.join(', ') + ': the payment is at or below the monthly interest.' })));
  const kpi = (l, v, s) => el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: l }), el('b', { class: 'num', text: v }), el('small', { text: s }));
  kids.push(el('div', { class: 'kpis' },
    kpi('Debt-free', p.debt_free_on ? mo(p.debt_free_on) : 'Not within 50 years', p.result.done ? p.result.months + ' months' : 'raise the payments'),
    kpi('Interest paid', money0(p.result.total_interest), 'from now until paid off'),
    kpi('Extra money saves', money0(p.interest_saved), p.months_sooner != null ? p.months_sooner + ' months sooner' : 'compared with payments only'),
    kpi(p.method === 'avalanche' ? 'Smallest first instead' : 'Highest rate first instead', (p.other_difference >= 0 ? '+' : '') + money0(p.other_difference),
      'interest · ' + (p.other_debt_free_on ? 'debt-free ' + mo(p.other_debt_free_on) : 'not debt-free within 50 years'))));
  kids.push(burnDown(p));
  const order = p.result.debts.slice().sort((a, b) => (a.paid_month || 9999) - (b.paid_month || 9999));
  kids.push(table(['Order', 'Debt', 'Paid off', { t: 'Interest', r: true }], order.map((d, i) => el('tr', {},
    el('td', { class: 'num', text: String(i + 1) }), el('td', { text: d.name }), el('td', { text: mo(d.paid_on) }), el('td', { class: 'r num', text: money0(d.interest) })))));
  if (p.excluded.length) kids.push(el('p', { class: 'muted small', text: p.excluded.join(', ') + ': tracked above but left out of this plan.' }));
  kids.push(el('p', { class: 'muted small', text: 'Estimate only: interest is figured monthly from the APR and payments stay the same each month.' }));
  box.replaceChildren(...kids);
}

// payoff settings are remembered in this browser, per household; the plan recalculates while typing
const PLAN = ['pMethod', 'pMortgage', 'pExtra', 'pOnce', 'pOnceM'];
const planKey = 'hb.payoff.' + page.hid;
$('pOnceM').value = new Date().getFullYear() + '-12';     // a one-time extra: December by default
try {
  const saved = JSON.parse(localStorage.getItem(planKey) || '{}');
  for (const id of PLAN) if (typeof saved[id] === 'string') $(id).value = saved[id];
} catch (e) { /* storage off or bad JSON: keep the defaults */ }
let pt;
const replan = () => {
  try { localStorage.setItem(planKey, JSON.stringify(Object.fromEntries(PLAN.map((id) => [id, $(id).value])))); } catch (e) { /* storage off */ }
  clearTimeout(pt);
  pt = setTimeout(() => renderPlan().catch((e) => say(msg, e.message, 'err')), 300);
};
for (const id of PLAN) { $(id).addEventListener('input', replan); $(id).addEventListener('change', replan); }
async function load() {
  const [ac, sr] = await Promise.all([api('GET', '/accounts'), api('GET', '/series')]);
  accounts = ac.accounts; reminder = ac.reminder; bills = sr.series.filter((s) => s.kind === 'bill');
  render(); await renderPlan(); loadSummary();
}
reset();
load().catch((e) => say(msg, e.message, 'err'));
