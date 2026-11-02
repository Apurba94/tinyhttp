import asyncio
import http.client
import os
import shutil
import tempfile
import unittest

from tinyhttp import App, Headers, HttpError, Request, Response, accept_key, decode_frame, encode_frame, parse_head


class TestParsing(unittest.TestCase):
    def head(self, text):
        return parse_head(text.encode())

    def test_good(self):
        m, t, v, h = self.head("GET /a?b=1 HTTP/1.1\r\nHost: x\r\nX-A: 1")
        self.assertEqual((m, t, v, h.get("host"), h.get("x-a")), ("GET", "/a?b=1", "HTTP/1.1", "x", "1"))

    def test_rejects(self):
        bad = {
            "GET /\r\nHost: x": 400, "GET / HTTP/2.0\r\nHost: x": 505, "GET / HTTP/1.1": 400, "G T / HTTP/1.1\r\nHost: x": 400,
            "GET / HTTP/1.1\r\nHost: x\r\n folded: y": 400, "GET / HTTP/1.1\r\nHost: x\r\nBad Name: y": 400,
            "POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2": 400,
            "POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked": 400,
            "POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip": 501, "POST / HTTP/1.1\r\nHost: x\r\nContent-Length: -1": 400, "GET http://evil HTTP/1.1\r\nHost: x": 400,
        }
        for text, status in bad.items():
            with self.assertRaises(HttpError) as c:
                self.head(text)
            self.assertEqual(c.exception.status, status, text)

    def test_http10_needs_no_host(self):
        self.assertEqual(self.head("GET / HTTP/1.0")[2], "HTTP/1.0")

    def test_request_helpers(self):
        r = Request("POST", "/a%20b?x=1&y=2&y=3&z=", "HTTP/1.1", Headers(), b"k=v&w=%20")
        self.assertEqual(r.path, "/a b")
        self.assertEqual(r.query, {"x": "1", "y": ["2", "3"], "z": ""})
        self.assertEqual(r.form(), {"k": "v", "w": " "})

    def test_header_injection_is_refused(self):
        with self.assertRaises(ValueError):
            Response(b"", 200, {"X": "a\r\nSet-Cookie: evil"}).head_bytes()


class TestFrames(unittest.TestCase):
    def test_rfc_accept_key(self):
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_round_trip_sizes(self):
        for n in (0, 1, 125, 126, 65535, 65536, 70000):
            data = bytes(i % 251 for i in range(n))
            for mask in (None, b"\x01\x02\x03\x04"):
                f = encode_frame(2, data, mask=mask)
                self.assertEqual(decode_frame(bytearray(f)), (True, 2, data, len(f)))

    def test_partial(self):
        f = encode_frame(1, b"hello", mask=b"abcd")
        for i in range(len(f)):
            self.assertIsNone(decode_frame(bytearray(f[:i])))

    def test_bad_frames(self):
        with self.assertRaises(HttpError):
            decode_frame(bytearray(b"\xf1\x00"))
        with self.assertRaises(HttpError):
            decode_frame(bytearray(encode_frame(9, b"x" * 200)))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = App(max_header=2048, max_body=1000, timeout=2)
        self.seen = []
        a = self.app

        @a.get("/hello/<name>")
        def hello(req, name):
            return f"hello {name}"

        @a.get("/n/<int:n>")
        async def double(req, n):
            await asyncio.sleep(0)
            return {"double": n * 2}

        @a.post("/echo")
        def echo(req):
            return Response(req.body, content_type="application/octet-stream")

        @a.get("/boom")
        def boom(req):
            raise RuntimeError("secret detail")

        @a.get("/empty")
        def empty(req):
            return None

        @a.get("/stream")
        def stream(req):
            return Response(stream=(f"part{i}" for i in range(3)))

        @a.websocket("/ws/<room>")
        async def ws(sock, req, room):
            while True:
                m = await sock.receive()
                if m is None:
                    break
                await sock.send(f"{room}:{m}")

        self.app.log = self.seen.append
        with open(os.path.join(self.dir, "a.txt"), "wb") as f:
            f.write(b"0123456789")
        with open(os.path.join(self.dir, "index.html"), "w") as f:
            f.write("<h1>home</h1>")
        with open(os.path.join(self.dir, "..", "secret.txt"), "w") as f:
            f.write("secret")
        self.addCleanup(os.remove, os.path.join(self.dir, "..", "secret.txt"))
        a.static("/files", self.dir)
        await a.start(port=0)
        self.port = a.port

    async def asyncTearDown(self):
        await self.app.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def raw(self, data, reads=1):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(data)
        await w.drain()
        out = b""
        try:
            while True:
                chunk = await asyncio.wait_for(r.read(65536), 0.5)
                if not chunk:
                    break
                out += chunk
        except asyncio.TimeoutError:
            pass
        w.close()
        return out

    async def get(self, path, method="GET", body=None, headers=None):
        def call():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request(method, path, body=body, headers=headers or {})
            res = c.getresponse()
            data = res.read()
            c.close()
            return res.status, dict(res.getheaders()), data
        return await asyncio.to_thread(call)


class TestServer(Base):
    async def test_route_and_param(self):
        status, _, body = await self.get("/hello/world")
        self.assertEqual((status, body), (200, b"hello world"))

    async def test_typed_param_and_json(self):
        status, h, body = await self.get("/n/21")
        self.assertEqual((status, body, h["Content-Type"]), (200, b'{"double": 42}', "application/json"))
        self.assertEqual((await self.get("/n/abc"))[0], 404)

    async def test_404_405(self):
        self.assertEqual((await self.get("/nope"))[0], 404)
        status, h, _ = await self.get("/echo")
        self.assertEqual((status, h["Allow"]), (405, "POST"))

    async def test_post_and_head(self):
        self.assertEqual((await self.get("/echo", "POST", b"abc"))[2], b"abc")
        status, h, body = await self.get("/hello/x", "HEAD")
        self.assertEqual((status, h["Content-Length"], body), (200, "7", b""))

    async def test_errors_do_not_leak(self):
        status, _, body = await self.get("/boom")
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertIn("secret detail", self.seen[0])

    async def test_204(self):
        self.assertEqual((await self.get("/empty"))[0], 204)

    async def test_streaming_is_chunked(self):
        status, h, body = await self.get("/stream")
        self.assertEqual((status, h["Transfer-Encoding"], body), (200, "chunked", b"part0part1part2"))

    async def test_chunked_request(self):
        out = await self.raw(b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n2;x=1\r\nde\r\n0\r\n\r\n")
        self.assertTrue(out.endswith(b"abcde"), out)

    async def test_keep_alive_and_pipelining(self):
        req = b"GET /hello/a HTTP/1.1\r\nHost: x\r\n\r\nGET /hello/b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        out = await self.raw(req)
        self.assertEqual(out.count(b"HTTP/1.1 200"), 2)
        self.assertIn(b"hello a", out)
        self.assertIn(b"hello b", out)

    async def test_http10_closes(self):
        out = await self.raw(b"GET /hello/a HTTP/1.0\r\n\r\n")
        self.assertIn(b"Connection: close", out)

    async def test_limits(self):
        self.assertIn(b"431", await self.raw(b"GET / HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 4000))
        self.assertIn(b"413", await self.raw(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n\r\n"))
        self.assertIn(b"400", await self.raw(b"BAD\r\n\r\n"))

    async def test_many_at_once(self):
        results = await asyncio.gather(*[self.get(f"/n/{i}") for i in range(30)])
        self.assertTrue(all(r[0] == 200 for r in results))

    async def test_middleware_order_and_short_circuit(self):
        order = []

        @self.app.use
        async def first(req, next):
            order.append("in1")
            res = await next(req)
            order.append("out1")
            res.headers.set("X-Mw", "yes")
            return res

        @self.app.use
        async def gate(req, next):
            if req.path == "/hello/blocked":
                return Response("no", 403)
            return await next(req)
        status, h, _ = await self.get("/hello/x")
        self.assertEqual((status, h["X-Mw"], order), (200, "yes", ["in1", "out1"]))
        self.assertEqual((await self.get("/hello/blocked"))[0], 403)


class TestStatic(Base):
    async def test_file_and_index(self):
        status, h, body = await self.get("/files/a.txt")
        self.assertEqual((status, body, h["Content-Type"]), (200, b"0123456789", "text/plain"))
        self.assertEqual((await self.get("/files/index.html"))[2], b"<h1>home</h1>")

    async def test_path_traversal_is_blocked(self):
        for p in ("/files/../secret.txt", "/files/..%2fsecret.txt", "/files/%2e%2e/secret.txt"):
            out = await self.raw(f"GET {p} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            self.assertNotIn(b"secret", out.split(b"\r\n\r\n", 1)[-1], p)
            self.assertIn(b"404", out)

    async def test_etag(self):
        _, h, _ = await self.get("/files/a.txt")
        status, _, body = await self.get("/files/a.txt", headers={"If-None-Match": h["ETag"]})
        self.assertEqual((status, body), (304, b""))

    async def test_ranges(self):
        cases = {"bytes=2-4": (206, b"234", "bytes 2-4/10"), "bytes=7-": (206, b"789", "bytes 7-9/10"), "bytes=-2": (206, b"89", "bytes 8-9/10"), "bytes=8-99": (206, b"89", "bytes 8-9/10")}
        for rng, (status, body, cr) in cases.items():
            s, h, b = await self.get("/files/a.txt", headers={"Range": rng})
            self.assertEqual((s, b, h["Content-Range"]), (status, body, cr), rng)
        self.assertEqual((await self.get("/files/a.txt", headers={"Range": "bytes=50-"}))[0], 416)


class TestWebSocket(Base):
    async def open(self, path="/ws/room1"):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        return r, w, head

    async def read_frame(self, r):
        buf = bytearray()
        while True:
            f = decode_frame(buf)
            if f:
                return f
            buf += await asyncio.wait_for(r.read(4096), 2)

    async def test_echo(self):
        r, w, head = await self.open()
        self.assertIn(b"101", head)
        self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", head)
        w.write(encode_frame(1, "héllo".encode(), mask=b"abcd"))
        await w.drain()
        self.assertEqual((await self.read_frame(r))[2].decode(), "room1:héllo")
        w.close()

    async def test_ping_and_fragments(self):
        r, w, _ = await self.open()
        w.write(encode_frame(9, b"hi", mask=b"abcd"))
        self.assertEqual((await self.read_frame(r))[1:3], (10, b"hi"))
        w.write(encode_frame(1, b"he", fin=False, mask=b"abcd") + encode_frame(0, b"llo", mask=b"abcd"))
        await w.drain()
        self.assertEqual((await self.read_frame(r))[2], b"room1:hello")
        w.close()

    async def test_close_handshake(self):
        r, w, _ = await self.open()
        w.write(encode_frame(8, (1000).to_bytes(2, "big"), mask=b"abcd"))
        await w.drain()
        self.assertEqual((await self.read_frame(r))[1], 8)
        w.close()


if __name__ == "__main__":
    unittest.main()
