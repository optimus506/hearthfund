// Import tab: a bill-system file (YAML / JSON / bills CSV), or any workbook with the column-mapping step.
import { api, $, el, money, longD, say, busy, submitter, table } from './api.js';
import { loadSummary } from './summary.js';

let token = null;
let guess = null;

// what the import does to the open rows (#64): added (flagged when they may already be paid), removed, changed
function rowChanges(x, before, after) {
  if (!x) return null;
  const c = x.counts;
  if (!c.added && !c.removed && !c.changed) return el('p', { text: 'No open rows are added, removed or changed.' });
  const more = (n, list) => (n > list.length ? el('p', { class: 'muted small', text: 'and ' + (n - list.length) + ' more' }) : null);
  const part = (label, n, list, rowsOf) => (n ? el('details', { open: label === 'Added' && c.check > 0 },
    el('summary', { text: label + ': ' + n }), table(['Name', 'Due', { t: 'Amount', r: true }, ''], list.map(rowsOf)), more(n, list)) : null);
  const tr = (r, note) => el('tr', {}, el('td', { text: r.name }), el('td', { class: 'num', text: longD(r.due) }),
    el('td', { class: 'r num', text: money(r.amount) }), el('td', { class: 'small', text: note || '' }));
  return el('div', {},
    before && after ? el('p', { class: 'num', text: 'Low ' + money(before.low) + ' → ' + money(after.low) + ' · end ' + money(before.end) + ' → ' + money(after.end) }) : null,
    c.check ? el('div', { class: 'verdict warn' }, el('b', { text: c.check + ' added row(s) fall before rows you already have for the same bill.' }),
      el('span', { text: ' Check they are not already paid (marked below).' })) : null,
    part('Added', c.added, x.added, (r) => tr(r, r.check ? 'Check: may already be paid' : '')),
    part('Removed', c.removed, x.removed, (r) => tr(r)),
    part('Changed', c.changed, x.changed, (r) => tr(r, 'was ' + money(r.amount_was) + (r.due_was !== r.due ? ' on ' + longD(r.due_was) : ''))));
}

function summaryBox(r) {
  const h = r.replaces;
  return el('div', { class: 'card' },
    r.is_backup ? el('div', { class: 'verdict bad' }, el('b', { text: 'This is a full backup. Restoring it replaces everything in this household.' }),
      el('span', { class: 'num', text: 'It would delete ' + h.rows + ' rows, ' + h.bank_txns + ' bank and ' + h.card_txns + ' card transactions, ' +
        h.uploads + ' uploads' + (h.uploads_hidden ? ' (' + h.uploads_hidden + ' of them hidden)' : '') + ', ' + h.reconciliations + ' reconciliations and ' + h.profiles + ' saved layouts, then load the backup. ' +
        'To go ahead, choose Replace, untick Dry run and type the household name.' })) : null,
    el('p', { text: r.dry_run ? 'Dry run: nothing was saved.' : 'Imported.' }),
    r.balance ? el('p', { class: 'num', text: r.balance.taken
      ? 'Checkbook balance: ' + money(r.balance.start_balance) + ' as of ' + longD(r.balance.as_of) + ', taken from the file.'
      : 'Checkbook balance: ' + money(r.balance.start_balance) + ' as of ' + longD(r.balance.as_of) + ' is kept' +
        (r.balance.file_start_balance != null ? ' (the file says ' + money(r.balance.file_start_balance) + '; tick "Also set the checkbook balance" to use it).' : '.') }) : null,
    el('p', { class: 'num', text: r.counts.series + ' recurring items, ' + r.counts.rows + ' rows, ' + r.counts.rules + ' payee rules, ' + r.counts.accounts + ' accounts.' }),
    r.forecast ? el('p', { class: 'num', text: 'Start ' + money(r.forecast.start) + ' · low ' + money(r.forecast.low) + (r.forecast.low_date ? ' on ' + longD(r.forecast.low_date) : '') + ' · end ' + money(r.forecast.end) }) : null,
    rowChanges(r.rows, r.forecast_before, r.forecast),
    r.stored_check ? el('p', { class: 'num', text: 'Check against the sheet\'s own cells: ' + Object.entries(r.stored_check).map(([k, v]) => k + ' ' + (v === null ? 'not checked' : v ? 'match' : 'MISMATCH')).join(', ') }) : null,
    r.warnings.length ? el('details', {}, el('summary', { text: r.warnings.length + ' warning(s)' }), el('ul', {}, ...r.warnings.map((w) => el('li', { text: w })))) : null);
}

$('bsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = $('bsFile').files[0];
  if (!f) { say(e.target, 'Choose a .yaml, .json or .csv file.', 'err'); return; }
  const fd = new FormData(); fd.append('file', f); fd.append('mode', $('bsMode').value); fd.append('dry_run', $('bsDry').checked ? '1' : '0');
  fd.append('confirm', $('bsConfirm').value); fd.append('set_balance', $('bsBalance').checked ? '1' : '0');
  try { const r = await busy(submitter(e), () => api('POST', '/import/billsystem', fd), 'Importing…'); $('bsResult').replaceChildren(summaryBox(r)); }
  catch (err) { say(e.target, err.message, 'err'); }
});

function colSelect(id, cols, val, optional) {
  return el('select', { id }, optional ? el('option', { value: '', text: '(none)' }) : null, ...cols.map((c) => el('option', { value: c, text: c, selected: c === val })));
}

function renderMapping(p) {
  guess = p.guess;
  const box = $('wbMap'); box.hidden = false; box.replaceChildren();
  const g = p.guess;
  const f = (label, node) => el('div', { class: 'field' }, el('label', { for: node.id, text: label }), node);
  const txt = (id, val) => el('input', { type: 'text', id, value: val == null ? '' : String(val) });
  box.append(el('h3', { text: 'Map the columns' }),
    el('p', { class: 'muted small', text: 'Sheet "' + p.sheet + '". The first rows are shown below; pick which column holds the name, amount and date.' }),
    table(['Row', ...p.columns], p.rows.map((r, i) => el('tr', {}, el('td', { class: 'num', text: String(i + 1) }), ...r.map((c) => el('td', { text: c }))))),
    el('div', { class: 'form' },
      f('Sheet', el('select', { id: 'wSheet' }, ...p.sheets.map((s) => el('option', { value: s, text: s, selected: s === p.sheet })))),
      f('Header row', txt('wHeader', g.header_row)), f('First data row', txt('wFirst', g.first_row)),
      f('Name column', colSelect('wName', p.columns, g.name_col)), f('Amount column', colSelect('wAmount', p.columns, g.amount_col)),
      f('Date column', colSelect('wDate', p.columns, g.date_col)),
      f('Amounts', el('select', { id: 'wSign' }, el('option', { value: 'positive_out', text: 'positive = money out' }), el('option', { value: 'positive_in', text: 'positive = money in' }))),
      f('Starting balance cell (e.g. B1)', txt('wStartCell', g.start_balance_cell)), f('…or the starting balance', txt('wStart', '')),
      f('Skip rows named (patterns, comma separated)', txt('wSkip', (g.skip_names || []).join(', '))),
      f('Spending lines (names, comma separated)', txt('wSpend', (g.spending_names || []).join(', '))),
      f('Check: low value and date cells (e.g. C2,C4)', txt('wLow', g.expected_low ? g.expected_low.join(',') : '')),
      f('Check: end balance cell or column', txt('wEnd', g.expected_end_cell)),
      el('div', { class: 'field' }, el('label', { for: 'wColours' }, el('input', { type: 'checkbox', id: 'wColours', checked: g.read_status_colours }), ' Read bill status from the name cell colour')),
      f('Save mapping as profile (optional)', txt('wProfile', ''))),
    el('p', { class: 'muted small', text: 'Names in the sheet: ' + p.names.slice(0, 40).join(', ') + (p.names.length > 40 ? '…' : '') }),
    el('div', { class: 'row-actions' }, el('button', { class: 'btn primary', type: 'button', text: 'Import', onclick: (ev) => commit(ev.currentTarget) })));
  $('wSign').value = g.sign;
}

async function commit(btn) {
  const list = (v) => v.split(',').map((x) => x.trim()).filter(Boolean);
  const intOr = (v) => (v.trim() === '' ? null : Number(v));
  const low = list($('wLow').value);
  const startTxt = $('wStart').value.trim();
  const mapping = Object.assign({}, guess, {
    sheet: $('wSheet').value, header_row: intOr($('wHeader').value), first_row: Number($('wFirst').value) || 1,
    name_col: $('wName').value, amount_col: $('wAmount').value, date_col: $('wDate').value, sign: $('wSign').value,
    start_balance_cell: $('wStartCell').value.trim() || null, start_balance_value: startTxt ? Math.round(Number(startTxt.replace(/[$,]/g, '')) * 100) : null,
    skip_names: list($('wSkip').value), spending_names: list($('wSpend').value), read_status_colours: $('wColours').checked,
    expected_low: low.length === 2 ? low : null, expected_end_cell: $('wEnd').value.trim() || null });
  try {
    const r = await busy(btn, () => api('POST', '/import/workbook/commit', { token, mapping, save_profile: $('wProfile').value.trim() || null }), 'Importing…');
    $('wbResult').replaceChildren(summaryBox(r)); $('wbMap').hidden = true; say($('wbForm'), 'Workbook imported.', 'ok');
  } catch (err) { say(btn, err.message, 'err'); }
}

$('wbForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = $('wbFile').files[0];
  if (!f) { say(e.target, 'Choose an .ods, .xlsx or .csv file.', 'err'); return; }
  const fd = new FormData(); fd.append('file', f);
  try { const p = await busy(submitter(e), () => api('POST', '/import/workbook/preview', fd), 'Reading…'); token = p.token; renderMapping(p); }
  catch (err) { say(e.target, err.message, 'err'); }
});
loadSummary();
