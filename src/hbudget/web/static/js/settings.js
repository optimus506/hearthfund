// Settings tab: checkbook balance, alerts, reconcile matching, forecast horizon, colours and week start.
// Spending caps live on Check-up and report groups on Report (D-074); saving here keeps them.
import { api, $, centsInput, toCents, can, say, busy, submitter } from './api.js';
import { loadSummary } from './summary.js';

const msg = $('msg');
let data = null;

function render() {
  const s = data.settings;
  $('stBalance').value = centsInput(data.start_balance); $('stAsOf').value = data.as_of;
  $('stCushion').value = centsInput(s.alerts.cushion); $('stVariance').value = (s.alerts.bill_variance_bp / 100).toString();
  $('stUnsched').value = s.alerts.unscheduled_days; $('stGrace').value = s.alerts.expected_grace_days; $('stRecurring').checked = s.alerts.unmatched_recurring;
  $('stBefore').value = s.reconcile.days_before; $('stAfter').value = s.reconcile.days_after;
  $('stTolPct').value = (s.reconcile.tolerance_bp / 100).toString(); $('stTolAbs').value = centsInput(s.reconcile.tolerance_abs);
  $('stMode').value = data.horizon.mode; $('stMonths').value = data.horizon.months; $('stEnd').value = data.horizon.end || '';
  $('stAutopay').value = s.status_colours.autopay; $('stManual').value = s.status_colours.manual;
  $('stWeek').value = String(s.week_starts);
  $('stWins').checked = s.celebrate !== false;
  $('stCardMove').checked = !!s.card_transfers_on_payday;
}

$('settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pctBp = (v) => Math.round(Number(v) * 100);
  const body = {
    alerts: { cushion: toCents($('stCushion').value), bill_variance_bp: pctBp($('stVariance').value), unscheduled_days: Number($('stUnsched').value),
      expected_grace_days: Number($('stGrace').value), unmatched_recurring: $('stRecurring').checked },
    reconcile: { days_before: Number($('stBefore').value), days_after: Number($('stAfter').value), tolerance_bp: pctBp($('stTolPct').value), tolerance_abs: toCents($('stTolAbs').value) },
    horizon: { mode: $('stMode').value, months: Number($('stMonths').value), end: $('stEnd').value || null },
    status_colours: { autopay: $('stAutopay').value, manual: $('stManual').value }, week_starts: Number($('stWeek').value),
    card_transfers_on_payday: $('stCardMove').checked,
    start_balance: toCents($('stBalance').value), as_of: $('stAsOf').value || null,
    expected_version: data.version,
  };
  try {
    await busy(submitter(e), async () => { await api('PUT', '/settings', body); await api('PUT', '/celebrate', { on: $('stWins').checked }); });
    say(e.target, 'Settings saved.', 'ok'); await load();
  }
  catch (err) { say(e.target, err.message, 'err'); }
});
if (!can('editor')) $('settingsForm').hidden = true;

async function load() {
  data = await api('GET', '/rules');
  render(); loadSummary();
}
load().catch((e) => say(msg, e.message, 'err'));
