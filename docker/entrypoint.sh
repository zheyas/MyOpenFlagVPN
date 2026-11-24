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
  # MODE=l3: raw IPv4 + NAT (needs NET_ADMIN and /dev/net/tun; still serves l4).
  set -- --role exit --transport "$transport" --mode "${MODE:-l4}"
else
  # INBOUND=tun: L3 client (root, TUN); default socks5.
  set -- --role client --transport "$transport" --socks5 "$listen" --inbound "${INBOUND:-socks5}"
fi
[ -n "${DIRECT_LISTEN:-}" ] && set -- "$@" --direct-listen "$DIRECT_LISTEN"
[ -n "${DIRECT_DIAL:-}" ] && set -- "$@" --direct-dial "$DIRECT_DIAL"

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
# Failover: TRANSPORTS=cupsonline:100,yandex:50 runs several carriers at once;
# each takes its own document URL from <TYPE>_URL (else DOC_URL).
[ -n "${TRANSPORTS:-}" ] && set -- "$@" --transports "$TRANSPORTS"
[ -n "${CUPSONLINE_URL:-}" ] && set -- "$@" --cupsonline-url "$CUPSONLINE_URL"
[ -n "${YANDEX_URL:-}" ] && set -- "$@" --yandex-url "$YANDEX_URL"
[ -n "${VYANDEX_URL:-}" ] && set -- "$@" --vyandex-url "$VYANDEX_URL"
[ -n "${BOARDS_URL:-}" ] && set -- "$@" --boards-url "$BOARDS_URL"
[ -n "${MAILRU_URL:-}" ] && set -- "$@" --mailru-url "$MAILRU_URL"
[ -n "${MAX_TOKEN:-}" ] && set -- "$@" --oneme-token "$MAX_TOKEN"
[ -n "${MAX_UID:-}" ] && set -- "$@" --oneme-uid "$MAX_UID"
case "${DEBUG:-0}" in 1|true|yes) set -- "$@" --debug ;; esac

# The exit's HTTP endpoint (health + GET /rooms) binds $PORT when set (Render).
echo "[entrypoint] exec: openflux $*"
exec python3 -m openflux "$@"
