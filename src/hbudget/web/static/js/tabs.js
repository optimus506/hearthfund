// The second row of tabs on a narrow screen scrolls sideways (#102): keep the current page's tab in view, fade
// the edge that has more tabs past it, and show a › button that scrolls on, so no page looks missing.
const nav = document.getElementById('subtabs');
const more = document.getElementById('tabmore');

function edges() {
  if (!nav) return;
  const left = nav.scrollLeft > 2;
  const right = nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2;
  nav.classList.toggle('more-left', left);
  nav.classList.toggle('more-right', right);
  if (more) more.hidden = !right;
}

if (nav) {
  const here = nav.querySelector('[aria-current="page"]');
  if (here && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, here.offsetLeft - nav.offsetLeft - nav.clientWidth / 2 + here.offsetWidth / 2);
  }
  nav.addEventListener('scroll', edges, { passive: true });
  window.addEventListener('resize', edges);
  if (more) more.addEventListener('click', () => nav.scrollBy({ left: nav.clientWidth * 0.7, behavior: 'smooth' }));
  edges();
}

// The weekly check-in as a guided path (#103): a page opened from a check-in step (?checkin=N) shows a bar back
// to Home with the step, so the weekly routine never needs the menus.
const STEPS = ['Bring in the bank transactions', 'Schedule what is due', 'Things to look at', 'Changes by others'];
const step = Number(new URLSearchParams(location.search).get('checkin'));
const main = document.getElementById('main');
if (step >= 1 && step <= STEPS.length && main) {
  let hid = '';
  try { hid = JSON.parse(document.getElementById('page-data').textContent).hid || ''; } catch { /* no page data */ }
  if (hid) {
    const bar = document.createElement('p');
    bar.className = 'guide small';
    const back = document.createElement('a');
    back.href = '/h/' + encodeURIComponent(hid) + '/home';
    back.textContent = '← Back to the check-in';
    const where = document.createElement('span');
    where.textContent = 'Step ' + step + ' of ' + STEPS.length + ': ' + STEPS[step - 1];
    bar.append(back, where);
    main.prepend(bar);
  }
}
