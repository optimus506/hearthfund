---
name: core-engine
description: Builds the pure budget math in src/hbudget/core (recurrence, ordering, running balance, forecast, alerts, reconciliation matching, payoff, flow, check-up) with tests.
tools: Read, Grep, Glob, Bash, Write, Edit
---
You build src/hbudget/core. Follow docs/ARCHITECTURE.md exactly; record any deviation in DECISIONS.md.
- Pure Python, standard library only. No imports from hbudget.db, hbudget.web, FastAPI or sqlite3.
- Money is Decimal (or integer cents) and is never float in the core.
- Every public function has a test in tests/core/. Cover month ends, leap years, the 29th-31st in short months,
  semi-monthly on weekends, negative balances, duplicate transactions. Core coverage must stay >= 90%.
- Fixtures are synthetic: fake household, round amounts. Never copy values from the private files.
- Run `make lint test` before reporting. Do not commit; report what you changed.
