"""Local dev harness: Postgres (pgserver) + a fake useSend + a dev token + the MCP server.

No Docker and no real useSend needed.

    .venv/bin/python dev/run.py

Then:
    curl -sf http://127.0.0.1:8080/healthz
    curl -s -X POST http://127.0.0.1:8080/ -H "Authorization: Bearer $(cut -d: -f2 .dev/tokens)" …

Ctrl-C / SIGTERM stops the server, the fake and Postgres.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / ".dev"
FAKE_PORT = 3999
PORT = 8080


def ensure_token_file() -> Path:
    path = DEV / "tokens"
    if not path.exists():
        path.write_text(f"dev:{secrets.token_hex(32)}\n")
    path.chmod(0o600)
    return path


def on_sigterm(*_) -> None:
    raise KeyboardInterrupt


def main() -> int:
    DEV.mkdir(exist_ok=True)
    tokens = ensure_token_file()

    pg = pgserver.get_server(str(DEV / "pg"), cleanup_mode="stop")
    db_url = pg.get_uri()
    procs: list[subprocess.Popen] = []

    def stop(*_):
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        pg.cleanup()

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, str(ROOT / "dev" / "fake_usesend.py"), "--port", str(FAKE_PORT)]
                + ["--log", str(DEV / "usesend.log")],
                cwd=ROOT,
            )
        )
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("MCP_") and k != "USESEND_BASE_URL"
        } | {
            "MCP_TOKENS_FILE": str(tokens),
            "USESEND_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
            "MCP_USESEND_API_KEY": "us_dev_fake_key",
            "MCP_DATABASE_URL": db_url,
            "MCP_ALLOWED_FROM_DOMAINS": "example.com",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(PORT),
        }
        print(f"audit DB:  {db_url}", flush=True)
        print(f"  psql:    psql '{db_url}' -c 'SELECT * FROM mcp.audit_log ORDER BY id DESC LIMIT 5'", flush=True)
        print(f"token:     {tokens} (Authorization: Bearer $(cut -d: -f2 {tokens.relative_to(ROOT)}))", flush=True)
        print(f"useSend:   http://127.0.0.1:{FAKE_PORT} (log: {DEV / 'usesend.log'})", flush=True)
        print(f"MCP:       http://127.0.0.1:{PORT}/  health: http://127.0.0.1:{PORT}/healthz", flush=True)
        server = subprocess.Popen([sys.executable, "-m", "usesend_mcp.server"], cwd=ROOT, env=env)
        procs.append(server)
        return server.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        stop()


if __name__ == "__main__":
    sys.exit(main())
