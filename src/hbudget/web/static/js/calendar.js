// Plan > Calendar: the forecast laid out as a month, with the balance at the end of each day.
import { api, $, el, money, money0, longD, say } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
const SHOWN = 3;      // entries listed in a day before "+N more"
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let month = null;     // 'YYYY-MM'; null = this month

const monthName = (ym) => { const [y, m] = ym.split('-').map(Number); return new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' }); };

function entry(e) {
  const income = e.amount < 0;
  return el('li', { class: 'cal-entry' + (income ? ' in' : '') + (e.late ? ' late' : '') + (e.kind === 'expected' || e.kind === 'virtual' ? ' virtual' : ''),
    title: e.label + ' ' + money(Math.abs(e.amount)) + (e.due ? ' (due ' + longD(e.due) + ')' : '') },
  el('span', { class: 'cal-label', text: e.label }), el('span', { class: 'num', text: (income ? '+' : '−') + money0(Math.abs(e.amount)) }));
}

function dayCell(d, cushion) {
  const day = Number(d.date.slice(8));
  const cls = ['cal-day'];
  if (d.past) cls.push('past');
  if (d.date === cal.today) cls.push('today');
  if (d.below_zero) cls.push('below-zero'); else if (d.below_cushion) cls.push('below-cushion');
  if (!d.entries.length) cls.push('quiet');
  const shown = d.entries.slice(0, SHOWN), rest = d.entries.length - shown.length;
  const flag = d.below_zero ? 'below zero' : d.below_cushion ? 'under the cushion of ' + money0(cushion) : '';
  return el('td', { class: cls.join(' ') },
    el('div', { class: 'cal-head' }, el('span', { class: 'cal-num', text: String(day) }),
      el('span', { class: 'cal-wd', text: new Date(d.date + 'T00:00').toLocaleDateString('en-US', { weekday: 'short' }) })),
    d.entries.length ? el('ul', { class: 'cal-entries' }, ...shown.map(entry),
      rest ? el('li', { class: 'muted small', text: '+' + rest + ' more' }) : null) : null,
    el('div', { class: 'cal-bal num', title: 'Balance at the end of the day' + (flag ? ': ' + flag : '') },
      money0(d.balance) + (flag ? ' ⚠' : '')),
    flag ? el('span', { class: 'sr-only', text: flag }) : null);
}

let cal = null;
function render() {
  $('calMonth').textContent = monthName(cal.month);
  $('calPrev').disabled = !cal.prev; $('calNext').disabled = !cal.next;
  $('calKpis').replaceChildren(
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Coming in' }), el('b', { class: 'num', text: money0(cal.money_in) })),
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Going out' }), el('b', { class: 'num', text: money0(cal.money_out) })),
    el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: 'Balance' }), el('b', { class: 'num', text: money0(cal.opening) + ' → ' + money0(cal.closing) }), el('small', { text: 'start → end of the month' })),
    cal.low ? el('div', { class: 'kpi' + (cal.low.balance < cal.cushion ? ' warn' : '') }, el('span', { class: 'eyebrow', text: 'Lowest' }), el('b', { class: 'num', text: money0(cal.low.balance) }), el('small', { text: 'on ' + longD(cal.low.date) })) : null);
  // weeks start on Monday (0) or Sunday (6), as in Settings
  const startDow = cal.week_starts === 6 ? 0 : 1;      // JS getDay(): 0 Sunday, 1 Monday
  const names = cal.week_starts === 6 ? ['Sun', ...WEEKDAYS.slice(0, 6)] : WEEKDAYS;
  const firstDow = new Date(cal.days[0].date + 'T00:00').getDay();
  const lead = (firstDow - startDow + 7) % 7;
  const cells = [...Array(lead).fill(null), ...cal.days];
  while (cells.length % 7) cells.push(null);
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(el('tr', {}, ...cells.slice(i, i + 7).map((d) => (d ? dayCell(d, cal.cushion) : el('td', { class: 'cal-day blank', 'aria-hidden': 'true' })))));
  }
  $('cal').replaceChildren(el('table', { class: 'cal' },
    el('caption', { class: 'sr-only', text: 'Cash-flow calendar for ' + monthName(cal.month) }),
    el('thead', {}, el('tr', {}, ...names.map((n) => el('th', { scope: 'col', text: n })))),
    el('tbody', {}, ...weeks)));
}

async function load() {
  const { calendar } = await api('GET', '/calendar' + (month ? '?month=' + month : ''));
  cal = calendar; month = cal.month;
  render();
}
$('calPrev').addEventListener('click', () => { if (cal && cal.prev) { month = cal.prev; load().catch((e) => say(msg, e.message, 'err')); } });
$('calNext').addEventListener('click', () => { if (cal && cal.next) { month = cal.next; load().catch((e) => say(msg, e.message, 'err')); } });
load().catch((e) => say(msg, e.message, 'err'));
loadSummary();
