// The search box in the tab row: filters what is on the page in place (bill rows, list items, table lines).
// It shows only on pages that have something to search, and re-applies itself when a page redraws.
// A page can also listen for the 'pagesearch' event on the box (Bills uses it to search beyond the weeks shown).
const box = document.getElementById('pageSearch');
const main = document.getElementById('main');

// what counts as one searchable thing; editors, placeholders and the search results' own bulk bar never hide
const ITEMS = '.bill:not(.editrow), .list > :not(.empty), table tbody tr';
const SKIP = '.empty, .editrow, .bulkbar';

function apply() {
  const q = box.value.trim().toLowerCase();
  const items = [...main.querySelectorAll(ITEMS)].filter((x) => !x.closest(SKIP));
  box.parentElement.hidden = items.length === 0 && !q;
  for (const x of items) x.classList.toggle('sf-hide', !!q && !x.textContent.toLowerCase().includes(q));
  // a payday group with no matching rows goes too, so the list closes up
  for (const g of main.querySelectorAll('.group')) {
    const rows = [...g.querySelectorAll('.bill:not(.editrow)')];
    g.classList.toggle('sf-hide', !!q && rows.length > 0 && rows.every((r) => r.classList.contains('sf-hide')));
  }
}

if (box && main) {
  let t, queued = false;
  box.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { apply(); box.dispatchEvent(new CustomEvent('pagesearch', { detail: box.value })); }, 150);
  });
  box.addEventListener('keydown', (e) => { if (e.key === 'Escape' && box.value) { box.value = ''; box.dispatchEvent(new Event('input')); } });
  box.closest('form').addEventListener('submit', (e) => e.preventDefault());
  // pages draw after loading their data: filter what arrives (class changes are not child changes, so no loop)
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; apply(); }); } })
    .observe(main, { childList: true, subtree: true });
  // "/" jumps to the box, unless typing somewhere already
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
        && !document.activeElement.isContentEditable && !box.parentElement.hidden) { e.preventDefault(); box.focus(); }
  });
  apply();
}
