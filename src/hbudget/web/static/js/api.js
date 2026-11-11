// Shared helpers: page data, JSON API calls with the CSRF header, DOM building with textContent only.

const dataEl = document.getElementById('page-data');
export const page = dataEl ? JSON.parse(dataEl.textContent) : {};
export const $ = (id) => document.getElementById(id);
export const base = page.hid ? '/api/h/' + encodeURIComponent(page.hid) : '';

export class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

export async function api(method, path, body, opts = {}) {
  const init = { method, headers: { 'X-CSRF-Token': page.csrf || '', Accept: 'application/json' }, credentials: 'same-origin' };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  Object.assign(init.headers, opts.headers || {});
  const url = (opts.absolute ? '' : base) + path;
  const res = await fetch(url, init);
  if (res.status === 401) { location.href = '/login'; throw new ApiError(401, 'auth', 'Signed out'); }
  const isJson = (res.headers.get('content-type') || '').includes('application/json');
  const data = isJson ? await res.json() : null;
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || 'error', (data && data.message) || ('Request failed (' + res.status + ')'));
  return data;
}

// With two-factor sign-in on, changes that open a way in (a token, a member, a reset) need a current code
// (D-095): the server says so, we ask and try once more.
export async function apiCode(method, path, body, opts = {}) {
  try { return await api(method, path, body, opts); } catch (e) {
    if (e.code !== 'two_factor') throw e;
    const code = prompt('Code from your authenticator app');
    if (!code) throw new Error('A code is needed for this.');
    return api(method, path, body, { ...opts, headers: { ...(opts.headers || {}), 'X-2FA-Code': code.trim() } });
  }
}

// el('div', {class: 'x', text: 'y', onclick: fn}, child, ...): never uses innerHTML
export function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c);
  return e;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function svg(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs || {}) if (attrs[k] != null) e.setAttribute(k, String(attrs[k]));
  if (parent) parent.append(e);
  return e;
}

// money: integer cents in, "$1,234.56" / "-$12.00" out
export function money(cents, sign) {
  const v = Number(cents) || 0;
  const s = (Math.abs(v) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 ? '-$' : (sign && v > 0 ? '+$' : '$')) + s;
}
export function money0(cents) {
  const v = Math.round((Number(cents) || 0) / 100);
  return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US');
}
// "12.34" -> 1234; returns NaN when not a number
export function toCents(text) {
  const t = String(text == null ? '' : text).replace(/[$,\s]/g, '');
  if (!/^-?\d+(\.\d{1,2})?$/.test(t)) return NaN;
  const neg = t.startsWith('-');
  const [w, f = ''] = t.replace('-', '').split('.');
  const c = Number(w) * 100 + Number((f + '00').slice(0, 2));
  return neg ? -c : c;
}
export const centsInput = (c) => (c == null ? '' : (Number(c) / 100).toFixed(2));

export const parseD = (s) => { const [y, m, d] = String(s).split('-').map(Number); return new Date(y, m - 1, d || 1); };
export const isoD = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
export const fmtD = (s, opts) => parseD(s).toLocaleDateString('en-US', opts || { weekday: 'short', month: 'short', day: 'numeric' });
export const longD = (s) => fmtD(s, { month: 'short', day: 'numeric', year: 'numeric' });
export const shortD = (s) => fmtD(s, { month: 'numeric', day: 'numeric' });

export const can = (role) => {
  const order = { viewer: 0, contributor: 1, editor: 2, owner: 3 };
  return (order[page.role] ?? -1) >= order[role];
};

// say(node, text, kind): a message in a .msg element (the page-level #msg), or, for a form, a button or any other
// element, in a status line placed right after the form it belongs to (role=status, read out politely)
export function say(node, text, kind) {
  if (!node) return;
  const out = node.classList.contains('msg') ? node : statusFor(node);
  out.hidden = !text;
  out.textContent = text || '';
  out.className = 'msg' + (out === node ? '' : ' formmsg') + (kind ? ' ' + kind : '');
}

export function statusFor(node) {
  const host = node.closest('form, .form, .row-actions, .editrow') || node;
  const next = host.nextElementSibling;
  if (next && next.classList.contains('formmsg')) return next;
  const s = el('p', { class: 'msg formmsg', role: 'status', 'aria-live': 'polite', hidden: true });
  host.after(s);
  return s;
}

// busy(button, work): the button is disabled and reads "Saving…" until work (a promise or a function returning one)
// settles; its status line is cleared first. Returns work's result and rethrows its error.
export async function busy(btn, work, label = 'Saving…') {
  if (!btn) return typeof work === 'function' ? work() : work;
  say(btn, '');
  const text = btn.textContent, wasDisabled = btn.disabled;
  btn.disabled = true; btn.textContent = label; btn.setAttribute('aria-busy', 'true');
  try { return await (typeof work === 'function' ? work() : work); }
  finally {
    btn.removeAttribute('aria-busy'); btn.disabled = wasDisabled;
    if (btn.textContent === label) btn.textContent = text;   // the handler may have relabelled it meanwhile
  }
}

// the submit button of a submit event (Enter in a field has no submitter in older browsers)
export const submitter = (e) => e.submitter || e.target.querySelector('[type="submit"]');

// a polite live region for short announcements that have no visible status line
const live = el('p', { class: 'sr-only', 'aria-live': 'polite', role: 'status' });
document.body.append(live);
export function announce(text) { live.textContent = ''; setTimeout(() => { live.textContent = text; }, 50); }

// "click again to confirm": the first click arms the button (new label, announced); a second click within
// 4 seconds returns true; otherwise the button goes back to its own label
export function arm(btn, question) {
  if (btn.dataset.armed === '1') {
    clearTimeout(Number(btn.dataset.armTimer)); btn.dataset.armed = ''; btn.textContent = btn.dataset.label;
    return true;
  }
  btn.dataset.armed = '1'; btn.dataset.label = btn.textContent; btn.textContent = question;
  announce(question + ' Press again within 4 seconds to confirm.');
  btn.dataset.armTimer = String(setTimeout(() => {
    if (btn.dataset.armed === '1') { btn.dataset.armed = ''; btn.textContent = btn.dataset.label; }
  }, 4000));
  return false;
}

// colours from the database reach style attributes only after this check
export function safeColour(c) {
  if (typeof c !== 'string') return 'var(--f-other)';
  if (/^#[0-9a-fA-F]{6}$/.test(c)) return c;
  if (/^--f-[a-z]+$/.test(c)) return 'var(' + c + ')';
  return 'var(--f-other)';
}

// a colour usable in an SVG attribute: CSS variables are resolved to their current value
export function cssColour(c) {
  const m = /^var\((--[a-z-]+)\)$/.exec(c);
  return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() : c;
}

// a swatch; colours go through the CSSOM (inline style attributes are blocked by the CSP)
export function swatch(colour) {
  const i = el('i', { class: 'sw' });
  i.style.background = safeColour(colour);
  return i;
}

// Every table: on a phone (narrow screens) each row becomes a small card, with the column names as labels (CSS
// .tablewrap:not(.tall) td::before reads data-label). Tall scrolling tables (a year of rows) stay tables.
export function table(head, rows) {
  const labels = head.map((h) => (h instanceof Node ? '' : (h.t || h)));
  for (const tr of rows) {
    let i = 0;
    for (const td of tr.children) {
      if (labels[i] && !td.hasAttribute('data-label')) td.setAttribute('data-label', labels[i]);
      i += Number(td.getAttribute('colspan') || 1);
    }
  }
  return el('div', { class: 'tablewrap' }, el('table', {},
    el('thead', {}, el('tr', {}, ...head.map((h) => (h instanceof Node ? el('th', {}, h) : el('th', { class: h.r ? 'r' : null, text: h.t || h }))))),
    el('tbody', {}, ...rows)));
}

export function download(path) { location.href = base + path; }

// theme toggle (remembered per browser only)
const themeBtn = document.getElementById('theme');
if (themeBtn) {
  try { const t = localStorage.getItem('hb.theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* storage off */ }
  themeBtn.addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('hb.theme', next); } catch (e) { /* storage off */ }
    window.dispatchEvent(new Event('themechange'));
  });
}
// with no theme chosen here, follow the system theme live; the charts read their colours when drawn, so redraw them
const systemDark = matchMedia('(prefers-color-scheme: dark)');
systemDark.addEventListener('change', () => {
  if (!document.documentElement.dataset.theme) window.dispatchEvent(new Event('themechange'));
});
// the header's account menu (<details>): closes on Escape and on a click elsewhere
const menu = document.querySelector('details.menu');
if (menu) {
  document.addEventListener('click', (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
  });
}

// the offline page when the server cannot be reached; the worker keeps no budget data (D-089)
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
