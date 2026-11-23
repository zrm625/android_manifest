"""The host test server.

Two listeners, same handler:
- 127.0.0.1:<local_port>, reached from the phone over ``adb reverse``;
- <lan_addr>:<lan_port> on forge's LAN, for the Wi-Fi check. forge's ufw must
  allow that port from the phone's address only (one-time setup, qa/README.md).

Routes (every token is fresh per run and per consumer):
  GET  /page/<token>     the test page with the token (Cache-Control: no-store)
  GET  /lan/<token>      plain text: the token
  POST /beacon/<token>   the page script's beacon (204)
  GET  /clip.mp4         the H.264 fixture
  POST /upload/<run>/<name>   helper uploads (frames, audio, JPEGs)

Every request is logged with path, token, User-Agent, client address and time.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class TestServer:
    def __init__(self, page: Path, clip: Path, upload_dir: Path, log_path: Path, local_port: int,
                 lan_addr: str | None, lan_port: int | None, upload_token: str):
        self.page_tmpl = page.read_text()
        self.clip = clip
        self.upload_dir = upload_dir
        self.log_path = log_path
        self.upload_token = upload_token
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.servers: list[ThreadingHTTPServer] = []
        self.lan_error: str | None = None
        self._start("127.0.0.1", local_port)
        if lan_addr and lan_port:
            try:
                self._start(lan_addr, lan_port)
            except OSError as e:
                self.lan_error = f"cannot listen on {lan_addr}:{lan_port}: {e}"

    def _start(self, addr: str, port: int) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _log(self, kind, token, extra=None):
                rec = {"t": time.time(), "kind": kind, "path": self.path, "token": token,
                       "ua": self.headers.get("User-Agent", ""), "client": self.client_address[0],
                       "listener": f"{addr}:{port}", "method": self.command, **(extra or {})}
                with outer.lock:
                    outer.requests.append(rec)
                    with open(outer.log_path, "a") as f:
                        f.write(json.dumps(rec) + "\n")

            def _send(self, code, body=b"", ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) == 2 and parts[0] in ("page", "lan") and TOKEN_RE.match(parts[1]):
                    self._log(parts[0], parts[1])
                    if parts[0] == "page":
                        page = outer.page_tmpl
                        if "nobeacon" in self.path:  # the beacon-blocked control
                            page = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
                            page = page.replace("<title>loading</title>", f"<title>{parts[1]}</title>")
                        body = page.replace("@TOKEN@", parts[1]).encode()
                        return self._send(200, body, "text/html; charset=utf-8")
                    return self._send(200, f"river-qa {parts[1]}\n".encode())
                if parts == ["clip.mp4"]:
                    self._log("clip", None)
                    return self._send(200, outer.clip.read_bytes(), "video/mp4")
                self._log("unknown", None)
                self._send(404)

            def do_POST(self):
                parts = self.path.split("?")[0].strip("/").split("/")
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64 << 20:
                    return self._send(413)
                data = self.rfile.read(n) if n else b""
                if len(parts) == 2 and parts[0] == "beacon" and TOKEN_RE.match(parts[1]):
                    self._log("beacon", parts[1])
                    return self._send(204)
                if (len(parts) == 3 and parts[0] == "upload" and parts[1] == outer.upload_token
                        and NAME_RE.match(parts[2]) and addr == "127.0.0.1"):
                    outer.upload_dir.mkdir(parents=True, exist_ok=True)
                    (outer.upload_dir / parts[2]).write_bytes(data)
                    self._log("upload", None, {"name": parts[2], "bytes": len(data)})
                    return self._send(200, b"ok")
                self._log("unknown", None)
                self._send(404)

            def do_HEAD(self):
                self._send(405)

        srv = ThreadingHTTPServer((addr, port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)

    def find(self, kind: str, token: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["kind"] == kind and r["token"] == token]

    def stop(self) -> None:
        for s in self.servers:
            s.shutdown()
            s.server_close()
