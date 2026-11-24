"""Failover of a multi-transport session, end to end through the mux.

Client and exit each run a MultiTransport over two in-memory carriers
(primary priority 100, backup 50). An echo stream must survive the primary
going silently dead mid-stream, and traffic must return to the primary once
it comes back.

Run:  .venv/bin/python -m unittest tests.test_multi_failover -v
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openflux.exit_node import ExitNode  # noqa: E402
from openflux.mux import Mux  # noqa: E402
from openflux.transport import multi  # noqa: E402
from openflux.transport.base import Transport  # noqa: E402


class Pipe(Transport):
    """One end of an in-memory carrier; `alive=False` swallows everything
    while still claiming to be connected (the nasty failure mode)."""

    def __init__(self, link):
        self.link, self.peer, self.cb = link, None, None

    async def start(self): pass
    async def stop(self): pass
    def is_connected(self): return True
    def set_receive(self, cb): self.cb = cb

    async def send(self, data):
        self.link["sent"] += 1
        if not self.link["alive"]:
            return
        asyncio.get_running_loop().call_later(0.02, self.peer.cb, data)


def pipe_pair():
    link = {"alive": True, "sent": 0}
    a, b = Pipe(link), Pipe(link)
    a.peer, b.peer = b, a
    return link, a, b


class FailoverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        multi.PING_INTERVAL = 1.0      # speed the test up
        multi.SILENCE_LIMIT = 2.0
        self.primary, pc, pe = pipe_pair()
        self.backup, bc, be = pipe_pair()
        self.client_t = multi.MultiTransport([multi.Child("primary", 100, pc), multi.Child("backup", 50, bc)])
        self.exit_t = multi.MultiTransport([multi.Child("primary", 100, pe), multi.Child("backup", 50, be)])

        async def echo(r, w):
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
            w.close()
        self.srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]

        self.exit_node = ExitNode(self.exit_t)
        await self.exit_t.start()
        await self.exit_node.start()
        self.mux = Mux(self.client_t, is_exit=False)
        await self.client_t.start()
        await self.mux.start()

    async def asyncTearDown(self):
        self.srv.close()
        await self.client_t.stop()
        await self.exit_t.stop()

    async def roundtrip(self, st, payload, timeout=20):
        await st.write(payload)
        got = b""
        deadline = time.monotonic() + timeout
        while len(got) < len(payload) and time.monotonic() < deadline:
            chunk = await asyncio.wait_for(st.read(), deadline - time.monotonic())
            got += chunk
        return got

    async def test_failover_and_back(self):
        st = await self.mux.open_tcp("127.0.0.1", self.port, timeout=10)
        msg = os.urandom(50_000)
        self.assertEqual(await self.roundtrip(st, msg), msg)
        self.assertGreater(self.primary["sent"], 0)
        backup_before = self.backup["sent"]

        # Primary dies silently, mid-stream.
        self.primary["alive"] = False
        t0 = time.monotonic()
        msg2 = os.urandom(200_000)
        self.assertEqual(await self.roundtrip(st, msg2, timeout=30), msg2)
        took = time.monotonic() - t0
        print(f"\n  failover: 200 KB echoed via backup in {took:.1f}s")
        self.assertGreater(self.backup["sent"] - backup_before, 20)

        # Primary comes back: traffic should return to it.
        self.primary["alive"] = True
        await asyncio.sleep(3)          # a ping round revives it
        sent_before = self.primary["sent"]
        msg3 = os.urandom(50_000)
        self.assertEqual(await self.roundtrip(st, msg3), msg3)
        self.assertGreater(self.primary["sent"] - sent_before, 10)
        live = {c["name"]: c["live"] for c in self.client_t.status()}
        self.assertTrue(live["primary"])

    async def test_single_peer_ignores_pings(self):
        # A mux peer that runs a single transport must not RST on our pings.
        st = await self.mux.open_tcp("127.0.0.1", self.port, timeout=10)
        await self.client_t.children[0].transport.send(multi.CTRL_PING)
        msg = b"still fine"
        self.assertEqual(await self.roundtrip(st, msg), msg)


if __name__ == "__main__":
    unittest.main()
