// Members tab (owner only): who can see and change this household.
import { api, apiCode, $, el, say, busy, submitter, arm, table } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
const ROLES = ['owner', 'editor', 'contributor', 'viewer'];

async function load() {
  const { members } = await api('GET', '/members');
  $('memberList').replaceChildren(table(['User', 'Role', ''], members.map((m) => {
    const sel = el('select', { 'aria-label': 'Role for ' + m.username }, ...ROLES.map((r) => el('option', { value: r, text: r, selected: r === m.role })));
    sel.addEventListener('change', () => apiCode('PUT', '/members/' + m.user_id, { role: sel.value }).then(load, (e) => { say(msg, e.message, 'err'); load(); }));
    return el('tr', {}, el('td', { text: m.username }), el('td', {}, sel),
      el('td', {}, el('button', { class: 'btn danger', type: 'button', text: 'Remove', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Remove ' + m.username + '?')) api('DELETE', '/members/' + m.user_id).then(load, (e) => say(msg, e.message, 'err'));
      } })));
  })));
}
$('addForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = { username: $('mUser').value.trim(), role: $('mRole').value, password: $('mPass').value || null };
  try { await busy(submitter(e), () => apiCode('POST', '/members', body)); $('mUser').value = ''; $('mPass').value = ''; say(e.target, 'Added.', 'ok'); await load(); }
  catch (err) { say(e.target, err.message, 'err'); }
});
async function audit() {
  const { entries } = await api('GET', '/audit');
  $('auditList').replaceChildren(entries.length ? table(['When', 'Who', 'What', ''], entries.map((a) => el('tr', {},
    el('td', { class: 'num', text: a.at.replace('T', ' ').slice(0, 16) }), el('td', { text: (a.user || '—') + (a.via ? ' (' + a.via + ')' : '') }),
    el('td', { text: a.action + (a.row ? ' · ' + a.row : '') }), el('td', {}, a.imported ? el('span', { class: 'chip', text: 'restored' }) : null))))
    : el('div', { class: 'empty', text: 'No activity yet.' }));
}
load().then(audit).catch((e) => say(msg, e.message, 'err'));
loadSummary();
