// Check-up tab: what the bank actually paid per bill compared with the budget, and cap-rule totals.
import { api, $, el, money, shortD, fmtD, say, table, can, arm, busy, submitter, toCents } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
const LABEL = { ok: ['Close to budget', 'good'], over: ['Higher than budget', 'warn'], under: ['Lower than budget', 'warn'], none: ['No budget row', ''] };
// pay rows: more pay than budgeted is good news, less needs a look
const PAY_LABEL = { ...LABEL, over: ['More than budget', 'good'], under: ['Less than budget', 'warn'] };
// monthly bills are totalled per calendar month (dated the 1st): show the month, not a day
const when = (b, d) => (b.per === 'month' ? fmtD(d, { month: 'short', year: 'numeric' }) : shortD(d));

async function render() {
  const { checkup: c } = await api('GET', '/checkup');
  if (!c.bills.length) { $('cuTable').replaceChildren(el('div', { class: 'empty', text: 'Upload bank exports and add payee rules to see the check-up.' })); }
  else {
    $('cuRange').textContent = c.transactions + ' bank transactions' + (c.since ? ', ' + shortD(c.since) + ' to ' + shortD(c.through) : '');
    $('cuTable').replaceChildren(table(['Bill', { t: 'Budget', r: true }, 'Recent', { t: 'Average', r: true }, { t: 'Diff', r: true }, 'Status'],
      c.bills.map((b) => {
        const labels = b.kind === 'pay' ? PAY_LABEL : LABEL;
        const [text, cls] = b.count < 3 && b.verdict !== 'ok' && b.budget != null ? ['Too few to tell', ''] : (labels[b.verdict] || [b.verdict, '']);
        return el('tr', {}, el('td', { text: b.name }), el('td', { class: 'r num', text: b.budget == null ? '—' : money(b.budget) }),
          el('td', { class: 'num', text: b.actuals.map(([d, v]) => when(b, d) + ' ' + money(v)).join(', ') }),
          el('td', { class: 'r num', text: money(b.average) + (b.per === 'month' ? '/mo' : '') }),
          el('td', { class: 'r num', text: b.diff_pct == null ? '' : (b.diff_pct > 0 ? '+' : '') + b.diff_pct.toFixed(0) + '%' }),
          el('td', {}, el('span', { class: 'chip ' + cls, text })));
      })));
  }
  // tracked payees: one row of bars per rule; each bar is labelled, so the numbers are there without the picture
  const tr = $('cuTracked'); tr.replaceChildren();
  if (!c.tracked.length) tr.append(el('div', { class: 'empty', text: 'Nothing tracked in the last 8 weeks.' }));
  for (const t of c.tracked) {
    const top = Math.max(1, ...t.weeks.map(([, v]) => v)), total = t.weeks.reduce((a, [, v]) => a + v, 0);
    tr.append(el('div', { class: 'tracked' }, el('div', { class: 'thead' }, el('b', { text: t.name }), el('span', { class: 'muted small num', text: money(total) + ' in 8 weeks' })),
      el('ol', { class: 'wbars', 'aria-label': t.name + ' per week' }, ...t.weeks.map(([w, v]) => {
        const bar = el('i', {}); bar.style.height = Math.round(100 * v / top) + '%';    // CSSOM: allowed by the CSP
        return el('li', { title: 'Week of ' + shortD(w) + ': ' + money(v) }, bar, el('span', { class: 'sr-only', text: 'Week of ' + shortD(w) + ': ' + money(v) }));
      }))));
  }
  const caps = $('cuCaps'); caps.replaceChildren();
  if (!c.cap_weeks.length) caps.append(el('div', { class: 'empty', text: 'No spending caps set.' + (can('editor') ? ' Add one above.' : '') }));
  for (const cap of c.cap_weeks) {
    caps.append(el('h3', { text: cap.name + ' (cap ' + money(cap.amount) + ' per ' + cap.period + ')' }),
      el('div', { class: 'list' }, ...cap.periods.slice().reverse().map(([p, v]) => el('div', { class: 'item' },
        el('span', { class: 'num', text: 'From ' + shortD(p) }), el('b', { class: 'num' + (v > cap.amount ? ' chip warn' : ''), text: money(v) })))));
  }
}
render().catch((e) => say(msg, e.message, 'err'));
loadSummary();

// spending caps: saved as soon as one is added or removed
let setup = null;
async function renderCaps() {
  setup = await api('GET', '/rules');
  const names = Object.fromEntries(setup.rules.map((r) => [r.id, r.name]));
  $('capList').replaceChildren(...setup.caps.map((c) => el('div', { class: 'item' },
    el('span', { text: c.name + ': ' + money(c.amount) + ' per ' + c.period + ' (' + c.rule_ids.map((r) => names[r] || r).join(', ') + ')' }),
    can('editor') ? el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: (ev) => {
      if (arm(ev.currentTarget, 'Remove cap?')) saveCaps(setup.caps.filter((x) => x !== c), ev.currentTarget);
    } }) : null)));
  $('capRules').replaceChildren(...setup.rules.map((r) => el('option', { value: r.id, text: r.name })));
}
async function saveCaps(caps, where) {
  try { await api('PUT', '/caps', { caps }); await Promise.all([renderCaps(), render()]); return true; }
  catch (e) { say(where && where.isConnected ? where : msg, e.message, 'err'); return false; }
}
$('capForm').hidden = !can('editor');
$('capForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const amount = toCents($('capAmount').value), ids = [...$('capRules').selectedOptions].map((o) => o.value);
  if (!$('capName').value.trim() || !(amount > 0) || !ids.length) { say(e.target, 'A cap needs a name, an amount and at least one rule.', 'err'); return; }
  const cap = { id: 'cap-' + Date.now(), name: $('capName').value.trim(), rule_ids: ids, period: $('capPeriod').value, amount };
  if (await busy(submitter(e), () => saveCaps([...setup.caps, cap], e.target))) { $('capName').value = ''; $('capAmount').value = ''; say(e.target, 'Cap added.', 'ok'); }
});
renderCaps().catch((e) => say(msg, e.message, 'err'));
