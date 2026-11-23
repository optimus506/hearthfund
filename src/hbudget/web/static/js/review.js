// The reconcile review (dry run, decisions, Apply) in its own window: a modal <dialog>, full screen on a phone.
// openReview(id) opens it from any page; its address is #reconcile/<upload id>, so Back closes it and a reload
// reopens it. Closing it changes nothing: the upload stays waiting.
import { api, $, el, money, shortD, longD, say, busy, table, can, page } from './api.js';
import { loadSummary } from './summary.js';

const HASH = '#reconcile/';
let current = null;     // upload id under review
let preview = null;
let picks = {};         // bank line id -> { rows: Set of row ids the person says it paid, keep: the row that keeps the rest }
let opener = null;      // focus goes back here when the window closes
let pushed = false;     // opening added a history entry: closing goes back over it
let changed = null;     // the page's own refresh after Apply

export const txnLabel = (t) => shortD(t.posted) + ' ' + money(t.amount) + ' ' + t.description;

function dialog() {
  let d = $('recDialog');
  if (d) return d;
  d = el('dialog', { id: 'recDialog', class: 'sheet', 'aria-labelledby': 'recTitle' },
    el('div', { class: 'sheet-head' }, el('h2', { id: 'recTitle', tabindex: '-1', text: 'Dry run' }),
      el('button', { class: 'btn', type: 'button', text: 'Close', 'aria-label': 'Close the review', onclick: () => d.close() })),
    el('div', { id: 'preview', class: 'sheet-body', 'aria-live': 'polite' }));
  document.body.append(d);
  d.addEventListener('close', () => {
    document.documentElement.classList.remove('sheet-open');
    if (location.hash.startsWith(HASH)) {
      if (pushed) history.back(); else history.replaceState(history.state, '', location.pathname + location.search);
    }
    pushed = false;
    if (opener && opener.isConnected) opener.focus();
  });
  return d;
}

/** Open the review of a waiting bank upload. from: the control that opened it (focus returns there);
 * onChange: called after Apply so the page underneath can refresh. */
export async function openReview(id, { from, onChange } = {}) {
  const d = dialog();
  opener = from || document.activeElement; changed = onChange || null;
  const want = HASH + encodeURIComponent(id);
  if (location.hash !== want) { history.pushState(history.state, '', want); pushed = true; }
  $('recTitle').textContent = 'Dry run';
  $('preview').replaceChildren(el('p', { class: 'muted', text: 'Loading…' }));
  if (!d.open) d.showModal();
  document.documentElement.classList.add('sheet-open');
  $('recTitle').focus();
  await load(id);
}

/** A page that offers the review calls this once: #reconcile/<id> in the address (a reload, Back/Forward) opens it. */
export function reviewFromAddress(opts) {
  const sync = () => {
    const d = $('recDialog'), on = location.hash.startsWith(HASH);
    if (on && !(d && d.open)) openReview(decodeURIComponent(location.hash.slice(HASH.length)), opts);
    else if (!on && d && d.open) { pushed = false; d.close(); }
  };
  window.addEventListener('popstate', sync);
  sync();
}

async function load(id) {
  current = id;
  try { preview = await api('POST', '/reconcile/preview', { upload_id: id }); }
  catch (e) {
    $('preview').replaceChildren(el('p', { class: 'msg err', role: 'alert', text: e.status === 404
      ? 'That upload is not on the list any more: a newer sync may have replaced it.' : e.message }),
    el('div', { class: 'row-actions' }, el('button', { class: 'btn', type: 'button', text: 'Close', onclick: () => dialog().close() })));
    if (changed) await changed();
    return;
  }
  renderPreview();
}

// What the rows chosen for one bank line add up to. Fine when they total no more than the payment (what is left
// stays unbudgeted), or when one larger row keeps the rest: it takes what is left of the payment and stays open.
function pickState(line, cands) {
  const pk = picks[line.id], pay = -line.amount;
  const rows = cands.filter((r) => pk && pk.rows.has(r.id));
  const total = rows.reduce((n, r) => n + r.amount, 0), over = total - pay;
  const keepers = over > 0 ? rows.filter((r) => r.amount > over) : [];
  const keep = pk && keepers.find((r) => r.id === pk.keep) || null;
  return { rows, pay, total, over, keepers, keep, ok: !rows.length || over <= 0 || !!keep };
}

// "This paid…": tick the open rows a money-out bank line paid, with the running total against the payment
function picker(line, cands, preset) {
  picks[line.id] = { rows: new Set(preset || []), keep: '' };
  const pk = picks[line.id], status = el('p', { class: 'small num', role: 'status' });
  const keepSel = el('select', { 'aria-label': 'Row that keeps the rest, for ' + txnLabel(line) });
  const keepField = el('div', { class: 'field', hidden: true }, el('label', {}, 'Leave the rest on ', keepSel));
  const refresh = () => {
    const st = pickState(line, cands);
    keepSel.replaceChildren(el('option', { value: '', text: '(choose a row)' }),
      ...st.keepers.map((r) => el('option', { value: r.id, text: r.name + ' ' + shortD(r.due) + ' ' + money(r.amount), selected: st.keep === r })));
    keepField.hidden = st.over <= 0 || !st.keepers.length;
    status.className = 'small num' + (st.ok ? '' : ' bad');
    status.textContent = !st.rows.length ? 'Nothing chosen: the payment stays unbudgeted.'
      : st.over < 0 ? 'Chosen ' + money(st.total) + ' of the ' + money(st.pay) + ' payment. ' + money(-st.over) + ' is left over and stays unbudgeted.'
        : st.over === 0 ? 'Chosen ' + money(st.total) + ': the whole payment.'
          : st.keep ? 'Chosen ' + money(st.total) + ' for a ' + money(st.pay) + ' payment: ' + st.keep.name + ' stays open with ' + money(st.over) + ' left.'
            : 'Chosen ' + money(st.total) + ', ' + money(st.over) + ' more than the ' + money(st.pay) + ' payment. Take a row off' +
              (st.keepers.length ? ', or pick the row that keeps the rest.' : '.');
    // a row pays one bank line: ticked here, it cannot be ticked for another line
    for (const c of document.querySelectorAll('input[data-pick-row]')) {
      const other = Object.entries(picks).some(([tid, x]) => tid !== c.dataset.pickLine && x.rows.has(c.dataset.pickRow));
      c.disabled = other && !c.checked;
    }
    checkApply();
  };
  keepSel.addEventListener('change', () => { pk.keep = keepSel.value; refresh(); });
  const list = el('div', { class: 'picklist' }, ...cands.map((r) => {
    const c = el('input', { type: 'checkbox', 'data-pick-row': r.id, 'data-pick-line': line.id, checked: pk.rows.has(r.id) });
    c.addEventListener('change', () => { if (c.checked) pk.rows.add(r.id); else pk.rows.delete(r.id); refresh(); });
    return el('label', { class: 'num' }, c, ' ' + r.name + ' ' + shortD(r.due) + ' ' + money(r.amount), r.card ? el('span', { class: 'muted small', text: ' · ' + r.card }) : null);
  }));
  const box = el('div', { class: 'picker' }, list, keepField, status);
  box.refresh = refresh;
  return box;
}

// Apply waits while the rows chosen for a line total more than its payment
function checkApply() {
  const btn = $('recApply'); if (!btn) return;
  const lines = preview.unknown.concat(preview.proposals).filter((x) => x.txn && picks[x.txn.id]);
  const bad = lines.find((x) => !pickState(x.txn, x.candidates).ok);
  btn.disabled = !can('editor') || !!bad;
  $('recApplyNote').textContent = bad ? 'The rows chosen for ' + txnLabel(bad.txn) + ' total more than the payment.' : '';
}

function renderPreview() {
  const p = preview, box = $('preview'); box.replaceChildren();
  picks = {};
  $('recTitle').textContent = 'Dry run';
  box.append(el('p', { class: 'num', text: 'Starting point: ' + (p.basis ? txnLabel(p.basis) : 'the last reconciled date') + '. ' + p.new_count +
      ' new postings through ' + longD(p.through) + '. New checkbook balance ' + money(p.latest_balance) + '.' }));
  if (p.basis_ambiguous) box.append(el('p', { class: 'msg', text: 'That balance appears more than once in the export; the newest was used.' }));
  box.append(el('h3', { text: 'Cleared automatically' }), p.auto.length ? table(['Budget row', { t: 'Budget', r: true }, 'Bank'],
    p.auto.map((a) => el('tr', {}, el('td', { text: a.row.name + ' ' + shortD(a.row.due) }), el('td', { class: 'r num', text: a.amount == null ? money(a.row.amount) : money(a.amount) + ' of ' + money(a.row.amount) }), el('td', { text: txnLabel(a.txn) })))) : el('p', { class: 'muted', text: 'None.' }));
  box.append(el('h3', { text: 'Needs your decision' }));
  if (!p.proposals.length) box.append(el('p', { class: 'muted', text: 'None.' }));
  for (const pr of p.proposals) {
    const id = 'acc-' + pr.id;
    const choice = el('select', { id, 'aria-label': 'Decision for ' + pr.id },
      el('option', { value: '', text: 'Leave it' }), el('option', { value: 'clear', text: pr.kind === 'leftover' ? 'Release the leftover' : 'Clear the row' + (pr.rows.length > 1 ? 's' : '') }),
      pr.can_reduce ? el('option', { value: 'reduce', text: 'Reduce the row by the payment' }) : null,
      // the proposal is wrong or incomplete: the person ticks the rows the payment really paid
      pr.txn && pr.txn.amount < 0 && pr.candidates.length ? el('option', { value: 'pick', text: 'Choose the rows it paid…' }) : null);
    if (pr.group) choice.dataset.group = pr.group;
    const slot = el('div', { class: 'pickslot', hidden: true });
    // alternatives share one bank item: choosing one disables the others in its group
    choice.addEventListener('change', () => {
      for (const other of document.querySelectorAll('select[data-group]')) {
        if (pr.group && other !== choice && other.dataset.group === pr.group) { if (choice.value) other.value = ''; other.disabled = !!choice.value; }
      }
      slot.hidden = choice.value !== 'pick'; slot.replaceChildren();
      if (pr.txn) delete picks[pr.txn.id];
      if (choice.value === 'pick') { const pk = picker(pr.txn, pr.candidates, pr.rows.map((r) => r.id)); slot.append(pk); pk.refresh(); }
      else { document.querySelector('.picker')?.refresh(); checkApply(); }
    });
    box.append(el('div', { class: 'item' },
      el('div', { class: 'what' }, el('b', { text: pr.id + ' · ' + (pr.txn ? txnLabel(pr.txn) : 'Month over') }),
        el('span', { class: 'small', text: pr.rows.map((r) => r.name + ' ' + shortD(r.due) + ' ' + money(r.amount)).join('; ') }),
        el('span', { class: 'muted small', text: pr.note + (pr.group ? ' · alternatives share a bank item; pick at most one' : '') }),
        slot),
      choice));
  }
  box.append(el('h3', { text: 'Bank items with no budget row' }));
  // an unmatched money-out line with open rows near it: "This paid…" lets the person say which rows it paid
  const open = p.unknown.filter((u) => can('editor') && u.txn.amount < 0 && u.candidates.length);
  for (const u of open) {
    const pk = picker(u.txn, u.candidates);
    box.append(el('div', { class: 'item' }, el('div', { class: 'what' }, el('b', { text: txnLabel(u.txn) }),
      el('span', { class: 'muted small', text: 'unmatched: ' + u.reason }),
      el('details', { class: 'pickslot' }, el('summary', { text: 'This paid…' }),
        el('p', { class: 'muted small', text: 'Tick the open rows this payment paid. Apply clears them with it; Undo reopens them.' }), pk))));
    pk.refresh();
  }
  const items = p.unknown.filter((u) => !open.includes(u)).map((u) => el('tr', {}, el('td', { text: txnLabel(u.txn) }), el('td', { text: 'unmatched: ' + u.reason })))
    .concat(p.tracked.map((t) => el('tr', {}, el('td', { text: txnLabel(t.txn) }), el('td', { text: 'tracked: ' + t.rule }))));
  box.append(items.length ? table(['Transaction', 'Status'], items) : open.length ? null : el('p', { class: 'muted', text: 'None.' }));
  box.append(el('div', { class: 'row-actions' },
    el('button', { class: 'btn primary', type: 'button', id: 'recApply', text: 'Apply', onclick: (ev) => apply(ev.currentTarget), disabled: !can('editor') }),
    el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => dialog().close() })),
  el('p', { class: 'small bad', id: 'recApplyNote', role: 'status' }));
}

async function apply(btn) {
  const accept = {};
  for (const pr of preview.proposals) { const v = $('acc-' + pr.id).value; if (v && v !== 'pick') accept[pr.id] = v; }
  const assign = {};
  for (const [tid, pk] of Object.entries(picks)) {
    if (pk.rows.size) assign[tid] = { clear: [...pk.rows].filter((id) => id !== pk.keep), reduce: pk.keep || null };
  }
  try {
    const r = await busy(btn, () => api('POST', '/reconcile/apply', { upload_id: current, accept, assign, fingerprint: preview.fingerprint }), 'Applying…');
    $('recTitle').textContent = 'Applied';
    const done = 'Reconciled. The checkbook balance is now ' + money(r.balance) + '.';
    $('preview').replaceChildren(el('p', { class: 'msg ok', role: 'status', text: done }), el('div', { class: 'pre', text: r.report_md }),
      el('div', { class: 'row-actions' }, el('a', { class: 'btn primary', href: '/h/' + encodeURIComponent(page.hid) + '/bills', text: 'Go to Bills' }),
        el('button', { class: 'btn', type: 'button', text: 'Close', onclick: () => dialog().close() })));
    $('recTitle').focus();
    say($('msg'), done, 'ok');
    loadSummary();
    if (changed) await changed();
  } catch (e) { say(btn.isConnected ? btn : $('msg'), e.message, 'err'); if (e.status === 409) await load(current); }
}
