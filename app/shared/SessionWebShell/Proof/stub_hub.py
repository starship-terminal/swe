#!/usr/bin/env python3
"""A stand-in hub for the G2 proof harness. Stdlib only, loopback only.

Two servers on 127.0.0.1:

* the hub (``--port``, default 8765) serves the fixture page and a few
  routes shaped like the real hub's: ``/v1/events`` (an event stream, one
  event every ``interval`` ms), ``/v1/snapshot`` (JSON with a fresh token
  each call), ``/v1/echo`` (POST). Every request must carry the shell's
  headers: ``Authorization: Bearer <credential>``, ``X-Swe-Pair: 1`` and
  ``Origin: http://127.0.0.1:<port>``; anything else gets a 401. The
  credential is never echoed back.
* the control port (``--control``, default 8766), which the UI tests use:
  ``POST /hub/stop`` closes the hub's listening socket (connections are
  refused, as with a stopped hub), ``POST /hub/start`` opens it again,
  ``GET /requests`` lists the requests the hub saw (headers included),
  ``GET /health``.
"""

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture")


class Hub:
    def __init__(self, port, credential):
        self.port = port
        self.credential = credential
        self.origin = f"http://{HOST}:{port}"
        self.server = None
        self.thread = None
        self.lock = threading.Lock()
        self.log = []

    def record(self, handler, status):
        with self.lock:
            self.log.append({
                "at": time.time(),
                "method": handler.command,
                "path": handler.path,
                "status": status,
                "headers": {k: v for k, v in handler.headers.items()},
            })
            del self.log[:-500]

    def start(self):
        with self.lock:
            if self.server:
                return
            hub = self

            class Handler(HubHandler):
                pass

            Handler.hub = hub
            server = ThreadingHTTPServer((HOST, self.port), Handler)
            server.daemon_threads = True
            self.server = server
            self.thread = threading.Thread(target=server.serve_forever, daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    @property
    def running(self):
        return self.server is not None


class HubHandler(BaseHTTPRequestHandler):
    hub = None
    # HTTP/1.0 with no Content-Length on the stream: the body runs until
    # the connection closes, and every write goes out at once.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("hub: " + (fmt % args) + "\n")

    def gate(self):
        auth = self.headers.get("Authorization") == f"Bearer {self.hub.credential}"
        pair = self.headers.get("X-Swe-Pair") == "1"
        origin = self.headers.get("Origin") == self.hub.origin
        return auth, pair, origin

    def send(self, status, body, ctype, extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.hub.record(self, status)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def refuse(self):
        self.send(401, json.dumps({"error": "unauthorized"}), "application/json")

    def do_GET(self):
        if not all(self.gate()):
            return self.refuse()
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self.file("index.html", "text/html; charset=utf-8")
        if url.path == "/proof.js":
            return self.file("proof.js", "text/javascript; charset=utf-8")
        if url.path == "/v1/snapshot":
            body = {
                "token": uuid.uuid4().hex[:8],
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "rows": ["Last reply from the agent, saved with the snapshot."],
            }
            return self.send(200, json.dumps(body), "application/json")
        if url.path == "/v1/events":
            return self.events(parse_qs(url.query))
        self.send(404, json.dumps({"error": "not found"}), "application/json")

    def do_POST(self):
        auth, pair, origin = self.gate()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if not (auth and pair and origin):
            return self.refuse()
        if urlparse(self.path).path == "/v1/echo":
            reply = {"body": body, "auth_ok": auth, "pair_ok": pair, "origin_ok": origin}
            return self.send(200, json.dumps(reply), "application/json")
        self.send(404, json.dumps({"error": "not found"}), "application/json")

    def file(self, name, ctype):
        with open(os.path.join(FIXTURE, name), "rb") as f:
            self.send(200, f.read(), ctype)

    def events(self, qs):
        count = int(qs.get("count", ["10"])[0])
        interval = int(qs.get("interval", ["200"])[0]) / 1000
        self.hub.record(self, 200)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            for i in range(count):
                time.sleep(interval)
                sent = int(time.time() * 1000)
                line = f"id: {i}\ndata: {json.dumps({'i': i, 'sent': sent})}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def control_handler(hub):
    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("control: " + (fmt % args) + "\n")

        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/requests":
                with hub.lock:
                    return self.reply(list(hub.log))
            self.reply({"hub_running": hub.running, "port": hub.port})

        def do_POST(self):
            if self.path == "/hub/stop":
                hub.stop()
            elif self.path == "/hub/start":
                hub.start()
            elif self.path == "/requests/clear":
                with hub.lock:
                    hub.log.clear()
            self.reply({"hub_running": hub.running})

    return Control


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--control", type=int, default=8766)
    parser.add_argument("--credential", default="fake-proof-credential-0000")
    args = parser.parse_args()
    hub = Hub(args.port, args.credential)
    hub.start()
    control = ThreadingHTTPServer((HOST, args.control), control_handler(hub))
    control.daemon_threads = True
    sys.stderr.write(f"stub hub on {hub.origin}, control on http://{HOST}:{args.control}\n")
    try:
        control.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        control.server_close()


if __name__ == "__main__":
    main()
