#!/bin/sh
# Run the OpenFlux (Python) exit node using .env.server as the source of truth.
#
#   ENCRYPTION_KEY      shared AES key (same value the client uses) — the binding
#   DOC_URL             optional: pin the cups.online room list (else create new)
#   OPENFLUX_HTTP_ADDR  optional: bind the health + /rooms endpoint (e.g. :8080).
#                       On Render this is $PORT instead — do not set it there.
#   DEBUG               1 for verbose logging
#
# Usage:  ./run-exit.sh                 # reads ./.env.server
#         ENV_FILE=other ./run-exit.sh
set -eu

env_file="${ENV_FILE:-./.env.server}"
[ -f "$env_file" ] || { echo "no env file: $env_file" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

: "${ENCRYPTION_KEY:?set ENCRYPTION_KEY in $env_file}"

# DOC_URL is optional (empty => create fresh rooms); URL is an accepted alias.
DOC_URL="${DOC_URL:-${URL:-}}"

# The HTTP endpoint binds $PORT (Render) or OPENFLUX_HTTP_ADDR; export the
# latter so the CLI picks it up.
[ -n "${OPENFLUX_HTTP_ADDR:-}" ] && export OPENFLUX_HTTP_ADDR

keyfile="$(mktemp)"
trap 'rm -f "$keyfile"' EXIT
printf '%s' "$ENCRYPTION_KEY" > "$keyfile"

set -- python3 -m openflux --role exit --transport "${TRANSPORT:-cupsonline}" \
  --encryption-key-file "$keyfile"
[ -n "$DOC_URL" ] && set -- "$@" --url "$DOC_URL"
[ "${DEBUG:-0}" = "1" ] && set -- "$@" --debug

echo "[run-exit] starting exit (${TRANSPORT:-cupsonline}); watch for the room list / GET /rooms"
exec "$@"
