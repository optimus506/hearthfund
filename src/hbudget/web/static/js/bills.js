// Bills tab: rows grouped by the payday that funds them, status marks, Change / Already paid / Won’t be paid / Undo, chart, table.
import { api, $, el, money, fmtD, shortD, toCents, centsInput, can, say, busy, table, arm } from './api.js';
import { loadSummary } from './summary.js';
import { lineChart } from './chart.js';

// editVal: what is typed in the open change editor, so a re-render (a phone keyboard opening resizes the window,
// a status mark reloads the list) does not lose it; cleared whenever the editor closes
// open: rows showing their status choice and Change (compact rows, #100); spend: grouped spending lines opened
const state = { data: null, expected: [], weeks: 8, editing: null, editVal: null, focusEdit: false, q: '', picked: new Set(),
  open: new Set(), spend: new Set() };
const STATUS_TEXT = { none: 'Not yet', manual: 'Scheduled', autopay: 'Autopay' };
const msg = $('msg');
const DAY = 86400000;

function segment(r) {
  const seg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Status for ' + r.name });
  for (const [v, label] of [['none', 'Not yet'], ['manual', 'Scheduled'], ['autopay', 'Autopay']]) {
    seg.append(el('button', { type: 'button', 'data-v': v, 'aria-pressed': String(r.status === v), text: label,
      disabled: !can('contributor'), onclick: () => mark(r, v) }));
  }
  return seg;
}

async function mark(r, status) {
  try { await api('POST', '/rows/' + encodeURIComponent(r.id) + '/status', { status }); state.open.delete(r.id); await load(); }
  catch (e) { say(msg, e.message, 'err'); }
}

async function rowAction(r, action, body, btn) {
  try {
    await busy(btn, () => api('POST', '/rows/' + encodeURIComponent(r.id) + '/' + action, body || { expected_amount: r.amount }));
    closeEditor(); state.open.delete(r.id); await load();
  } catch (e) {
    say(btn && btn.closest('.editrow') ? btn : msg, e.message, 'err');
    if (e.status === 409) await load();
  }
}

function openEditor(id) {
  state.editing = state.editing === id ? null : id;
  state.editVal = null; state.focusEdit = state.editing != null;
  renderList();
}
function closeEditor() { state.editing = null; state.editVal = null; state.focusEdit = false; }

function editor(r) {
  const income = r.kind === 'income';
  const inp = el('input', { type: 'text', inputmode: 'decimal', id: 'chg-' + r.id, value: state.editVal ?? centsInput(r.amount), 'aria-label': 'New amount' });
  inp.addEventListener('input', () => { state.editVal = inp.value; });
  const saveBtn = el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => save() });
  const save = () => {
    const c = toCents(inp.value);
    if (!(c >= 0)) { say(saveBtn, 'Enter the new amount, for example 120.00, or use Won’t be paid.', 'err'); return; }
    rowAction(r, 'change', { amount: c, expected_amount: r.amount }, saveBtn);
  };
  const cancel = () => { closeEditor(); renderList(); };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') cancel(); });
  if (state.focusEdit) { state.focusEdit = false; setTimeout(() => inp.focus(), 0); }
  return el('div', { class: 'bill editrow' }, el('i', { class: 'stripe' }),
    el('div', { class: 'form' }, el('div', { class: 'field' }, el('label', { for: inp.id, text: 'New amount' }), inp)),
    el('div', { class: 'acts' },
      saveBtn,
      el('button', { class: 'btn', type: 'button', text: income ? 'Already received' : 'Already paid', onclick: (ev) => rowAction(r, 'done', null, ev.currentTarget) }),
      el('button', { class: 'btn', type: 'button', text: income ? 'Won’t come in' : 'Won’t be paid', onclick: (ev) => rowAction(r, 'remove', null, ev.currentTarget) }),
      el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: cancel })),
    el('p', { class: 'muted small', text: (income ? 'Already received: the money is in the bank, take it out of the forecast. Won’t come in: it is not coming.'
      : 'Already paid: it has left the bank, take it out of the forecast. Won’t be paid: skip it and keep the money in the budget.') + ' Both can be undone.' }),
    noteForm(r));
}

// a note on this one row ("paid by phone"); saved on its own, an empty note clears it
function noteForm(r) {
  const inp = el('input', { type: 'text', id: 'note-' + r.id, maxlength: '300', value: r.note || '', 'aria-label': 'Note on this bill' });
  const btn = el('button', { class: 'btn', type: 'button', text: 'Save note', onclick: () => rowAction(r, 'note', { note: inp.value }, btn) });
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') btn.click(); });
  return el('div', { class: 'form' }, el('div', { class: 'field' }, el('label', { for: inp.id, text: 'Note' }), inp), el('div', { class: 'field' }, btn));
}

function groups(rows) {
  const out = [{ pay: null, rows: [] }];
  for (const r of rows) {
    if (r.kind === 'income' && r.state === 'open') { out.push({ pay: r, rows: [] }); continue; }
    out[out.length - 1].rows.push(r);
  }
  return out.filter((g) => g.pay || g.rows.length);
}

function render() { renderList(); renderChart(); renderAutopay(); renderFind(); }

// bill finder: every open row whose name matches, across the whole forecast; ticked rows change together, all or
// nothing (POST /rows/bulk refuses if any changed since this page loaded them)
const MAX_FOUND = 300;
function found() {
  const q = state.q.trim().toLowerCase();
  if (!q) return [];
  return state.data.rows.filter((r) => r.state === 'open' && r.name.toLowerCase().includes(q)).slice(0, MAX_FOUND);
}
function renderFind() {
  const rows = found(), box = $('findResults');
  const ids = new Set(rows.map((r) => r.id));
  for (const id of [...state.picked]) if (!ids.has(id)) state.picked.delete(id);
  $('findCard').hidden = !state.q.trim();
  if (!state.q.trim()) box.replaceChildren();
  else if (!rows.length) box.replaceChildren(el('div', { class: 'empty', text: 'No open rows match.' }));
  else {
    const all = el('input', { type: 'checkbox', 'aria-label': 'Select all ' + rows.length + ' rows', checked: rows.every((r) => state.picked.has(r.id)),
      onchange: () => { for (const r of rows) { if (all.checked) state.picked.add(r.id); else state.picked.delete(r.id); } renderFind(); } });
    const t = table([all, 'Due', 'Item', { t: 'Amount', r: true }, 'Status'], rows.map((r) => el('tr', {},
      el('td', {}, el('input', { type: 'checkbox', 'aria-label': 'Select ' + r.name + ' ' + r.due, checked: state.picked.has(r.id),
        onchange: (e) => { if (e.target.checked) state.picked.add(r.id); else state.picked.delete(r.id); renderBulk(); } })),
      el('td', { class: 'num', text: shortD(r.due) + '/' + r.due.slice(2, 4) }), el('td', { text: r.name + (r.amount_was != null ? ' (changed)' : '') }),
      el('td', { class: 'r num', text: money(-r.amount, true) }), el('td', { text: r.spending ? 'budget line' : { none: 'Not yet', manual: 'Scheduled', autopay: 'Autopay' }[r.status] || r.status }))));
    t.classList.add('tall');
    box.replaceChildren(t, rows.length === MAX_FOUND ? el('p', { class: 'muted small', text: 'Showing the first ' + MAX_FOUND + '; narrow the search.' }) : '');
  }
  renderBulk();
}
function renderBulk() {
  const n = state.picked.size;
  $('bulkBar').hidden = n === 0 || !can('contributor');
  $('bulkCount').textContent = n + (n === 1 ? ' row selected' : ' rows selected');
  for (const id of ['bulkChange', 'bulkDone', 'bulkRemove']) $(id).disabled = !can('editor');
}
async function bulk(action, extra, btn) {
  const rows = state.data.rows.filter((r) => state.picked.has(r.id)).map((r) => ({ id: r.id, expected_amount: r.amount }));
  try {
    const out = await busy(btn, () => api('POST', '/rows/bulk', { rows, action, ...extra }));
    state.picked.clear(); closeEditor(); await load();
    say(msg, out.rows + (out.rows === 1 ? ' row' : ' rows') + ' updated.', 'ok');
  } catch (e) { say(msg, e.message, 'err'); if (e.status === 409 || e.status === 404) await load(); }
}
// the page search in the tab row (search.js) also searches every open row, not only the weeks shown
$('pageSearch').addEventListener('pagesearch', (e) => { state.q = e.detail; renderFind(); });
$('bulkChange').addEventListener('click', (ev) => {
  const c = toCents($('bulkAmount').value);
  if (!(c >= 0)) { say(msg, 'Enter the new amount, for example 120.00.', 'err'); $('bulkAmount').focus(); return; }
  bulk('change', { amount: c }, ev.currentTarget);
});
$('bulkMark').addEventListener('click', (ev) => bulk('status', { status: $('bulkStatus').value }, ev.currentTarget));
$('bulkDone').addEventListener('click', (ev) => { if (arm(ev.currentTarget, 'Mark ' + state.picked.size + ' done?')) bulk('done', {}, ev.currentTarget); });
$('bulkRemove').addEventListener('click', (ev) => { if (arm(ev.currentTarget, 'Skip ' + state.picked.size + '?')) bulk('remove', {}, ev.currentTarget); });

function renderList() {
  const d = state.data;
  const box = $('groups'); box.replaceChildren();
  if (!d.rows.length) { box.append(el('div', { class: 'empty', text: 'No bills yet. Add pay and bills on Bills > Bill setup, or import a bill file.' })); }
  const today = new Date(d.today + 'T00:00');   // the server's "today": never before the budget's as-of date
  const horizon = new Date(today.getTime() + state.weeks * 7 * DAY);
  const gs = groups(d.rows);
  const pays = gs.filter((g) => g.pay).map((g) => g.pay.order);
  let hidden = 0;
  for (const g of gs) {
    const start = g.pay ? new Date(g.pay.order) : today;
    if (g.pay && start > horizon) { hidden++; continue; }
    const last = g.rows.length ? g.rows[g.rows.length - 1].balance : (g.pay ? g.pay.balance : d.start);
    const card = el('section', { class: 'group', 'aria-label': g.pay ? g.pay.name + ' ' + g.pay.due : 'From the checkbook now' });
    card.append(el('div', { class: 'ghead' },
      el('span', { class: 'who' }, g.pay ? g.pay.name + ' · ' + fmtD(g.pay.due) + ' · ' : 'From the checkbook now',
        g.pay ? el('span', { class: 'num', text: money(-g.pay.amount, true) }) : null),
      el('span', { class: 'muted num small', text: 'Balance after: ' + money(last) })));
    // card-funded rows in this stretch, per card: what to move from checking to the card (#31)
    const toCard = new Map();
    for (const r of g.rows) if (r.card && r.state === 'open' && r.amount > 0) toCard.set(r.card, [...(toCard.get(r.card) || []), r]);
    for (const [c, rs] of toCard) {
      card.append(el('div', { class: 'bill', 'data-status': 'expected' }, el('i', { class: 'stripe' }),
        el('div', { class: 'what' }, el('span', { class: 'name', text: 'Move to ' + ((d.cards || {})[c] || c) }),
          el('span', { class: 'meta', text: 'Covers ' + rs.map((r) => r.name).join(', ') })),
        el('div', { class: 'right' }, el('span', { class: 'amt num', text: money(rs.reduce((a, r) => a + r.amount, 0)) }))));
    }
    const lo = g.pay ? g.pay.order : '0000-00-00';
    const next = pays.find((p) => p > lo);
    for (const x of state.expected.filter((x) => x.status === 'open' && x.date >= lo && (!next || x.date < next))) {
      card.append(el('div', { class: 'bill', 'data-status': 'expected' }, el('i', { class: 'stripe' }),
        el('div', { class: 'what' }, el('span', { class: 'name', text: x.label }), el('span', { class: 'meta', text: 'Expected ' + fmtD(x.date) + ' · not in the bank yet' })),
        el('div', { class: 'right' }, el('span', { class: 'amt num', text: money(x.direction === 'in' ? x.amount : -x.amount, true) }))));
    }
    if (!g.rows.length) card.append(el('div', { class: 'bill' }, el('i', { class: 'stripe' }), el('span', { class: 'meta muted', text: 'No bills in this stretch.' })));
    // repeating spending lines of one payday fold into one line, "Grocery ×2 · $320.00", opened on tap (#100)
    const folds = new Map();
    for (const r of g.rows) {
      if (!r.spending || r.state !== 'open' || r.amount_was != null) continue;
      folds.set(r.name, [...(folds.get(r.name) || []), r]);
    }
    const shown = new Set();
    for (const r of g.rows) {
      const fold = folds.get(r.name);
      if (fold && fold.length > 1 && fold.includes(r)) {
        const key = (g.pay ? g.pay.order : 'now') + '|' + r.name;
        if (shown.has(key)) { if (state.spend.has(key)) card.append(...billRow(r, d)); continue; }
        shown.add(key);
        const open = state.spend.has(key);
        card.append(el('div', { class: 'bill fold', 'data-status': 'none' }, el('i', { class: 'stripe' }),
          el('div', { class: 'what' }, el('span', { class: 'name', text: r.name + ' ×' + fold.length }),
            el('span', { class: 'meta', text: 'budget lines · ' + fold.map((x) => fmtD(x.due)).join(', ') })),
          el('div', { class: 'right' }, el('span', { class: 'amt num', text: money(fold.reduce((a, x) => a + x.amount, 0)) }),
            el('button', { class: 'btn small', type: 'button', 'aria-expanded': String(open), text: open ? 'Hide' : 'Show',
              'aria-label': (open ? 'Hide ' : 'Show ') + r.name + ' lines', onclick: () => { toggle(state.spend, key); renderList(); } }))));
        if (open) card.append(...billRow(r, d));
        continue;
      }
      card.append(...billRow(r, d));
    }
    box.append(card);
  }
  $('more').hidden = hidden === 0;
}

function toggle(set, key) { if (set.has(key)) set.delete(key); else set.add(key); }

// one bill: a status dot and the amount; tapping the row (or its status) opens the status choice and Change (#100)
function billRow(r, d) {
  const done = r.state === 'cleared';   // marked already paid by hand since the last reconciliation
  const removed = r.state === 'removed' || done;
  const changed = r.amount_was != null && !removed;
  const late = r.state === 'open' && r.due < d.today;
  const open = state.open.has(r.id) || state.editing === r.id;
  const meta = el('span', { class: 'meta' }, 'Due ' + fmtD(r.due),
    done ? (r.kind === 'income' ? ' · already received' : ' · already paid') : removed ? (r.kind === 'income' ? ' · won’t come in' : ' · won’t be paid, ' + money(r.amount) + ' back in the budget') : null,
    changed ? ' · changed from ' + money(r.amount_was) : null,
    r.paid ? ' · ' + money(r.paid) + ' already paid' : null,
    late ? el('span', { class: 'late', text: ' · past due date, not posted yet' }) : null,
    r.spending ? ' · budget line' : null,
    r.note ? el('span', { class: 'note', text: ' · ' + r.note }) : null);
  const itemNotes = r.series_id && (d.series.find((s) => s.id === r.series_id) || {}).notes;
  const flip = () => { toggle(state.open, r.id); if (!state.open.has(r.id) && state.editing === r.id) closeEditor(); renderList(); };
  const right = el('div', { class: 'right' }, el('span', { class: 'amt num' }, changed ? el('span', { class: 'was', text: money(r.amount_was) }) : null, money(r.amount)));
  const label = r.spending || removed ? (removed ? 'Undo…' : 'Change…') : STATUS_TEXT[r.status] || r.status;
  right.append(el('button', { class: 'pill' + (r.spending || removed ? ' plain' : ' s-' + r.status), type: 'button', 'aria-expanded': String(open),
    'aria-label': (r.spending || removed ? '' : 'Status: ' + label + '. ') + 'Change ' + r.name, text: label, onclick: flip }));
  const row = el('div', { class: 'bill compact' + (removed ? ' removed' : '') + (open ? ' open' : ''), 'data-status': removed || r.spending ? 'none' : r.status },
    el('i', { class: 'stripe' }), el('div', { class: 'what', onclick: flip }, el('span', { class: 'name', text: r.name, title: itemNotes || null }), meta), right);
  const out = [row];
  if (open) {
    const acts = el('div', { class: 'acts' });
    if (!r.spending && !removed) acts.append(segment(r));
    if (removed || changed) acts.append(el('button', { class: 'btn', type: 'button', text: 'Undo', disabled: !can('editor'), onclick: (ev) => rowAction(r, 'undo', null, ev.currentTarget) }));
    else acts.append(el('button', { class: 'btn', type: 'button', text: 'Change', 'aria-expanded': String(state.editing === r.id), disabled: !can('editor'),
      onclick: () => openEditor(r.id) }));
    out.push(el('div', { class: 'bill more' }, el('i', { class: 'stripe' }), acts));
  }
  if (state.editing === r.id) out.push(editor(r));
  return out;
}

function renderChart() {
  const d = state.data;
  lineChart($('chart'), [{ points: d.chart, name: 'Forecast', colour: '--accent' }], { cushion: d.cushion, label: 'Forecast balance; low ' + money(d.low) });
  const t = table(['Due', 'Item', { t: 'Amount', r: true }, { t: 'Balance after', r: true }],
    d.rows.filter((r) => r.state === 'open').map((r) => el('tr', {}, el('td', { text: shortD(r.due) + '/' + r.due.slice(2, 4) }), el('td', { text: r.name }),
      el('td', { class: 'r num', text: money(-r.amount, true) }), el('td', { class: 'r num', text: money(r.balance) }))));
  t.classList.add('tall');     // a year of rows: scroll inside the box, the header stays put
  $('tbl').replaceChildren(t);
}

function renderAutopay() {
  const box = $('autopay'); if (!box) return;
  box.replaceChildren();
  const bills = state.data.series.filter((s) => s.kind === 'bill');
  if (!bills.length) { box.append(el('div', { class: 'empty', text: 'No recurring bills yet.' })); return; }
  for (const s of bills) {
    const on = s.status === 'autopay';
    box.append(el('div', { class: 'item' }, el('span', { text: s.name }),
      el('button', { class: 'btn', type: 'button', role: 'switch', 'aria-checked': String(on), 'aria-label': 'Autopay for ' + s.name,
        text: on ? 'Autopay on' : 'Autopay off', disabled: !can('contributor'),
        onclick: async () => {
          try { await api('POST', '/series/' + encodeURIComponent(s.id) + '/status', { status: on ? 'none' : 'autopay' }); await load(); }
          catch (e) { say(msg, e.message, 'err'); }
        } })));
  }
}

async function load() {
  const [d, ex] = await Promise.all([api('GET', '/ledger'), api('GET', '/expected')]);
  state.data = d; state.expected = ex.items;
  render();
  loadSummary();
}

$('more').addEventListener('click', () => { state.weeks += 4; renderList(); });
window.addEventListener('themechange', () => state.data && renderChart());
// only the chart depends on the width; redrawing the list would close the phone keyboard mid-typing
let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => state.data && renderChart(), 150); });
load().catch((e) => say(msg, e.message, 'err'));
