// Money > Recap: the monthly recap and the year in review (D-090).
import { api, $, el, money, money0, longD, shortD, say, table } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let span = 'month';
let period = null;      // 'YYYY-MM' or 'YYYY'; null = the last full one
let r = null;

const label = (p) => (p.length === 4 ? p : new Date(Number(p.slice(0, 4)), Number(p.slice(5)) - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' }));
const before = () => (span === 'year' ? 'last year' : 'the month before');

function delta(now, was, goodWhenUp) {
  if (!was) return null;
  const d = now - was;
  if (!d) return el('small', { text: 'same as ' + before() });
  const up = d > 0, good = up === goodWhenUp;
  return el('small', { class: good ? 'good' : 'warn', text: (up ? '▲ ' : '▼ ') + money0(Math.abs(d)) + ' vs ' + before() });
}

function kpi(title, value, extra, cls) {
  return el('div', { class: 'kpi' + (cls ? ' ' + cls : '') }, el('span', { class: 'eyebrow', text: title }), el('b', { class: 'num', text: value }), extra);
}

function render() {
  const t = r.totals, b = r.before;
  $('rcPeriod').textContent = label(r.period) + (r.complete ? '' : ' (so far)');
  $('rcPrev').disabled = !r.prev; $('rcNext').disabled = !r.next;
  $('rcKpis').replaceChildren(
    kpi('Money in', money0(t.money_in), delta(t.money_in, b.money_in, true)),
    kpi('Money out', money0(t.money_out), delta(t.money_out, b.money_out, false)),
    kpi(t.net >= 0 ? 'Kept' : 'Spent more than came in', money0(Math.abs(t.net)), delta(t.net, b.net, true), t.net >= 0 ? 'good' : 'warn'),
    kpi('On the card', money0(t.card_spent), delta(t.card_spent, b.card_spent, false)));
  $('rcNote').replaceChildren(!r.has_history ? el('div', { class: 'empty', text: 'No bank postings in ' + label(r.period) + ' yet. Upload the bank export on Money > Uploads.' })
    : el('p', { class: 'muted small', text: 'Pay ' + money(t.pay) + ' of the money in' + (r.last_balance != null ? '; the bank balance after the last posting: ' + money(r.last_balance) : '') + '.' }));

  const row = (x, note) => el('tr', {}, el('td', { text: x.name }), el('td', { class: 'num', text: shortD(x.due) }), el('td', { class: 'r num', text: money(x.amount) }), el('td', { class: 'small', text: note || '' }));
  $('rcBills').replaceChildren(
    el('p', { class: 'num', text: r.bills_paid.length + ' paid, ' + money(r.bills_paid.reduce((s, x) => s + x.amount, 0)) + (r.bills_open.length ? '; ' + r.bills_open.length + ' still open' : '') }),
    r.bills_open.length ? el('div', { class: 'verdict warn' }, el('b', { text: 'Still open: ' }), el('span', { text: r.bills_open.map((x) => x.name + ' (' + shortD(x.due) + ')').join(', ') })) : null,
    r.bills_paid.length ? el('details', {}, el('summary', { text: 'The bills paid' }), table(['Bill', 'Due', { t: 'Amount', r: true }, ''], r.bills_paid.map((x) => row(x)))) : null);

  $('rcCard').replaceChildren(r.card.length ? table(['Category', { t: 'Spent', r: true }, { t: 'Budget', r: true }, ''], r.card.map((c) => el('tr', {},
    el('td', { text: c.category }), el('td', { class: 'r num', text: money(c.spent) }),
    el('td', { class: 'r num', text: c.budget == null ? '—' : money(c.budget) }),
    el('td', {}, c.budget == null ? null : el('span', { class: 'chip ' + (c.spent > c.budget ? 'bad' : 'good'), text: c.spent > c.budget ? 'Over by ' + money0(c.spent - c.budget) : 'Under by ' + money0(c.budget - c.spent) })))))
    : el('div', { class: 'empty', text: 'No card purchases in this period.' }));

  $('rcPayees').replaceChildren(r.top_payees.length ? table(['Payee or rule', { t: 'Amount', r: true }], r.top_payees.map((p) => el('tr', {}, el('td', { text: p.name }), el('td', { class: 'r num', text: money(p.amount) }))))
    : el('div', { class: 'empty', text: 'Nothing went out of the bank in this period.' }));

  const more = [];
  for (const w of r.wins) more.push(el('li', {}, el('span', { class: 'chip good', text: 'Win' }), ' ', w.text, el('span', { class: 'muted small', text: ' · ' + longD(w.on) })));
  for (const n of r.new_recurring) more.push(el('li', {}, el('span', { class: 'chip', text: 'New' }), ' ' + n.payee + ' ' + money(n.amount) + (n.cadence === 'yearly' ? ' a year' : ' a month') + ', from ' + shortD(n.first)));
  for (const c of r.price_changes) more.push(el('li', {}, el('span', { class: 'chip ' + (c.new > c.old ? 'warn' : 'good'), text: c.new > c.old ? 'Price up' : 'Price down' }), ' ' + c.payee + ' ' + money(c.old) + ' → ' + money(c.new) + ' from ' + shortD(c.from)));
  $('rcMore').replaceChildren(more.length ? el('ul', { class: 'plain' }, ...more) : el('div', { class: 'empty', text: 'No new subscriptions, price changes or wins in this period.' }));
}

async function load() {
  const q = '?span=' + span + (period ? '&period=' + period : '');
  ({ recap: r } = await api('GET', '/recap' + q));
  period = r.period;
  render();
}
const go = (p) => { if (p) { period = p; load().catch((e) => say(msg, e.message, 'err')); } };
$('rcPrev').addEventListener('click', () => go(r && r.prev));
$('rcNext').addEventListener('click', () => go(r && r.next));
$('rcSpan').addEventListener('change', () => { span = $('rcSpan').value; period = null; load().catch((e) => say(msg, e.message, 'err')); });
load().catch((e) => say(msg, e.message, 'err'));
loadSummary();
