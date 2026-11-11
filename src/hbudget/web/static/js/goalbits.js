// Goal progress shown on the Goals tab and on Home (no side effects: safe to import anywhere).
import { el, money0 } from './api.js';

export const KIND = { emergency: 'Emergency fund', savings: 'Savings', debt_free: 'Debt-free' };
const mo = (d) => new Date(d + 'T00:00').toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

export function goalBar(g) {
  const bar = el('i', {}); bar.style.width = g.pct + '%';     // CSSOM: allowed by the CSP
  return el('div', { class: 'meter goal' + (g.reached ? ' reached' : ''), role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100,
    'aria-valuenow': g.pct, 'aria-label': g.name + ' ' + g.pct + '%' }, bar);
}

export function goalLine(g) {
  if (g.kind === 'debt_free') {
    if (g.reached) return 'Debt-free!';
    return money0(g.current) + ' paid down of ' + money0(g.target) + (g.done_on ? ' · debt-free by ' + mo(g.done_on) + ' (' + g.months_left + ' months)' : ' · not within the payoff plan yet');
  }
  if (g.reached) return money0(g.current) + ' put aside: reached!';
  return money0(g.current) + ' of ' + money0(g.target) + (g.kind === 'emergency' ? ' (' + g.months + ' months of bills)' : '') +
    (g.monthly_needed ? ' · ' + money0(g.monthly_needed) + ' a month to get there by ' + mo(g.by) : '');
}
