"""Token list parsing, loading and lookup."""

from __future__ import annotations

import hmac

import pytest

from usesend_mcp import auth
from usesend_mcp.auth import TokenConfigError, load_tokens, load_tokens_or_exit, parse_tokens

ALICE = "a" * 64
BOB = "b1" * 32
GOOD = f"alice:{ALICE},bob:{BOB}"


def test_parse_good_list_with_whitespace():
    table = parse_tokens(f"  alice:{ALICE} , bob:{BOB}\n")
    assert table.names == ["alice", "bob"]
    assert table.lookup(ALICE) == "alice"
    assert table.lookup(BOB) == "bob"


@pytest.mark.parametrize(
    "raw, message",
    [
        ("", "empty"),
        ("   \n", "empty"),
        (f"alice{ALICE}", "entry #1 is malformed"),
        (f"alice:{ALICE},", "entry #2 is malformed"),
        (f"Alice:{ALICE}", "entry #1 is malformed"),
        (f"alice:{ALICE[:20]} {ALICE[20:]}", "entry #1 is malformed"),
        (f"alice:{ALICE},bob:", "entry #2 is malformed"),
        ("alice:short", "shorter than 32"),
        (f"alice:{ALICE},alice:{BOB}", "duplicate name 'alice'"),
        (f"alice:{ALICE},bob:{ALICE}", "duplicate token"),
        (f"alice:{ALICE},bob:CHANGE-ME-{BOB}", "placeholder CHANGE-ME"),
    ],
)
def test_parse_rejects(raw, message):
    with pytest.raises(TokenConfigError, match=message) as exc:
        parse_tokens(raw)
    assert ALICE not in str(exc.value)
    assert BOB not in str(exc.value)


def test_lookup_unknown_and_missing():
    table = parse_tokens(GOOD)
    assert table.lookup("x" * 64) is None
    assert table.lookup("") is None
    assert table.lookup(None) is None
    assert table.lookup(ALICE[:-1]) is None


def test_lookup_compares_every_entry_in_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    table = parse_tokens(GOOD)
    assert table.lookup(ALICE) == "alice"
    # A match on the first entry still compares against the rest.
    assert len(calls) == 2


def test_load_from_file(tmp_path):
    f = tmp_path / "tokens"
    f.write_text(GOOD + "\n")
    assert load_tokens({"MCP_TOKENS_FILE": str(f)}).names == ["alice", "bob"]


def test_load_from_env():
    assert load_tokens({"MCP_TOKENS": GOOD}).names == ["alice", "bob"]


def _write(tmp_path, content):
    f = tmp_path / "tokens"
    f.write_text(content)
    return str(f)


@pytest.mark.parametrize(
    "make_env, message",
    [
        (lambda p: {"MCP_TOKENS_FILE": str(p / "missing")}, "can't read MCP_TOKENS_FILE"),
        (lambda p: {"MCP_TOKENS_FILE": _write(p, "\n")}, "empty"),
        (lambda p: {"MCP_TOKENS_FILE": _write(p, f"alice:{ALICE},bob:CHANGE-ME")}, "CHANGE-ME"),
        (lambda p: {"MCP_TOKENS_FILE": _write(p, f"alice={ALICE};bob={BOB}")}, "malformed"),
        (lambda p: {"MCP_TOKENS_FILE": _write(p, GOOD), "MCP_TOKENS": GOOD}, "both MCP_TOKENS and MCP_TOKENS_FILE"),
        (lambda p: {}, "no token list"),
    ],
    ids=["missing-file", "empty-file", "change-me", "malformed", "both-set", "neither-set"],
)
def test_load_failures_exit_without_leaking(tmp_path, capsys, make_env, message):
    with pytest.raises(SystemExit) as exc:
        load_tokens_or_exit(make_env(tmp_path))
    assert exc.value.code != 0
    out = capsys.readouterr()
    logged = out.out + out.err
    assert message in logged
    assert logged.count("\n") == 1, "one log line"
    assert ALICE not in logged
    assert BOB not in logged
