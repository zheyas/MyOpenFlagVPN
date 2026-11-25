"""Rooms mailbox and in-band announcements (openflux.rooms_sync).

The Yandex Disk REST API is replaced by a small local fake with the same
endpoints, so the publish -> public download -> decrypt path runs for real.

Run:  .venv/bin/python -m unittest tests.test_rooms_sync -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from openflux import rooms_sync as rs  # noqa: E402

SECRET = "test-secret-0123456789abcdef"
TOKEN = "fake-oauth-token"


class FakeDisk:
    def __init__(self):
        self.files = {}
        self.public = {}
        self.base = ""

    def app(self):
        a = web.Application()
        a.router.add_get("/v1/disk/resources/upload", self.upload_link)
        a.router.add_put("/upload/{path}", self.upload)
        a.router.add_put("/v1/disk/resources/publish", self.publish)
        a.router.add_get("/v1/disk/resources", self.meta)
        a.router.add_get("/v1/disk/resources/download", self.own_download)
        a.router.add_get("/v1/disk/public/resources/download", self.public_download)
        a.router.add_get("/file/{path}", self.file)
        return a

    def _auth(self, req):
        if req.headers.get("Authorization") != f"OAuth {TOKEN}":
            raise web.HTTPUnauthorized(text="bad token")

    async def upload_link(self, req):
        self._auth(req)
        return web.json_response({"href": f"{self.base}/upload/{req.query['path'].replace('/', '_')}", "method": "PUT"})

    async def upload(self, req):
        self.files[req.match_info["path"]] = await req.read()
        return web.Response(status=201)

    async def publish(self, req):
        self._auth(req)
        key = req.query["path"].replace("/", "_")
        self.public[f"https://disk.yandex.ru/d/{len(self.public) + 1}"] = key
        return web.json_response({"href": "ok"})

    async def meta(self, req):
        self._auth(req)
        key = req.query["path"].replace("/", "_")
        url = next(u for u, k in self.public.items() if k == key)
        return web.json_response({"public_url": url})

    async def own_download(self, req):
        self._auth(req)
        key = req.query["path"].replace("/", "_")
        if key not in self.files:
            raise web.HTTPNotFound(text="DiskNotFoundError")
        return web.json_response({"href": f"{self.base}/file/{key}"})

    async def public_download(self, req):
        key = self.public.get(req.query["public_key"])
        if key is None:
            raise web.HTTPNotFound(text="DiskNotFoundError")
        return web.json_response({"href": f"{self.base}/file/{key}"})

    async def file(self, req):
        return web.Response(body=self.files[req.match_info["path"]])


class RoomsSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.disk = FakeDisk()
        self.runner = web.AppRunner(self.disk.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.disk.base = f"http://127.0.0.1:{port}"
        self._api = rs.API
        rs.API = f"{self.disk.base}/v1/disk"

    async def asyncTearDown(self):
        rs.API = self._api
        await self.runner.cleanup()

    def test_announce_frames(self):
        f = rs.announce_frame("ROOMS123")
        self.assertEqual(rs.parse_announce(f), "ROOMS123")
        self.assertIsNone(rs.parse_announce(b"\x01mux packet"))
        self.assertEqual(f[0], 0)          # never a mux type, never IPv4

    def test_seal_roundtrip_and_wrong_key(self):
        blob = rs.seal(SECRET, "LIST")
        self.assertEqual(rs.unseal(SECRET, blob)["rooms"], "LIST")
        self.assertNotIn(b"LIST", blob)    # the list is not readable from the file
        with self.assertRaises(Exception):
            rs.unseal("another-secret-0123456789", blob)

    async def test_publish_then_client_fetch(self):
        url = await rs.publish(TOKEN, rs.seal(SECRET, "ROOMS-V1"))
        self.assertTrue(url.startswith("https://disk.yandex.ru/d/"))
        self.assertEqual(await rs.fetch_rooms(url, SECRET), "ROOMS-V1")
        # The exit republishes a new list under the same link.
        url2 = await rs.publish(TOKEN, rs.seal(SECRET, "ROOMS-V2"))
        self.assertEqual(await rs.fetch_rooms(url, SECRET), "ROOMS-V2")
        self.assertTrue(url2)
        # The exit reads its own mailbox back after a restart.
        self.assertEqual(rs.unseal(SECRET, await rs.read_own(TOKEN))["rooms"], "ROOMS-V2")

    async def test_read_own_missing(self):
        self.assertIsNone(await rs.read_own(TOKEN))


class FakeCups:
    """Stands in for CupsonlineTransport's room management."""

    class _Cfg:
        num_rooms = 2

    def __init__(self):
        self._cfg = self._Cfg()
        self.rooms = ["old-1", "old-2"]
        self.made = 0

    def room_list(self):
        from openflux.transport.cupsonline import pack_rooms
        return pack_rooms(self.rooms)

    def live_room_ids(self):
        return list(self.rooms)

    async def add_rooms(self, n):
        new = [f"new-{self.made + i + 1}" for i in range(n)]
        self.made += n
        self.rooms += new
        return new

    async def retire_rooms(self, ids):
        self.rooms = [r for r in self.rooms if r not in ids]


class Wire:
    """Exit-side transport whose sends land in the client's receive path."""

    def __init__(self, client_rx):
        self.client_rx = client_rx

    async def send(self, data):
        self.client_rx(data)


class AnnounceRotationTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_follows_rotation(self):
        import asyncio
        from openflux import cli
        from openflux.transport.cupsonline import unpack_rooms
        from chortvpn.engine import _Counting

        class Inner:                       # minimal inner transport for _Counting
            def set_receive(self, cb): self.cb = cb

        inner = Inner()
        client = _Counting(inner)
        saved, mux_got = [], []
        client.on_announce = lambda r: saved.append(unpack_rooms(r))
        client.set_receive(mux_got.append)

        old_interval = rs.ANNOUNCE_INTERVAL
        rs.ANNOUNCE_INTERVAL = 0.05
        os.environ["OPENFLUX_CUPS_ROTATE_HOURS"] = str(0.4 / 3600)        # rotate every 0.4 s
        os.environ["OPENFLUX_CUPS_ROTATE_GRACE_HOURS"] = str(0.2 / 3600)  # retire 0.2 s later
        os.environ.pop("YADISK_TOKEN", None)
        cups = FakeCups()
        task = asyncio.ensure_future(cli._exit_rooms_manager(Wire(inner.cb), cups, SECRET))
        try:
            await asyncio.sleep(1.2)
        finally:
            task.cancel()
            rs.ANNOUNCE_INTERVAL = old_interval
            for k in ("OPENFLUX_CUPS_ROTATE_HOURS", "OPENFLUX_CUPS_ROTATE_GRACE_HOURS"):
                os.environ.pop(k, None)

        self.assertEqual(mux_got, [])                       # announcements never reach the mux
        self.assertEqual(saved[0], ["old-1", "old-2"])
        self.assertTrue(any(set(s) == {"old-1", "old-2", "new-1", "new-2"} for s in saved),
                        "overlap list (old + new) announced")
        self.assertTrue(any(s[:2] == ["new-1", "new-2"] and "old-1" not in s for s in saved),
                        "after the grace period only fresh rooms are announced")


if __name__ == "__main__":
    unittest.main()
