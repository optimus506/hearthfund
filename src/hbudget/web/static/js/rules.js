// Rules tab: payee rules (ordered, first match wins) with a test box. Settings, caps and report groups have
// their own pages (D-074).
import { api, base, $, el, money, toCents, centsInput, can, say, busy, submitter, arm, table } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let data = null;
const KINDS = { bill: 'Bill (clears one row)', pay: 'Pay (clears an income row)', ask: 'Ask me (always a proposal)', card: 'Card payment (bundle)',
  track: 'Track only', ignore: 'Ignore', savings_draw: 'From savings', loan_in: 'Loan paid in', allowance: 'Allowance (several payments a month)' };
const list = (v) => v.split(',').map((x) => x.trim()).filter(Boolean);

function renderRules() {
  const box = $('ruleList');
  const names = Object.fromEntries(data.series.map((s) => [s.id, s.name]).concat(data.row_names.map((n) => [n, n])));
  if (!data.rules.length) { box.replaceChildren(el('div', { class: 'empty', text: 'No payee rules yet. Add one per payee that shows up in your bank export.' })); return; }
  box.replaceChildren(table(['#', 'Rule', 'Matches', 'Clears', ''], data.rules.map((r, i) => el('tr', {},
    el('td', { class: 'num', text: String(i + 1) }), el('td', { text: r.name + ' · ' + (KINDS[r.kind] || r.kind) }),
    el('td', { class: 'small', text: [r.contains.length ? 'contains ' + r.contains.join(' / ') : '', r.regex ? 'pattern ' + r.regex : '',
      r.direction !== 'any' ? 'money ' + r.direction : '', r.min_amount != null ? 'from ' + money(r.min_amount) : '', r.max_amount != null ? 'up to ' + money(r.max_amount) : '',
      r.card ? 'card ' + r.card : ''].filter(Boolean).join(', ') }),
    el('td', { class: 'small', text: r.targets.map((t) => names[t] || t).join(', ') }),
    el('td', { class: 'actions' }, can('editor') ? el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: '↑', 'aria-label': 'Move up', disabled: i === 0, onclick: () => move(i, -1) }),
      el('button', { class: 'btn', type: 'button', text: '↓', 'aria-label': 'Move down', disabled: i === data.rules.length - 1, onclick: () => move(i, 1) }),
      el('button', { class: 'btn', type: 'button', text: 'Edit', onclick: () => edit(r) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Delete', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Delete rule?')) api('DELETE', '/rules/' + encodeURIComponent(r.id)).then(load, (e) => say(msg, e.message, 'err'));
      } })) : null)))));
  $('rTargets').replaceChildren(...data.series.map((s) => el('option', { value: s.id, text: s.name })),
    ...data.row_names.map((n) => el('option', { value: n, text: n + ' (one-off rows)' })));
}

async function move(i, d) {
  const ids = data.rules.map((r) => r.id);
  [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
  try { await api('POST', '/rules/order', { ids }); await load(); } catch (e) { say(msg, e.message, 'err'); }
}

function edit(r) {
  $('rId').value = r.id; $('rName').value = r.name; $('rKind').value = r.kind; $('rContains').value = r.contains.join(', ');
  $('rRegex').value = r.regex || ''; $('rDir').value = r.direction; $('rMin').value = centsInput(r.min_amount); $('rMax').value = centsInput(r.max_amount);
  $('rCard').value = r.card || ''; $('rNote').value = r.note || '';
  for (const o of $('rTargets').options) o.selected = r.targets.includes(o.value);
  $('rSave').textContent = 'Save rule'; $('rName').focus();
}
function resetRule() {
  for (const id of ['rId', 'rName', 'rContains', 'rRegex', 'rMin', 'rMax', 'rCard', 'rNote']) $(id).value = '';
  $('rKind').value = 'bill'; $('rDir').value = 'any'; for (const o of $('rTargets').options) o.selected = false; $('rSave').textContent = 'Add rule';
}
$('ruleForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const opt = (id) => ($(id).value.trim() ? toCents($(id).value) : null);
  const body = { name: $('rName').value.trim(), kind: $('rKind').value, contains: list($('rContains').value), regex: $('rRegex').value.trim() || null,
    direction: $('rDir').value, min_amount: opt('rMin'), max_amount: opt('rMax'), targets: [...$('rTargets').selectedOptions].map((o) => o.value),
    card: $('rCard').value.trim() || null, note: $('rNote').value.trim() };
  const form = e.target;
  if (!body.name || (!body.contains.length && !body.regex)) { say(form, 'A rule needs a name and a word to look for (or a pattern).', 'err'); return; }
  try {
    const id = $('rId').value;
    await busy(submitter(e), () => (id ? api('PUT', '/rules/' + encodeURIComponent(id), body) : api('POST', '/rules', body)));
    resetRule(); say(form, 'Rule saved.', 'ok'); await load();
  } catch (err) { say(form, err.message, 'err'); }
});

$('testForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const amount = toCents($('tAmount').value || '0');
  try {
    const r = await busy(submitter(e), () => api('POST', '/rules/test', { description: $('tDesc').value, amount: Number.isNaN(amount) ? 0 : amount }), 'Testing…');
    $('testResult').replaceChildren(r.first ? el('p', { class: 'msg ok', text: 'First match: ' + r.first }) : el('p', { class: 'msg', text: 'No rule matches.' }),
      el('div', { class: 'small muted', text: r.results.map((x) => (x.matched ? '✓ ' : '· ') + x.name).join('  ') }));
  } catch (err) { say(e.target, err.message, 'err'); }
});

// Starter rules (D-094): a dry run first, then one button to add what it listed
let packText = null;   // null = the pack shipped with the app, else the text of the chosen file
async function pack(dryRun, btn) {
  const box = $('packResult');
  try {
    const r = await busy(btn, () => api('POST', '/rules/pack', { text: packText, dry_run: dryRun }), dryRun ? 'Checking…' : 'Adding…');
    const lines = [];
    if (r.added.length) {
      lines.push(el('p', { class: dryRun ? 'msg' : 'msg ok', text: (dryRun ? 'Would add ' : 'Added ') + r.added.length + (r.added.length === 1 ? ' rule' : ' rules') + ' from "' + r.pack + '".' }));
      lines.push(table(['Rule', 'Clears'], r.added.map((a) => el('tr', {}, el('td', { text: a.name + ' · ' + (KINDS[a.kind] || a.kind) }),
        el('td', { class: 'small', text: a.clears.join(', ') || (a.kind === 'track' ? 'counted in reports' : '') })))));
    } else {
      lines.push(el('p', { class: 'msg', text: 'Nothing to add from "' + r.pack + '".' }));
    }
    if (r.kept) lines.push(el('p', { class: 'small muted', text: r.kept + ' already here (left as they are).' }));
    if (r.skipped.length) lines.push(el('p', { class: 'small muted', text: 'Skipped: ' + r.skipped.map((x) => x.name + ' (' + x.why + ')').join('; ') + '.' }));
    if (r.groups.length) lines.push(el('p', { class: 'small muted', text: 'New report groups: ' + r.groups.join(', ') + '.' }));
    if (r.categories.length) lines.push(el('p', { class: 'small muted', text: 'New card categories: ' + r.categories.join(', ') + '.' }));
    if (dryRun && r.added.length) {
      lines.push(el('button', { class: 'btn primary', type: 'button', id: 'packApply', text: 'Add ' + r.added.length + (r.added.length === 1 ? ' rule' : ' rules'),
        onclick: (ev) => pack(false, ev.currentTarget) }));
    }
    box.replaceChildren(...lines);
    if (!dryRun) await load();
  } catch (e) { box.replaceChildren(el('p', { class: 'msg err', text: e.message })); }
}
$('packPreview').addEventListener('click', (e) => { packText = null; $('packFile').value = ''; pack(true, e.currentTarget); });
$('packFile').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 64 * 1024) { $('packResult').replaceChildren(el('p', { class: 'msg err', text: 'A rule pack is at most 64 KB.' })); return; }
  try { packText = await f.text(); } catch (err) {
    $('packResult').replaceChildren(el('p', { class: 'msg err', text: 'That file could not be read.' }));
    return;
  }
  pack(true, null);
});
$('packDownload').href = base + '/rules/starter';

if (!can('editor')) for (const id of ['ruleForm', 'packCard']) $(id).hidden = true;

async function load() {
  data = await api('GET', '/rules');
  renderRules(); loadSummary();
}
// "Make a rule" from a Home alert (#104): ?payee=... fills the form, the household picks the kind and lines
function prefill() {
  const payee = new URLSearchParams(location.search).get('payee');
  if (!payee || !can('editor')) return;
  resetRule();
  $('rName').value = payee.slice(0, 60); $('rContains').value = payee;
  $('ruleForm').scrollIntoView({ block: 'start' }); $('rKind').focus();
  say($('ruleForm'), 'Choose what "' + payee + '" is: a bill (pick the lines it clears), spending to track, or something to ignore.', '');
}
load().then(prefill).catch((e) => say(msg, e.message, 'err'));
