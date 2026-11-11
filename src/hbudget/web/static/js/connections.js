// Bank connection (docs/PLAID.md): turn Plaid on or off for the household, link a bank through Plaid Link, choose
// what each account is for, sync. Also finishes Link when an OAuth bank sends the browser back to /plaid/oauth.
import { $, api, arm, busy, can, el, fmtD, money, page, say } from './api.js';

const KEY = 'hb-plaid-link';     // sessionStorage: the Link token and household while the bank's site is open
const ROLE_TEXT = { bank: 'Checking: reconcile with the budget', card: 'Card: add to the Card page', ignore: 'Not used' };
const box = $('conn');

const keep = (v) => { try { sessionStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private mode: OAuth banks then need a retry */ } };
const kept = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; } };
const drop = () => { try { sessionStorage.removeItem(KEY); } catch { /* nothing kept */ } };
const at = (iso) => (iso ? fmtD(iso.slice(0, 10), { month: 'short', day: 'numeric', year: 'numeric' }) : 'never');

// Plaid's own script is fetched only when someone starts Link, so opening this page (or having Plaid off) loads
// nothing from outside the server
let plaidLoading = null;
function loadPlaid() {
  if (window.Plaid) return Promise.resolve();
  if (!plaidLoading) {
    plaidLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = page.plaid_script;
      s.onload = () => resolve();
      s.onerror = () => { plaidLoading = null; reject(new Error('Plaid Link did not load. Check the connection and try again.')); };
      document.head.append(s);
    });
  }
  return plaidLoading;
}

// Plaid Link: a new bank (item_id empty) or signing in again to one already linked
async function openLink(hid, token, itemId, receivedRedirectUri) {
  try { await loadPlaid(); } catch (e) { say($('msg'), e.message, 'err'); return; }
  if (!window.Plaid) { say($('msg'), 'Plaid Link did not load. Check the connection and reload the page.', 'err'); return; }
  const handler = window.Plaid.create({
    token,
    receivedRedirectUri,
    onSuccess: async (publicToken, meta) => {
      const path = '/api/h/' + encodeURIComponent(hid) + '/plaid';
      try {
        if (itemId) await api('POST', path + '/items/' + encodeURIComponent(itemId) + '/relinked', {}, { absolute: true });
        else await api('POST', path + '/exchange', { public_token: publicToken, institution: (meta && meta.institution && meta.institution.name) || '' }, { absolute: true });
        drop();
        if (receivedRedirectUri) { location.replace('/h/' + encodeURIComponent(hid) + '/connections'); return; }
        say($('msg'), itemId ? 'Signed in again.' : 'Bank linked. Check what each account is for, then sync.', 'ok');
        await render();
      } catch (e) { say($('msg'), e.message, 'err'); }
    },
    onExit: (err) => {
      if (err) say($('msg'), (err.display_message || err.error_message || 'Linking stopped.'), 'err');
      if (receivedRedirectUri) { const k = kept(); drop(); location.replace(k ? '/h/' + encodeURIComponent(k.hid) + '/connections' : '/'); }
    },
  });
  handler.open();
}

async function startLink(btn, itemId) {
  const days = Number(($('connDays') || {}).value || 90);
  try {
    const r = await busy(btn, () => api('POST', '/plaid/link-token', itemId ? { item_id: itemId } : { days }), 'Opening…');
    keep({ hid: page.hid, token: r.link_token, item_id: itemId || null });
    openLink(page.hid, r.link_token, itemId, undefined);
  } catch (e) { say(btn, e.message, 'err'); }
}

function syncResult(r) {
  const parts = [];
  parts.push(r.added ? r.added + ' new posting' + (r.added === 1 ? '' : 's') : 'Nothing new');
  if (r.pending) parts.push(r.pending + ' pending left out until they post');
  if (r.changed) parts.push(r.changed + ' changed by the bank');
  if (r.removed) parts.push(r.removed + ' withdrawn by the bank');
  if (r.not_ready) parts.push('Plaid is still fetching your history; try again in a few minutes');
  for (const p of r.problems) parts.push((p.institution || 'Bank') + ': ' + p.message);
  return parts.join(' · ') + '.';
}

function accountRow(a) {
  const label = a.name + (a.mask ? ' ··' + a.mask : '');
  const what = el('span', { class: 'what' }, el('b', { text: label }),
    el('span', { class: 'small muted', text: [a.subtype || a.type, a.current != null ? 'balance ' + money(a.current) : ''].filter(Boolean).join(' · ') }));
  if (!can('owner')) return el('li', { class: 'item' }, what, el('span', { class: 'small', text: ROLE_TEXT[a.role] }));
  const sel = el('select', { 'aria-label': 'What ' + label + ' is for' },
    ...Object.entries(ROLE_TEXT).map(([v, t]) => el('option', { value: v, text: t, selected: v === a.role })));
  sel.addEventListener('change', async () => {
    try { await api('POST', '/plaid/accounts/' + encodeURIComponent(a.account_id) + '/role', { role: sel.value }); await render(); say($('msg'), 'Saved.', 'ok'); }
    catch (e) { say(sel, e.message, 'err'); }
  });
  return el('li', { class: 'item' }, what, sel);
}

function itemCard(it) {
  const owner = can('owner');
  const unlink = owner ? el('button', { class: 'btn', type: 'button', text: 'Unlink', onclick: async (ev) => {
    const b = ev.currentTarget;
    if (!arm(b, 'Unlink ' + (it.institution || 'this bank') + '?')) return;
    try { const r = await busy(b, () => api('DELETE', '/plaid/items/' + encodeURIComponent(it.item_id)), 'Unlinking…'); await render(); say($('msg'), r.note || 'Unlinked. The postings already brought in stay in the history.', r.note ? 'err' : 'ok'); }
    catch (e) { say(b, e.message, 'err'); }
  } }) : null;
  const relink = owner && it.last_error ? el('button', { class: 'btn', type: 'button', text: 'Sign in again', onclick: (ev) => startLink(ev.currentTarget, it.item_id) }) : null;
  return el('div', { class: 'card inner' },
    el('div', { class: 'section-head' }, el('h3', { text: it.institution || 'Linked bank' }),
      el('span', { class: 'muted small', text: 'Linked ' + at(it.linked_at) + ' · last sync ' + at(it.last_sync_at) })),
    it.last_error ? el('p', { class: 'msg err', text: it.last_error }) : null,
    el('ul', { class: 'list plain' }, ...it.accounts.map(accountRow)),
    el('div', { class: 'row-actions' }, relink, unlink));
}

// the optional daily sync: it only stores what the bank sent; the check-in turns it into one upload to review
function autoSyncField(s, owner) {
  const help = el('p', { class: 'small muted', id: 'connAutoHelp', text: 'Daily: the app asks Plaid once a day and keeps what it sends. Nothing changes in the budget until someone brings it in from the check-in, as one upload however many days it covers.' });
  if (!owner) return el('div', { class: 'field' }, el('b', { text: 'Daily sync: ' + (s.auto_sync === 'daily' ? 'on' : 'off') }), help);
  const sel = el('select', { id: 'connAuto', 'aria-describedby': 'connAutoHelp' },
    el('option', { value: 'off', text: 'Off: only when someone syncs', selected: s.auto_sync !== 'daily' }),
    el('option', { value: 'daily', text: 'Daily', selected: s.auto_sync === 'daily' }));
  sel.addEventListener('change', async () => {
    try { await api('POST', '/plaid/auto-sync', { value: sel.value }); await render(); say($('msg'), 'Saved.', 'ok'); }
    catch (e) { say(sel, e.message, 'err'); }
  });
  return el('div', { class: 'field' }, el('label', { for: 'connAuto', text: 'Sync automatically' }), sel, help);
}

async function render() {
  let s;
  try { s = await api('GET', '/plaid'); } catch (e) { box.replaceChildren(el('p', { class: 'msg err', text: e.message })); return; }
  $('connServer').textContent = s.server === 'sandbox' ? 'Plaid sandbox (test banks only)' : 'Plaid';
  const owner = can('owner');
  if (!s.enabled) {
    const confirm = el('input', { id: 'connConfirm', type: 'text', autocomplete: 'off', maxlength: '200' });
    const on = el('button', { class: 'btn primary', type: 'submit', text: 'Turn on the bank connection' });
    const form = el('form', { class: 'form' },
      el('div', { class: 'field' }, el('label', { for: 'connConfirm', text: 'Type the household name (' + s.household_name + ') to confirm' }), confirm),
      el('div', { class: 'field' }, on));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      try { await busy(on, () => api('POST', '/plaid/enable', { confirm: confirm.value })); await render(); }
      catch (e) { say(on, e.message, 'err'); }
    });
    box.replaceChildren(
      el('p', { text: 'Off. This household brings in bank and card transactions by uploading files, and the app makes no connection to anything outside this server for it.' }),
      el('p', { class: 'small', text: 'Turned on, the app can fetch your checking and card transactions through Plaid instead. You sign in to your bank on Plaid\'s page (or your bank\'s own page); the app never sees your bank password. Plaid and your bank will know this app is connected. Pending transactions are left out until they post. Each sync is reviewed in the weekly check-in like an uploaded file.' }),
      owner ? form : el('p', { class: 'muted small', text: 'An owner of the household can turn it on.' }));
    return;
  }
  const kids = [];
  if (!s.oauth) kids.push(el('p', { class: 'msg', text: 'Banks that sign you in on their own website need this server to be reached over HTTPS: set HBUDGET_PUBLIC_URL to the https address and register ' + 'that address + /plaid/oauth in the Plaid dashboard.' }));
  if (!s.items.length) kids.push(el('p', { class: 'muted', text: 'No bank linked yet.' }));
  kids.push(...s.items.map(itemCard));
  const actions = el('div', { class: 'row-actions' });
  if (s.items.length && can('editor')) {
    actions.append(el('button', { class: 'btn primary', type: 'button', text: 'Sync now', onclick: async (ev) => {
      const b = ev.currentTarget;
      try {
        const r = await busy(b, () => api('POST', '/plaid/sync', {}), 'Syncing…');
        await render();
        say($('msg'), syncResult(r) + (r.bank ? ' A bank upload is waiting: review it on the Bank page or in the check-in.' : ''), r.problems.length ? 'err' : 'ok');
      } catch (e) { say(b, e.message, 'err'); }
    } }));
  }
  if (s.items.length) kids.push(autoSyncField(s, owner));
  if (owner) {
    kids.push(el('div', { class: 'field' }, el('label', { for: 'connDays', text: 'When linking, bring in history from the last' }),
      el('select', { id: 'connDays' }, ...s.days_choices.map((d) => el('option', { value: String(d), text: d + ' days', selected: d === 90 })))));
    actions.append(el('button', { class: 'btn', type: 'button', text: s.items.length ? 'Link another bank' : 'Link a bank', onclick: (ev) => startLink(ev.currentTarget, null) }));
    actions.append(el('button', { class: 'btn', type: 'button', text: 'Turn off', onclick: async (ev) => {
      const b = ev.currentTarget;
      if (!arm(b, 'Turn off and unlink every bank?')) return;
      try { const r = await busy(b, () => api('POST', '/plaid/disable', {}), 'Turning off…'); await render(); say($('msg'), r.note || 'Turned off. Transactions already brought in stay in the history.', r.note ? 'err' : 'ok'); }
      catch (e) { say(b, e.message, 'err'); }
    } }));
  }
  kids.push(actions);
  box.replaceChildren(...kids);
}

if (page.oauth) {
  // back from the bank's own sign-in page: finish Link with the token kept before leaving
  const k = kept();
  if (!k) box.replaceChildren(el('p', { class: 'msg err', text: 'The bank sign-in could not be finished in this tab. Start again from the Bank connection page.' }));
  else { box.replaceChildren(el('p', { class: 'muted', text: 'Finishing the bank sign-in…' })); openLink(k.hid, k.token, k.item_id, location.href); }
} else {
  render();
}
