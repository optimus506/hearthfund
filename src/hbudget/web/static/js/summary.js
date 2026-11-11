// Summary strip at the top of the Bills, Plan and Money pages and on Home. The alert list is on Home only;
// other pages show one line linking to it.
import { api, $, el, money, longD, page, can, busy, say } from './api.js';

const TAG = { below_zero: 'Below $0', cushion: 'Cushion', bill_variance: 'Bill', unmatched: 'Bank', unmatched_recurring: 'Repeat', unbudgeted: 'Not budgeted',
  income_short: 'Income', income_check_short: 'Pay', cap: 'Cap', unscheduled: 'Schedule', expected_late: 'Expected' };

export function renderSummary(s) {
  const strip = $('strip'), box = $('alerts'), link = $('alertlink');
  if (!strip || !s) return;
  const stat = (label, value, sub, cls) => el('div', { class: 'stat' + (cls ? ' ' + cls : '') },
    el('span', { class: 'eyebrow', text: label }), el('b', { class: 'num', text: value }), el('small', { text: sub || '' }));
  const lowCls = s.low < 0 ? 'bad' : (s.low < s.cushion ? 'warn' : '');
  const ye = s.year_end, un = s.unscheduled, ex = s.expected_open;
  if (strip.classList.contains('slim')) { slim(strip, s, lowCls, link); return; }
  const lowSub = [s.low_date ? longD(s.low_date) : '', ex ? 'with ' + ex + ' expected' : ''].filter(Boolean).join(' · ');
  strip.replaceChildren(
    stat('Checkbook', money(s.balance), s.reconciled_through ? 'reconciled through ' + longD(s.reconciled_through) : 'as of ' + longD(s.as_of)),
    stat('Forecast low', money(s.low), lowSub, lowCls),
    stat(ye ? 'Year end ' + ye.year : 'End', money(ye ? ye.balance : s.end), ye ? '' : (s.end_date ? longD(s.end_date) : ''), (ye ? ye.balance : s.end) < 0 ? 'bad' : ''),
    stat('Not scheduled', money(un.total), un.count ? (un.count === 1 ? '1 bill' : un.count + ' bills') + ' due within ' + un.days + ' days'
      : 'nothing due in the next ' + un.days + ' days', un.count ? 'warn' : ''));
  if (link) {
    // every alert counts, notes included, so no page says "no alerts" while Home shows one (#104)
    const n = s.alerts.filter((a) => a.severity !== 'info').length, notes = s.alerts.length - n;
    const bad = s.alerts.some((a) => a.severity === 'bad');
    const words = [n ? (n === 1 ? '1 alert' : n + ' alerts') : '', notes ? (notes === 1 ? '1 note' : notes + ' notes') : ''].filter(Boolean);
    link.hidden = !words.length;
    link.className = 'alertlink small' + (bad ? ' bad' : '');
    if (words.length) link.replaceChildren(el('a', { href: '/h/' + page.hid + '/home#alerts', text: words.join(' and ') + ' on Home' }));
  }
  const skip = box && box.dataset.skip ? box.dataset.skip.split(',') : [];
  if (box) box.replaceChildren(...s.alerts.filter((a) => !skip.includes(a.code)).map((a) => el('div', { class: 'alert' + (a.severity === 'bad' ? ' bad' : a.severity === 'info' ? ' info' : '') },
    el('span', { class: 'tag', text: TAG[a.code] || a.code }), el('span', { text: a.message }), actions(a))));
}

// other pages than Home (#99): one line, the balance, the low and what needs scheduling, linking to Home, where
// the full strip and the alerts are
function slim(strip, s, lowCls, link) {
  const n = s.alerts.filter((a) => a.severity !== 'info').length, notes = s.alerts.length - n, un = s.unscheduled;
  const bits = [
    el('b', { class: 'num', text: money(s.balance) }),
    el('span', { class: lowCls, text: 'low ' + money(s.low) + (s.low_date ? ' on ' + longD(s.low_date) : '') }),
    un.count ? el('span', { class: 'warn', text: (un.count === 1 ? '1 bill' : un.count + ' bills') + ' to schedule' }) : null,
    n ? el('span', { class: s.alerts.some((a) => a.severity === 'bad') ? 'bad' : 'warn', text: n === 1 ? '1 alert' : n + ' alerts' }) : null,
    notes ? el('span', { text: notes === 1 ? '1 note' : notes + ' notes' }) : null,
  ].filter(Boolean);
  const parts = [];
  bits.forEach((b, i) => { if (i) parts.push(' · '); parts.push(b); });
  strip.replaceChildren(el('a', { href: '/h/' + page.hid + '/home', 'aria-label': 'Summary: open Home' }, ...parts));
  if (link) link.hidden = true;
}

// a repeating payee: make a rule for it, or set the alert aside until the payee posts in a later month (#104, #95)
// where each alert is dealt with (#98): the check-in step links there, and the page shows the way back
const FIX_AT = { below_zero: ['bills', 'Bills'], cushion: ['bills', 'Bills'], bill_variance: ['checkup', 'Check-up'],
  unmatched: ['reconcile', 'Uploads'], income_short: ['series', 'Bill setup'], income_check_short: ['series', 'Bill setup'],
  cap: ['checkup', 'Check-up'], expected_late: ['bills', 'Bills'] };

function actions(a) {
  const payee = a.data && a.data.payee;
  const h = '/h/' + page.hid + '/';
  if (FIX_AT[a.code]) return el('a', { class: 'btn small', href: h + FIX_AT[a.code][0] + '?checkin=3', text: 'Open ' + FIX_AT[a.code][1] });
  if (!payee || !['unmatched_recurring', 'unbudgeted'].includes(a.code) || !can('contributor')) return null;
  const ignore = el('button', { class: 'btn small', type: 'button', text: 'Ignore until it posts again', onclick: async (ev) => {
    const b = ev.currentTarget;
    try { await busy(b, () => api('PUT', '/alerts/snooze', { alert: a.code, payee }), '…'); await loadSummary(); }
    catch (e) { say(b, e.message, 'err'); }
  } });
  return el('span', { class: 'row-actions' },
    a.code === 'unbudgeted' ? el('a', { class: 'btn small', href: h + 'series?checkin=3', text: 'Add it to the bills' })
      : can('editor') ? el('a', { class: 'btn small', href: h + 'rules?checkin=3&payee=' + encodeURIComponent(payee), text: 'Make a rule' }) : null,
    ignore);
}

export async function loadSummary() {
  try {
    const s = await api('GET', '/summary'); renderSummary(s);
    document.dispatchEvent(new CustomEvent('hb:summary', { detail: s }));
    return s;
  } catch (e) { return null; }
}
