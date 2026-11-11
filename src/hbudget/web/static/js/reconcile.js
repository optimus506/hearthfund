// Money > Uploads (bank and card uploads): upload a bank or card export, map unknown columns, preview matches, accept proposals, apply.
import { api, $, el, money, shortD, longD, say, busy, submitter, arm, table, can } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let current = null;     // upload id being previewed
let preview = null;

const txnLabel = (t) => shortD(t.posted) + ' ' + money(t.amount) + ' ' + t.description;

async function renderUploads() {
  const { uploads } = await api('GET', '/uploads');
  const box = $('uploads'); box.replaceChildren();
  if (!uploads.length) { box.append(el('div', { class: 'empty', text: 'No exports uploaded yet.' })); return; }
  for (const u of uploads) {
    const chip = { imported: ['Imported', ''], rejected: ['Not used', 'bad'], waiting: ['Waiting', 'warn'] }[u.status];
    box.append(el('div', { class: 'item' },
      el('div', { class: 'what' }, el('span', { text: (u.kind === 'card' ? 'Card · ' : 'Bank · ') + (u.filename || 'export') }),
        el('span', { class: 'muted small num', text: u.rows + ' rows' + (u.date_from ? ' · ' + shortD(u.date_from) + ' to ' + shortD(u.date_to) : '') +
          (u.latest_balance != null ? ' · balance ' + money(u.latest_balance) : '') + (u.note ? ' · ' + u.note : '') })),
      el('div', { class: 'row-actions' }, el('span', { class: 'chip ' + chip[1], text: chip[0] }),
        can('editor') && u.kind === 'bank' && u.status === 'waiting' ? el('button', { class: 'btn primary', type: 'button', text: 'Preview', onclick: () => doPreview(u.id) }) : null,
        // Hide keeps the postings in the history (#88); deleting them is a separate, spelled-out step for a wrong file
        can('editor') ? el('button', { class: 'btn', type: 'button', text: 'Hide', title: 'Take it off this list. Its postings stay in Report, Check-up, Recap and Subscriptions.', onclick: async (ev) => {
          if (!arm(ev.currentTarget, 'Hide it?')) return;
          try { await api('DELETE', '/uploads/' + encodeURIComponent(u.id)); await renderUploads(); } catch (e) { say(msg, e.message, 'err'); } } }) : null,
        can('editor') && u.status === 'waiting' ? el('button', { class: 'btn danger', type: 'button', text: 'Delete with postings…', onclick: (ev) => confirmDelete(u, ev.currentTarget) }) : null)));
  }
}

// a wrong file (another account, say): say exactly what goes before deleting it with its postings
async function confirmDelete(u, btn) {
  const item = btn.closest('.item');
  let r;
  try { r = await busy(btn, () => api('GET', '/uploads/' + encodeURIComponent(u.id) + '/removal')); } catch (e) { say(msg, e.message, 'err'); return; }
  item.querySelector('.confirm')?.remove();
  const n = r.bank_postings + r.card_purchases;
  const what = !r.can_delete ? 'This upload was used by a reconciliation, so it can only be hidden.'
    : n === 0 ? 'No postings would be removed: every one is also in another upload or cleared a bill.'
      : 'This removes ' + n + (n === 1 ? ' posting' : ' postings') + (r.first ? ', ' + longD(r.first) + ' to ' + longD(r.last) : '') +
        ', from Report, Check-up, Recap and Subscriptions. Only do this for a wrong file; Hide keeps them.';
  const box = el('div', { class: 'confirm msg', role: 'alert' }, el('p', { text: what }), el('div', { class: 'row-actions' },
    r.can_delete ? el('button', { class: 'btn danger', type: 'button', text: n ? 'Delete file and ' + n + ' postings' : 'Delete file', onclick: async (ev) => {
      try { await busy(ev.currentTarget, () => api('DELETE', '/uploads/' + encodeURIComponent(u.id) + '?postings=true')); await renderUploads(); }
      catch (e) { say(msg, e.message, 'err'); } } }) : null,
    el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => box.remove() })));
  item.append(box);
}

function mappingForm(up) {
  const box = $('mapping'); box.hidden = false; box.replaceChildren();
  const cols = up.header.filter(Boolean).map((h) => [h, h]);   // BankMapping columns are header names
  const pick = (id, label, val, optional) => {
    const s = el('select', { id }, optional ? el('option', { value: '', text: '(none)' }) : null, ...cols.map(([v, t]) => el('option', { value: v, text: t, selected: String(val) === v })));
    return el('div', { class: 'field' }, el('label', { for: id, text: label }), s);
  };
  const g = up.guess || {};
  box.append(el('h3', { text: 'Which column is which?' }),
    el('p', { class: 'muted small', text: 'This layout is new. Pick the columns once; save it as a profile to reuse it next time.' }),
    table(up.header.map((h) => h || '—'), up.sample.map((r) => el('tr', {}, ...r.map((c) => el('td', { text: c }))))),
    el('div', { class: 'form' }, pick('mDate', 'Date', g.date_col, false), pick('mDesc', 'Description', g.description_col, false),
      pick('mAmount', 'Amount (one column)', g.amount_col, true), pick('mDebit', 'Debit (money out)', g.debit_col, true),
      pick('mCredit', 'Credit (money in)', g.credit_col, true), pick('mBalance', 'Running balance', g.balance_col, true),
      pick('mId', 'Transaction ID (stops the same posting being stored twice)', g.id_col, true),
      el('div', { class: 'field' }, el('label', { for: 'mFmt', text: 'Date format' }), el('select', { id: 'mFmt' },
        ...['', '%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d', '%m/%d/%y'].map((f) => el('option', { value: f, text: f || 'Guess' })))),
      el('div', { class: 'field' }, el('label', { for: 'mNeg' }, el('input', { type: 'checkbox', id: 'mNeg' }), ' Money out is positive in this file')),
      el('div', { class: 'field' }, el('label', { for: 'mSave', text: 'Save as profile (optional)' }), el('input', { type: 'text', id: 'mSave', maxlength: 60 })),
      el('button', { class: 'btn primary', type: 'button', text: 'Use these columns', onclick: async (ev) => {
        const b = ev.currentTarget;
        const n = (id) => ($(id).value === '' ? null : $(id).value);
        const mapping = { date_col: n('mDate'), description_cols: [n('mDesc')], amount_col: n('mAmount'), debit_col: n('mDebit'), credit_col: n('mCredit'),
          balance_col: n('mBalance'), id_col: n('mId'), date_format: $('mFmt').value || null, negate: $('mNeg').checked };
        try {
          await busy(b, () => api('POST', '/uploads/' + encodeURIComponent(up.id) + '/mapping', { mapping, save_as: $('mSave').value.trim() || null }));
          box.hidden = true; say($('upForm'), ''); await renderUploads(); if (up.kind === 'bank') await doPreview(up.id);
        } catch (e) { say(b, e.message, 'err'); }
      } })));
}

$('upForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = $('upFile').files[0];
  const form = e.target;
  if (!f) { say(form, 'Choose the exported file first.', 'err'); return; }
  const fd = new FormData(); fd.append('file', f); fd.append('kind', $('upKind').value);
  if ($('upProfile').value) fd.append('profile_id', $('upProfile').value);
  try {
    const up = await busy(submitter(e), () => api('POST', '/uploads', fd), 'Uploading…');
    $('upFile').value = '';
    if (up.needs_mapping) { say(form, 'Map the columns below.'); mappingForm(up); return; }
    say(form, up.status === 'rejected' ? 'Not used: ' + up.note : 'Uploaded ' + up.rows + ' rows.', up.status === 'rejected' ? 'err' : 'ok');
    await renderUploads();
    if (up.kind === 'bank' && up.status === 'waiting') await doPreview(up.id);
  } catch (err) { say(form, err.message, 'err'); }
});

async function doPreview(id) {
  current = id;
  try { preview = await api('POST', '/reconcile/preview', { upload_id: id }); }
  catch (e) { say(msg, e.message, 'err'); await renderUploads(); return; }
  renderPreview();
}

function renderPreview() {
  const p = preview, box = $('preview'); box.hidden = false; box.replaceChildren();
  box.append(el('h2', { text: 'Dry run' }),
    el('p', { class: 'num', text: 'Starting point: ' + (p.basis ? txnLabel(p.basis) : 'the last reconciled date') + '. ' + p.new_count +
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
      pr.can_reduce ? el('option', { value: 'reduce', text: 'Reduce the row by the payment' }) : null);
    if (pr.group) choice.dataset.group = pr.group;
    // alternatives share one bank item: choosing one disables the others in its group
    choice.addEventListener('change', () => {
      for (const other of document.querySelectorAll('select[data-group]')) {
        if (pr.group && other !== choice && other.dataset.group === pr.group) { if (choice.value) other.value = ''; other.disabled = !!choice.value; }
      }
    });
    box.append(el('div', { class: 'item' },
      el('div', { class: 'what' }, el('b', { text: pr.id + ' · ' + (pr.txn ? txnLabel(pr.txn) : 'Month over') }),
        el('span', { class: 'small', text: pr.rows.map((r) => r.name + ' ' + shortD(r.due) + ' ' + money(r.amount)).join('; ') }),
        el('span', { class: 'muted small', text: pr.note + (pr.group ? ' · alternatives share a bank item; pick at most one' : '') })),
      choice));
  }
  const items = p.unknown.map((u) => el('tr', {}, el('td', { text: txnLabel(u.txn) }), el('td', { text: 'unmatched: ' + u.reason })))
    .concat(p.tracked.map((t) => el('tr', {}, el('td', { text: txnLabel(t.txn) }), el('td', { text: 'tracked: ' + t.rule }))));
  box.append(el('h3', { text: 'Bank items with no budget row' }), items.length ? table(['Transaction', 'Status'], items) : el('p', { class: 'muted', text: 'None.' }));
  box.append(el('div', { class: 'row-actions' },
    el('button', { class: 'btn primary', type: 'button', text: 'Apply', onclick: (ev) => apply(ev.currentTarget), disabled: !can('editor') }),
    el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => { box.hidden = true; } })));
}

async function apply(btn) {
  const accept = {};
  for (const pr of preview.proposals) { const v = $('acc-' + pr.id).value; if (v) accept[pr.id] = v; }
  try {
    const r = await busy(btn, () => api('POST', '/reconcile/apply', { upload_id: current, accept, fingerprint: preview.fingerprint }), 'Applying…');
    $('preview').replaceChildren(el('h2', { text: 'Applied' }), el('div', { class: 'pre', text: r.report_md }));
    say(msg, 'Reconciled. The checkbook balance is now ' + money(r.balance) + '.', 'ok');
    await Promise.all([renderUploads(), renderHistory()]); loadSummary();
  } catch (e) { say(btn.isConnected ? btn : msg, e.message, 'err'); if (e.status === 409) await doPreview(current); }
}

async function renderHistory() {
  const { reconciliations } = await api('GET', '/reconciliations');
  const box = $('history'); box.replaceChildren();
  if (!reconciliations.length) { box.append(el('div', { class: 'empty', text: 'No reconciliations yet.' })); return; }
  reconciliations.forEach((r, i) => {
    const det = el('details', {}, el('summary', { text: longD(r.through) + ' · balance ' + money(r.new_balance) }));
    if (i === 0 && can('editor')) {
      det.append(el('button', { class: 'btn', type: 'button', text: 'Undo this reconciliation', onclick: async (ev) => {
        if (!confirm('Reopen the rows it cleared, put back the balance before it and let its upload be reconciled again?')) return;
        try {
          await busy(ev.currentTarget, () => api('POST', '/reconciliations/' + encodeURIComponent(r.id) + '/undo'), 'Undoing…');
          say(msg, 'Reconciliation undone.', 'ok'); await Promise.all([renderUploads(), renderHistory()]); loadSummary();
        } catch (e) { say(msg, e.message, 'err'); }
      } }));
    }
    det.addEventListener('toggle', async () => {
      if (!det.open || det.dataset.loaded) return;
      const full = await api('GET', '/reconciliations/' + encodeURIComponent(r.id));
      det.dataset.loaded = '1'; det.append(el('div', { class: 'pre', text: full.report_md }));
    }, { once: false });
    box.append(det);
  });
}

async function profiles() {
  const { profiles: ps } = await api('GET', '/profiles?kind=' + $('upKind').value);
  $('upProfile').replaceChildren(el('option', { value: '', text: 'Detect automatically' }), ...ps.map((p) => el('option', { value: p.id, text: p.name + (p.builtin ? ' (built in)' : '') })));
}

if (!can('editor')) $('upForm').hidden = true;
else $('upKind').addEventListener('change', () => profiles().catch((e) => say(msg, e.message, 'err')));
Promise.all([renderUploads(), renderHistory(), can('editor') ? profiles() : null]).catch((e) => say(msg, e.message, 'err'));
loadSummary();
