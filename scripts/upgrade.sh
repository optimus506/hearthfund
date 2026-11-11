#!/bin/sh
# Upgrade a Docker Compose deployment to the newest released image, safely (docs/DEPLOY.md, "Upgrading").
#
#   scripts/upgrade.sh            run from the folder with docker-compose.yml (or pass the folder: -C DIR)
#
# 1. pulls the image; stops here if it is already the one running
# 2. backs up the database from the running container (`hbudget backup`) and remembers the running image
# 3. starts the new image and waits until Docker reports it healthy
# 4. if it does not become healthy: keeps the failed database for inspection, puts the backup back and starts
#    the previous image again
#
# Exit status: 0 upgraded or already up to date, 1 rolled back, 2 could not start (nothing was changed).
set -eu

dir=.
while [ $# -gt 0 ]; do
  case "$1" in
    -C) dir="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
cd "$dir"
[ -f docker-compose.yml ] || { echo "no docker-compose.yml in $(pwd)" >&2; exit 2; }

say() { printf '%s\n' "$*"; }

# A deployment made before the app was renamed (0.15 and earlier) runs as the Compose project
# "household-budget". Keep that name for this folder, or Compose would start a second app next to the old one.
if [ -z "${COMPOSE_PROJECT_NAME:-}" ] && ! grep -qs '^COMPOSE_PROJECT_NAME=' .env; then
  old=$(docker ps -aq --filter label=com.docker.compose.project=household-budget \
        --filter "label=com.docker.compose.project.working_dir=$(pwd -P)" 2>/dev/null || true)
  if [ -n "$old" ]; then
    printf '\n# Set by scripts/upgrade.sh: this deployment was made under the old name; keep its containers.\nCOMPOSE_PROJECT_NAME=household-budget\n' >> .env
    say "This deployment was made before the rename to Hearthfund: added COMPOSE_PROJECT_NAME=household-budget to .env"
    say "so Docker Compose keeps using the same containers. Nothing else changes."
  fi
fi

TIMEOUT=${HBUDGET_UPGRADE_TIMEOUT:-180}
case "$TIMEOUT" in
  ''|*[!0-9]*) echo "HBUDGET_UPGRADE_TIMEOUT must be a number of seconds, not '$TIMEOUT'" >&2; exit 2 ;;
esac
ROLLBACK_TAG=hearthfund:rollback

image=$(docker compose config --images | head -n 1)
case "$image" in
  */household-budget:*|*/household-budget)
    say "Note: HBUDGET_IMAGE in .env names the old image ($image). Releases after 0.15 are published as"
    say "ghcr.io/optimus506/hearthfund; change or remove HBUDGET_IMAGE to get them." ;;
esac
cid=$(docker compose ps -q app 2>/dev/null || true)
id_of() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }
# the image the app is running now (not the tag: the image may have a new name since the last upgrade)
old_id=$( [ -n "$cid" ] && docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)

if [ -z "$cid" ] || [ -z "$old_id" ]; then
  say "The app is not running (or its image is gone), so there is nothing to roll back to: pulling and starting it."
  docker compose pull --quiet app init || { say "Could not pull $image. Check the network and HBUDGET_IMAGE in .env."; exit 2; }
  docker compose up -d || { say "Docker Compose could not start the app; see the messages above."; exit 2; }
  say "Started $image."
  exit 0
fi
# Keep a name on the current image before the pull moves the tag: with Docker's containerd image store an
# untagged image is removed at once, and the rollback must be able to start it by name.
docker tag "$old_id" "$ROLLBACK_TAG"

say "Pulling $image ..."
docker compose pull --quiet app init || {
  say "Could not pull $image. Check the network and the image name (HBUDGET_IMAGE in .env; docs/DEPLOY.md)."
  say "Built from source? This script is for released images: use 'git pull && docker compose up -d --build'."
  exit 2
}
if [ "$(id_of "$image")" = "$old_id" ]; then
  say "Already up to date ($image)."
  exit 0
fi

say "Backing up the database ..."
backup=$(docker compose exec -T app hbudget backup 2>&1) || {
  say "$backup"
  say "The backup failed, so nothing was changed. (Versions before 0.2.0 have no 'hbudget backup';"
  say "stop the app, copy data/hbudget.db somewhere safe, then run: docker compose up -d)"
  exit 2
}
say "$backup"
# "backup: /data/backups/hbudget-<time>.db (...)" -> the file name
file=$(printf '%s\n' "$backup" | sed -n 's#^backup: /data/backups/\([^ ]*\) .*#\1#p' | head -n 1)
case "$file" in
  hbudget-*.db) ;;
  *) say "Could not tell which backup file was just made, so a rollback could not restore it."
     say "Nothing was changed: the current version is still running."
     exit 2 ;;
esac

say "Starting the new version ..."
docker compose up -d || say "Docker Compose reported an error starting the new version (see above)."

waited=0
status=starting
while [ "$waited" -lt "$TIMEOUT" ]; do
  cid=$(docker compose ps -q app 2>/dev/null || true)
  status=$( [ -n "$cid" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo missing)
  [ "$status" = healthy ] && break
  sleep 3
  waited=$((waited + 3))
done

if [ "$status" = healthy ]; then
  say "Upgraded: $image is healthy. The previous image is kept as $ROLLBACK_TAG until the next upgrade."
  exit 0
fi

say "The new version is not healthy after ${TIMEOUT}s (status: $status). Its last log lines:"
docker compose logs --tail 30 app || true
say "Rolling back to the previous image ..."
docker compose stop app || true
# with the app stopped: keep the database the new version left, then put the backup in its place
# (the file name goes in as an argument, never into the program text)
HBUDGET_IMAGE=$ROLLBACK_TAG docker compose run --rm --no-deps -T --entrypoint python app -c "
import os, shutil, sys, time
name = sys.argv[1]
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
shutil.copy2('/data/hbudget.db', '/data/backups/hbudget-failed-upgrade-%s.db' % stamp)
for side in ('-wal', '-shm'):
    if os.path.exists('/data/hbudget.db' + side):
        os.remove('/data/hbudget.db' + side)
shutil.copy2(os.path.join('/data/backups', os.path.basename(name)), '/data/hbudget.db')
print('restored /data/backups/%s; the failed database is backups/hbudget-failed-upgrade-%s.db' % (name, stamp))
" "$file" || {
  say "Could not put the backup back. The database the new version left is still in place, and the previous"
  say "version will refuse to start on it. Restore data/backups/$file by hand (docs/DEPLOY.md, \"Upgrading\")."
  exit 1
}
HBUDGET_IMAGE=$ROLLBACK_TAG docker compose up -d || {
  say "The backup was put back, but the previous version did not start; see the messages above."
  exit 1
}
say ""
say "Rolled back: the previous version is running again from $ROLLBACK_TAG, on the database from before."
say "Plain 'docker compose up -d' would start the new image again. To stay on the previous release, set"
say "HBUDGET_IMAGE to its version in .env (for example ghcr.io/optimus506/hearthfund:1.0.0),"
say "and report the log lines above."
exit 1
