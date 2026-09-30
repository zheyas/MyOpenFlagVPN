#!/bin/sh
# Maps environment variables to openflux (Python) flags.
#   client    — SOCKS5 proxy.
#   exit-node — L4 stream proxy (the only backend in the Python port).
set -eu

role="${ROLE:-client}"
transport="${TRANSPORT:-cupsonline}"
listen="${SOCKS5_LISTEN:-:1080}"

case "$role" in
  client|exit-node) ;;
  *) echo "ROLE must be 'client' or 'exit-node' (got '$role')" >&2; exit 2 ;;
esac

if [ "$role" = exit-node ]; then
  set -- --role exit --transport "$transport"
else
  set -- --role client --transport "$transport" --socks5 "$listen"
fi

# Shared AES-256-GCM secret.
if [ -n "${ENCRYPTION_KEY:-}" ]; then
  printf '%s' "$ENCRYPTION_KEY" > /tmp/openflux.key
  set -- "$@" --encryption-key-file /tmp/openflux.key
elif [ -n "${ENCRYPTION_KEY_FILE:-}" ]; then
  set -- "$@" --encryption-key-file "$ENCRYPTION_KEY_FILE"
fi

# DOC_URL is the room list / document URL; URL is a backward-compatible alias.
doc_url="${DOC_URL:-${URL:-}}"
[ -n "$doc_url" ] && set -- "$@" --url "$doc_url"
[ -n "${MAX_TOKEN:-}" ] && set -- "$@" --oneme-token "$MAX_TOKEN"
[ -n "${MAX_UID:-}" ] && set -- "$@" --oneme-uid "$MAX_UID"
case "${DEBUG:-0}" in 1|true|yes) set -- "$@" --debug ;; esac

# The exit's HTTP endpoint (health + GET /rooms) binds $PORT when set (Render).
echo "[entrypoint] exec: openflux $*"
exec python3 -m openflux "$@"
