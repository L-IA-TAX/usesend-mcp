"""Audit module: schema validation, DDL shape and the JSON log line."""

from __future__ import annotations

import json

import pytest

from usesend_mcp.audit import DDL, log_json, validate_schema


@pytest.mark.parametrize("schema", ["mcp", "audit_2", "_x"])
def test_schema_names_accepted(schema):
    assert validate_schema(schema) == schema


@pytest.mark.parametrize("schema", ["", "MCP", "1abc", "mcp;drop", "a-b", 'mcp"'])
def test_schema_names_rejected(schema):
    with pytest.raises(ValueError):
        validate_schema(schema)


def test_ddl_is_idempotent_and_has_outcome_check():
    ddl = DDL.format(schema="mcp")
    assert ddl.count("IF NOT EXISTS") == 4
    assert "CHECK (outcome IN ('pending','ok','error','refused','preview'))" in ddl


def test_log_json_writes_one_line(capsys):
    log_json(event="tool_call", actor="alice", tool="list_domains", outcome="ok")
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    line = json.loads(out)
    assert line["actor"] == "alice"
    assert line["outcome"] == "ok"
    assert "ts" in line
