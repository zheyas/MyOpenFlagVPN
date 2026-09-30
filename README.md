# OpenFlux (Python port)

A pluggable-transport tunnel, ported from the original Go
[OpenFlux](https://github.com/p1neappleXpress) to Python (`asyncio`).

A SOCKS5 client multiplexes reliable TCP/UDP streams over an exotic carrier —
cups.online interview rooms, Yandex / Mail.ru collaborative documents, a MAX
call, or a plain TCP link — to an **L4 exit node** that re-dials the real
destination. The two ends never talk directly; they meet on the transport
channel.

> Research / educational port. No paid features. You are responsible for how
> you use it.

## What this port is (and isn't)

The Go project tunnels **raw IPv4 packets** and relies on a userspace TCP/IP
stack (gVisor) or a `utun`/packet-tunnel on the client, plus two exit backends
(`l3` raw SNAT/DNAT and `l4` gVisor proxy). Python has no practical equivalent
of gVisor, so this port takes the **L4-only** route:

- Instead of IP packets, the client and exit exchange **multiplexed logical
  streams** (see [`openflux/mux.py`](openflux/mux.py)). Because the document/room
  transports can drop, reorder and duplicate messages, the mux adds its own
  reliability (cumulative-ACK, windowed, retransmitting — TCP-lite), which is
  the job gVisor did in the Go client.
- The exit is always **L4**: it terminates each stream and re-dials the target.
  The Go `l3` raw-SNAT backend and the `utun`/iOS clients are intentionally
  omitted (they need root/kernel sockets or platform-specific packet tunnels).

Wire format is **not** compatible with the Go build — run the Python client
against the Python exit on both ends.

### Go → Python map

| Go | Python |
|----|--------|
| `transport/transport.go` | [`openflux/transport/base.py`](openflux/transport/base.py) |
| `transport/framing.go` (batched zstd) | [`openflux/transport/framing.py`](openflux/transport/framing.py) |
| `transport/batched.go` | [`openflux/transport/batched.py`](openflux/transport/batched.py) |
| `transport/encrypted.go` (AES-256-GCM + scrypt) | [`openflux/transport/encrypted.py`](openflux/transport/encrypted.py) |
| `transport/direct.go` | [`openflux/transport/direct.py`](openflux/transport/direct.py) |
| `transport/cupsonline/` | [`openflux/transport/cupsonline.py`](openflux/transport/cupsonline.py) |
| `transport/yandex/yandex.go` | [`openflux/transport/yandex_docs.py`](openflux/transport/yandex_docs.py) |
| `transport/yandex/vyandex.go` | [`openflux/transport/yandex_volga.py`](openflux/transport/yandex_volga.py) |
| `transport/yandex/boards.go` | [`openflux/transport/yandex_boards.py`](openflux/transport/yandex_boards.py) |
| `transport/yandex/captcha.go` (PoW) | [`openflux/transport/yandex_captcha.py`](openflux/transport/yandex_captcha.py) |
| `transport/mailru/mailru.go` | [`openflux/transport/mailru.py`](openflux/transport/mailru.py) |
| `transport/oneme/` (MAX / WebRTC) | [`openflux/transport/oneme.py`](openflux/transport/oneme.py) |
| gVisor client + raw IP | [`openflux/mux.py`](openflux/mux.py) + [`openflux/socks5.py`](openflux/socks5.py) |
| `tunnel/proxy_exit.go` (l4) | [`openflux/exit_node.py`](openflux/exit_node.py) |
| `main.go`, `conf.go` | [`openflux/cli.py`](openflux/cli.py), [`openflux/conf.py`](openflux/conf.py) |

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # + pip install lz4  for --transport=oneme
```

## Run

Every transport needs the **same** `--encryption-key-file` on both ends
(`direct` requires it). Generate one:

```bash
openssl rand -hex 32 > secret.txt
```

### cupsonline (recommended — no account)

Exit (creates rooms, prints a base64 room list, also serves `GET /rooms`):

```bash
python3 -m openflux --role exit --transport cupsonline --encryption-key-file secret.txt
```

Client (paste the printed room list as `--url`):

```bash
python3 -m openflux --role client --transport cupsonline \
  --url "<base64 room list>" --socks5 127.0.0.1:1080 --encryption-key-file secret.txt
```

### direct (self-hosted TCP exit)

```bash
# exit
python3 -m openflux --role exit  --transport direct --direct-listen 0.0.0.0:8445 --encryption-key-file secret.txt
# client
python3 -m openflux --role client --transport direct --direct-dial EXIT_HOST:8445 \
  --socks5 127.0.0.1:1080 --encryption-key-file secret.txt
```

Then point apps at `socks5h://127.0.0.1:1080`:

```bash
curl -x socks5h://127.0.0.1:1080 https://example.com
```

Other transports: `--transport yandex|vyandex|boards` with `--url <public doc>`,
`--transport mailru --url <weblink>`, `--transport oneme --oneme-token <t> --oneme-uid <uid>`.

### .conf files

Same INI-like format as the Go build (see `client.conf.example` /
`exit.conf.example`). Multi-transport failover is not ported — with several
`[Transport]` sections the highest-priority one is used.

```bash
python3 -m openflux --config client.conf
```

## Docker / Render

`docker compose --profile exit-node up -d --build` (or `--profile client`), or
deploy the exit to Render with the included `render.yaml`. See `.env.example`.

## Status

Verified end to end: `direct` and live `cupsonline` (create/join/round-trip),
the batched+zstd codec, and AES-256-GCM. The Yandex (`yandex`/`vyandex`/
`boards`), `mailru` and `oneme` transports are faithful ports of the Go auth /
signaling flows but require live public documents / MAX accounts to exercise,
which were not available during the port — treat them as untested against the
live services.
