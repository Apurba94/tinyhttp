"""HTTP/1.1 parsing and WebSocket framing, with no sockets in sight."""
import base64
import hashlib
import struct
from urllib.parse import parse_qs, unquote, urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STATUS = {200: "OK", 201: "Created", 204: "No Content", 206: "Partial Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout", 413: "Payload Too Large", 416: "Range Not Satisfiable", 431: "Request Header Fields Too Large", 500: "Internal Server Error", 501: "Not Implemented", 505: "HTTP Version Not Supported"}
TOKEN = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


class HttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or STATUS.get(status, ""))
        self.status = status


class Headers:
    """Case-insensitive headers that keep repeated values."""

    def __init__(self, items=()):
        self.items = list(items)

    def get(self, name, default=None):
        low = name.lower()
        for k, v in self.items:
            if k.lower() == low:
                return v
        return default

    def get_all(self, name):
        low = name.lower()
        return [v for k, v in self.items if k.lower() == low]

    def add(self, name, value):
        self.items.append((name, value))

    def set(self, name, value):
        low = name.lower()
        self.items = [(k, v) for k, v in self.items if k.lower() != low]
        self.items.append((name, value))

    def __contains__(self, name):
        return self.get(name) is not None


class Request:
    def __init__(self, method, target, version, headers, body=b""):
        self.method, self.target, self.version, self.headers, self.body = method, target, version, headers, body
        parts = urlsplit(target)
        self.path = unquote(parts.path)
        self.query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.params = {}
        self.state = {}

    def json(self):
        import json
        try:
            return json.loads(self.body)
        except ValueError:
            raise HttpError(400, "body is not valid JSON") from None

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def keep_alive(self):
        c = (self.headers.get("Connection") or "").lower()
        return "close" not in c if self.version == "HTTP/1.1" else "keep-alive" in c


def parse_head(data):
    """Parse a request line and headers. Everything a smuggling attack relies on is rejected."""
    try:
        lines = data.decode("latin-1").split("\r\n")
    except UnicodeDecodeError:
        raise HttpError(400) from None
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HttpError(400, "bad request line")
    method, target, version = parts
    if not method or not set(method) <= TOKEN:
        raise HttpError(400, "bad method")
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(505)
    if not target.startswith("/") and target != "*":
        raise HttpError(400, "bad target")
    headers = Headers()
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t":
            raise HttpError(400, "folded headers are not allowed")
        name, sep, value = line.partition(":")
        if not sep or not name or not set(name) <= TOKEN:
            raise HttpError(400, "bad header")
        if "\r" in value or "\n" in value or "\0" in value:
            raise HttpError(400, "bad header value")
        headers.add(name, value.strip(" \t"))
    if version == "HTTP/1.1" and "Host" not in headers:
        raise HttpError(400, "missing Host header")
    lengths = headers.get_all("Content-Length")
    if len(set(lengths)) > 1:
        raise HttpError(400, "conflicting Content-Length")
    if lengths and "Transfer-Encoding" in headers:
        raise HttpError(400, "both Content-Length and Transfer-Encoding")
    te = headers.get("Transfer-Encoding")
    if te and te.lower() != "chunked":
        raise HttpError(501, "only chunked is supported")
    if lengths and not lengths[0].isdigit():
        raise HttpError(400, "bad Content-Length")
    return method, target, version, headers


class Response:
    def __init__(self, body=b"", status=200, headers=None, content_type="text/plain; charset=utf-8", stream=None):
        self.status = status
        self.headers = headers if isinstance(headers, Headers) else Headers(list((headers or {}).items()))
        if isinstance(body, str):
            body = body.encode()
        self.body = body
        self.stream = stream
        if "Content-Type" not in self.headers and (body or stream):
            self.headers.add("Content-Type", content_type)

    @classmethod
    def json(cls, value, status=200):
        import json
        return cls(json.dumps(value, ensure_ascii=False), status, content_type="application/json")

    @classmethod
    def html(cls, text, status=200):
        return cls(text, status, content_type="text/html; charset=utf-8")

    @classmethod
    def redirect(cls, url, status=302):
        return cls(b"", status, {"Location": url})

    def head_bytes(self, length=None):
        lines = [f"HTTP/1.1 {self.status} {STATUS.get(self.status, 'Unknown')}"]
        for k, v in self.headers.items:
            if "\r" in str(v) or "\n" in str(v):
                raise ValueError("header value contains a line break")
            lines.append(f"{k}: {v}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


TEXT, BINARY, CLOSE, PING, PONG, CONT = 1, 2, 8, 9, 10, 0


def encode_frame(opcode, payload, fin=True, mask=None):
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        head.append(flag | n)
    elif n < 65536:
        head.append(flag | 126)
        head += struct.pack(">H", n)
    else:
        head.append(flag | 127)
        head += struct.pack(">Q", n)
    if mask:
        head += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def decode_frame(buf):
    """Return (fin, opcode, payload, bytes used), or None if more data is needed."""
    if len(buf) < 2:
        return None
    fin, opcode = bool(buf[0] & 0x80), buf[0] & 0x0F
    if buf[0] & 0x70:
        raise HttpError(400, "reserved bits set")
    masked, n = bool(buf[1] & 0x80), buf[1] & 0x7F
    pos = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack_from(">H", buf, 2)[0]
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack_from(">Q", buf, 2)[0]
        pos = 10
    if opcode >= 8 and (n > 125 or not fin):
        raise HttpError(400, "bad control frame")
    mask = None
    if masked:
        if len(buf) < pos + 4:
            return None
        mask = buf[pos:pos + 4]
        pos += 4
    if len(buf) < pos + n:
        return None
    data = bytes(buf[pos:pos + n])
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, opcode, data, pos + n
