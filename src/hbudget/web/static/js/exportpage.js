// Export tab: downloads are plain links; the wipe form (owner only) needs the household name typed in.
import { api, $, can, say, busy, submitter, page } from './api.js';
import { loadSummary } from './summary.js';

if (!can('owner')) $('wipeCard').hidden = true;
$('wipeForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if ($('wipeName').value !== page.household_name) { say(e.target, 'Type the household name exactly to confirm.', 'err'); return; }
  try {
    await busy(submitter(e), () => api('POST', '/wipe', { confirm: $('wipeName').value }), 'Deleting…');
    say(e.target, 'All budget data of this household was deleted. Export a backup first next time.', 'ok');
  } catch (err) { say(e.target, err.message, 'err'); }
});
loadSummary();
