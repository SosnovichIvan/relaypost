#!/usr/bin/env bash
# Trusted release archive and credentials must already be in root/incoming.
set -Eeuo pipefail
umask 077
version=${1:?version required}
expected_sha=${2:?archive checksum required}
if [[ ! "$version" =~ ^([0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+)$ ]] || [[ ! "$expected_sha" =~ ^[0-9a-f]{64}$ ]]; then
  echo 'Invalid deployment identifier or checksum' >&2
  exit 1
fi
root=${RELAYPOST_ROOT:-/opt/relaypost}
archive="$root/incoming/$version.tgz"
credentials="$root/incoming/$version.env"
exec 9>"$root/deploy.lock"
flock -w 300 9
actual_sha=$(sha256sum "$archive")
[[ "${actual_sha%% *}" == "$expected_sha" ]] || { echo 'Archive checksum mismatch' >&2; exit 1; }
release="$root/releases/$version"
install -d -m 700 "$release"
tar --extract --gzip --file="$archive" --directory="$release" --no-same-owner
test -f "$release/Dockerfile"
test -f "$release/deploy/compose.yml"
old_release=''
if [[ -L "$root/current" ]]; then old_release=$(readlink -f "$root/current"); fi
export RELAYPOST_IMAGE="relaypost:$version"
export RELAYPOST_TUNNEL_IMAGE="relaypost-tunnel:$version"
export RELAYPOST_ENV_FILE="$root/shared/bot.env"
export RELAYPOST_DATA_DIR="$root/data"
export RELAYPOST_PROXY_KEY_FILE="$root/shared/proxy_ed25519"
export RELAYPOST_PROXY_KNOWN_HOSTS_FILE="$root/shared/proxy_known_hosts"
export RELAYPOST_PROXY_SSH_TARGET
RELAYPOST_PROXY_SSH_TARGET=$(cat "$root/shared/proxy_target")
[[ "$RELAYPOST_PROXY_SSH_TARGET" =~ ^[a-zA-Z0-9_-]+@[a-zA-Z0-9.-]+$ ]] || { echo 'Invalid proxy SSH target' >&2; exit 1; }
test -s "$RELAYPOST_PROXY_KEY_FILE"
test -s "$RELAYPOST_PROXY_KNOWN_HOSTS_FILE"
compose=(docker compose --project-name relaypost --file "$release/deploy/compose.yml")
# Compare encryption keys before touching the running service.
python3 - "$credentials" "$RELAYPOST_ENV_FILE" <<'PY'
import sys
from pathlib import Path
def key(path):
    return next(line.split('=', 1)[1] for line in path.read_text().splitlines()
                if line.startswith('RELAYPOST_ENCRYPTION_KEY='))
new, old = map(Path, sys.argv[1:])
if old.exists() and key(new) != key(old):
    raise SystemExit('Encryption key changed; explicit data migration is required')
PY
docker build --target bot --tag "$RELAYPOST_IMAGE" "$release"
docker build --target tunnel --tag "$RELAYPOST_TUNNEL_IMAGE" "$release"
# Verify the closed tunnel before stopping a running poller or touching its DB.
# The preflight container receives no bot token, key, env_file, or data mount.
RELAYPOST_ENV_FILE="$credentials" "${compose[@]}" up -d --wait --wait-timeout 60 --no-build telegram-proxy
docker run --rm --network relaypost_default --read-only \
  --env TELEGRAM_PROXY_URL=http://telegram-proxy:19840 \
  --entrypoint python "$RELAYPOST_IMAGE" -m relaypost.network
backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$version"
install -d -m 700 "$backup"
if [[ -f "$RELAYPOST_ENV_FILE" ]]; then cp "$RELAYPOST_ENV_FILE" "$backup/bot.env"; fi
rollback() {
  result=$?
  trap - ERR
  echo 'Deployment failed; stopping candidate' >&2
  "${compose[@]}" stop bot || true
  if [[ -n "$old_release" && -f "$backup/bot.env" ]]; then
    cp "$backup/bot.env" "$RELAYPOST_ENV_FILE"
    export RELAYPOST_IMAGE="relaypost:$(basename "$old_release")"
    export RELAYPOST_TUNNEL_IMAGE="relaypost-tunnel:$(basename "$old_release")"
    docker compose --project-name relaypost --file "$old_release/deploy/compose.yml" up -d --no-build --remove-orphans
    echo 'Previous container restored; inspect readiness and database compatibility' >&2
  fi
  exit "$result"
}
trap rollback ERR
# Stop the previous poller before opening the same token/database from the candidate.
if [[ -n "$old_release" ]]; then
  docker compose --project-name relaypost --file "$old_release/deploy/compose.yml" stop bot
fi
if [[ -f "$root/data/relaypost.db" ]]; then
  python3 - "$root/data/relaypost.db" "$backup/relaypost.db" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
PY
fi
install -m 600 "$credentials" "$RELAYPOST_ENV_FILE"
"${compose[@]}" config --quiet
"${compose[@]}" up -d --no-build --remove-orphans
container=$("${compose[@]}" ps --quiet bot)
ready=0
for ((attempt=0; attempt<75; attempt++)); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
  if [[ "$health" == healthy ]]; then ready=1; break; fi
  sleep 2
done
[[ "$ready" == 1 ]] || { echo 'Telegram polling readiness deadline exceeded' >&2; false; }
ln -sfn "$release" "$root/current"
printf '%s\n' "$version" > "$root/shared/version"
rm -f "$credentials" "$archive"
printf 'RelayPost deployed: %s; polling healthy\n' "$version"
