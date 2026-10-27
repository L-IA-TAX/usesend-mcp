"""Static token list: loading (MCP_TOKENS / MCP_TOKENS_FILE) and constant-time lookup.

The list is `name:token[,name:token…]`, read once at startup. Any problem with it
refuses startup with one log line naming the problem. No token value is ever
included in an error message.
"""

from __future__ import annotations

import hmac
import os
import re
import sys
from collections.abc import Mapping

NAME_RE = re.compile(r"[a-z0-9._-]+")
MIN_TOKEN_LEN = 32
PLACEHOLDER = "CHANGE-ME"


class TokenConfigError(Exception):
    """The token list can't be used. The message never contains a token value."""


class TokenTable:
    def __init__(self, entries: list[tuple[str, str]]):
        self._entries = [(name, token.encode()) for name, token in entries]

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._entries]

    def secret_values(self) -> list[str]:
        """The token values, for scrubbing them out of error reports."""
        return [token.decode() for _, token in self._entries]

    def lookup(self, token: str | None) -> str | None:
        """The actor name for `token`, or None.

        Compares against every entry with hmac.compare_digest and never stops early,
        so timing doesn't reveal which entry (if any) matched.
        """
        if not token:
            return None
        candidate = token.encode()
        actor = None
        for name, expected in self._entries:
            if hmac.compare_digest(candidate, expected) and actor is None:
                actor = name
        return actor


def parse_tokens(raw: str) -> TokenTable:
    raw = raw.strip()
    if not raw:
        raise TokenConfigError("token list is empty")
    if PLACEHOLDER in raw:
        raise TokenConfigError(f"token list still contains the placeholder {PLACEHOLDER}")
    entries: list[tuple[str, str]] = []
    names: set[str] = set()
    owner_of: dict[str, str] = {}
    for i, part in enumerate(raw.split(","), start=1):
        name, sep, token = part.strip().partition(":")
        if not sep or not NAME_RE.fullmatch(name) or not token or any(ch.isspace() for ch in token):
            raise TokenConfigError(f"token list entry #{i} is malformed (expected name:token, name in [a-z0-9._-])")
        if len(token) < MIN_TOKEN_LEN:
            raise TokenConfigError(f"token for {name!r} is shorter than {MIN_TOKEN_LEN} characters")
        if name in names:
            raise TokenConfigError(f"duplicate name {name!r} in token list")
        if token in owner_of:
            raise TokenConfigError(f"duplicate token in token list: {owner_of[token]!r} and {name!r} share one")
        names.add(name)
        owner_of[token] = name
        entries.append((name, token))
    return TokenTable(entries)


def load_tokens(environ: Mapping[str, str] = os.environ) -> TokenTable:
    path = environ.get("MCP_TOKENS_FILE")
    inline = environ.get("MCP_TOKENS")
    if path is not None and inline is not None:
        raise TokenConfigError("both MCP_TOKENS and MCP_TOKENS_FILE are set; set exactly one")
    if path is None and inline is None:
        raise TokenConfigError("no token list: set MCP_TOKENS_FILE (or MCP_TOKENS)")
    if path is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                inline = fh.read()
        except OSError as e:
            raise TokenConfigError(f"can't read MCP_TOKENS_FILE {path}: {e.strerror}") from None
    return parse_tokens(inline)


def load_tokens_or_exit(environ: Mapping[str, str] = os.environ) -> TokenTable:
    try:
        return load_tokens(environ)
    except TokenConfigError as e:
        print(f"usesend-mcp: refusing to start: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
