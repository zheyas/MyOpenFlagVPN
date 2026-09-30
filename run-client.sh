#!/bin/sh
# Run the OpenFlux (Python) cupsonline client using .env as the source of truth.
#
#   ENCRYPTION_KEY   shared AES key (same value the exit uses) — the real binding
#   DOC_URL          cups.online room list (same value the exit uses)
#   ROOMS_URL        exit's /rooms endpoint; fetched live so you never copy the
#                    room list by hand. Overrides DOC_URL when it succeeds.
#   SOCKS5_LISTEN    local SOCKS5 address (default :1080)
#
# Usage:  ./run-client.sh            # reads ./.env
#         ENV_FILE=other ./run-client.sh
set -eu

env_file="${ENV_FILE:-./.env}"
[ -f "$env_file" ] || { echo "no env file: $env_file" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

: "${ENCRYPTION_KEY:?set ENCRYPTION_KEY in $env_file}"
listen="${SOCKS5_LISTEN:-:1080}"

# URL is a backward-compatible alias for DOC_URL.
DOC_URL="${DOC_URL:-${URL:-}}"

# Room list: prefer a live fetch from the exit's /rooms endpoint (ROOMS_URL) so
# you never copy it by hand; fall back to a pinned DOC_URL in .env.
#
# The endpoint answers 503 while the exit is still creating its rooms, and a
# free Render instance may be asleep and take up to a minute to wake — so retry
# for a while (ROOMS_RETRIES x ROOMS_RETRY_DELAY, default ~90s) before giving up.
if [ -n "${ROOMS_URL:-}" ]; then
  retries="${ROOMS_RETRIES:-30}"
  delay="${ROOMS_RETRY_DELAY:-3}"
  echo "[run-client] fetching room list from $ROOMS_URL (up to ${retries} tries)"
  fetched=""
  i=0
  while [ "$i" -lt "$retries" ]; do
    fetched="$(curl -fsS -m 20 "$ROOMS_URL" | tr -d '[:space:]')" && [ -n "$fetched" ] && break
    fetched=""
    i=$((i + 1))
    [ "$i" -lt "$retries" ] && { echo "[run-client] exit not ready yet, retrying in ${delay}s ($i/$retries)"; sleep "$delay"; }
  done
  if [ -n "$fetched" ]; then
    DOC_URL="$fetched"
    echo "[run-client] got room list from exit"
  elif [ -n "${DOC_URL:-}" ]; then
    echo "[run-client] fetch failed; using DOC_URL from $env_file" >&2
  fi
fi
: "${DOC_URL:?set ROOMS_URL or DOC_URL in $env_file}"

keyfile="$(mktemp)"
trap 'rm -f "$keyfile"' EXIT
printf '%s' "$ENCRYPTION_KEY" > "$keyfile"

set -- python3 -m openflux --role client --transport "${TRANSPORT:-cupsonline}" \
  --socks5 "$listen" --url "$DOC_URL" --encryption-key-file "$keyfile"
[ "${DEBUG:-0}" = "1" ] && set -- "$@" --debug

echo "[run-client] SOCKS5 on $listen, rooms ${DOC_URL%${DOC_URL#??????}}… (Ctrl+C to stop)"
exec "$@"
