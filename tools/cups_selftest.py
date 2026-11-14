"""cups.online self-test: does data actually flow through cups.online from
THIS network, without involving the exit node?

Creates one fresh private room, joins it twice (peer A and peer B) and sends
messages of growing size A->B and B->A, reporting what arrived and how fast.
Run it on the network you want to check (e.g. with the VPN off):

    .venv/bin/python tools/cups_selftest.py

Nothing secret is sent; the room is new and used only by this test.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openflux.transport import cupsonline as cups  # noqa: E402
from openflux.transport.base import default_config  # noqa: E402

SIZES = [64, 1024, 4096, 8000, 16000]
PER_SIZE = 3
WAIT = 15.0


async def main() -> int:
    os.environ["OPENFLUX_CUPS_ROOMS"] = "1"
    print("1) creating a fresh room on cups.online ...")
    t0 = time.monotonic()
    auths = await cups._create_rooms(1, 0.5)
    rid = auths[0].room_uuid
    print(f"   ok in {time.monotonic() - t0:.1f}s; websocket host: {urlparse(auths[0].conn_url).netloc}")
    if auths[0].session is not None:
        await auths[0].session.close()   # creator session is not needed further

    url = cups.pack_rooms([rid])
    peers = {name: cups.CupsonlineTransport(url, default_config(), True) for name in ("A", "B")}
    got = {name: [] for name in peers}
    for name, tr in peers.items():
        tr.set_receive(lambda data, n=name: got[n].append((time.monotonic(), data)))

    print("2) joining the room twice (peer A and peer B) ...")
    for tr in peers.values():
        await tr.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not all(
            any(ws.connected for ws in tr._wss) for tr in peers.values()):
        await asyncio.sleep(0.2)
    for name, tr in peers.items():
        print(f"   peer {name}: websocket {'connected' if any(ws.connected for ws in tr._wss) else 'NOT connected'}")

    ok_all = True
    for src, dst in (("A", "B"), ("B", "A")):
        print(f"3) sending {src} -> {dst}")
        for size in SIZES:
            sent_at = {}
            for i in range(PER_SIZE):
                payload = bytes([size % 251, i]) + os.urandom(size - 2)
                sent_at[payload] = time.monotonic()
                try:
                    await peers[src].send(payload)
                except Exception as e:  # noqa: BLE001
                    print(f"   {size:>6} B: send failed: {e}")
            end = time.monotonic() + WAIT
            while time.monotonic() < end:
                if sum(1 for _, d in got[dst] if d in sent_at) >= PER_SIZE:
                    break
                await asyncio.sleep(0.1)
            arrived = [(t, d) for t, d in got[dst] if d in sent_at]
            lat = [t - sent_at[d] for t, d in arrived]
            status = "OK " if len(arrived) == PER_SIZE else "LOST"
            ok_all &= len(arrived) == PER_SIZE
            extra = f", latency {min(lat):.2f}-{max(lat):.2f}s" if lat else ""
            print(f"   {status} {size:>6} B: {len(arrived)}/{PER_SIZE} arrived{extra}")

    for tr in peers.values():
        await tr.stop()
    print("\nRESULT:", "data flows through cups.online from this network"
          if ok_all else "some messages did NOT get through (see LOST lines)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
