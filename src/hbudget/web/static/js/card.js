// Card tab: card spending by week and category against the budget lines, and the merchant-keyword categories.
import { api, $, el, money, money0, shortD, say, busy, table, can } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let cats = [];
let win = 'all';
let storeCat = '';                       // '' = every category
try { win = localStorage.getItem('hb.cardwin') === 'recent' ? 'recent' : 'all'; } catch (e) { /* storage off */ }

function renderSummary(s) {
  const box = $('cardSpend');
  if (!s.weeks.length) { box.replaceChildren(el('div', { class: 'empty', text: win === 'recent' ? 'No card purchases in the last 2 months.' : 'Upload a card export on Money > Uploads (choose "Card") to fill this in.' })); return; }
  const over = Object.fromEntries(s.weeks_over);     // category -> finished weeks more than 10% over its budget
  const weeks = (n) => n + (n === 1 ? ' week' : ' weeks');
  const kpi = (k) => {
    const a = s.avg_per_week[k] || 0, b = s.budget_per_week[k], n = over[k];
    return el('div', { class: 'kpi' }, el('span', { class: 'eyebrow', text: k }), el('b', { class: 'num', text: money0(a) + '/wk' }),
      el('small', {}, b ? el('span', { class: 'chip ' + (a > b * 1.1 ? 'warn' : 'good'), text: 'budget ' + money0(b) + '/wk' }) : 'not budgeted'),
      n != null && b > 0 && s.weeks_averaged ? el('small', { text: n ? k + ' was more than 10% over budget in ' + n + ' of ' + weeks(s.weeks_averaged) + '.'
        : k + ' stayed within 10% of budget in all ' + weeks(s.weeks_averaged) + '.' }) : null);
  };
  const rows = s.weeks.slice().reverse().map((w) => el('tr', {}, el('td', { class: 'num', text: 'Week of ' + shortD(w.week) }),
    ...s.categories.map((k) => {
      const a = w.actual[k] || 0, b = w.budget[k];
      return el('td', { class: 'r num' }, money(a) + (b ? ' of ' + money0(b) : ''), b && a > b * 1.1 ? el('span', { class: 'chip warn', text: ' Over' }) : null);   // no budget that week: the amount alone (#45)
    })));
  const tot = s.by_category.reduce((a, x) => a + x[1], 0) || 1;
  box.replaceChildren(
    el('p', { class: 'muted small', text: 'Average per week, ' + shortD(s.since) + ' to ' + shortD(s.through) + ' (' + s.weeks_averaged + ' finished weeks; the table shows the last ' + s.weeks_shown + '). All card spending averages ' + money0(s.monthly_spend) + ' a month.' }),
    el('div', { class: 'kpis' }, ...s.categories.map(kpi)),
    table(['Last weeks', ...s.categories.map((k) => ({ t: k, r: true }))], rows),
    el('h3', { text: 'Where the card money went' }),
    el('div', { class: 'list' }, ...s.by_category.map(([n, v]) => el('div', { class: 'item' },
      el('button', { class: 'btn', type: 'button', text: n, title: 'Show the stores in ' + n, onclick: () => showStores(n) }),
      el('b', { class: 'num', text: money0(v) + ' · ' + Math.round(100 * v / tot) + '%' })))));
}

// ---- stores and single purchases: move them to another category (D-083)
const targets = () => [...cats.map((c) => c.name), 'Other'];

function picker(current, label, onpick, extra) {
  const sel = el('select', { 'aria-label': label, disabled: !can('editor') },
    ...(extra || []), ...targets().map((n) => el('option', { value: n, text: n, selected: n === current })));
  if (!targets().includes(current)) sel.prepend(el('option', { value: current, text: current, selected: true }));
  sel.addEventListener('change', async () => {
    sel.disabled = true;
    try { await onpick(sel.value, sel); } catch (e) { say(sel, e.message, 'err'); } finally { sel.disabled = !can('editor'); }
  });
  return sel;
}

function showStores(category) {
  storeCat = category; $('stCat').value = category;
  loadStores().catch((e) => say(msg, e.message, 'err'));
  $('h-st').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function refresh() {         // the totals above change with every move
  renderSummary((await api('GET', '/card?window=' + win)).summary);   // cats kept: unsaved category edits stay
}

async function purchasesOf(store, box) {
  const d = await api('GET', '/card/purchases?window=' + win + '&store=' + encodeURIComponent(store) + (storeCat ? '&category=' + encodeURIComponent(storeCat) : ''));
  const rows = d.purchases.map((p) => el('tr', {}, el('td', { class: 'num', text: shortD(p.purchased) }), el('td', { text: p.description }),
    el('td', { class: 'r num', text: money(p.amount) }),
    el('td', {}, picker(p.category, 'Category for the purchase of ' + money(p.amount) + ' on ' + shortD(p.purchased), async (v, sel) => {
      const r = await api('PUT', '/card/purchases/' + encodeURIComponent(p.id), { category: v === '' ? null : v });
      say(sel, r.by_hand ? 'Moved to ' + r.category + '.' : 'Sorted by the categories again: ' + r.category + '.', 'ok');
      await refresh();
    }, p.by_hand ? [el('option', { value: '', text: 'Sort automatically' })] : null),
    p.by_hand ? el('span', { class: 'chip', text: 'by hand' }) : null)));
  box.replaceChildren(rows.length ? table(['Date', 'Description', { t: 'Amount', r: true }, 'Category'], rows) : el('p', { class: 'muted small', text: 'No purchases.' }),
    d.more ? el('p', { class: 'muted small', text: d.more + ' older purchases are not shown.' }) : null);
}

async function loadStores() {
  const box = $('stores');
  const d = await api('GET', '/card/stores?window=' + win + (storeCat ? '&category=' + encodeURIComponent(storeCat) : ''));
  if (!d.stores.length) { box.replaceChildren(el('div', { class: 'empty', text: 'No card purchases here.' })); return; }
  const rows = [];
  for (const s of d.stores) {
    const sub = el('td', { colspan: 5, 'data-label': '' }); const subRow = el('tr', { hidden: true }, sub);
    const open = el('button', { class: 'btn', type: 'button', 'aria-expanded': 'false', text: s.purchases + (s.purchases === 1 ? ' purchase' : ' purchases') });
    open.addEventListener('click', async () => {
      const show = subRow.hidden; subRow.hidden = !show; open.setAttribute('aria-expanded', String(show));
      if (show) { sub.replaceChildren(el('p', { class: 'muted small', text: 'Loading…' })); await purchasesOf(s.store, sub).catch((e) => say(open, e.message, 'err')); }
    });
    rows.push(el('tr', {}, el('td', {}, el('b', { text: s.store }), s.moved ? el('span', { class: 'chip', text: ' moved' }) : null),
      el('td', {}, picker(s.category, 'Move ' + s.store + ' to', async (v, sel) => {
        const r = await api('PUT', '/card/stores', { store: s.store, category: v });
        say($('stMsg'), r.note || (s.store + ' moved to ' + r.category + (r.changed ? ' (' + r.changed + (r.changed === 1 ? ' purchase).' : ' purchases).') : '.')), r.note ? 'err' : 'ok');
        await refresh(); await loadStores();
      })),
      el('td', { class: 'r num', text: money(s.spent) }), el('td', { class: 'num', text: shortD(s.last) }), el('td', {}, open)), subRow);
  }
  box.replaceChildren(table(['Store', 'Category', { t: 'Spent', r: true }, 'Last', 'Purchases'], rows),
    d.more ? el('p', { class: 'muted small', text: d.more + ' smaller stores are not shown.' }) : null);
}

function fillStoreCats() {
  const sel = $('stCat');
  sel.replaceChildren(el('option', { value: '', text: 'All categories' }), ...targets().map((n) => el('option', { value: n, text: n })));
  sel.value = targets().includes(storeCat) ? storeCat : '';
  storeCat = sel.value;
}
$('stCat').addEventListener('change', () => { storeCat = $('stCat').value; loadStores().catch((e) => say(msg, e.message, 'err')); });

function renderCats() {
  const box = $('cats'); box.replaceChildren();
  cats.forEach((c, i) => {
    const kw = el('input', { type: 'text', value: c.keywords.join(', '), 'aria-label': 'Keywords for ' + c.name, disabled: !can('editor') });
    kw.addEventListener('change', () => { c.keywords = kw.value.split(',').map((x) => x.trim()).filter(Boolean); });
    box.append(el('div', { class: 'form' },
      el('div', { class: 'field' }, el('span', { class: 'label', text: 'Category' }), el('b', { text: c.name })),
      el('div', { class: 'field', style: null }, el('span', { class: 'label', text: 'Merchant keywords (comma separated)' }), kw),
      can('editor') ? el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: () => { cats.splice(i, 1); renderCats(); } }) : null));
  });
}
$('catAdd').addEventListener('submit', (e) => {
  e.preventDefault();
  const name = $('catName').value.trim();
  if (!name || cats.some((c) => c.name === name)) { say(e.target, 'Enter a new category name.', 'err'); return; }
  say(e.target, 'Added. Save the categories to keep it.', 'ok');
  cats.push({ name, keywords: $('catKw').value.split(',').map((x) => x.trim()).filter(Boolean), bank_categories: [], weekly_series: [], has_own_rows: false });
  $('catName').value = ''; $('catKw').value = ''; renderCats();
});
$('catSave').addEventListener('click', async (ev) => {
  const b = ev.currentTarget;
  try { await busy(b, () => api('PUT', '/card/categories', { categories: cats })); say(b, 'Categories saved. Existing card rows are re-sorted.', 'ok'); await load(); }
  catch (e) { say(b, e.message, 'err'); }
});
if (!can('editor')) { $('catAdd').hidden = true; $('catSave').hidden = true; }

document.querySelectorAll('[data-win]').forEach((b) => b.addEventListener('click', () => {
  win = b.dataset.win; try { localStorage.setItem('hb.cardwin', win); } catch (e) { /* storage off */ }
  load().catch((e) => say(msg, e.message, 'err'));
}));

async function load() {
  document.querySelectorAll('[data-win]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.win === win)));
  const d = await api('GET', '/card?window=' + win);
  cats = d.categories; renderSummary(d.summary); renderCats(); fillStoreCats();
  await loadStores();
}
load().catch((e) => say(msg, e.message, 'err'));
loadSummary();
