// Balance-over-time line chart (hand-written SVG). series: [{points: [[iso, cents, row name, rows that day], ...],
// name, colour, dash}]. One series gets an area fill; hovering shows a dot per series and the row behind the step.
import { el, svg, money, money0, longD, parseD } from './api.js';

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

export function lineChart(host, series, opts = {}) {
  host.replaceChildren();
  const pts0 = (series[0] && series[0].points) || [];
  if (!pts0.length) { host.append(el('div', { class: 'empty', text: 'No rows to chart yet.' })); return; }
  const W = Math.max(300, host.clientWidth || 640), H = opts.h || 220, L = 56, R = 12, T = 14, B = 26;
  const all = series.map((s) => s.points.map(([d, b, row, n]) => ({ t: parseD(d).getTime(), b, d, row, n })));
  const x0 = Math.min(...all.map((p) => p[0].t)), x1 = Math.max(...all.map((p) => p[p.length - 1].t));
  const vals = all.flat().map((p) => p.b).concat([0, opts.cushion || 0]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || 100000;
  const step = [10000, 50000, 100000, 200000, 500000, 1000000].find((s) => span / s <= 6) || 2000000;
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const X = (t) => L + (t - x0) / Math.max(1, x1 - x0) * (W - L - R);
  const Y = (v) => T + (hi - v) / Math.max(1, hi - lo) * (H - T - B);
  // keyboard: Tab to the chart, then Left/Right (Page Up/Down for a week or so, Home/End) read one point at a time
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', tabindex: 0, class: 'kbchart',
    'aria-label': (opts.label || 'Balance forecast') + '. Use the left and right arrow keys to read the balance on each date.' });
  for (let v = lo; v <= hi; v += step) {
    svg('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: css('--line'), 'stroke-width': 1 }, root);
    svg('text', { x: L - 6, y: Y(v) + 4, 'text-anchor': 'end' }, root).textContent = money0(v);
  }
  const first = new Date(x0); first.setDate(1);
  for (let d = new Date(first.getFullYear(), first.getMonth() + 1, 1); d.getTime() <= x1; d = new Date(d.getFullYear(), d.getMonth() + 2, 1)) {
    svg('text', { x: X(d.getTime()), y: H - 6, 'text-anchor': 'middle' }, root).textContent =
      d.toLocaleDateString('en-US', { month: 'short' }) + (d.getMonth() === 0 ? ' ' + String(d.getFullYear()).slice(2) : '');
  }
  svg('line', { x1: L, x2: W - R, y1: Y(0), y2: Y(0), stroke: css('--bad'), 'stroke-dasharray': '4 4' }, root);
  if (opts.cushion) svg('line', { x1: L, x2: W - R, y1: Y(opts.cushion), y2: Y(opts.cushion), stroke: css('--warn'), 'stroke-dasharray': '2 4' }, root);
  all.forEach((pts, si) => {
    const s = series[si];
    let d = 'M' + X(pts[0].t) + ',' + Y(pts[0].b);
    for (let i = 1; i < pts.length; i++) d += 'H' + X(pts[i].t) + 'V' + Y(pts[i].b);
    if (series.length === 1) {
      // the area between the line and $0 (or the bottom when the axis starts above $0)
      const base = Y(Math.max(lo, Math.min(0, hi)));
      svg('path', { d: d + 'V' + base + 'H' + X(pts[0].t) + 'Z', fill: css(s.colour || '--accent'), 'fill-opacity': 0.12, stroke: 'none' }, root);
    }
    svg('path', { d, fill: 'none', stroke: css(s.colour || '--accent'), 'stroke-width': 2, 'stroke-dasharray': s.dash || null }, root);
    if (opts.lows === false) return;     // a burn-down's low is always $0 at the end: nothing to mark
    // the low leaves out the starting balance (point 0), like the forecast readout; the first minimum wins
    let li = pts.length > 1 ? 1 : 0; for (let i = li + 1; i < pts.length; i++) if (pts[i].b < pts[li].b) li = i;
    svg('circle', { cx: X(pts[li].t), cy: Y(pts[li].b), r: 4, fill: css(s.colour || '--accent'), stroke: css('--surface'), 'stroke-width': 2 }, root);
    // right-aligned to the dot, unless that would run into the axis labels on the left
    const lx = X(pts[li].t), left = lx < L + 110;
    const t = svg('text', { x: left ? Math.max(lx, L + 4) : Math.min(lx, W - R - 4), y: Y(pts[li].b) + 16 + si * 13,
      'text-anchor': left ? 'start' : 'end', class: 'lbl' }, root);
    t.textContent = (series.length > 1 ? s.name + ' low ' : 'Low ') + money0(pts[li].b);
  });
  const tip = el('div', { class: 'tip', hidden: true, 'aria-live': 'polite' });
  const cross = svg('line', { y1: T, y2: H - B, stroke: css('--ink-3'), visibility: 'hidden' }, root);
  const dots = series.map((s) => svg('circle', { r: 4, fill: css(s.colour || '--accent'), stroke: css('--surface'), 'stroke-width': 2, visibility: 'hidden' }, root));
  const hit = svg('rect', { x: L, y: 0, width: W - L - R, height: H, fill: 'transparent' }, root);
  let at0 = 0;     // the point shown, an index into the first series
  const show = (best) => {
    at0 = best;
    const r = root.getBoundingClientRect();
    const p = all[0][best];
    cross.setAttribute('x1', X(p.t)); cross.setAttribute('x2', X(p.t)); cross.setAttribute('visibility', 'visible');
    tip.hidden = false;
    // each series is read by date: the balance after its last point on or before the hovered date
    const at = (pts) => { let q = pts[0]; for (const x of pts) if (x.t <= p.t) q = x; return q; };
    const after = p.row ? ' after ' + p.row + (p.n > 1 ? ' and ' + (p.n - 1) + ' more' : '') : '';
    tip.textContent = longD(p.d) + after + ' · ' + all.map((pts, i) => (series.length > 1 ? series[i].name + ' ' : '') + money(at(pts).b)).join(' · ');
    all.forEach((pts, i) => {
      const q = at(pts);
      dots[i].setAttribute('cx', X(p.t)); dots[i].setAttribute('cy', Y(q.b)); dots[i].setAttribute('visibility', 'visible');
    });
    tip.style.left = Math.min(Math.max(X(p.t) * r.width / W, 100), r.width - 100) + 'px';
    tip.style.top = Math.max(Y(p.b) * r.height / H - 10, 28) + 'px';
  };
  const move = (ev) => {
    const r = root.getBoundingClientRect();
    const t = x0 + ((ev.clientX - r.left) * (W / r.width) - L) / (W - L - R) * (x1 - x0);
    let best = 0;
    for (let i = 0; i < all[0].length; i++) if (all[0][i].t <= t) best = i;
    show(best);
  };
  const hide = () => {
    cross.setAttribute('visibility', 'hidden'); tip.hidden = true;
    for (const c of dots) c.setAttribute('visibility', 'hidden');
  };
  root.addEventListener('focus', () => show(at0));
  root.addEventListener('blur', hide);
  root.addEventListener('keydown', (ev) => {
    const n = all[0].length, jump = Math.max(1, Math.round(n / 20));
    const to = { ArrowRight: at0 + 1, ArrowLeft: at0 - 1, PageDown: at0 + jump, PageUp: at0 - jump, Home: 0, End: n - 1 }[ev.key];
    if (to === undefined) return;
    ev.preventDefault(); show(Math.max(0, Math.min(n - 1, to)));
  });
  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerdown', move);
  hit.addEventListener('pointerleave', () => { if (document.activeElement !== root) hide(); });
  host.append(root, tip);
}
