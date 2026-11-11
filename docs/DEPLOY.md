# Deploying Hearthfund

One container, one command. Your data stays in the `./data` folder next to `docker-compose.yml`;
nothing is stored in the image and the app makes no outbound connections.

You need Docker with the Compose plugin **2.24 or later** (`docker compose version`); older versions cannot
treat `.env` as optional.

## Quick start

```sh
git clone https://github.com/optimus506/hearthfund.git
cd hearthfund
docker compose up -d                          # pulls the released image (amd64 or arm64)
docker compose exec app cat /data/setup-token # the one-time setup token
```

Open <http://127.0.0.1:8080>. The setup screen asks for the token, an admin name and password, and a
household name. The token file is deleted once setup is done.

`docker compose ps` shows the container as `healthy` when it is ready. The short-lived `init` service
only fixes the ownership of `./data` (see "File ownership") and then exits; that is expected.

The image is `ghcr.io/optimus506/hearthfund`, published for every release (CHANGELOG.md) with the tags
`X.Y.Z`, `X.Y` and `latest`. `docker-compose.yml` runs `latest`; to upgrade only when you choose, pin a release
in `.env`:

```
HBUDGET_IMAGE=ghcr.io/optimus506/hearthfund:1.0.0
```

The image is public: no sign-in to the registry is needed.

### Install without cloning

The app needs only `docker-compose.yml`; everything else comes in the image. In an empty folder:

```sh
mkdir -p hearthfund/certs && cd hearthfund     # certs: only used for HTTPS with your own certificate
curl -fsSLO https://raw.githubusercontent.com/optimus506/hearthfund/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/optimus506/hearthfund/main/.env.example -o .env.example   # optional: the settings, explained
mkdir -p scripts && curl -fsSL https://raw.githubusercontent.com/optimus506/hearthfund/main/scripts/upgrade.sh -o scripts/upgrade.sh && chmod +x scripts/upgrade.sh
docker compose up -d
docker compose exec app cat /data/setup-token
```

To change a setting, copy `.env.example` to `.env` and edit it. To upgrade, run `scripts/upgrade.sh` (or
`docker compose pull && docker compose up -d`); download the three files again now and then, since a new release
can come with a newer `docker-compose.yml`. Everything else in this guide applies unchanged. The example
files the README mentions (`examples/bill-system.example.yaml`, `examples/sample-bank.csv`) are in the
[repository](https://github.com/optimus506/hearthfund/tree/main/examples).

**Building from the source instead** (development, or a machine that cannot reach the registry): `compose.build.yml` adds the
build and names the image `hearthfund:local`.

```sh
docker compose -f docker-compose.yml -f compose.build.yml up -d --build
```

To make that the default for this folder, put `COMPOSE_FILE=docker-compose.yml:compose.build.yml` in `.env`;
then use `docker compose up -d --build` after every `git pull`.

The Compose project is called `hearthfund`. To run a second, separate deployment on the same machine,
clone into another folder and give it its own project and port in that folder's `.env` (and, when building
from source, its own image tag so it does not replace the first deployment's):

```
COMPOSE_PROJECT_NAME=budget-test
HBUDGET_HOST_PORT=8081
HBUDGET_PUBLIC_URL=http://127.0.0.1:8081
HBUDGET_IMAGE=hearthfund:test        # only when building from source
```

Then plain `docker compose ...` commands in that folder always target that project (`COMPOSE_PROJECT_NAME`
wins over the name in `docker-compose.yml`). Do not use `-p` for this: every later command would need the
same `-p`.

## Settings

Copy `.env.example` to `.env` and uncomment what you need. Every setting has a safe default, so a
missing `.env` is fine. The ones you are most likely to change:

| Setting | When |
|---|---|
| `HBUDGET_HOST_PORT` | Port 8080 is taken on this machine. Also set `HBUDGET_PUBLIC_URL=http://127.0.0.1:<port>`. |
| `HBUDGET_TLS_CERT` / `HBUDGET_TLS_KEY` | You want HTTPS with your own certificate (see "HTTPS with your own certificate"). |
| `HBUDGET_BIND_ADDRESS` | `0.0.0.0` to reach the app from other devices on your home network (see "On your home network"). |
| `HBUDGET_ALLOWED_HOSTS` | You reach the app by another name (a reverse proxy or Tailscale name). Add the name and keep `127.0.0.1,localhost` in the list: the container's health check uses it, and without it the app shows as unhealthy. |
| `HBUDGET_PUBLIC_URL` | The address you type in the browser is not `http://127.0.0.1:8080`. |
| `HBUDGET_COOKIE_SECURE` | Set `true` when the app is only used over HTTPS. |
| `HBUDGET_UID` / `HBUDGET_GID` | You want the files in `./data` owned by your own user (`id -u`, `id -g`). 0 (root) is refused. |

After editing `.env`, run `docker compose up -d` again.

## Your data and backups

`./data` holds everything: `hbudget.db` (SQLite, with its `-wal`/`-shm` files), `secret_key`, and
`backups/` (copies the app makes before a database upgrade; the folder appears after the first upgrade that
changes the database). The folder is mode 700 and its files mode 600, owned by the app user.

Three ways to back up:

1. **From the app** (recommended): on the Export page, **Backup (YAML)** or **Backup (JSON)**. It holds the budget, bank and card
   history, uploads, reconciliations, rules, notes and settings. Restore it on the Import page; a
   restore replaces everything in that household and asks you to type the household name first.
2. **The whole folder**: `docker compose stop`, copy `./data` somewhere safe (for example
   `sudo sh -c 'umask 077; tar czf budget-data-$(date +%F).tgz data'`), then `docker compose start`. Do not copy the
   database while the app is running.
3. **Scheduled database copies, no downtime** (recommended in addition to 1):

   ```sh
   docker compose exec -T app hbudget backup --keep 14
   ```

   copies the database while the app runs, checks the copy (`PRAGMA integrity_check`) and saves it as
   `data/backups/hbudget-<UTC time>.db`, a single self-contained file. It keeps the newest 14 of these and the
   newest 5 pre-upgrade copies (`--keep-upgrade`), and never touches other files. A copy that fails the check
   is deleted and the command exits with an error, so the newest file is always a good one.

   Run it nightly on the machine that runs Docker (for example a Proxmox LXC), with a systemd timer:

   ```ini
   # /etc/systemd/system/hbudget-backup.service
   [Unit]
   Description=Hearthfund database backup
   [Service]
   Type=oneshot
   WorkingDirectory=/opt/hearthfund
   ExecStart=/usr/bin/docker compose exec -T app hbudget backup --keep 14

   # /etc/systemd/system/hbudget-backup.timer
   [Unit]
   Description=Nightly Hearthfund backup
   [Timer]
   OnCalendar=*-*-* 02:30
   Persistent=true
   [Install]
   WantedBy=timers.target
   ```

   (`WorkingDirectory` is the folder with `docker-compose.yml`.) Then
   `sudo systemctl daemon-reload && sudo systemctl enable --now hbudget-backup.timer`;
   `systemctl list-timers hbudget-backup` shows the next run and `journalctl -u hbudget-backup` the result.

   Copies on the same machine do not survive a lost disk. Also copy the newest one somewhere else,
   **encrypted**, for example with [age](https://age-encryption.org) (only the public key lives on this machine):

   ```sh
   newest=$(ls -1 data/backups/hbudget-2*.db | tail -n 1)
   sudo age -r age1yourpublickey... -o /mnt/nas/budget/$(basename "$newest").age "$newest"
   ```

   On Proxmox, a scheduled `vzdump` of the whole container (Datacenter → Backup) is a good second layer.

   **Restore one:** `docker compose stop app`, then replace the database and remove its side files, keeping the
   app user as owner: `sudo sh -c 'cp data/backups/hbudget-<time>.db data/hbudget.db && rm -f data/hbudget.db-wal
   data/hbudget.db-shm && chown 10001:10001 data/hbudget.db && chmod 600 data/hbudget.db'` (use your
   `HBUDGET_UID`/`HBUDGET_GID` if you set them), then `docker compose start app`.

All of these hold the household's full financial history, **unencrypted**. Keep them readable by you only
(`umask 077` when you create them, `chmod 600` on a downloaded file), store them encrypted or on a disk only you use,
and do not email them or put them in a shared cloud folder.

To start over completely, stop the app and delete `./data`. Its files belong to the app user, so either use
`sudo rm -r data`, or without sudo:

```sh
docker compose down
docker compose run --rm --no-deps --entrypoint python app -c \
  "import os, shutil; [shutil.rmtree(p) if os.path.isdir(p) else os.remove(p) for p in (os.path.join('/data', n) for n in os.listdir('/data'))]"
rmdir data
docker compose down     # the run above recreated the project's network; this removes it again
```

`docker compose run` uses this folder's project, image (`HBUDGET_IMAGE`), user (`HBUDGET_UID`) and `./data`, so
the container runs as the owner of the files and empties the folder; `rmdir` then removes the empty folder,
which belongs to you.

## Upgrading

```sh
git pull               # the newest docker-compose.yml and scripts
scripts/upgrade.sh
```

`scripts/upgrade.sh` pulls the image and, when it is newer than the running one:

1. backs up the database from the running app (`hbudget backup`) and keeps the current image as
   `hearthfund:rollback` (one tag per machine: with two deployments on one machine, the last upgrade owns it);
2. starts the new version and waits up to 3 minutes (`HBUDGET_UPGRADE_TIMEOUT` seconds) for it to be healthy;
3. if it is not healthy, shows its last log lines, keeps the database it left as
   `data/backups/hbudget-failed-upgrade-<time>.db`, puts the backup from step 1 back and starts the previous
   image again. Pin the previous release in `.env` (`HBUDGET_IMAGE=...:<version>`) to stay on it.

**Coming from 0.15 or earlier**, when the app was called household-budget: `git pull` and `scripts/upgrade.sh`
as usual. The image is now `ghcr.io/optimus506/hearthfund`, and the script adds
`COMPOSE_PROJECT_NAME=household-budget` to `.env` so Docker Compose keeps using your existing containers. If
`.env` pins `HBUDGET_IMAGE` to the old image name, change it to the new one. Your data, settings
(`HBUDGET_*`), backups and exported files are unchanged.

Without the script: `docker compose pull && docker compose up -d`. When building from source: `git pull` and
`docker compose up -d --build` (with `compose.build.yml`, see Quick start).

On start the app applies any new database migrations. Before it changes anything it writes
`data/backups/hbudget-before-<version>-<time>.db`. If a migration fails, nothing is changed and the
container stops with the error in `docker compose logs app`. To go back by hand, pin the previous release in
`.env`, stop the app, copy the backup file over `data/hbudget.db` (removing `hbudget.db-wal` and
`hbudget.db-shm`), and start it again.

**An older release will not start on newer data.** If you pin an earlier release without putting the backup
back, the app stops at once with "This database was last used by a newer version of the app" instead of
running on tables it does not know. Put the backup back as above, or return to the newer release. What
upgrades promise in general: [STABILITY.md](STABILITY.md).

**Moving from a source build to the released image** (deployments made before 0.2.0): after `git pull`, run
`docker compose pull && docker compose up -d`. The data in `./data` is used as it is.

## Remote access

By default the app listens on 127.0.0.1 only. Do not edit the port line to `"8080:8080"`: Docker would then
publish it on every network interface, and Docker's own firewall rules bypass host firewalls such as `ufw`.
To use the app from your phone or another computer, put HTTPS in front of it with one of the options below,
or, on a trusted home network, publish it on your LAN.

**What it is built for.** A home network you trust, or Tailscale: the app is then reachable only by your own
devices, and that is the recommended setup. If you put it behind a reverse proxy that the internet can reach,
it must have HTTPS, a long admin password, two-factor sign-in for every user, and current releases
(`scripts/upgrade.sh`). **Never forward a
router port straight to the app**, with or without HTTPS on the app itself: it then answers anyone on the
internet, and it holds your whole financial history.

**Two-factor sign-in.** Each user can turn it on under **Account > Two-factor sign-in**: signing in then needs
the password and a 6-digit code from an authenticator app (any app that does time-based codes). Ten recovery
codes are shown once at set-up; keep them off the computer. Turn it on whenever the app is reachable beyond
your own home network, and set `HBUDGET_REQUIRE_2FA=1` in `.env` to make every user set it up. The server's
clock must be right (within about 30 seconds) for codes to match. A user who lost the phone and the codes: an
admin presses **Reset 2FA** next to them on the Admin page, or run
`docker compose exec app hbudget disable-2fa USERNAME`; they then sign in with the password and set it up
again. Access tokens for the API are separate and do not ask for a code.

### On your home network (plain HTTP)

For a machine on a home network you trust (for example a Proxmox LXC), set in `.env`, using the
machine's LAN address:

```
HBUDGET_BIND_ADDRESS=0.0.0.0
HBUDGET_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.1.50
HBUDGET_PUBLIC_URL=http://192.168.1.50:8080
```

Run `docker compose up -d`, then open `http://192.168.1.50:8080` from any device on the network. Leave
`HBUDGET_COOKIE_SECURE` at `auto`: with plain HTTP, secure cookies would stop sign-in working.

Know what this means: traffic is not encrypted, so anyone on the same network (including guest Wi-Fi on
the same LAN) could read passwords and figures in transit. Add your own certificate (next section) to fix
that. Never forward the port on your router. If the machine's address changes, update both settings; a
DHCP reservation keeps it fixed.

### HTTPS with your own certificate

The app can serve HTTPS itself, with no proxy. It needs two PEM files: the certificate (including any
intermediate certificates, usually called `fullchain.pem`) and its private key without a passphrase.

1. Put them in the `certs` folder next to `docker-compose.yml` (or point `HBUDGET_CERTS_DIR` at another
   folder; it is mounted read-only at `/certs`). Copy the files themselves: links that point outside the
   folder, such as Let's Encrypt's `live/` links, do not work inside the container.
2. Let the app user read the key (use your own ids if you set `HBUDGET_UID`/`HBUDGET_GID`):

   ```sh
   sudo chown 10001:10001 certs/privkey.pem && sudo chmod 600 certs/privkey.pem
   ```

3. In `.env`, with the name or address on the certificate:

   ```
   HBUDGET_TLS_CERT=/certs/fullchain.pem
   HBUDGET_TLS_KEY=/certs/privkey.pem
   HBUDGET_ALLOWED_HOSTS=127.0.0.1,localhost,budget.home.example.com
   HBUDGET_PUBLIC_URL=https://budget.home.example.com:8080
   HBUDGET_COOKIE_SECURE=true
   ```

4. `docker compose up -d`, then open `https://budget.home.example.com:8080`.

The port now speaks only HTTPS: `http://` addresses to it stop working. If a file is missing or
unreadable, the key does not match the certificate, or the key has a passphrase, the container stops
with that reason in `docker compose logs app`. The healthcheck trusts exactly this certificate, so an
expired certificate shows as `unhealthy` in `docker compose ps`. After renewing, copy the new files in
and run `docker compose restart app`.

**Where to get a certificate.** A certificate from a public authority (for example Let's Encrypt with a
DNS challenge, for a domain you own) works on every device with no warnings. For a home network without a
domain, you can make your own: the simplest is a self-signed certificate for the machine's address, which
each device must trust once (or click past a warning):

```sh
openssl req -x509 -newkey rsa:2048 -days 825 -noenc -subj /CN=budget \
  -addext subjectAltName=IP:192.168.1.50,DNS:budget.lan \
  -keyout certs/privkey.pem -out certs/fullchain.pem
```

A small private certificate authority (such as `mkcert`) is nicer when several devices use the app:
install its root certificate on each device once, and every certificate it issues is trusted.

### Tailscale (simplest)

On the machine running the app, with Tailscale installed:

```sh
tailscale serve --bg http://127.0.0.1:8080
```

Then in `.env` (use your machine's Tailscale name):

```
HBUDGET_ALLOWED_HOSTS=127.0.0.1,localhost,budget-box.your-tailnet.ts.net
HBUDGET_PUBLIC_URL=https://budget-box.your-tailnet.ts.net
HBUDGET_COOKIE_SECURE=true
```

Only devices on your tailnet can reach it. Do **not** use `tailscale funnel`: that makes the app public
on the internet.

### A reverse proxy with HTTPS (Caddy example)

```
budget.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```
HBUDGET_ALLOWED_HOSTS=127.0.0.1,localhost,budget.example.com
HBUDGET_PUBLIC_URL=https://budget.example.com
HBUDGET_COOKIE_SECURE=true
HBUDGET_TRUST_PROXY=1
HBUDGET_FORWARDED_ALLOW_IPS=172.18.0.1
```

`HBUDGET_FORWARDED_ALLOW_IPS` is the address the proxy's connections come from **as the container sees
them**. With the default Compose network that is the network's gateway; with the app running, find it with
`docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' $(docker compose ps -q app)`
(this works whatever your project name is).
Without `HBUDGET_TRUST_PROXY`, every visitor looks like that one address, so the per-address login limit
would apply to everyone at once (the per-user limit still works).

Be aware what this trusts: **every** process on this machine that connects to the published port arrives
from that same gateway address, so any of them could send a made-up `X-Forwarded-For` header and get past
the per-address login limit. On a machine you share with other users or untrusted services, run the proxy as
a second service instead, and do not publish the app's port at all. For example, in a
`docker-compose.override.yml` next to `docker-compose.yml` (Compose reads it automatically):

```yaml
services:
  app:
    ports: !reset []           # no port on the host; only the proxy can reach the app
  proxy:
    image: caddy:2             # pin a version you trust
    ports: ["443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile:ro", "caddy-data:/data"]
volumes:
  caddy-data:
```

with `reverse_proxy app:8080` in the Caddyfile, and `HBUDGET_FORWARDED_ALLOW_IPS` set to the proxy
container's address (give it a fixed one with an `ipv4_address` on a user-defined network).

### nginx (or another proxy) on a different machine

A common home setup: nginx, or Nginx Proxy Manager, on another machine or LXC handles HTTPS and forwards to
this one. The app then stays on plain HTTP and needs no certificate (leave `HBUDGET_TLS_CERT` and
`HBUDGET_TLS_KEY` unset). In `.env` on the app's machine, with `192.168.1.60` being the nginx machine:

```
HBUDGET_BIND_ADDRESS=0.0.0.0
HBUDGET_ALLOWED_HOSTS=127.0.0.1,localhost,budget.example.com
HBUDGET_PUBLIC_URL=https://budget.example.com
HBUDGET_COOKIE_SECURE=true
HBUDGET_TRUST_PROXY=1
HBUDGET_FORWARDED_ALLOW_IPS=192.168.1.60
```

and in nginx:

```nginx
location / {
    proxy_pass http://192.168.1.50:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 6m;      # a little over HBUDGET_MAX_UPLOAD_MB
}
```

- `X-Forwarded-For $remote_addr` replaces whatever the browser sent. Do not use
  `$proxy_add_x_forwarded_for` here: it appends to a header the visitor controls.
- `Host $host` is required: the app answers only to the names in `HBUDGET_ALLOWED_HOSTS` (400 otherwise).
- `X-Forwarded-Proto` is how the app knows the browser used HTTPS (secure cookies, HSTS). Docker keeps the
  real source address of connections from other machines, so `HBUDGET_FORWARDED_ALLOW_IPS` is simply the
  nginx machine's address. To check, set `HBUDGET_TRUST_PROXY=0` for a moment: `docker compose logs app`
  then shows every request coming from the address to put there.
- The app's port still accepts plain HTTP from anything on the network, past nginx. Allow port 8080 only
  from the nginx machine with a firewall outside the app's machine; on Proxmox, the container's own firewall
  (Datacenter → container → Firewall), which Docker cannot bypass. Only nginx's address is trusted for
  `X-Forwarded-For`, so a direct visitor cannot fake their address either way.

Think twice before putting a financial app on the open internet; a private network such as Tailscale is
the safer choice.

## Bank connection through Plaid (optional)

Off by default: the app makes no outbound connections at all. A household whose bank has no usable export can
turn on Plaid instead (design and choices: [PLAID.md](PLAID.md)). You need **your own** Plaid developer account;
for real banks Plaid must approve it for production, which takes days or weeks and may cost money. Uploading
the bank's export stays the normal path and needs none of this.

1. **Try it with test banks first.** In the Plaid dashboard copy the client ID and the **sandbox** secret. Put the
   secret in a file next to the database, readable only by the app:
   ```sh
   printf '%s' 'the-secret' > data/plaid_secret
   chmod 600 data/plaid_secret && sudo chown 10001:10001 data/plaid_secret   # the app's user (HBUDGET_UID)
   ```
   and add to `.env`:
   ```sh
   HBUDGET_PLAID=sandbox
   HBUDGET_PLAID_CLIENT_ID=your-client-id
   HBUDGET_PLAID_SECRET_FILE=/data/plaid_secret
   ```
   Restart (`docker compose up -d`). The app refuses to start if the ID or secret is missing.
2. **Banks that sign you in on their own website (OAuth)** send the browser back to the app, which must be reached
   over HTTPS. Set `HBUDGET_PUBLIC_URL` to the https address people use (behind nginx or Caddy this is usually set
   already) and add `<that address>/plaid/oauth` under **Allowed redirect URIs** in the Plaid dashboard.
3. In the app an **owner** opens **More > Bank connection**, types the household name to turn it on, and links a
   bank (Plaid's sandbox user is `user_good` / `pass_good`). Choose what each account is for: one checking account
   is reconciled, credit cards go to the Card page, the rest is not used.
4. **Real banks:** replace the secret file with the **production** secret and set `HBUDGET_PLAID=production`.
   Links made in sandbox do not carry over: unlink them first (or turn the connection off), then link the real bank.

**Sync** is manual by default: **Sync with the bank** in the weekly check-in on Home, or **Sync now** on the Bank
connection page. An owner can set **Sync automatically** to *Daily* there: the app then fetches once a day and
keeps what it finds until the check-in brings it in. A sync becomes a waiting bank upload, reviewed like a file. Pending transactions are left out until they post.
Once the card comes through Plaid, stop uploading the card's CSV: its descriptions differ from Plaid's, so the
same purchase could be added twice.

**Turning it off** (Bank connection > Turn off, or `HBUDGET_PLAID=off` on the server) unlinks the banks at Plaid and
deletes the stored access tokens; the transactions already brought in stay. Access tokens are stored encrypted
with the key in `data/secret_key`: a database moved without that file (or a changed `HBUDGET_SECRET_KEY`) needs the
bank linked again. Backups and exports never include them.

## File ownership

The app runs as uid/gid 10001 (no root, read-only root filesystem, all capabilities dropped,
`no-new-privileges`). Docker creates a missing `./data` as root, so the one-shot `init` service sets the
folder's owner to the app user. It runs with only the `CHOWN` and `FOWNER` capabilities and no network,
touches the folder itself (never the files in it) and exits.

Because of this, your normal user cannot read `./data` directly; use `docker compose exec app ...` or
`sudo`. To own the files yourself instead, set `HBUDGET_UID` and `HBUDGET_GID` in `.env` to the output of
`id -u` and `id -g`, then `docker compose up -d`.

## Using the command-line tools with Docker

The image includes the `hbudget` command (run `docker compose exec app hbudget --help`). It reads files from
inside the container, and the container has no copy of this repository (no `examples/` folder) and a
read-only file system, so pass files on standard input with `-T`:

```sh
# import a bill-system file (YAML or JSON) into household 1; --dry-run first to see what it would do
docker compose exec -T app hbudget import-bills --household 1 --dry-run /dev/stdin < examples/bill-system.example.yaml
docker compose exec -T app hbudget import-bills --household 1 /dev/stdin < examples/bill-system.example.yaml

# reconcile a bank CSV export: a dry run lists the proposals (P1, P2, ...); then accept some and write.
# With the example bill system imported, examples/sample-bank.csv gives P1 (electric bill over budget) and
# P2 (a card payment covering four card-funded rows):
docker compose exec -T app hbudget reconcile --household 1 /dev/stdin < examples/sample-bank.csv
docker compose exec -T app hbudget reconcile --household 1 --accept P1:clear,P2:clear --write /dev/stdin < examples/sample-bank.csv

# back up (YAML or JSON) or export the forecast (csv, xlsx) to a file on this machine
(umask 077; docker compose exec -T app hbudget export --household 1 --format yaml > backup.yaml)   # mode 600
```

- **Accepting proposals:** `P1:clear` clears the proposed row(s); `P1:reduce` treats the posting as a partial
  payment and lowers the row by that amount, leaving the rest open (only offered when one row is larger than the payment). A bare `P1`
  means `P1:clear`. Proposals you leave out stay open for next time.
- **The household id** is the number in the page address, `/h/<id>/`. With only one household, `--household`
  can be left out.
- **File types on standard input:** YAML, JSON, OFX/QFX and the simple bills CSV are detected from the content,
  and bank CSV layouts from the header row.
- **Scripting the web API:** every request that changes something needs the `X-CSRF-Token` header with the
  token of a signed-in session. Pages carry it in their page-data JSON
  (`<script type="application/json" id="page-data">`, field `csrf`), and requests must come from an allowed
  origin. For automation, the CLI above is simpler.

## Without Docker

```sh
make install      # .venv with the app and dev tools
make dev          # http://127.0.0.1:8080, data in ./data, auto-reload
```

## Other machines (arm64)

Released images are published for `linux/amd64` and `linux/arm64` (for example a Raspberry Pi 4/5 with a 64-bit
OS); Docker pulls the right one. When building from source, `compose.build.yml` builds for the machine's own
platform. To build both platforms on one machine you need a buildx builder with QEMU emulation; then run
`make image-multiarch`.

## For maintainers: container tests

```sh
make container-test
```

builds the image and an image of an earlier release (`PREVIOUS_REF`, the last release tag; while it is empty the upgrade check is skipped), then,
in throwaway Compose projects with a clean `./data`, checks: first start and the setup screen, the token
file's mode and owner, setup and an import over HTTP, restart and `down`/`up` with the data intact, an
upgrade from the earlier image and to an image with one extra migration (data intact, migration applied,
backup written), and the hardening settings. CI runs the same tests on every push (without the upgrade
step) and builds both platforms.
