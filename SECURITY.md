# Security

This app holds a household's finances, so security reports are welcome and taken seriously.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private reporting instead: on the repository page, **Security >
Report a vulnerability**. You get a private thread with the maintainers.

Please include what you did, what happened and what you expected, the app version (Admin > System, or
`hbudget --help`), and how it is deployed (Docker Compose as documented, a reverse proxy, something else).

**Never send real financial data.** No bank files, payees, amounts, account numbers, backups, database copies or
screenshots of a real budget. Reproduce the problem with the demo household (`HBUDGET_DEMO=1`, or Admin >
Households) or with made-up data. If a report cannot be shown without real data, describe it in words first.

You can expect a first answer within a week. This is a volunteer project: there is no bug bounty.

## Supported versions

Fixes go into the newest release only. Upgrading is `./scripts/upgrade.sh` ([docs/DEPLOY.md](docs/DEPLOY.md),
"Upgrading"); every release can be upgraded to from any earlier one ([docs/STABILITY.md](docs/STABILITY.md)).

## In scope

- Signing in, two-factor sign-in, sessions, roles and household separation: one household's member reading or changing another's.
- Access tokens, the REST API (`/api/v1`) and the MCP endpoint.
- Uploads and imports: bank files, bill-system files, workbooks, rule packs, backups.
- Injection of any kind, cross-site scripting, cross-site request forgery, path traversal.
- The container image and the published Compose setup: privileges, secrets, exposed ports.
- Anything that sends data out of the app without the household having turned it on.

## Out of scope

- A deployment that ignores the documented setup: the port published to the internet, no HTTPS on an untrusted
  network, a weak admin password, no two-factor sign-in on an app the internet can reach. See "Remote access" in [docs/DEPLOY.md](docs/DEPLOY.md).
- Someone who already has the server's files: the database, `data/secret_key` and backups are not encrypted
  at rest, by design. Protect the machine and the backups.
- Denial of service by a signed-in member of the household.
- Problems in Plaid's or a bank's own services.

## How the app is meant to be run

On your own machine or home network, or reached through Tailscale or a reverse proxy with HTTPS. It makes no
outbound connections unless a household turns on the optional Plaid connection. Details and trade-offs:
[docs/DEPLOY.md](docs/DEPLOY.md) and section 16 of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
