// Admin page: users and households on this server (admin only).
import { apiCode, $, el, say, busy, submitter, arm, table } from './api.js';

const msg = $('msg');
const A = (method, path, body) => apiCode(method, '/api/admin' + path, body, { absolute: true });

async function load() {
  const { users, households, audit } = await A('GET', '/overview');
  $('users').replaceChildren(table(['User', 'Admin', 'Status', ''], users.map((u) => el('tr', {},
    el('td', { text: u.username }), el('td', { text: u.is_admin ? 'yes' : '' }), el('td', { text: (u.disabled ? 'disabled' : 'active') + (u.two_factor ? ' · 2FA' : '') }),
    el('td', { class: 'actions' }, el('div', { class: 'row-actions' },
      el('button', { class: 'btn', type: 'button', text: u.disabled ? 'Enable' : 'Disable', onclick: () => A('POST', '/users/' + u.id + '/disabled', { disabled: !u.disabled }).then(load, (e) => say(msg, e.message, 'err')) }),
      el('button', { class: 'btn', type: 'button', text: 'Reset password', onclick: async () => {
        const p = prompt('New password for ' + u.username + ' (at least 10 characters)');
        if (!p) return;
        try { await A('POST', '/users/' + u.id + '/password', { password: p }); say(msg, 'Password reset; their sessions were signed out. Access tokens they made still work: if the account may have been misused, revoke them under More > API access in each household.', 'ok'); } catch (e) { say(msg, e.message, 'err'); }
      } }),
      u.two_factor ? el('button', { class: 'btn', type: 'button', text: 'Reset 2FA', onclick: (ev) => {
        if (arm(ev.currentTarget, 'Turn off 2FA for ' + u.username + '?')) A('POST', '/users/' + u.id + '/2fa/reset', {}).then(() => { say(msg, 'Two-factor sign-in is off for ' + u.username + '; their sessions were signed out.', 'ok'); return load(); }, (e) => say(msg, e.message, 'err'));
      } }) : null))))));
  $('households').replaceChildren(table(['Household', 'Members', ''], households.map((h) => el('tr', {},
    el('td', {}, el('a', { href: '/h/' + h.id, text: h.name })), el('td', { class: 'num', text: String(h.members) }),
    el('td', {}, h.is_member ? null : el('button', { class: 'btn', type: 'button', text: 'Add me as owner', onclick: () => A('POST', '/households/' + h.id + '/owner', {}).then(load, (e) => say(msg, e.message, 'err')) }))))));
  $('audit').replaceChildren(table(['When', 'Who', 'What', ''], audit.map((a) => el('tr', {},
    el('td', { class: 'num', text: a.at.replace('T', ' ').slice(0, 16) }), el('td', { text: a.user || '—' }),
    el('td', { text: a.action + (a.row ? ' · ' + a.row : '') }), el('td', {}, a.imported ? el('span', { class: 'chip', text: 'restored' }) : null)))));
}
$('userForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await busy(submitter(e), () => A('POST', '/users', { username: $('uName').value.trim(), password: $('uPass').value, is_admin: $('uAdmin').checked }));
    $('uName').value = ''; $('uPass').value = ''; say(e.target, 'User created.', 'ok'); await load();
  } catch (err) { say(e.target, err.message, 'err'); }
});
$('hhForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await busy(submitter(e), () => A('POST', '/households', { name: $('hName').value.trim(), demo: $('hDemo').checked }));
    $('hName').value = ''; say(e.target, 'Household created.', 'ok'); await load();
  } catch (err) { say(e.target, err.message, 'err'); }
});
// the System box: versions, uptime, database, backups and free disk
const bytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.round(n / 1e3) + ' kB');
const ago = (h) => (h < 48 ? h.toFixed(0) + ' hours ago' : (h / 24).toFixed(0) + ' days ago');
function uptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? d + ' d ' + h + ' h' : h ? h + ' h ' + Math.floor((s % 3600) / 60) + ' min' : Math.floor(s / 60) + ' min';
}
async function loadSystem() {
  const s = await A('GET', '/system');
  const b = s.backups, db = s.database;
  const row = (k, v, cls) => el('tr', {}, el('th', { scope: 'row', text: k }), el('td', { class: cls || '', text: v }));
  const backupText = !b.folder_exists ? 'No backups folder yet: set up the nightly backup (docs/DEPLOY.md, "Backups").'
    : b.newest ? b.newest.name + ', ' + ago(b.newest.age_hours) + ' (' + bytes(b.newest.size) + '); ' + b.scheduled + ' kept, ' + b.before_upgrade + ' from upgrades'
      : 'None yet. Set up the nightly backup (docs/DEPLOY.md, "Backups").';
  $('system').replaceChildren(
    b.stale ? el('div', { class: 'verdict warn' }, el('b', { text: b.newest ? 'The newest backup is more than a day and a half old.' : 'There is no scheduled backup yet.' }),
      el('span', { text: ' Check the backup timer, or run "hbudget backup".' })) : null,
    db.schema !== db.schema_latest ? el('div', { class: 'verdict bad' }, el('b', { text: 'The database is at schema ' + db.schema + ', the app expects ' + db.schema_latest + '.' })) : null,
    el('table', { class: 'kv' }, el('tbody', {},
      row('Version', s.app.version), row('Running for', uptime(s.app.uptime_seconds)),
      row('Python · SQLite', s.app.python + ' · ' + s.app.sqlite),
      row('Database', db.file + ', ' + bytes(db.size) + ', schema ' + db.schema + ' (' + db.journal_mode + ')', 'num'),
      row('Stored', db.rows.households + ' households, ' + db.rows.users + ' users, ' + db.rows.ledger_rows + ' budget rows, ' +
        db.rows.bank_txns + ' bank and ' + db.rows.card_txns + ' card transactions, ' + db.rows.uploads + ' uploads', 'num'),
      row('Last backup', backupText, 'num'),
      row('Free disk', bytes(s.disk.free) + ' of ' + bytes(s.disk.total), 'num'))));
}
load().catch((e) => say(msg, e.message, 'err'));
loadSystem().catch((e) => say(msg, e.message, 'err'));
