"""Fake useSend for local development: canned answers to the /api/v1 routes the tools use.

Standard library only. Every request is appended to a log file as one JSON line,
so you can check that a preview really called nothing.

    python dev/fake_usesend.py [--port 3999] [--log .dev/usesend.log]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOMAIN = {"id": 1, "name": "example.com", "status": "SUCCESS"}
BOOK = {"id": "book_1", "name": "Customers", "emoji": "📙", "properties": {}, "_count": {"contacts": 3}}
CONTACTS = [
    {"id": f"contact_{i}", "email": f"{name}@example.com", "firstName": name.title(), "subscribed": True}
    for i, name in enumerate(("alice", "bob", "carol"), start=1)
]
CAMPAIGN = {
    "id": "campaign_1",
    "name": "October news",
    "from": "News <news@example.com>",
    "subject": "October news",
    "contactBookId": "book_1",
    "status": "DRAFT",
    "total": 0,
    "sent": 0,
    "delivered": 0,
    "opened": 0,
    "clicked": 0,
    "bounced": 0,
}

ROUTES: list[tuple[str, str, object]] = [
    ("GET", r"/api/v1/domains", [DOMAIN]),
    ("GET", r"/api/v1/contactBooks", [BOOK]),
    ("POST", r"/api/v1/contactBooks", {**BOOK, "id": "book_new", "_count": {"contacts": 0}}),
    ("GET", r"/api/v1/contactBooks/[^/]+", BOOK),
    ("GET", r"/api/v1/contactBooks/[^/]+/contacts", CONTACTS),
    ("POST", r"/api/v1/contactBooks/[^/]+/contacts", {"contactId": "contact_new"}),
    ("POST", r"/api/v1/contactBooks/[^/]+/contacts/bulk", {"message": "ok", "count": None}),
    ("PATCH", r"/api/v1/contactBooks/[^/]+/contacts/[^/]+", {"contactId": "contact_1"}),
    ("GET", r"/api/v1/campaigns", {"campaigns": [CAMPAIGN], "totalPage": 1}),
    ("POST", r"/api/v1/campaigns", {**CAMPAIGN, "id": "campaign_new"}),
    ("GET", r"/api/v1/campaigns/[^/]+", CAMPAIGN),
    ("POST", r"/api/v1/campaigns/[^/]+/(schedule|pause|resume)", {"success": True}),
    ("GET", r"/api/v1/emails", {"data": [], "count": 0}),
    ("POST", r"/api/v1/emails", {"emailId": "email_new"}),
    ("GET", r"/api/v1/emails/[^/]+", {"id": "email_1", "latestStatus": "DELIVERED"}),
    ("POST", r"/api/v1/emails/[^/]+/cancel", {"emailId": "email_1"}),
    ("GET", r"/api/v1/analytics/email-time-series", {"result": [], "totalCounts": {"sent": 0}}),
    ("GET", r"/api/v1/analytics/reputation-metrics", {"bounceRate": 0, "complaintRate": 0}),
]


def make_handler(log_path: str):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            path = self.path.split("?", 1)[0]
            body = None
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = raw.decode(errors="replace")
            entry = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "method": self.command,
                "path": self.path,
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "body": body,
            }
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._reply(401, {"error": {"code": "UNAUTHORIZED", "message": "No API key"}})
            for method, pattern, reply in ROUTES:
                if method == self.command and re.fullmatch(pattern, path):
                    return self._reply(200, reply)
            return self._reply(404, {"error": {"code": "NOT_FOUND", "message": f"{self.command} {path}"}})

        def _reply(self, status: int, payload: object):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

        def log_message(self, *args):
            pass

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=3999)
    p.add_argument("--log", default=".dev/usesend.log")
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.log))
    print(f"fake useSend on http://127.0.0.1:{args.port} (log: {args.log})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
