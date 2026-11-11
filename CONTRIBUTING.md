# Contributing

Thanks for helping. This is a small, plain codebase on purpose: Python 3.12+, FastAPI, SQLite from the standard
library, server-rendered pages with a little vanilla JavaScript, and no build step. How it fits together:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why things are the way they are: [DECISIONS.md](DECISIONS.md).

## Set up

```sh
make install   # .venv with the app (editable) and the dev tools
make dev       # http://127.0.0.1:8080 with auto-reload; data in ./data (git-ignored)
```

The first start writes `data/setup-token`; `make dev` then shows the setup page.

## Checks

```sh
make lint      # ruff + mypy (strict)
make test      # all tests
make cov       # coverage report; hbudget.core must stay at 90% or more
make privacy   # personal-data scan of the tree and the git history
make audit     # known vulnerabilities in the dependencies
```

All of them must pass before a change is merged; CI runs them too. Tests that touch the web layer use FastAPI's
`TestClient` with a temporary data folder and a fixed date (`app.state.clock`).

**Where code goes.** `hbudget.core` is pure: standard library only, no I/O and no clock (callers pass `today`); a test
enforces it. Money is integer cents everywhere (in the ledger, positive = money out). Importers live in `hbudget.io`,
the database in `hbudget.db` (always scoped by household id), the web app in `hbudget.web`. Every public core and io
function has a direct test; new edge cases get a test too.

## Private regression tests

`private_tests/` is git-ignored. It holds tests against a real household's files, which must never be committed. They
find the files through `BUDGET_PRIVATE_DIR` and skip (saying "private regression test did not run") when it is not
set. The helper `tests/private_support.py` only ever reports match or mismatch, never values.

```sh
BUDGET_PRIVATE_DIR=~/my-private-folder make test
```

Never copy names, payees, amounts or dates from private files into the repository, fixtures, examples, docs or
screenshots. Fixtures use made-up round amounts and the years 2028-2031; the demo household is made up and dated from
today.

## The privacy scan

`scripts/privacy_scan.py` checks the tree and the whole git history for card and account numbers, emails and phone
numbers. With `BUDGET_PRIVATE_DIR` (and `BUDGET_PRIVATE_EXTRA`, more folders with bank files) it also builds a deny-list
in memory from your private files (names, payees, amounts, account suffixes) and reports any match, masked. The
deny-list is never written to disk. It must report `0 hit(s)`.

```sh
BUDGET_PRIVATE_DIR=~/my-private-folder .venv/bin/python scripts/privacy_scan.py --repo --history
```

## Browser test

`tests/e2e/test_browser_smoke.py` runs the app in headless Chromium: setup, an import, every tab, Bills edits, a
reconciliation, the theme toggle and phone width, with no page errors, console errors or failed requests allowed.
It is the only check of the JavaScript. CI runs it for every release and once a week, not on pull requests, so
run it yourself for any change to the pages. It is skipped unless `HBUDGET_E2E=1`:

```sh
.venv/bin/pip install -e '.[e2e]'                 # or: pip install playwright
.venv/bin/python -m playwright install chromium-headless-shell
HBUDGET_E2E=1 .venv/bin/python -m pytest tests/e2e
```

On a minimal Linux you may need the ALSA library (`libasound2`, or `libasound2t64`); `playwright install-deps`
lists what is missing.

The README pictures come from the demo household: `PYTHONPATH=src .venv/bin/python scripts/screenshots.py`
writes `docs/img/*.png`. Take them again when a pictured page changes; never add a picture of a real budget.

## Containers

The Dockerfile, Compose file and their tests are described in [docs/DEPLOY.md](docs/DEPLOY.md); `make container-test`
runs the container checks (first start, restart, upgrade, hardening).

## Agent definitions

`.claude/agents/` holds the roles used to build and review this project with Claude Code: builders (`architect`,
`core-engine`, `importers`, `web-app`, `packaging`) and read-only checkers (`code-reviewer`, `test-auditor`,
`security-auditor`, `privacy-auditor`, `docs-reviewer`). You can use them by name in Claude Code opened in this folder,
or read them as review checklists.

## Reviews

A change is ready when lint, tests, the coverage gate and the privacy scan pass, and the checks that fit it have been
done: correctness and readability; tests for new behaviour and edge cases; security for anything that touches auth,
uploads, parsing or the database; privacy for fixtures and docs; the docs for anything a user sees. Design choices go
into [DECISIONS.md](DECISIONS.md). Blocking findings
(wrong numbers, data loss, a security hole, personal data) are fixed before merging.

Commit messages say what changed and why, in plain words. Do not commit `data/`, `.env`, private files or anything
from `private_tests/`.

## Issues and security

Issues and pull requests are public: never put real bank data, payees, amounts or screenshots of a real budget in
them. Reproduce with the demo household or made-up data. Security problems are reported privately:
[SECURITY.md](SECURITY.md). What 1.x keeps compatible, which a change must respect: [docs/STABILITY.md](docs/STABILITY.md).

Issue and pull-request numbers such as #87 that appear before 1.0 refer to the project's earlier private
tracker; those issues are not public. The same goes for such numbers in code comments and
tests; they are kept because they explain why the code is the way it is.

## Conduct

Be kind and assume good faith. Talk about the work, not the person. Money is a private and sometimes stressful
subject: never comment on anyone's finances, and never ask anyone for their real data. Harassment or personal
attacks are not accepted here; the maintainers may remove comments and block accounts. This project follows the
[Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/); to report a
problem, contact the repository owner privately through their GitHub profile.
