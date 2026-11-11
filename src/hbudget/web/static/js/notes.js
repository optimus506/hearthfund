// Notes tab: free text for the household, shown escaped with its line breaks.
import { api, $, can, say, busy, submitter, page } from './api.js';
import { loadSummary } from './summary.js';

let updatedAt = page.updated_at;
$('notesView').textContent = page.notes || 'No notes yet.';
$('notesText').value = page.notes || '';
if (!can('editor')) $('notesForm').hidden = true;
$('notesForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const r = await busy(submitter(e), () => api('PUT', '/notes', { notes: $('notesText').value, expected_updated_at: updatedAt }));
    updatedAt = r.updated_at; $('notesView').textContent = $('notesText').value || 'No notes yet.'; say(e.target, 'Saved.', 'ok');
  } catch (err) { say(e.target, err.message, 'err'); }
});
loadSummary();
