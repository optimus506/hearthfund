// More > API access (owner only): create, list and revoke the household's access tokens.
import { api, apiCode, $, el, say, busy, submitter, arm, table, page, longD } from './api.js';

const msg = $('msg');
const when = (iso) => (iso ? longD(iso.slice(0, 10)) : '—');

function state(t) {
  if (t.revoked_at) return el('span', { class: 'chip bad', text: 'revoked' });
  if (t.expires_at && new Date(t.expires_at) <= new Date()) return el('span', { class: 'chip warn', text: 'expired' });
  return el('span', { class: 'chip good', text: 'active' });
}

async function load() {
  const { tokens } = await api('GET', '/tokens');
  $('tokenList').replaceChildren(tokens.length ? el('div', { class: 'tablewrap' }, table(
    ['Name', 'Access', 'Ends in', 'Made', 'Expires', 'Last used', '', ''],
    tokens.map((t) => el('tr', {},
      el('td', { text: t.name }), el('td', { text: t.scope === 'write' ? 'Read and write' : 'Read only' }),
      el('td', { class: 'num', text: '…' + t.hint }),
      el('td', { text: when(t.created_at) + (t.creator ? ' by ' + t.creator : '') }),
      el('td', { text: t.expires_at ? when(t.expires_at) : 'never' }), el('td', { text: when(t.last_used_at) }),
      el('td', {}, state(t)),
      el('td', {}, t.revoked_at ? null : el('button', { class: 'btn danger', type: 'button', text: 'Revoke', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Revoke ' + t.name + '?')) apiCode('DELETE', '/tokens/' + t.id).then(load, (e) => say(msg, e.message, 'err'));
      } }))))))
    : el('div', { class: 'empty', text: 'No tokens yet.' }));
}

$('newForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const days = $('tDays').value;
  const body = { name: $('tName').value.trim(), scope: $('tScope').value, days: days ? Number(days) : null };
  try {
    const r = await busy(submitter(e), () => apiCode('POST', '/tokens', body));
    const base = page.base_url;
    $('secret').textContent = r.secret;
    $('claudeCmd').textContent = 'claude mcp add --transport http hearthfund ' + base + '/mcp \\\n  --header "Authorization: Bearer ' + r.secret + '"';
    $('curlCmd').textContent = 'curl -H "Authorization: Bearer ' + r.secret + '" ' + base + '/api/v1/summary';
    $('shown').hidden = false;
    $('tName').value = '';
    await load();
  } catch (err) { say(e.target, err.message, 'err'); }
});
$('copy').addEventListener('click', async (ev) => {
  try { await navigator.clipboard.writeText($('secret').textContent); say(ev.currentTarget, 'Copied.', 'ok'); }
  catch (e) { say(ev.currentTarget, 'Select the token and copy it by hand.', 'err'); }
});
load().catch((e) => say(msg, e.message, 'err'));
