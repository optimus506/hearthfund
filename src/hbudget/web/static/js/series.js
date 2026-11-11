// Bill setup tab (Bills > Bill setup; the page key stays "series"): income, bills and spending lines (the recurring items rows are generated from), seasonal amounts.
import { api, $, el, money, longD, toCents, centsInput, can, say, busy, submitter, arm, table } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let series = [];
const CADENCE = { once: 'One time', weekly: 'Weekly', biweekly: 'Every 2 weeks', semimonthly: 'Twice a month', monthly: 'Monthly', yearly: 'Yearly' };

function describe(s) {
  const c = s.schedule;
  let when = CADENCE[c.cadence] || c.cadence;
  if (c.cadence === 'monthly' && c.day) when += ' on day ' + c.day;
  if (c.cadence === 'semimonthly' && c.days) when += ' on days ' + c.days.join(' and ');
  if (c.cadence === 'yearly' && c.month) when += ' in month ' + c.month + (c.day ? ', day ' + c.day : '');
  if (c.interval > 1 && c.cadence !== 'biweekly') when += ', every ' + c.interval;
  if (c.weekend !== 'none') when += ' (weekend ' + c.weekend + ')';
  return when + ' · from ' + longD(c.start) + (c.end ? ' to ' + longD(c.end) : '');
}

function render() {
  const box = $('seriesList');
  if (!series.length) { box.replaceChildren(el('div', { class: 'empty', text: 'Nothing yet. Add your paychecks first, then the bills.' })); return; }
  // three lists, so pay is never read as a bill (D-079)
  const GROUPS = [['income', 'Pay', 'Money coming in.'], ['bill', 'Bills', 'Paid from checking: rent, utilities, insurance, loan and card payments.'],
    ['spending', 'Spending budgets', 'Money set aside for everyday spending, such as groceries or fuel.']];
  box.replaceChildren(...GROUPS.flatMap(([kind, title, about]) => {
    const items = series.filter((s) => s.kind === kind);
    return [el('h3', { text: title + ' (' + items.length + ')' }), el('p', { class: 'muted small', text: about }),
      items.length ? list(items) : el('div', { class: 'empty', text: kind === 'income' ? 'No pay yet. Add your paychecks first.' : 'None yet.' })];
  }));
  $('sFunded').replaceChildren(el('option', { value: '', text: '(on its due date)' }),
    ...series.filter((s) => s.kind === 'income').map((s) => el('option', { value: s.id, text: 'after ' + s.name })));
}

function list(items) {
  return table(['Name', { t: 'Amount', r: true }, 'Schedule', 'Status', ''], items.map((s) => el('tr', {},
    el('td', {}, s.name + (s.active ? '' : ' (paused)'), s.notes ? el('div', { class: 'muted small', text: s.notes }) : null),
    el('td', { class: 'r num', text: money(s.amount) + (s.month_amounts.length ? ' (seasonal)' : '') + (s.cycle_amounts.length ? ' (cycle)' : '') }),
    el('td', { class: 'small', text: describe(s) + (s.funded_by ? ' · paid from ' + (series.find((x) => x.id === s.funded_by) || {}).name : '') }),
    el('td', { text: s.status }),
    el('td', { class: 'actions' }, can('editor') ? el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: 'Edit', onclick: () => edit(s) }),
      el('button', { class: 'btn', type: 'button', text: 'Seasonal', onclick: () => seasonal(s) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Delete', onclick: (ev) => {
        if (!arm(ev.currentTarget, 'Delete future rows too?')) return;
        api('DELETE', '/series/' + encodeURIComponent(s.id)).then(load, (e) => say(msg, e.message, 'err')); } })) : null))));
}

const fields = ['sId', 'sName', 'sKind', 'sAmount', 'sCadence', 'sStart', 'sEnd', 'sInterval', 'sDay', 'sDays', 'sMonth', 'sWeekend', 'sFunded', 'sStatus', 'sCard', 'sDebt', 'sGroup', 'sActive', 'sNotes'];
function edit(s) {
  $('sId').value = s.id; $('sName').value = s.name; $('sKind').value = s.kind; $('sAmount').value = centsInput(s.amount);
  const c = s.schedule;
  $('sCadence').value = c.cadence; $('sStart').value = c.start; $('sEnd').value = c.end || ''; $('sInterval').value = c.interval;
  $('sDay').value = c.day || ''; $('sDays').value = (c.days || []).join(','); $('sMonth').value = c.month || ''; $('sWeekend').value = c.weekend;
  $('sFunded').value = s.funded_by || ''; $('sStatus').value = s.status; $('sCard').value = s.card || ''; $('sGroup').value = s.group || '';
  $('sActive').checked = s.active; $('sNotes').value = s.notes || ''; $('sSave').textContent = 'Save changes'; $('sCancel').hidden = false; $('sName').focus();
  // the debt this item pays down (#97); an item with none is pre-filled with the debt of the same name
  const same = !s.debt && s.kind === 'bill' ? debts.find((a) => norm(a.name) === norm(s.name)) : null;
  $('sDebt').value = s.debt || (same ? same.id : '');
  $('sDebtHint').hidden = !same;
  $('sDebtHint').textContent = same ? 'Filled in because the names match; save to link them, or choose None.' : '';
}
const norm = (t) => String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
let debts = [];
function fillDebts() {
  $('sDebt').replaceChildren(el('option', { value: '', text: 'None' }), ...debts.map((a) => el('option', { value: a.id, text: a.name })));
}
function reset() {
  for (const f of fields) { const n = $(f); if (n.type === 'checkbox') n.checked = true; else if (n.tagName !== 'SELECT') n.value = ''; }
  $('sKind').value = 'bill'; $('sCadence').value = 'monthly'; $('sWeekend').value = 'none'; $('sStatus').value = 'none'; $('sFunded').value = '';
  $('sInterval').value = '1'; $('sSave').textContent = 'Add'; $('sCancel').hidden = true; $('sDebt').value = ''; $('sDebtHint').hidden = true;
}
$('sCancel').addEventListener('click', reset);
$('seriesForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const amount = toCents($('sAmount').value);
  const num = (id) => ($(id).value.trim() === '' ? null : Number($(id).value));
  const body = { name: $('sName').value.trim(), kind: $('sKind').value, amount,
    schedule: { cadence: $('sCadence').value, start: $('sStart').value, end: $('sEnd').value || null, interval: num('sInterval') || 1,
      day: num('sDay'), days: $('sDays').value.trim() ? $('sDays').value.split(',').map((x) => Number(x.trim())) : null, month: num('sMonth'), weekend: $('sWeekend').value },
    funded_by: $('sFunded').value || null, status: $('sStatus').value, card: $('sCard').value.trim() || null, debt: $('sDebt').value || null, group: $('sGroup').value.trim() || null, active: $('sActive').checked,
    notes: $('sNotes').value.trim() };
  const form = e.target;
  if (!body.name || !(amount >= 0) || !body.schedule.start) { say(form, 'Enter a name, a positive amount and a start date.', 'err'); return; }
  try {
    const id = $('sId').value;
    await busy(submitter(e), () => (id ? api('PUT', '/series/' + encodeURIComponent(id), body) : api('POST', '/series', body)));
    reset(); say(form, 'Saved. Future rows were updated.', 'ok'); await load();
  } catch (err) { say(form, err.message, 'err'); }
});

function seasonal(s) {
  const box = $('seasonal'); box.hidden = false;
  $('seaName').textContent = s.name; box.dataset.id = s.id;
  $('seaRules').value = s.month_amounts.map(([m, c]) => m + '=' + (c / 100)).join(', ');
  $('seaResult').replaceChildren(); $('seaRules').focus();
}
async function runSeasonal(dry, btn) {
  const id = $('seasonal').dataset.id;
  const rules = $('seaRules').value.split(',').map((x) => x.trim()).filter(Boolean).map((x) => {
    const [m, a] = x.split('='); return { months: (m || '').trim(), amount: toCents((a || '').trim()) };
  });
  if (!rules.length || rules.some((r) => !r.months || !(r.amount >= 0))) { say(btn, 'Write rules like 5-9=120, 10-4=40.', 'err'); return; }
  try {
    const r = await busy(btn, () => api('POST', '/series/' + encodeURIComponent(id) + '/seasonal', { rules, from: $('seaFrom').value || null, dry_run: dry }),
      dry ? 'Checking…' : 'Saving…');
    $('seaResult').replaceChildren(r.changes.length ? table(['Row', { t: 'Was', r: true }, { t: 'Now', r: true }], r.changes.map((c) =>
      el('tr', {}, el('td', { text: c.name + ' ' + longD(c.due) }), el('td', { class: 'r num', text: money(c.was) }), el('td', { class: 'r num', text: money(c.now) }))))
      : el('p', { class: 'muted', text: 'No rows change.' }),
      el('p', { class: 'num', text: 'Lowest balance ' + money(r.low) + ' (was ' + money(r.low_before) + ').' }));
    if (!dry) { say(btn, 'Seasonal amounts saved.', 'ok'); await load(); }
  } catch (e) { say(btn, e.message, 'err'); }
}
$('seaDry').addEventListener('click', (e) => runSeasonal(true, e.currentTarget));
$('seaSave').addEventListener('click', (e) => runSeasonal(false, e.currentTarget));

$('genBtn').addEventListener('click', async (ev) => {
  const b = ev.currentTarget;
  try { const r = await busy(b, () => api('POST', '/generate', {}), 'Working…'); say(b, 'Rows generated through ' + longD(r.through) + '.', 'ok'); loadSummary(); }
  catch (e) { say(b, e.message, 'err'); }
});
if (!can('editor')) { $('seriesForm').hidden = true; $('genBtn').hidden = true; }

async function load() {
  const [sr, ac] = await Promise.all([api('GET', '/series'), api('GET', '/accounts')]);
  series = sr.series; debts = ac.accounts.filter((a) => !['checking', 'savings'].includes(a.kind));
  fillDebts(); render(); loadSummary();
}
reset();
load().catch((e) => say(msg, e.message, 'err'));
