"""Shared fixtures: a local useSend mock, a fake audit sink and an MCP test client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

ALICE_TOKEN = "a" * 64
BOB_TOKEN = "b" * 64
TOKENS = f"alice:{ALICE_TOKEN},bob:{BOB_TOKEN}"


class MockUseSend:
    """Records requests; replies from a queue of (status, body), else from `routes`, else 200 {}."""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[tuple[int, bytes]] = []
        self.routes: dict[tuple[str, str], Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(raw) if raw else None,
                    }
                )
                if outer.replies:
                    status, body = outer.replies.pop(0)
                elif (self.command, self.path) in outer.routes:
                    status, body = 200, json.dumps(outer.routes[(self.command, self.path)]).encode()
                else:
                    status, body = 200, b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def writes(self) -> list[dict]:
        return [r for r in self.requests if r["method"] != "GET"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def mock():
    m = MockUseSend()
    yield m
    m.close()


class FakeAudit:
    """In-memory audit sink. `fail_record` / `fail_ping` simulate a database outage."""

    def __init__(self):
        self.rows: list[dict] = []
        self.fail_record = False
        self.fail_ping = False

    def record(self, actor, tool, mutating, args):
        if self.fail_record:
            raise ConnectionError("audit database down")
        self.rows.append({"actor": actor, "tool": tool, "mutating": mutating, "args": args, "outcome": "pending"})
        return len(self.rows)

    def finish(self, audit_id, outcome, **fields):
        self.rows[audit_id - 1].update(outcome=outcome, **fields)

    def ping(self):
        if self.fail_ping:
            raise ConnectionError("audit database down")


@pytest.fixture
def audit():
    return FakeAudit()


ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class McpClient:
    """Minimal JSON-RPC over the stateless streamable-HTTP endpoint."""

    def __init__(self, http, token: str | None = ALICE_TOKEN):
        self.http = http
        self.token = token
        self._id = 0

    def headers(self) -> dict[str, str]:
        h = dict(ACCEPT)
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def rpc(self, method: str, params: dict | None = None, path: str = "/"):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        return self.http.post(path, json=body, headers=self.headers())

    def initialize(self, path: str = "/"):
        return self.rpc(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            path=path,
        )

    def tools(self) -> dict[str, dict]:
        r = self.rpc("tools/list")
        assert r.status_code == 200, r.text
        return {t["name"]: t for t in r.json()["result"]["tools"]}

    def call(self, tool: str, /, **arguments) -> dict:
        """Returns the tools/call result: {"content": [...], "isError": bool}."""
        r = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert r.status_code == 200, r.text
        return r.json()["result"]

    @staticmethod
    def text(result: dict) -> str:
        return "".join(c.get("text", "") for c in result["content"])

    @classmethod
    def data(cls, result: dict) -> Any:
        return json.loads(cls.text(result))
