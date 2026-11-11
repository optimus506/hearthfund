// Subscriptions tab: recurring charges found in the bank and card history (#34, D-084).
import { api, $, announce, busy, can, el, money, money0, shortD, say, table } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
const EVERY = { monthly: 'Monthly', yearly: 'Yearly' };

function flags(i) {
  const out = [];
  if (i.status === 'new') out.push(el('span', { class: 'chip', text: 'New' }));
  if (i.status === 'stopped') out.push(el('span', { class: 'chip bad', text: 'Stopped' }));
  if (i.status === 'irregular') out.push(el('span', { class: 'chip warn', text: 'Irregular: last ' + money(i.latest.amount) + ' on ' + shortD(i.latest.date) }));
  if (i.group === 'bill') out.push(el('span', { class: 'chip', text: 'Bill' }));
  if (i.generic) out.push(el('span', { class: 'chip', title: 'A payment service or marketplace: two amounts may be two different services', text: 'Via a payment service' }));
  for (const p of i.price_changes) {
    out.push(el('span', { class: 'chip ' + (p.new > p.old ? 'warn' : 'good'), text: (p.new > p.old ? 'Price up' : 'Price down') + ' ' + money(p.old) + ' → ' + money(p.new) + ' on ' + shortD(p.from) }));
  }
  return out;
}

function budgeted(i) {
  if (i.budget_lines.length) return i.budget_lines.map((b) => b.name).join(', ');
  if (i.rule) return 'Rule: ' + i.rule.name;
  if (i.category) return 'Card: ' + i.category;
  return i.generic ? 'Not in the budget (an allowance rule can budget several of these as one line)' : 'Not in the budget';
}

// "Not a subscription" hides a payee (out of the totals); "Show again" undoes it
function toggle(i, hidden) {
  if (!can('editor')) return null;
  const b = el('button', { class: 'btn small', type: 'button', text: hidden ? 'Not a subscription' : 'Show again',
    'aria-label': (hidden ? 'Mark ' : 'Show ') + i.payee + (hidden ? ' as not a subscription' : ' again') });
  b.addEventListener('click', () => busy(b, async () => {
    try {
      await api('PUT', '/recurring/hidden', { source: i.source, payee: i.payee, hidden });
      announce(hidden ? i.payee + ' hidden' : i.payee + ' shown again');
      await load();
    } catch (e) { say(msg, e.message, 'err'); }
  }));
  return b;
}

function rows(items, stopped, hidden = false) {
  return items.map((i) => el('tr', {},
    el('td', {}, el('b', { text: i.payee }), el('div', { class: 'muted small', text: (i.source === 'card' ? 'Card' : 'Bank') + ' · ' + i.count + ' charges since ' + shortD(i.first) })),
    el('td', { class: 'r num', text: money(i.amount) }),
    el('td', { text: EVERY[i.cadence] }),
    el('td', { class: 'num', text: hidden ? shortD(i.last) : stopped ? 'Expected ' + shortD(i.next) + ', last ' + shortD(i.last) : shortD(i.next) }),
    el('td', { class: 'r num', text: money(i.monthly) }),
    el('td', { text: budgeted(i) }),
    el('td', {}, ...flags(i)),
    el('td', {}, toggle(i, !hidden))));
}

async function load() {
  const { recurring: r } = await api('GET', '/recurring');
  const live = r.items.filter((i) => i.status !== 'stopped'), stopped = r.items.filter((i) => i.status === 'stopped');
  $('subTotal').textContent = live.length ? money0(r.monthly_total) + ' a month' : '';
  const unbudgeted = live.filter((i) => !i.budget_lines.length && !i.rule).length;
  $('subKpis').replaceChildren(...(live.length ? [
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Per month' }), el('b', { class: 'num', text: money0(r.monthly_total) }), el('small', { text: 'Bills and loans ' + money0(r.bills_monthly) + ' · subscriptions ' + money0(r.subscriptions_monthly) + '; yearly ones spread over 12 months' })),
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Per year' }), el('b', { class: 'num', text: money0(r.yearly_total) })),
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Price rises' }), el('b', { class: 'num', text: String(r.price_rises) }), el('small', { text: r.price_rises ? 'cost more than when they started' : 'none so far' })),
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Not in the budget' }), el('b', { class: 'num', text: String(unbudgeted) }), el('small', { text: 'bank charges no payee rule matches' })),
  ] : []));
  const head = ['Payee', { t: 'Amount', r: true }, 'Every', 'Next', { t: 'Per month', r: true }, 'Budgeted as', 'Flags', ''];
  $('subLive').replaceChildren(live.length ? table(head, rows(live, false))
    : el('div', { class: 'empty', text: 'No recurring charges found yet. Upload a few months of bank or card exports on Money > Uploads.' }));
  $('subStopped').replaceChildren(stopped.length ? table(['Payee', { t: 'Amount', r: true }, 'Every', 'When', { t: 'Per month', r: true }, 'Budgeted as', 'Flags', ''], rows(stopped, true))
    : el('div', { class: 'empty', text: 'Nothing stopped lately.' }));
  $('hHidden').hidden = !r.hidden.length;
  $('subHidden').replaceChildren(r.hidden.length ? table(['Payee', { t: 'Amount', r: true }, 'Every', 'Last', { t: 'Per month', r: true }, 'Budgeted as', 'Flags', ''],
    rows(r.hidden, false, true)) : '');
}
load().catch((e) => say(msg, e.message, 'err'));
loadSummary();
