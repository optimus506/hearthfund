// Report tab: where the money goes (Sankey), Actual (bank history) or Budget (next 12 months of rows).
import { api, $, el, money0, say, swatch, can, arm, busy, submitter } from './api.js';
import { loadSummary } from './summary.js';
import { sankey, sourceColour } from './sankey.js';

const msg = $('msg');
// narrow screens: start with the chart folded (it scrolls sideways there) and say so (#48)
if ($('flowBox').clientWidth < 600) { $('flowBox').open = false; $('flowToggle').textContent = 'Show the chart (scroll it sideways)'; }
let view = 'actual';
try { const v = localStorage.getItem('hb.flow'); view = ['planned', 'recent'].includes(v) ? v : 'actual'; } catch (e) { /* storage off */ }

async function render() {
  document.querySelectorAll('[data-flow]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.flow === view)));
  const { flow } = await api('GET', '/flow?view=' + view);
  sankey($('flow'), flow);
  $('flowNote').textContent = flow.note || '';
  const months = flow.months || 12;
  const t = flow.totals.per_month;
  const T = flow.totals, big = T.biggest;
  const pct = (n) => (n == null ? '' : n + '%');
  const kpi = (l, v, s) => el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: l }), el('b', { class: 'num', text: v }), el('small', { text: s }));
  $('flowKpis').replaceChildren(el('div', { class: 'kpis' },
    kpi('Pay', money0(t.pay) + '/mo', 'of ' + money0(t.money_in) + '/mo money in'),
    kpi('Biggest group', big ? big.name : '—', big ? money0(big.per_month) + '/mo · ' + pct(big.pct) + ' of spending' : 'no spending'),
    kpi('Pay covers', T.pay_covers_pct == null ? '—' : pct(T.pay_covers_pct), 'of ' + money0(t.money_out) + '/mo spending'),
    t.short > 0 ? kpi('Short', money0(t.short) + '/mo', 'taken from the balance') : kpi('Left over', money0(t.left_over) + '/mo', 'stays in checking')));
  const list = el('div', {});
  const line = (colour, name, cents, share, of) => el('span', { class: 'bdline' }, swatch(colour), el('span', { text: name }),
    el('span', { class: 'num', text: money0(cents / months) + '/mo' + (share == null ? '' : ' · ' + share + '% of ' + of) }));
  list.append(el('details', {}, el('summary', { text: 'Money in · ' + money0(t.money_in) + '/mo' }),
    ...flow.sources.map((s) => el('div', { class: 'item' }, line(sourceColour(s.name), s.name, s.amount, s.pct, 'money in')))));
  for (const g of flow.groups) {
    list.append(el('details', {}, el('summary', {}, line(g.colour, g.name, g.total, g.pct, 'spending')),
      ...g.items.map((x) => el('div', { class: 'item' }, el('span', { text: x.name }), el('span', { class: 'num', text: money0(x.amount / months) })))));
  }
  list.append(T.short > 0 ? el('div', { class: 'item' }, line('--f-savings', 'Short: taken from the balance', T.short, null))
    : el('div', { class: 'item' }, line('--f-in', 'Left over', T.left_over, T.left_over_pct, 'money in')));
  $('flowList').replaceChildren(list);
  if (!flow.complete && view !== 'planned') say(msg, 'The bank history is shorter than this view: the figures are monthly averages over the ' + months + (months === 1 ? ' full month' : ' full months') + ' it covers.', '');
  else say(msg, '');
}

document.querySelectorAll('[data-flow]').forEach((b) => b.addEventListener('click', () => {
  view = b.dataset.flow; try { localStorage.setItem('hb.flow', view); } catch (e) { /* storage off */ }
  render().catch((e) => say(msg, e.message, 'err'));
}));
window.addEventListener('themechange', () => render());

// report groups: saved as soon as one is added or removed
let setup = null;
async function renderGroups() {
  setup = await api('GET', '/rules');
  const box = $('flowGroups'); box.replaceChildren();
  if (!setup.flow_groups.length) box.append(el('div', { class: 'empty', text: 'No groups yet: each payee and line is shown on its own.' }));
  for (const g of setup.flow_groups) {
    box.append(el('div', { class: 'item' }, el('span', {}, swatch(g.colour), ' ' + g.name + ' · ' + [...g.rule_ids, ...g.series_ids, ...g.row_names].length + ' members'),
      can('editor') ? el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Remove group?')) saveGroups(setup.flow_groups.filter((x) => x !== g), ev.currentTarget);
      } }) : null));
  }
  $('fgMembers').replaceChildren(...setup.rules.map((r) => el('option', { value: 'r:' + r.id, text: 'Rule: ' + r.name })),
    ...setup.series.map((s) => el('option', { value: 's:' + s.id, text: 'Line: ' + s.name })));
}
async function saveGroups(groups, where) {
  try { await api('PUT', '/flow-groups', { groups }); await Promise.all([renderGroups(), render()]); return true; }
  catch (e) { say(where && where.isConnected ? where : msg, e.message, 'err'); return false; }
}
$('fgForm').hidden = !can('editor');
$('fgForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = $('fgName').value.trim(), sel = [...$('fgMembers').selectedOptions].map((o) => o.value);
  if (!name) { say(e.target, 'Give the group a name.', 'err'); return; }
  const g = { name, colour: $('fgColour').value, rule_ids: sel.filter((v) => v.startsWith('r:')).map((v) => v.slice(2)),
    series_ids: sel.filter((v) => v.startsWith('s:')).map((v) => v.slice(2)), row_names: [] };
  if (await busy(submitter(e), () => saveGroups([...setup.flow_groups, g], e.target))) { $('fgName').value = ''; say(e.target, 'Group added.', 'ok'); }
});
renderGroups().catch((e) => say(msg, e.message, 'err'));
render().catch((e) => say(msg, e.message, 'err'));
loadSummary();
