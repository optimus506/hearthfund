// Plan tab: what-if (kept in this browser only) and expected items (shared, in the forecast while open).
import { api, $, el, money, money0, longD, shortD, toCents, can, say, busy, submitter, isoD, table } from './api.js';
import { loadSummary } from './summary.js';
import { lineChart } from './chart.js';

const msg = $('msg');
let whatifs = [];
try { whatifs = JSON.parse(localStorage.getItem('hb.whatifs.' + location.pathname) || '[]'); } catch (e) { whatifs = []; }
const keep = () => { try { localStorage.setItem('hb.whatifs.' + location.pathname, JSON.stringify(whatifs)); } catch (e) { /* storage off */ } };

const today = new Date();
$('wiDate').value = isoD(new Date(today.getTime() + 7 * 86400000));
$('exDate').value = isoD(today);

async function renderWhatif() {
  const list = $('wiList'); list.replaceChildren();
  if (!whatifs.length) { $('wiResult').replaceChildren(); for (const id of ['wiChart', 'wiLegend', 'wiTableBox']) $(id).hidden = true; return; }
  const r = await api('POST', '/whatif', { items: whatifs.map(({ label, amount, direction, date }) => ({ label, amount, direction, date })) });
  const v = r.delta.verdict;
  const head = { no: 'No. The balance would go below $0.', tight: 'Tight. It stays above $0 but dips under the ' + money0(r.cushion) + ' cushion.',
    yes: 'Yes. The lowest balance stays above the ' + money0(r.cushion) + ' cushion.' }[v];
  $('wiResult').replaceChildren(el('div', { class: 'verdict ' + ({ no: 'bad', tight: 'warn', yes: 'good' }[v]) }, el('b', { text: head }),
    el('span', { class: 'num', text: 'Lowest balance ' + money(r.whatif.low) + ' on ' + longD(r.whatif.low_date) + ' (now ' + money(r.base.low) +
      (r.base.low_date ? ' on ' + longD(r.base.low_date) : '') + '). Year end ' + money(r.whatif.year_end ? r.whatif.year_end.balance : r.whatif.end) + '.' })));
  for (const w of whatifs) {
    list.append(el('div', { class: 'item' },
      el('div', { class: 'what' }, el('span', { text: w.label }), el('span', { class: 'muted num', text: (w.direction === 'in' ? 'Money in ' : 'Money out ') + money(w.amount) + ' on ' + longD(w.date) })),
      el('div', { class: 'row-actions' },
        can('editor') ? el('button', { class: 'btn', type: 'button', text: 'Add to expected', onclick: async (ev) => {
          const b = ev.currentTarget;
          try {
            await busy(b, () => api('POST', '/expected', { label: w.label, amount: w.amount, direction: w.direction, date: w.date }));
            whatifs = whatifs.filter((x) => x !== w); keep(); await refresh(); say($('exForm'), 'Added to expected items.', 'ok');
          } catch (e) { say(b, e.message, 'err'); } } }) : null,
        el('button', { class: 'btn', type: 'button', text: 'Remove', onclick: () => { whatifs = whatifs.filter((x) => x !== w); keep(); renderWhatif(); } }))));
  }
  for (const id of ['wiChart', 'wiLegend', 'wiTableBox']) $(id).hidden = false;
  lineChart($('wiChart'), [{ points: r.base.chart, name: 'Now', colour: '--accent' }, { points: r.whatif.chart, name: 'What-if', colour: '--whatif', dash: '6 3' }],
    { cushion: r.cushion, h: 200, label: 'Forecast now compared with the what-if' });
  // the chart as a table: every date either line changes on, with the balance of each after that date
  const at = (pts, d) => { let v = pts[0][1]; for (const p of pts) if (p[0] <= d) v = p[1]; return v; };
  const dates = [...new Set(r.base.chart.map((p) => p[0]).concat(r.whatif.chart.map((p) => p[0])))].sort();
  const t = table(['Date', { t: 'Now', r: true }, { t: 'What-if', r: true }], dates.map((d) => el('tr', {},
    el('td', { class: 'num', text: shortD(d) + '/' + d.slice(2, 4) }), el('td', { class: 'r num', text: money(at(r.base.chart, d)) }),
    el('td', { class: 'r num', text: money(at(r.whatif.chart, d)) }))));
  t.classList.add('tall');
  $('wiTable').replaceChildren(t);
}

$('wiForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const form = e.target, amount = toCents($('wiAmt').value), date = $('wiDate').value;
  if (!(amount > 0) || !date) { say(form, 'Enter an amount and a date.', 'err'); return; }
  whatifs.push({ id: String(Date.now()), label: $('wiWhat').value.trim() || 'What-if', amount, date, direction: $('wiDir').value });
  keep(); $('wiWhat').value = ''; $('wiAmt').value = '';
  busy(submitter(e), renderWhatif, 'Checking…').catch((err) => say(form, err.message, 'err'));
});

async function renderExpected() {
  const { items } = await api('GET', '/expected');
  const box = $('exList'); box.replaceChildren();
  const shown = items.slice().sort((a, b) => a.date.localeCompare(b.date));
  if (!shown.length) { box.append(el('div', { class: 'empty', text: 'No expected items.' })); return; }
  for (const x of shown) {
    const done = x.status === 'done';
    box.append(el('div', { class: 'item' },
      el('div', { class: 'what' }, el('span', { text: x.label + (done ? ' (posted)' : '') }),
        el('span', { class: 'muted num', text: (x.direction === 'in' ? 'Money in ' : 'Money out ') + money(x.amount) + ' · expected ' + longD(x.date) })),
      el('div', { class: 'row-actions' },
        el('button', { class: 'btn', type: 'button', text: done ? 'Mark open' : 'Posted', disabled: !can('contributor'),
          onclick: () => api('POST', '/expected/' + encodeURIComponent(x.id) + '/status', { status: done ? 'open' : 'done' }).then(refresh, (e) => say(msg, e.message, 'err')) }),
        can('editor') ? el('button', { class: 'btn danger', type: 'button', text: 'Delete',
          onclick: () => api('DELETE', '/expected/' + encodeURIComponent(x.id)).then(refresh, (e) => say(msg, e.message, 'err')) }) : null)));
  }
}

$('exForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target, amount = toCents($('exAmt').value), date = $('exDate').value, label = $('exWhat').value.trim();
  if (!label || !(amount > 0) || !date) { say(form, 'Enter what it is, the amount and the date.', 'err'); return; }
  try {
    await busy(submitter(e), () => api('POST', '/expected', { label, amount, date, direction: $('exDir').value }));
    $('exWhat').value = ''; $('exAmt').value = ''; say(form, 'Added.', 'ok'); await refresh();
  } catch (err) { say(form, err.message, 'err'); }
});
if (!can('editor')) $('exForm').hidden = true;

async function refresh() { await Promise.all([renderExpected(), renderWhatif()]); loadSummary(); }
window.addEventListener('themechange', () => renderWhatif());
refresh().catch((e) => say(msg, e.message, 'err'));
