# TinyHTTP

An HTTP/1.1 and WebSocket server written from raw sockets in pure Python, with a small web framework on top. It shows what a framework does for you, from the first byte on the wire.

```python
from tinyhttp import App, Response

app = App()

@app.get("/hello/<name>")
def hello(req, name):
    return f"hello {name}"

@app.post("/items")
def add(req):
    return Response.json(req.json(), status=201)

@app.websocket("/chat/<room>")
async def chat(ws, req, room):
    while (message := await ws.receive()) is not None:
        await ws.send(f"{room}: {message}")

app.static("/files", "./public")
app.run(port=8000)
```

To serve a folder straight away:

```
python -m tinyhttp ./public --port 8000
```

## Features

- HTTP/1.1 parsing written by hand: keep-alive, pipelining, chunked bodies and `100-continue`
- Routing with typed parameters such as `<int:id>`, plus 404 and 405 replies with `Allow`
- Middleware that can wrap a request or stop it
- Static files with `ETag`, `If-None-Match`, byte ranges and protection against `../` paths
- Streaming responses, using sync or async generators
- WebSockets (RFC 6455): handshake, fragmented messages, ping, pong and close

## Security choices

The parser refuses what request-smuggling attacks depend on: conflicting `Content-Length` headers, `Content-Length` together with `Transfer-Encoding`, folded headers and bad header names. Size limits and timeouts apply to headers and bodies. A line break in a response header raises an error, which blocks header injection, and unhandled exceptions return a plain 500 without details.

## Limits

There is no TLS, HTTP/2 or compression, and it handles single byte ranges only. Put a production server such as nginx in front of it if it ever faces the internet. It was written to teach, and it has not been through a security review.

## Tests

The tests talk to a real server over real sockets, using Python's `http.client` and hand-written bytes.

```
python -m unittest discover -s tests
```

MIT licence.
