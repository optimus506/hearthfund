# What stays compatible

From version 1.0.0 the app follows [semantic versioning](https://semver.org). Within one major version (1.x),
an upgrade does not break a household's data, files or integrations. This page says exactly what that covers.

## Kept compatible in 1.x

- **Your data and upgrades.** Any 1.x release starts on the data of any earlier release (0.x included) and
  migrates it by itself, after making a copy (`data/backups/hbudget-before-*.db`). Upgrades skip versions freely.
- **Backups and the bill-system file format** (`format: household-budget/bill-system`, `version: 1`). A backup
  or bill-system file written by any 1.x release, or by 0.x, can be imported by every later 1.x release. Fields
  may be added; none is removed or changes its meaning.
  The ids keep the app's earlier name, household-budget, on purpose: they never change (D-096).
- **Rule packs** (`format: household-budget/rule-pack`, `version: 1`), on the same terms.
- **The REST API under `/api/v1`**: paths, methods, request fields and the fields of responses. New paths and
  new response fields may appear; clients should ignore fields they do not know.
- **MCP tools**: the tool names and their arguments. New tools and new optional arguments may appear.
- **Environment variables**: the `HBUDGET_*` names in `.env.example` and their meaning. New ones are optional
  and default to the earlier behaviour.
- **The command line**: the `hbudget` commands and options in the README and docs/DEPLOY.md.
- **The container**: the image runs as a non-root user with its data in `/data`, listens on port 8080 and has a
  health check; `docker-compose.yml` from an older 1.x release keeps working with a newer image.

## Not covered

- The pages themselves: layout, wording, navigation and the internal endpoints the pages call (`/api/h/...`).
  Use `/api/v1` for anything scripted.
- The database tables. Read data through the API or an export, not from the SQLite file.
- The text of messages, alerts and exports meant for people (the CSV and spreadsheet exports keep their
  columns, but wording may change).
- What Plaid or a bank changes on their side.
- Running an **older** release on data that a newer release has migrated. The app refuses to start and says
  so; go back with the copy made before the upgrade (docs/DEPLOY.md, "Upgrading").

## Known limits in 1.0

- **US dollars and US formats only.** Amounts are shown as `$1,234.56` and dates the US way. A bill-system file may name another `currency`; it is kept in backups but nothing
  is converted or formatted differently. Other currencies and formats are on the [roadmap](ROADMAP.md).
- English only.

## Changes that would break something

They wait for the next major version, are listed under a **Breaking** heading in CHANGELOG.md, and where
possible the old behaviour stays available for at least one minor release first.
