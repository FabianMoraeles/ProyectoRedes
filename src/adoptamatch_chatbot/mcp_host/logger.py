"""Persistent JSON Lines log of every MCP interaction.

Two files are written per session, both under ``logs/``:

* ``session-<id>.jsonl`` -- the **host-level** log. One object per event, with a
  ``request_id`` that correlates a ``request`` with its ``response`` or ``error``.
  This is the log the assignment asks for and the one the Wireshark walkthrough
  correlates against.
* ``session-<id>.wire.jsonl`` -- the **wire log**: every JSON-RPC frame that
  crossed a transport, in *both* directions, with the kind the specification
  defines (``request`` / ``notification`` / ``response`` / ``error``) and whether
  it belongs to the lifecycle handshake. This is complete because the host
  implements the protocol itself: it is the code writing and reading the bytes,
  so nothing is hidden behind an SDK.
* ``session-<id>.<server>.stderr.log`` -- whatever each stdio subprocess wrote to
  its standard error. Written by
  :class:`~adoptamatch_chatbot.mcp_host.manager.MCPManager`, not by this class,
  but it belongs to the same session and is named to match.

Together these are the plaintext side of an exchange that Wireshark can only see
as ciphertext once the remote server is behind TLS.

Nothing is buffered. Each line is flushed immediately so a crash (or a Ctrl-C in
the middle of a demo) still leaves a complete, valid log behind.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from adoptamatch_chatbot.mcp_wire.messages import LIFECYCLE_METHODS, classify, is_lifecycle

Direction = Literal["request", "response", "error", "notification", "lifecycle"]

#: Keys whose values are replaced with ``"[REDACTED]"`` before anything is written.
#: Matching is case-insensitive and substring-based, so ``ANTHROPIC_API_KEY`` and
#: ``authorization`` are both caught.
SECRET_KEY_PATTERNS = (
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "private_key",
)

#: Values that look like a credential regardless of the key they sit under.
_SECRET_VALUE_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{16,})")

REDACTED = "[REDACTED]"

#: Long tool payloads are truncated in the log so a single search result cannot
#: produce a multi-megabyte line. The full result still reaches the model.
MAX_LOGGED_CHARS = 4000


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


def redact(value: Any) -> Any:
    """Recursively remove anything that looks like a credential.

    Applied to every payload before it is serialised. Redaction is deliberately
    aggressive: a false positive costs a little debuggability, a false negative
    puts a key in a file that ends up in a report.
    """
    if isinstance(value, dict):
        return {k: (REDACTED if _looks_secret(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(REDACTED, value)
    return value


def _truncate(payload: Any) -> Any:
    """Cap the serialised size of one payload, keeping the JSON valid."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers nearly everything
        return str(payload)[:MAX_LOGGED_CHARS]
    if len(encoded) <= MAX_LOGGED_CHARS:
        return payload
    return {
        "_truncated": True,
        "_original_chars": len(encoded),
        "preview": encoded[:MAX_LOGGED_CHARS],
    }


class InteractionLogger:
    """Append-only JSONL writer for one chatbot session."""

    def __init__(self, log_dir: Path, session_id: str | None = None, protocol_log: bool = True) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"session-{self.session_id}.jsonl"
        self.protocol_path = self.log_dir / f"session-{self.session_id}.wire.jsonl"
        self._protocol_enabled = protocol_log
        self.event_count = 0
        self.tool_call_count = 0
        self.error_count = 0
        #: JSON-RPC frames seen per kind, for the `/logs` summary and the report.
        self.frame_counts: dict[str, int] = {}
        #: (server, request id) -> method, so a reply can name the call it answers.
        self._open_requests: dict[tuple[str, Any], str] = {}

    # ------------------------------------------------------------------ writing

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def log(
        self,
        *,
        server: str,
        transport: str,
        direction: Direction,
        method: str,
        request_id: str,
        tool: str | None = None,
        params: Any = None,
        result: Any = None,
        error: str | None = None,
        elapsed_ms: int = 0,
        status: str = "ok",
    ) -> dict[str, Any]:
        """Append one host-level event and return the record that was written."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "server": server,
            "transport": transport,
            "direction": direction,
            "method": method,
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
            "status": status,
        }
        if tool is not None:
            record["tool"] = tool
        if params is not None:
            record["params"] = _truncate(redact(params))
        if result is not None:
            record["result"] = _truncate(redact(result))
        if error is not None:
            record["error"] = redact(error)

        self._write(self.path, record)
        self.event_count += 1
        if method == "tools/call" and direction == "request":
            self.tool_call_count += 1
        if direction == "error" or status == "error":
            self.error_count += 1
        return record

    def log_frame(self, server: str, transport: str, direction: str, message: dict[str, Any]) -> None:
        """Append one raw JSON-RPC frame, classified.

        Args:
            direction: ``out`` for host to server, ``in`` for server to host.
            message: the decoded frame, exactly as it went on the wire.

        The ``kind`` and ``lifecycle`` fields are what make the network report
        writable: filter on them to separate the synchronisation messages from the
        ordinary calls and their replies.
        """
        if not self._protocol_enabled:
            return
        kind = classify(message)
        self.frame_counts[kind] = self.frame_counts.get(kind, 0) + 1

        # A reply carries no `method`, so on its own it cannot be told apart from
        # any other reply. Remembering which method each outbound id asked for
        # lets every response and error name the call it answers -- which is what
        # makes the file readable next to a packet capture.
        identifier = message.get("id")
        method = message.get("method")
        key = (server, identifier)
        if kind == "request":
            self._open_requests[key] = method or ""
            lifecycle = is_lifecycle(message)
        elif kind in ("response", "error"):
            method = self._open_requests.pop(key, None)
            lifecycle = method in LIFECYCLE_METHODS if method else False
        else:
            lifecycle = is_lifecycle(message)

        self._write(
            self.protocol_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "session_id": self.session_id,
                "server": server,
                "transport": transport,
                "direction": direction,
                "kind": kind,
                "lifecycle": lifecycle,
                "method": method,
                "id": identifier,
                "message": _truncate(redact(message)),
            },
        )

    # ------------------------------------------------------------------ reading

    def summary(self) -> dict[str, Any]:
        """Counts for the ``/logs`` command."""
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "protocol_path": str(self.protocol_path) if self._protocol_enabled else None,
            "events": self.event_count,
            "tool_calls": self.tool_call_count,
            "errors": self.error_count,
            "frames": dict(self.frame_counts),
        }

    def tail(self, count: int = 10) -> list[dict[str, Any]]:
        """Return the last ``count`` host-level events, oldest first."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            lines = handle.readlines()[-count:]
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
        return records


def new_request_id() -> str:
    """Correlation id shared by a request and its response or error."""
    return uuid.uuid4().hex[:12]
