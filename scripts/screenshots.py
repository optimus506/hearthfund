"""Take the README screenshots from the demo household (made-up data only).

  pip install playwright && python -m playwright install chromium-headless-shell
  PYTHONPATH=src python scripts/screenshots.py [--out docs/img]

Starts the app on a free local port with an empty temporary data folder, sets it up with the demo household on a
fixed date, and saves a few pages as PNG files. Nothing is read from a real data folder, so the pictures can
never show a real budget. Run `make privacy` afterwards all the same.
"""
from __future__ import annotations

import argparse
import socket
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

DAY = date(2029, 3, 12)                 # the demo data is laid out around "today"
PASSWORD = 'made-up demo password'  # noqa: S105 - for a throwaway data folder
DESKTOP = {'width': 1200, 'height': 820}
TALL = {'width': 1200, 'height': 1080}
PHONE = {'width': 390, 'height': 780}
# (file name, page under /h/<id>, viewport, whole page instead of the first screen)
SHOTS = [('home', '', DESKTOP, False), ('bills', '/bills', DESKTOP, False), ('debts', '/debts', DESKTOP, False),
         ('report', '/report', TALL, False), ('home-phone', '', PHONE, False)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default='docs/img', type=Path)
    a = ap.parse_args()

    import uvicorn
    from playwright.sync_api import sync_playwright

    from hbudget.web.app import create_app
    from hbudget.web.config import Settings

    a.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        port = free_port()
        app = create_app(Settings(data_dir=data, port=port, allowed_hosts=('127.0.0.1',), cookie_secure='false',
                                  demo=True))
        app.state.clock = lambda: DAY
        srv = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        while not srv.started:
            time.sleep(0.05)
        base = 'http://127.0.0.1:%d' % port
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                ctx = browser.new_context(viewport=DESKTOP, color_scheme='light', device_scale_factor=1)
                page = ctx.new_page()
                page.goto(base + '/')
                page.fill('#token', (data / 'setup-token').read_text().strip())
                page.fill('#username', 'demo')
                page.fill('#password', PASSWORD)
                page.fill('#household', 'Demo household')
                page.select_option('#start', 'demo')
                page.click('button[type=submit]')
                page.wait_for_url('**/h/**')
                home = page.url.rstrip('/')
                for name, path, viewport, full in SHOTS:
                    page.set_viewport_size(viewport)
                    page.goto(home + path)
                    page.wait_for_load_state('networkidle')
                    page.wait_for_function('() => !document.getElementById("main").innerText.includes("Loading…")')
                    target = a.out / (name + '.png')
                    page.screenshot(path=str(target), full_page=full)
                    print('%s (%d KB)' % (target, target.stat().st_size // 1024))
                browser.close()
        finally:
            srv.should_exit = True
            app.state.stop.set()
            t.join(timeout=5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
