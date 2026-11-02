"""The application: routing, middleware, static files, keep-alive and WebSockets."""
import asyncio
import hashlib
import inspect
import mimetypes
import os
import re
import time
import traceback
from email.utils import formatdate, parsedate_to_datetime

from .protocol import BINARY, CLOSE, CONT, PING, PONG, TEXT, Headers, HttpError, Request, Response, accept_key, decode_frame, encode_frame, parse_head

CONVERT = {"int": (r"\d+", int), "str": (r"[^/]+", str), "path": (r".+", str), "float": (r"\d+\.?\d*", float)}


class WebSocket:
    def __init__(self, reader, writer, max_message):
        self.r, self.w, self.max = reader, writer, max_message
        self.buf = bytearray()
        self.closed = False

    async def _frame(self):
        while True:
            frame = decode_frame(self.buf)
            if frame:
                del self.buf[:frame[3]]
                return frame
            chunk = await self.r.read(65536)
            if not chunk:
                self.closed = True
                return None
            self.buf += chunk
            if len(self.buf) > self.max + 16:
                await self.close(1009)
                return None

    async def receive(self):
        """Return the next message, or None once the peer has closed."""
        parts, kind = [], None
        while not self.closed:
            frame = await self._frame()
            if frame is None:
                return None
            fin, op, data, _ = frame
            if op == PING:
                await self._send(PONG, data)
            elif op == PONG:
                pass
            elif op == CLOSE:
                await self.close(int.from_bytes(data[:2], "big") if len(data) >= 2 else 1000)
                return None
            elif op in (TEXT, BINARY, CONT):
                if op != CONT:
                    kind = op
                parts.append(data)
                if sum(map(len, parts)) > self.max:
                    await self.close(1009)
                    return None
                if fin:
                    body = b"".join(parts)
                    return body.decode("utf-8", "replace") if kind == TEXT else body
        return None

    async def _send(self, op, data):
        self.w.write(encode_frame(op, data))
        await self.w.drain()

    async def send(self, message):
        await self._send(TEXT if isinstance(message, str) else BINARY, message.encode() if isinstance(message, str) else message)

    async def close(self, code=1000):
        if not self.closed:
            self.closed = True
            try:
                await self._send(CLOSE, code.to_bytes(2, "big"))
            except ConnectionError:
                pass


class App:
    def __init__(self, max_header=16384, max_body=10_000_000, timeout=30):
        self.routes = []
        self.middleware = []
        self.sockets = []
        self.max_header, self.max_body, self.timeout = max_header, max_body, timeout
        self.server = None
        self.log = None

    def _compile(self, pattern):
        rx, types, pos = "", {}, 0
        for m in re.finditer(r"<(?:(\w+):)?(\w+)>", pattern):
            rx += re.escape(pattern[pos:m.start()])
            kind = m.group(1) or "str"
            if kind not in CONVERT:
                raise ValueError(f"unknown type {kind}")
            rx += f"(?P<{m.group(2)}>{CONVERT[kind][0]})"
            types[m.group(2)] = CONVERT[kind][1]
            pos = m.end()
        return re.compile(rx + re.escape(pattern[pos:]) + "$"), types

    def route(self, pattern, methods=("GET",)):
        def add(fn):
            rx, types = self._compile(pattern)
            self.routes.append((rx, types, {m.upper() for m in methods}, fn))
            return fn
        return add

    def get(self, pattern):
        return self.route(pattern, ("GET",))

    def post(self, pattern):
        return self.route(pattern, ("POST",))

    def use(self, fn):
        """Add middleware: async def fn(request, next) -> Response."""
        self.middleware.append(fn)
        return fn

    def websocket(self, pattern):
        def add(fn):
            self.sockets.append((*self._compile(pattern), fn))
            return fn
        return add

    def static(self, prefix, folder):
        """Serve a folder, with ETags, ranges and protection against ../ paths."""
        folder = os.path.realpath(folder)
        prefix = prefix.rstrip("/")

        def serve(req, path):
            full = os.path.realpath(os.path.join(folder, path))
            if not (full == folder or full.startswith(folder + os.sep)):
                raise HttpError(404)
            if os.path.isdir(full):
                full = os.path.join(full, "index.html")
            if not os.path.isfile(full):
                raise HttpError(404)
            return send_file(req, full)
        self.route(prefix + "/<path:path>", ("GET", "HEAD"))(serve)
        return serve

    async def dispatch(self, req):
        allowed = set()
        handler = None
        for rx, types, methods, fn in self.routes:
            m = rx.match(req.path)
            if m:
                if req.method in methods or (req.method == "HEAD" and "GET" in methods):
                    req.params = {k: types[k](v) for k, v in m.groupdict().items()}
                    handler = fn
                    break
                allowed |= methods
        if handler is None:
            if allowed:
                return Response("method not allowed", 405, {"Allow": ", ".join(sorted(allowed))})
            raise HttpError(404)

        async def call(i, r):
            if i < len(self.middleware):
                return await self.middleware[i](r, lambda rr: call(i + 1, rr))
            result = handler(r, **r.params)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Response):
                return result
            if isinstance(result, (dict, list)):
                return Response.json(result)
            return Response(result if result is not None else b"", 200 if result is not None else 204)
        return await call(0, req)

    async def handle(self, req):
        try:
            return await self.dispatch(req)
        except HttpError as exc:
            return Response(str(exc), exc.status)
        except Exception:
            if self.log:
                self.log(traceback.format_exc())
            return Response("internal server error", 500)

    async def _read_body(self, reader, headers, extra):
        buf = bytearray(extra)

        async def more():
            chunk = await reader.read(65536)
            if not chunk:
                raise HttpError(400, "connection closed mid-body")
            buf.extend(chunk)
        if (headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = bytearray()
            while True:
                while b"\r\n" not in buf:
                    await more()
                    if len(buf) > 1024 and b"\r\n" not in buf:
                        raise HttpError(400, "bad chunk size")
                i = buf.index(b"\r\n")
                try:
                    size = int(bytes(buf[:i]).split(b";")[0], 16)
                except ValueError:
                    raise HttpError(400, "bad chunk size") from None
                if size < 0 or len(body) + size > self.max_body:
                    raise HttpError(413)
                del buf[:i + 2]
                while len(buf) < size + 2:
                    await more()
                if size == 0:
                    del buf[:2]
                    return bytes(body), bytes(buf)
                if bytes(buf[size:size + 2]) != b"\r\n":
                    raise HttpError(400, "bad chunk")
                body += buf[:size]
                del buf[:size + 2]
        n = int(headers.get("Content-Length") or 0)
        if n > self.max_body:
            raise HttpError(413)
        while len(buf) < n:
            await more()
        return bytes(buf[:n]), bytes(buf[n:])

    async def _send(self, writer, req, res, keep):
        res.headers.set("Date", formatdate(usegmt=True))
        res.headers.set("Server", "tinyhttp")
        if not keep:
            res.headers.set("Connection", "close")
        if res.status not in (204, 304):
            if res.stream is None:
                res.headers.set("Content-Length", str(len(res.body)))
            elif "Content-Length" not in res.headers:
                res.headers.set("Transfer-Encoding", "chunked")
        writer.write(res.head_bytes())
        if req.method != "HEAD" and res.status not in (204, 304):
            if res.stream is None:
                writer.write(res.body)
            else:
                chunked = "Transfer-Encoding" in res.headers
                async for part in _iterate(res.stream):
                    part = part.encode() if isinstance(part, str) else part
                    writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n" if chunked else part)
                    await writer.drain()
                if chunked:
                    writer.write(b"0\r\n\r\n")
        await writer.drain()

    async def _upgrade(self, reader, writer, req, extra):
        for rx, types, fn in self.sockets:
            m = rx.match(req.path)
            if m and (req.headers.get("Upgrade") or "").lower() == "websocket":
                key = req.headers.get("Sec-WebSocket-Key")
                if not key or req.headers.get("Sec-WebSocket-Version") != "13":
                    raise HttpError(400, "bad WebSocket request")
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
                await writer.drain()
                ws = WebSocket(reader, writer, self.max_body)
                ws.buf += extra
                req.params = {k: types[k](v) for k, v in m.groupdict().items()}
                await fn(ws, req, **req.params)
                await ws.close()
                return True
        return False

    async def client(self, reader, writer):
        extra = b""
        try:
            while True:
                try:
                    head, extra = await asyncio.wait_for(self._next_head(reader, extra), self.timeout)
                    if head is None:
                        return
                    req = Request(*parse_head(head))
                    if await self._upgrade(reader, writer, req, extra):
                        return
                    if "100-continue" in (req.headers.get("Expect") or "").lower():
                        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                        await writer.drain()
                    req.body, extra = await asyncio.wait_for(self._read_body(reader, req.headers, extra), self.timeout)
                except HttpError as exc:
                    writer.write(Response(str(exc), exc.status, {"Connection": "close", "Content-Length": str(len(str(exc)))}).head_bytes() + str(exc).encode())
                    await writer.drain()
                    return
                except asyncio.TimeoutError:
                    return
                keep = req.keep_alive()
                await self._send(writer, req, await self.handle(req), keep)
                if not keep:
                    return
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def _next_head(self, reader, extra):
        """Read a head, starting with bytes left from the last request, which keeps pipelining working."""
        buf = bytearray(extra)
        while True:
            i = buf.find(b"\r\n\r\n")
            if i >= 0:
                return bytes(buf[:i]), bytes(buf[i + 4:])
            if len(buf) > self.max_header:
                raise HttpError(431)
            chunk = await reader.read(4096)
            if not chunk:
                if not buf:
                    return None, b""
                raise HttpError(400, "connection closed mid-request")
            buf += chunk

    async def start(self, host="127.0.0.1", port=8000):
        self.server = await asyncio.start_server(self.client, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def run(self, host="127.0.0.1", port=8000):
        async def main():
            await self.start(host, port)
            print(f"listening on http://{host}:{self.port}")
            await self.server.serve_forever()
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass


async def _iterate(stream):
    if hasattr(stream, "__aiter__"):
        async for part in stream:
            yield part
    else:
        for part in stream:
            yield part


def send_file(req, path, chunk=65536):
    """Build a response for a file: conditional requests, and single byte ranges."""
    st = os.stat(path)
    etag = '"' + hashlib.md5(f"{st.st_size}-{st.st_mtime_ns}".encode()).hexdigest()[:16] + '"'
    kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
    headers = Headers([("ETag", etag), ("Accept-Ranges", "bytes"), ("Last-Modified", formatdate(st.st_mtime, usegmt=True))])
    match = req.headers.get("If-None-Match")
    if match and etag in [m.strip() for m in match.split(",")]:
        return Response(b"", 304, headers)
    since = req.headers.get("If-Modified-Since")
    if since and not match:
        try:
            if int(st.st_mtime) <= parsedate_to_datetime(since).timestamp():
                return Response(b"", 304, headers)
        except (TypeError, ValueError):
            pass
    headers.add("Content-Type", kind)
    size = st.st_size
    start, stop, status = 0, size, 200
    rng = req.headers.get("Range")
    if rng:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                stop = min(int(m.group(2)) + 1, size) if m.group(2) else size
            else:
                start, stop = max(size - int(m.group(2)), 0), size
            if start >= size or start >= stop:
                headers.add("Content-Range", f"bytes */{size}")
                return Response(b"", 416, headers)
            status = 206
            headers.add("Content-Range", f"bytes {start}-{stop - 1}/{size}")

    def body():
        with open(path, "rb") as f:
            f.seek(start)
            left = stop - start
            while left:
                part = f.read(min(chunk, left))
                if not part:
                    return
                left -= len(part)
                yield part
    headers.add("Content-Length", str(stop - start))
    return Response(b"", status, headers, stream=body())
