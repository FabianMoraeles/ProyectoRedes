"""The two MCP transports, implemented by hand.

A transport's whole job is to carry one JSON-RPC frame to the peer and bring the
matching reply back. Everything above it -- the handshake, ``tools/list``,
``tools/call`` -- is transport-agnostic and lives in
:mod:`adoptamatch_chatbot.mcp_wire.session`.

* :class:`StdioTransport` spawns the server as a subprocess and writes
  newline-delimited JSON to its stdin, reading replies from its stdout. **No
  socket is involved**, which is precisely why three of this project's servers
  are invisible to a packet capture.
* :class:`StreamableHttpTransport` posts each frame to an HTTP endpoint. This one
  runs over TCP and is the transport the network analysis observes.

Every frame that crosses a transport, in either direction, is handed to the
optional ``on_frame`` callback before anything else happens. That is what makes a
complete, plaintext wire log possible: the host is the one writing the bytes, so
it can record them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from adoptamatch_chatbot.mcp_wire.messages import ProtocolError

#: Called as ``on_frame(direction, message)`` with ``direction`` in
#: ``{"out", "in"}``. Never allowed to raise into the transport.
FrameHook = Callable[[str, dict[str, Any]], None]

#: A single JSON-RPC frame can be large (a full tool result), so the line reader
#: needs a generous ceiling. 16 MiB is far above anything these servers produce
#: and still bounded.
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: Header names defined by the Streamable HTTP transport.
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"


class TransportError(RuntimeError):
    """The transport could not deliver or receive a frame."""


def resolve_executable(command: str) -> str:
    """Find the real executable for ``command``.

    On Windows, ``npx`` and ``uvx`` are ``.cmd`` / ``.exe`` shims that
    ``CreateProcess`` will not find by bare name, so the extensions are
    probed explicitly. On POSIX the command is returned unchanged when it is
    already a path, and resolved through ``PATH`` otherwise.
    """
    found = shutil.which(command)
    if found:
        return found
    if sys.platform == "win32":
        for extension in (".cmd", ".bat", ".exe", ".ps1"):
            candidate = shutil.which(command + extension)
            if candidate:
                return candidate
    return command


class Transport:
    """Interface every transport implements."""

    #: Filled in by the transport when the peer reports one.
    session_id: str | None = None

    async def start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def send_request(
        self, message: dict[str, Any], timeout: float
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def send_notification(self, message: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class StdioTransport(Transport):
    """Talk to a server subprocess over its standard input and output."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        errlog: TextIO | None = None,
        on_frame: FrameHook | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = str(cwd) if cwd else None
        self.errlog = errlog
        self.on_frame = on_frame
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ start

    async def start(self) -> None:
        environment = {**os.environ, **self.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                resolve_executable(self.command),
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self.errlog or asyncio.subprocess.DEVNULL,
                cwd=self.cwd,
                env=environment,
                limit=MAX_FRAME_BYTES,
            )
        except (OSError, ValueError) as exc:
            raise TransportError(f"Could not start {self.command!r}: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"stdio-reader:{self.command}")

    async def _read_loop(self) -> None:
        """One reader task feeds every waiting request its reply, by id."""
        assert self._process and self._process.stdout
        stream = self._process.stdout
        try:
            while True:
                try:
                    line = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:  # pragma: no cover
                    self._fail_all(TransportError(f"Frame exceeded {MAX_FRAME_BYTES} bytes: {exc}"))
                    return
                if not line:
                    self._fail_all(TransportError("The server closed its stdout."))
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # A server that prints to stdout breaks the framing. Skip the
                    # line rather than killing the session, and leave a trace.
                    if self.errlog is not None:
                        self.errlog.write(f"[host] discarded non-JSON stdout line: {text[:200]}\n")
                        self.errlog.flush()
                    continue
                self._deliver(message)
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise

    def _deliver(self, message: dict[str, Any]) -> None:
        if self.on_frame is not None:
            self.on_frame("in", message)
        identifier = message.get("id")
        future = self._pending.pop(identifier, None)
        if future is not None and not future.done():
            future.set_result(message)

    def _fail_all(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # ------------------------------------------------------------------- send

    async def _write(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise TransportError("The transport is not started.")
        if self.on_frame is not None:
            self.on_frame("out", message)
        payload = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self._process.stdin.write(payload)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                raise TransportError(f"The server stopped reading its stdin: {exc}") from exc

    async def send_request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message["id"]] = future
        await self._write(message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message["id"], None)
            raise TransportError(f"No reply to {message.get('method')!r} within {timeout:g}s.") from exc

    async def send_notification(self, message: dict[str, Any]) -> None:
        await self._write(message)

    # --------------------------------------------------------------- shutdown

    async def aclose(self) -> None:
        """Close stdin, give the process a moment, then insist."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None

        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            # A server that has already exited leaves a broken pipe; that is the
            # outcome we wanted anyway.
            with contextlib.suppress(BrokenPipeError, RuntimeError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):  # pragma: no cover
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()


class StreamableHttpTransport(Transport):
    """Talk to a server over HTTP: one JSON-RPC frame per ``POST``.

    ``Accept`` advertises both ``application/json`` and ``text/event-stream``
    because the transport allows either; a server that answers with SSE is parsed
    too. The session id the server issues at ``initialize`` is echoed on every
    later request, as the transport specification requires.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        on_frame: FrameHook | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.extra_headers = headers or {}
        self.on_frame = on_frame
        self.connect_timeout = connect_timeout
        self.protocol_version: str | None = None
        self._client: Any = None

    async def start(self) -> None:
        import httpx2

        self._client = httpx2.AsyncClient(timeout=self.connect_timeout)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.extra_headers,
        }
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if self.protocol_version:
            headers[PROTOCOL_HEADER] = self.protocol_version
        return headers

    @staticmethod
    def _parse_body(content_type: str, text: str) -> dict[str, Any] | None:
        """Accept a plain JSON body or a Server-Sent Events stream."""
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        return json.loads(payload)
            return None
        text = text.strip()
        return json.loads(text) if text else None

    async def _post(self, message: dict[str, Any], timeout: float) -> tuple[Any, dict[str, Any] | None]:
        import httpx2

        if self._client is None:
            raise TransportError("The transport is not started.")
        if self.on_frame is not None:
            self.on_frame("out", message)
        try:
            response = await self._client.post(
                self.url,
                content=json.dumps(message, ensure_ascii=False, default=str).encode("utf-8"),
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx2.TimeoutException as exc:
            raise TransportError(f"No HTTP reply within {timeout:g}s: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise TransportError(f"HTTP transport failure: {exc}") from exc

        issued = response.headers.get(SESSION_HEADER)
        if issued:
            self.session_id = issued
        negotiated = response.headers.get(PROTOCOL_HEADER)
        if negotiated:
            self.protocol_version = negotiated

        if response.status_code == 202 or not response.content:
            return response, None
        if response.status_code >= 400:
            raise TransportError(f"The server answered HTTP {response.status_code}: {response.text[:300]}")
        try:
            parsed = self._parse_body(response.headers.get("content-type", ""), response.text)
        except json.JSONDecodeError as exc:
            raise TransportError(f"The server sent a body that is not JSON-RPC: {exc}") from exc
        return response, parsed

    async def send_request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        _, parsed = await self._post(message, timeout)
        if parsed is None:
            raise ProtocolError(
                f"The server returned no body for request {message.get('method')!r}, "
                "which is only valid for a notification."
            )
        if self.on_frame is not None:
            self.on_frame("in", parsed)
        return parsed

    async def send_notification(self, message: dict[str, Any]) -> None:
        await self._post(message, self.connect_timeout)

    async def aclose(self) -> None:
        """Terminate the session with ``DELETE``, then close the connection pool."""
        client = self._client
        self._client = None
        if client is None:
            return
        if self.session_id:
            # The session may already be gone, or the server may not implement
            # DELETE at all; either way there is nothing left to clean up.
            with contextlib.suppress(Exception):
                await client.request("DELETE", self.url, headers=self._headers(), timeout=5)
        with contextlib.suppress(Exception):  # shutdown must not raise
            await client.aclose()
