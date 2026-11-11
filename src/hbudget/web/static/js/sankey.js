// Sankey: money in (sources) -> one "Money in" node -> spending groups. Per-month figures.
import { el, svg, money0, safeColour, cssColour } from './api.js';

// a source's colour token (also the breakdown's swatch) and its resolved value for SVG attributes
export const sourceColour = (name) => (/loan/i.test(name) ? '--f-debt' : /saving|balance/i.test(name) ? '--f-savings' : '--f-in');
const SOURCE_COLOUR = (name) => cssColour('var(' + sourceColour(name) + ')');

export function sankey(host, flow) {
  host.replaceChildren();
  const months = flow.months || 12;
  const per = (c) => c / months;
  const srcs = flow.sources.map((s) => ({ name: s.name, v: per(s.amount), colour: SOURCE_COLOUR(s.name) }));
  const grps = flow.groups.map((g) => ({ name: g.name, v: per(g.total), colour: cssColour(safeColour(g.colour)), items: g.items }));
  // the server computes the totals; the chart only draws them
  const t = flow.totals;
  if (t.left_over > 0) grps.push({ name: 'Left over', v: per(t.left_over), colour: cssColour('var(--f-in)'), items: [] });
  if (t.short > 0) srcs.push({ name: 'From the checking balance', v: per(t.short), colour: cssColour('var(--f-savings)') });
  const tin = per(t.money_in), tout = per(t.money_out);
  const total = Math.max(srcs.reduce((a, x) => a + x.v, 0), grps.reduce((a, x) => a + x.v, 0));
  if (!total) { host.append(el('div', { class: 'empty', text: 'No money in or out in this period.' })); return { tin, tout }; }
  const W = 760, X0 = 190, X1 = 370, X2 = 560, NW = 12, HUB = 320, GAP = 10, SLOT = 30, T = 30;
  const k = HUB / total;
  const place = (list) => { let y = 0; for (const n of list) { n.h = Math.max(2, n.v * k); n.y = y; y += Math.max(n.h, SLOT) + GAP; } return y - GAP; };
  const hs = place(srcs), hg = place(grps), inner = Math.max(hs, hg, HUB), H = T + inner + 16;
  for (const n of srcs) n.y += T + (inner - hs) / 2;
  for (const n of grps) n.y += T + (inner - hg) / 2;
  const hubY = T + (inner - HUB) / 2;
  const pct = (v) => (100 * v / total).toFixed(v / total < 0.1 ? 1 : 0) + '%';
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Money in ' + money0(tin) + ' a month; out to ' + grps.map((g) => g.name + ' ' + money0(g.v)).join(', ') + '. The breakdown below lists every amount.' });
  const tip = el('div', { class: 'tip', hidden: true });
  const band = (xa, ya, xb, yb, h, colour, text) => {
    const xm = (xa + xb) / 2;
    const p = svg('path', { d: `M${xa},${ya}C${xm},${ya} ${xm},${yb} ${xb},${yb}L${xb},${yb + h}C${xm},${yb + h} ${xm},${ya + h} ${xa},${ya + h}Z`,
      fill: colour, 'fill-opacity': 0.3 }, root);
    const on = (ev) => {
      p.setAttribute('fill-opacity', 0.55);
      const r = host.getBoundingClientRect();
      tip.hidden = false; tip.textContent = text;
      tip.style.left = Math.min(Math.max(ev.clientX - r.left, 120), r.width - 120) + 'px';
      tip.style.top = Math.max(ev.clientY - r.top - 12, 28) + 'px';
    };
    p.addEventListener('pointermove', on); p.addEventListener('pointerdown', on);
    p.addEventListener('pointerleave', () => { p.setAttribute('fill-opacity', 0.3); tip.hidden = true; });
  };
  let yi = hubY, yo = hubY;
  for (const n of srcs) { band(X0 + NW, n.y, X1, yi, n.h, n.colour, n.name + ': ' + money0(n.v) + '/mo · ' + pct(n.v)); yi += n.h; }
  for (const n of grps) { band(X1 + NW, yo, X2, n.y, n.h, n.colour, n.name + ': ' + money0(n.v) + '/mo · ' + pct(n.v)); yo += n.h; }
  const label = (x, y, anchor, a, b) => {
    const t = svg('text', { x, y, 'text-anchor': anchor, class: 'lbl' }, root);
    svg('tspan', { x, dy: 0 }, t).textContent = a;
    svg('tspan', { x, dy: 13 }, t).textContent = b;
  };
  for (const n of srcs) { svg('rect', { x: X0, y: n.y, width: NW, height: n.h, rx: 2, fill: n.colour }, root); label(X0 - 8, n.y + n.h / 2 - 3, 'end', n.name, money0(n.v) + '/mo · ' + pct(n.v)); }
  for (const n of grps) { svg('rect', { x: X2, y: n.y, width: NW, height: n.h, rx: 2, fill: n.colour }, root); label(X2 + NW + 8, n.y + n.h / 2 - 3, 'start', n.name, money0(n.v) + '/mo · ' + pct(n.v)); }
  svg('rect', { x: X1, y: hubY, width: NW, height: HUB, rx: 2, fill: cssColour('var(--ink-2)') }, root);
  svg('text', { x: X1 + NW / 2, y: hubY - 10, 'text-anchor': 'middle', class: 'lbl' }, root).textContent = 'Money in ' + money0(tin) + '/mo';
  host.append(root, tip);
  return { tin, tout, srcs, grps, pct };
}
