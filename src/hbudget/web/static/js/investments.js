// Money > Investments (D-102): what the investment accounts hold and how that moved. Balances are typed in, or kept
// by a linked account (More > Bank connection). A summary only: nothing here plans or looks inside an account.
import { api, $, el, money, money0, longD, shortD, isoD, toCents, can, say, busy, submitter, arm, table } from './api.js';
import { lineChart } from './chart.js';

const msg = $('msg');
let data = null;
let open = null;            // {id, what: 'balance' | 'history'}: the row with its form or its list of balances open
const PERIOD = { month: '30 days', quarter: '3 months', year: '1 year', all: 'Since the start' };

const pct = (bp) => (bp == null ? '' : (bp > 0 ? '+' : '') + (bp / 100).toFixed(1) + '%');
const tone = (c) => (c && c.amount > 0 ? 'good' : c && c.amount < 0 ? 'bad' : null);
// a change in a table cell: the amount, with the percentage on a small second line
const changeCell = (c, strong) => el('td', { class: 'r num' + (tone(c) === 'bad' ? ' neg' : '') }, c ? money(c.amount, true) : '—',
  c && c.pct_bp != null && !strong ? el('small', { class: 'sub', text: pct(c.pct_bp) }) : (c && c.pct_bp != null ? ' (' + pct(c.pct_bp) + ')' : null));
const mdy = (iso) => (iso ? shortD(iso) + '/' + iso.slice(2, 4) : '');

function kpis() {
  const c = data.change;
  $('invKpis').replaceChildren(
    el('div', { class: 'kpi' }, el('small', { text: 'Total' }), el('b', { class: 'num', text: money(data.total) }),
      el('small', { text: data.as_of ? 'balances as of ' + longD(data.as_of) + ' or later' : '' })),
    ...Object.entries(PERIOD).map(([k, label]) => el('div', { class: 'kpi' + (tone(c[k]) === 'good' ? ' good' : '') },
      el('small', { text: label }), el('b', { class: 'num' + (tone(c[k]) === 'bad' ? ' neg' : ''), text: c[k] ? money(c[k].amount, true) : '—' }),
      el('small', { text: c[k] ? [pct(c[k].pct_bp), 'from ' + money0(c[k].was) + ' on ' + longD(c[k].since)].filter(Boolean).join(' · ') : 'no balance that far back yet' }))));
}

// the chosen account's balances (or the total), cut to the chosen range; the balance before the range starts it
function points() {
  const id = $('invShow').value, days = Number($('invRange').value);
  const acct = data.accounts.find((a) => a.id === id);
  const all = acct ? acct.history : data.series;
  if (!days || !all.length) return all;
  const from = new Date(); from.setDate(from.getDate() - days);
  const cut = isoD(from), inside = all.filter(([d]) => d >= cut), before = all.filter(([d]) => d < cut).pop();
  return before ? [[cut, before[1]], ...inside] : inside;
}

function chart() {
  const host = $('invChart'), pts = points();
  const acct = data.accounts.find((a) => a.id === $('invShow').value);
  $('invNote').replaceChildren();
  if (pts.length < 2) {
    host.replaceChildren(el('div', { class: 'empty', text: data.accounts.length
      ? 'One balance so far. The line starts with the second one: note a balance now and then, or let a linked account do it.'
      : 'No investment accounts yet. Add the first one below.' }));
    return;
  }
  const name = acct ? acct.name : 'All accounts';
  lineChart(host, [{ name, colour: '--accent', points: pts.map(([d, v]) => [d, v, null, 1]) }],
    { label: name + ': balance over time', h: 260, lows: false, zero: false, straight: true, left: 76 });
  const starts = new Set(data.accounts.map((a) => a.history.length && a.history[0][0]));
  if (!acct && starts.size > 1) $('invNote').append(el('p', { class: 'muted small', text: 'The total steps up on the day an account gets its first balance: that money was there before, only not noted here.' }));
  $('invNote').append(el('details', {}, el('summary', { text: 'Show as a table' }),
    table(['Date', { t: 'Balance', r: true }, { t: 'Change', r: true }], pts.map(([d, v], i) => el('tr', {},
      el('td', { text: longD(d) }), el('td', { class: 'r num', text: money(v) }),
      el('td', { class: 'r num', text: i ? money(v - pts[i - 1][1], true) : '' }))).reverse())));
}

function source(a) {
  if (a.source !== 'bank') return el('span', { class: a.stale ? 'chip warn' : 'chip', text: a.stale ? 'you · time to update' : 'you' });
  return el('span', { class: a.bank.problem ? 'chip warn' : 'chip good', title: a.bank.problem || '',
    text: (a.bank.institution || 'bank') + (a.bank.problem ? ' · needs attention' : '') });
}

function balanceForm(a) {
  const bal = el('input', { type: 'text', inputmode: 'decimal', id: 'ibBal', value: '' });
  const day = el('input', { type: 'date', id: 'ibDay', value: isoD(new Date()), max: isoD(new Date()) });
  const form = el('form', { class: 'form' },
    el('div', { class: 'field' }, el('label', { for: 'ibBal', text: 'Balance of ' + a.name }), bal),
    el('div', { class: 'field' }, el('label', { for: 'ibDay', text: 'As of' }), day),
    el('div', { class: 'field' }, el('button', { class: 'btn primary', type: 'submit', text: 'Save balance' })),
    el('div', { class: 'field' }, el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => { open = null; render(); } })));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const balance = toCents(bal.value);
    if (!bal.value.trim() || Number.isNaN(balance) || balance < 0) { say(form, 'Enter the balance, for example 12500.00.', 'err'); return; }
    try {
      await busy(submitter(e), () => api('POST', '/investments/' + encodeURIComponent(a.id) + '/balance', { balance, as_of: day.value || null }));
      open = null; await load(); say(msg, 'Balance saved.', 'ok');
    } catch (err) { say(form, err.message, 'err'); }
  });
  setTimeout(() => bal.focus(), 0);
  return form;
}

function historyList(a) {
  const rows = [...a.history].reverse().map(([d, v]) => el('tr', {},
    el('td', { text: longD(d) }), el('td', { class: 'r num', text: money(v) }),
    el('td', { class: 'actions' }, can('editor') && a.history.length > 1 ? el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: (ev) => {
      if (!arm(ev.currentTarget, 'Remove this balance?')) return;
      api('DELETE', '/investments/' + encodeURIComponent(a.id) + '/balance/' + d).then(load, (e) => say(msg, e.message, 'err'));
    } }) : null)));
  return el('div', {}, table(['Date', { t: 'Balance', r: true }, ''], rows),
    el('p', { class: 'muted small', text: 'Older balances are thinned out over time: every day for six weeks, then one a week, then one a month.' }),
    can('editor') ? el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: 'Rename or add a note', onclick: () => edit(a) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Delete account', onclick: (ev) => {
        if (!arm(ev.currentTarget, 'Delete with its history?')) return;
        api('DELETE', '/investments/' + encodeURIComponent(a.id)).then(() => { open = null; return load(); }, (e) => say(msg, e.message, 'err'));
      } })) : null);
}

function renderTable() {
  const box = $('invTable');
  if (!data.accounts.length) { box.replaceChildren(el('div', { class: 'empty', text: 'No investment accounts yet. Add the first one below. Do not enter account numbers.' })); return; }
  const rows = [];
  const toggle = (a, what, text) => el('button', { class: 'btn', type: 'button', text, 'aria-expanded': String(!!open && open.id === a.id && open.what === what),
    onclick: () => { open = open && open.id === a.id && open.what === what ? null : { id: a.id, what }; render(); } });
  for (const a of data.accounts) {
    rows.push(el('tr', {},
      el('td', {}, a.name, a.notes ? el('small', { class: 'sub', text: a.notes }) : null),
      el('td', { class: 'r num' }, money(a.balance), el('small', { class: 'sub', text: 'as of ' + mdy(a.as_of) })),
      changeCell(a.change.month), changeCell(a.change.year), changeCell(a.change.all),
      el('td', {}, source(a)),
      el('td', { class: 'actions' }, el('div', { class: 'row-actions' },
        can('editor') ? toggle(a, 'balance', 'Update') : null, toggle(a, 'history', 'History')))));
    if (open && open.id === a.id) rows.push(el('tr', {}, el('td', { colspan: 7 }, open.what === 'balance' ? balanceForm(a) : historyList(a))));
  }
  if (data.accounts.length > 1) {
    rows.push(el('tr', { class: 'total' }, el('td', { text: 'Total' }), el('td', { class: 'r num', text: money(data.total) }),
      changeCell(data.change.month), changeCell(data.change.year), changeCell(data.change.all), el('td', {}), el('td', {})));
  }
  box.replaceChildren(table(['Account', { t: 'Balance', r: true }, { t: '30 days', r: true }, { t: '1 year', r: true }, { t: 'Since the start', r: true }, 'Kept by', ''], rows));
}

function edit(a) {
  $('iId').value = a.id; $('iName').value = a.name; $('iNotes').value = a.notes || '';
  for (const f of document.querySelectorAll('#invForm [data-new]')) f.hidden = true;
  $('iSave').textContent = 'Save changes'; $('iCancel').hidden = false; $('iName').focus();
}
function reset() {
  $('iId').value = ''; $('iName').value = ''; $('iBal').value = ''; $('iNotes').value = ''; $('iAsOf').value = isoD(new Date()); $('iAsOf').max = isoD(new Date());
  for (const f of document.querySelectorAll('#invForm [data-new]')) f.hidden = false;
  $('iSave').textContent = 'Add account'; $('iCancel').hidden = true;
}
$('iCancel').addEventListener('click', reset);
$('invForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target, id = $('iId').value, name = $('iName').value.trim(), notes = $('iNotes').value.trim();
  if (!name) { say(form, 'Enter a name.', 'err'); return; }
  if (/\d{6,}/.test(name)) { say(form, 'Leave account numbers out of the name.', 'err'); return; }
  let body = { name, notes };
  if (!id) {
    const balance = toCents($('iBal').value);
    if (!$('iBal').value.trim() || Number.isNaN(balance) || balance < 0) { say(form, 'Enter the balance, for example 12500.00.', 'err'); return; }
    body = { ...body, balance, as_of: $('iAsOf').value || null };
  }
  try {
    await busy(submitter(e), () => (id ? api('PUT', '/investments/' + encodeURIComponent(id), body) : api('POST', '/investments', body)));
    reset(); await load(); say(form, 'Saved.', 'ok');
  } catch (err) { say(form, err.message, 'err'); }
});
if (!can('editor')) $('invForm').hidden = true;

// ---- bring in a file (D-103): the server reads it and stores nothing; what is chosen here is sent back to be saved
let found = null;
function foundRow(r, i) {
  const one = r.points.length === 1, last = r.points[r.points.length - 1];
  const to = el('select', { id: 'ifTo' + i, 'aria-label': 'Where these balances go' },
    el('option', { value: '', text: 'Choose…' }),
    ...data.accounts.filter((a) => a.source !== 'bank').map((a) => el('option', { value: a.id, text: a.name })),
    el('option', { value: '+', text: 'A new account…' }), el('option', { value: '-', text: 'Leave out' }));
  const name = el('input', { type: 'text', id: 'ifName' + i, maxlength: 60, placeholder: 'Name of the new account', 'aria-label': 'Name of the new account', value: /\d{6,}/.test(r.label) ? '' : r.label, hidden: true });
  to.value = r.match || (data.accounts.length ? '' : '+');
  const show = () => { name.hidden = to.value !== '+'; };
  to.addEventListener('change', show); show();
  const what = [];
  if (one) {
    what.push(el('input', { type: 'text', inputmode: 'decimal', id: 'ifBal' + i, 'aria-label': 'Balance', value: (last[1] / 100).toFixed(2) }), ' as of ',
      el('input', { type: 'date', id: 'ifDay' + i, 'aria-label': 'As of', value: last[0], max: found.today }));
    const notes = [];
    if (r.cash != null) notes.push('Holdings ' + money(r.holdings) + ' plus cash ' + money(r.cash) + '. If the statement shows ' + money(r.holdings) + ' as the total, the cash is already among the holdings: type that figure.');
    if (!r.dated) notes.push('The file gives no date, so today is filled in.');
    if (notes.length) what.push(el('small', { class: 'sub', text: notes.join(' ') }));
  } else {
    what.push(r.points.length + ' balances, ' + longD(r.points[0][0]) + ' to ' + longD(last[0]) + '; the newest is ' + money(last[1]),
      r.found > r.points.length ? el('small', { class: 'sub', text: 'The file has ' + r.found + '; older ones are thinned to one a week, then one a month.' }) : null);
  }
  return el('tr', {}, el('td', { text: r.label || 'This file' }), el('td', {}, ...what), el('td', {}, to, name));
}
function renderFound() {
  const box = $('invFound');
  if (!found) { box.replaceChildren(); return; }
  const note = found.skipped ? el('p', { class: 'muted small', text: found.skipped + (found.skipped === 1 ? ' row was' : ' rows were') + ' left out: no date or amount that could be read, a total line, or a date after today.' }) : null;
  const save = el('button', { class: 'btn primary', type: 'button', text: 'Save these balances' });
  save.addEventListener('click', async () => {
    const items = [];
    for (const [i, r] of found.readings.entries()) {
      const to = $('ifTo' + i).value;
      if (to === '-') continue;
      if (!to) { say(box, 'Choose where each set of balances goes, or leave it out.', 'err'); return; }
      const item = { points: r.points.map(([d, v]) => ({ as_of: d, balance: v })) };
      if (r.points.length === 1) {
        const balance = toCents($('ifBal' + i).value);
        if (Number.isNaN(balance) || balance < 0 || !$('ifDay' + i).value) { say(box, 'Enter the balance and its date.', 'err'); return; }
        item.points = [{ as_of: $('ifDay' + i).value, balance }];
      }
      if (to === '+') {
        const n = $('ifName' + i).value.trim();
        if (!n || /\d{6,}/.test(n)) { say(box, 'Give the new account a name, without account numbers.', 'err'); return; }
        item.name = n;
      } else item.account_id = to;
      items.push(item);
    }
    if (!items.length) { found = null; renderFound(); return; }
    try {
      const out = await busy(save, () => api('POST', '/investments/import', { items }));
      found = null; $('invFile').reset(); await load(); renderFound();
      say(msg, out.balances + (out.balances === 1 ? ' balance' : ' balances') + ' saved.', 'ok');
    } catch (err) { say(box, err.message, 'err'); }
  });
  box.replaceChildren(el('h3', { text: 'Read from ' + found.file }),
    table(['In the file', 'What was read', 'Goes to'], found.readings.map(foundRow)), note,
    el('div', { class: 'row-actions' }, save, el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => { found = null; $('invFile').reset(); renderFound(); } })));
}
$('invFile').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = $('ifFile').files[0];
  if (!f) { say(e.target, 'Choose a file first.', 'err'); return; }
  const body = new FormData(); body.append('file', f);
  try { found = await busy(submitter(e), () => api('POST', '/investments/file', body)); renderFound(); } catch (err) { found = null; renderFound(); say(e.target, err.message, 'err'); }
});
if (!can('editor')) $('invFileCard').hidden = true;

function render() {
  const sel = $('invShow'), was = sel.value;
  sel.replaceChildren(el('option', { value: '', text: 'All accounts' }), ...data.accounts.map((a) => el('option', { value: a.id, text: a.name })));
  sel.value = data.accounts.some((a) => a.id === was) ? was : '';
  sel.hidden = data.accounts.length < 2;
  kpis(); chart(); renderTable();
}
async function load() {
  try { data = await api('GET', '/investments'); render(); } catch (e) { say(msg, e.message, 'err'); }
}
$('invShow').addEventListener('change', chart);
$('invRange').addEventListener('change', chart);
window.addEventListener('themechange', () => { if (data) chart(); });
window.addEventListener('resize', () => { if (data) chart(); });
reset();
load();
