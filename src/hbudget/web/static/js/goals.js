// Goals tab (D-081): emergency fund in months of bills, savings targets, debt-free countdown, with progress bars.
// Saving sends the whole list (PUT /goals); the server works out the progress.
import { api, $, el, money0, toCents, centsInput, can, say, busy, submitter, arm } from './api.js';
import { loadSummary } from './summary.js';
import { KIND, goalBar, goalLine } from './goalbits.js';

const msg = $('msg');
let data = null;
function body(list) {
  return { goals: list.map((g) => ({ id: g.id, name: g.name, kind: g.kind, target: g.kind === 'savings' ? g.target : null, months: g.months, saved: g.saved, by: g.by })) };
}

async function save(list, where) {
  try { data = await api('PUT', '/goals', body(list)); render(); return true; }
  catch (e) { say(where && where.isConnected ? where : msg, e.message, 'err'); return false; }
}

function render() {
  $('gBills').textContent = 'Bills average ' + money0(data.monthly_bills) + ' a month';
  const box = $('goalList');
  if (!data.goals.length) { box.replaceChildren(el('div', { class: 'empty', text: 'No goals yet. A good first one: an emergency fund of three months of bills.' })); return; }
  box.replaceChildren(...data.goals.map((g) => el('div', { class: 'goalrow' },
    el('div', { class: 'thead' }, el('b', { text: g.name }), el('span', { class: 'chip', text: KIND[g.kind] })),
    goalBar(g), el('p', { class: 'small', text: goalLine(g) }),
    can('editor') ? el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: 'Edit', onclick: () => edit(g) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Remove goal?')) save(data.goals.filter((x) => x !== g), ev.currentTarget);
      } })) : null)));
}

function showFields() {
  const k = $('gKind').value;
  for (const f of document.querySelectorAll('#goalForm [data-for]')) f.hidden = !f.dataset.for.split(' ').includes(k);
}
function edit(g) {
  $('gId').value = g.id; $('gKind').value = g.kind; $('gName').value = g.name; $('gMonths').value = g.months;
  $('gTarget').value = g.kind === 'savings' ? centsInput(g.target) : ''; $('gSaved').value = centsInput(g.saved); $('gBy').value = g.by || '';
  $('gSave').textContent = 'Save goal'; $('gCancel').hidden = false; showFields(); $('gName').focus();
}
function reset() {
  $('gId').value = ''; $('gName').value = ''; $('gMonths').value = '3'; $('gTarget').value = ''; $('gSaved').value = '0'; $('gBy').value = '';
  $('gSave').textContent = 'Add goal'; $('gCancel').hidden = true; showFields();
}
$('gKind').addEventListener('change', showFields);
$('gCancel').addEventListener('click', reset);
$('goalForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const kind = $('gKind').value, name = $('gName').value.trim() || KIND[kind];
  const g = { id: $('gId').value || null, name, kind, months: Number($('gMonths').value) || 3, target: kind === 'savings' ? toCents($('gTarget').value) : null,
    saved: toCents($('gSaved').value || '0'), by: $('gBy').value || null };
  if (kind === 'savings' && !(g.target > 0)) { say(e.target, 'Enter the amount to save.', 'err'); return; }
  if (Number.isNaN(g.saved) || g.saved < 0) { say(e.target, 'Enter what is put aside so far, for example 500.00.', 'err'); return; }
  const list = g.id ? data.goals.map((x) => (x.id === g.id ? g : x)) : [...data.goals, g];
  if (await busy(submitter(e), () => save(list, e.target))) { reset(); say(e.target, 'Goal saved.', 'ok'); }
});
if (!can('editor')) $('goalForm').closest('section').hidden = true;
reset();
api('GET', '/goals').then((d) => { data = d; render(); }, (e) => say(msg, e.message, 'err'));
loadSummary();
