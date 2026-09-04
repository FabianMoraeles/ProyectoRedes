"""The JSONL interaction log: shape, correlation, redaction and truncation."""

from __future__ import annotations

import json
from pathlib import Path

from adoptamatch_chatbot.mcp_host.logger import (
    MAX_LOGGED_CHARS,
    REDACTED,
    InteractionLogger,
    new_request_id,
    redact,
)

REQUIRED_FIELDS = {
    "timestamp",
    "session_id",
    "server",
    "transport",
    "direction",
    "method",
    "request_id",
    "elapsed_ms",
    "status",
}


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_event_carries_the_required_fields(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    log.log(
        server="adoptamatch",
        transport="stdio",
        direction="request",
        method="tools/call",
        request_id=new_request_id(),
        tool="recommend_animals",
        params={"housing": "apartment"},
    )
    (record,) = read(log.path)
    assert set(record) >= REQUIRED_FIELDS
    assert record["tool"] == "recommend_animals"
    assert record["params"]["housing"] == "apartment"


def test_request_and_response_share_a_request_id(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    request_id = new_request_id()
    log.log(server="s", transport="stdio", direction="request", method="tools/call", request_id=request_id)
    log.log(
        server="s",
        transport="stdio",
        direction="response",
        method="tools/call",
        request_id=request_id,
        result={"ok": True},
        elapsed_ms=42,
    )
    request, response = read(log.path)
    assert request["request_id"] == response["request_id"]
    assert response["elapsed_ms"] == 42


def test_counters_track_calls_and_errors(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    log.log(server="s", transport="stdio", direction="request", method="tools/call", request_id="a")
    log.log(
        server="s",
        transport="stdio",
        direction="error",
        method="tools/call",
        request_id="a",
        error="nope",
        status="error",
    )
    summary = log.summary()
    assert summary["events"] == 2
    assert summary["tool_calls"] == 1
    assert summary["errors"] == 1
    assert summary["session_id"] in summary["path"]


def test_tail_returns_the_most_recent_events(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    for index in range(15):
        log.log(
            server="s",
            transport="stdio",
            direction="request",
            method="tools/call",
            request_id=str(index),
        )
    tail = log.tail(5)
    assert [record["request_id"] for record in tail] == ["10", "11", "12", "13", "14"]


# ------------------------------------------------------------------ redaction


def test_secret_keys_are_redacted_at_every_depth() -> None:
    payload = {
        "api_key": "sk-ant-should-not-appear",
        "nested": {"Authorization": "Bearer abc", "safe": "keep me"},
        "list": [{"password": "hunter2"}],
    }
    cleaned = redact(payload)
    assert cleaned["api_key"] == REDACTED
    assert cleaned["nested"]["Authorization"] == REDACTED
    assert cleaned["nested"]["safe"] == "keep me"
    assert cleaned["list"][0]["password"] == REDACTED


def test_credential_shaped_values_are_redacted_even_under_an_innocent_key() -> None:
    cleaned = redact({"note": "my key is sk-ant-api03-abcdefgh12345678 do not share"})
    assert "sk-ant-api03" not in cleaned["note"]
    assert REDACTED in cleaned["note"]


def test_secrets_never_reach_the_log_file(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    log.log(
        server="s",
        transport="stdio",
        direction="request",
        method="tools/call",
        request_id="x",
        params={"ANTHROPIC_API_KEY": "sk-ant-secret-value", "city": "Guatemala"},
    )
    raw = log.path.read_text(encoding="utf-8")
    assert "sk-ant-secret-value" not in raw
    assert "Guatemala" in raw


def test_long_payloads_are_truncated_but_stay_valid_json(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    log.log(
        server="s",
        transport="stdio",
        direction="response",
        method="tools/call",
        request_id="x",
        result={"blob": "x" * (MAX_LOGGED_CHARS * 2)},
    )
    (record,) = read(log.path)
    assert record["result"]["_truncated"] is True
    assert len(record["result"]["preview"]) <= MAX_LOGGED_CHARS


# --------------------------------------------------------------- protocol log


def test_protocol_log_can_be_disabled(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path, protocol_log=False)
    log.log_protocol_message("s", "stdio", {"jsonrpc": "2.0"})
    assert not log.protocol_path.exists()
    assert log.summary()["protocol_path"] is None


def test_protocol_log_records_raw_messages(tmp_path: Path) -> None:
    log = InteractionLogger(tmp_path)
    log.log_protocol_message("s", "streamable-http", {"jsonrpc": "2.0", "id": 1, "result": {}})
    (record,) = read(log.protocol_path)
    assert record["origin"] == "server"
    assert record["message"]["jsonrpc"] == "2.0"


def test_two_sessions_write_to_different_files(tmp_path: Path) -> None:
    first = InteractionLogger(tmp_path)
    second = InteractionLogger(tmp_path)
    assert first.path != second.path
    assert first.session_id != second.session_id
