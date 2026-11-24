#!/bin/sh
# ChortVPN full-tunnel helper for macOS. Runs as root (started once per
# connect via an administrator-privileges prompt) and owns everything that
# needs root:
#   * tun2socks on a utun device, forwarding into the app's local SOCKS5
#   * routes: all IPv4 (0/1 + 128/1) into the utun, the carrier's own hosts
#     (cups.online ...) around it via the original gateway
# It then watches the app: when the app writes the stop file, or the app
# process disappears (crash), or tun2socks dies, it removes the routes and
# stops tun2socks. So disconnecting needs no second password prompt and a
# crash never leaves the Mac without internet.
#
# usage: tunhelper.sh <tun2socks> <socks-port> <app-pid> <state-dir> <device> [bypass-ip ...]
set -u

T2S="$1"; PORT="$2"; APP_PID="$3"; STATE="$4"; DEV="$5"; shift 5
BYPASS="$*"
TUN_ADDR=198.18.0.1
LOG="$STATE/helper.log"

log() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }

case "$PORT" in ''|*[!0-9]*) log "bad port"; exit 2 ;; esac
case "$APP_PID" in ''|*[!0-9]*) log "bad pid"; exit 2 ;; esac
case "$DEV" in utun[0-9]*) ;; *) log "bad device"; exit 2 ;; esac
for ip in $BYPASS; do
  case "$ip" in *[!0-9.]*) log "bad bypass ip $ip"; exit 2 ;; esac
done

rm -f "$STATE/up" "$STATE/stop" "$STATE/failed"
GW=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2; exit}')
GW_IF=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')
if [ -n "$GW" ]; then VIA="$GW"
elif [ -n "$GW_IF" ]; then VIA="-interface $GW_IF"   # e.g. another VPN owns the default route
else log "no default route"; echo "no default route" > "$STATE/failed"; exit 1; fi
log "via $VIA, device $DEV, bypass: $BYPASS"

"$T2S" --device "$DEV" --proxy "socks5://127.0.0.1:$PORT" --loglevel warn >> "$STATE/tun2socks.log" 2>&1 &
T2S_PID=$!

i=0
while ! ifconfig "$DEV" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ $i -gt 50 ] || ! kill -0 $T2S_PID 2>/dev/null; then
    log "tun2socks did not create $DEV"; echo "tun2socks failed to start" > "$STATE/failed"
    kill $T2S_PID 2>/dev/null; exit 1
  fi
  sleep 0.1
done

ifconfig "$DEV" "$TUN_ADDR" "$TUN_ADDR" up
for ip in $BYPASS; do route -q -n add -host "$ip" $VIA >/dev/null 2>&1; done
route -q -n add -net 0.0.0.0/1 "$TUN_ADDR" >/dev/null 2>&1
route -q -n add -net 128.0.0.0/1 "$TUN_ADDR" >/dev/null 2>&1
echo "$T2S_PID" > "$STATE/up"
log "tunnel up"

cleanup() {
  route -q -n delete -net 0.0.0.0/1 "$TUN_ADDR" >/dev/null 2>&1
  route -q -n delete -net 128.0.0.0/1 "$TUN_ADDR" >/dev/null 2>&1
  for ip in $BYPASS; do route -q -n delete -host "$ip" >/dev/null 2>&1; done
  kill $T2S_PID 2>/dev/null
  rm -f "$STATE/up"
  log "tunnel down ($1)"
}
trap 'cleanup signal; exit 0' INT TERM HUP

while :; do
  if [ -f "$STATE/stop" ]; then cleanup "stop requested"; break; fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then cleanup "app exited"; break; fi
  if ! kill -0 $T2S_PID 2>/dev/null; then cleanup "tun2socks exited"; break; fi
  sleep 1
done
rm -f "$STATE/stop"
