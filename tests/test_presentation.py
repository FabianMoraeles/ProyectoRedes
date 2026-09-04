"""The terminal interface: the rules from docs/ui-design.md, asserted.

An interface is a deliverable like any other, so the properties it promises are
tested rather than assumed -- in particular the accessibility rule that colour is
never the only carrier of meaning, and the encoding fallbacks that keep the
program running on a Windows console in a legacy code page.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from adoptamatch_chatbot.mcp_host.models import ServerConfig, ServerStatus, ToolCallOutcome, ToolRef
from adoptamatch_chatbot.presentation import GLYPHS_ASCII, GLYPHS_UNICODE, Presenter


def build(**kwargs) -> tuple[Presenter, io.StringIO]:
    """A presenter writing into a buffer, so output can be asserted on."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=False, no_color=kwargs.pop("no_color", False))
    return Presenter(console, **kwargs), buffer


def status(name: str = "demo", **kwargs) -> ServerStatus:
    config = ServerConfig(name=name, transport="stdio", command="uv", **kwargs)
    return ServerStatus(config=config)


# ------------------------------------------------------------------- encoding


class TestEncodingFallbacks:
    def test_a_cp1252_stream_falls_back_to_ascii_glyphs(self) -> None:
        """A Windows console in a legacy code page must not get Unicode glyphs."""
        buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        presenter = Presenter(Console(file=buffer, width=100))
        assert presenter.unicode is False
        assert presenter.glyphs == GLYPHS_ASCII

    def test_a_utf8_stream_gets_the_unicode_glyphs(self) -> None:
        buffer = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        presenter = Presenter(Console(file=buffer, width=100))
        assert presenter.unicode is True
        assert presenter.glyphs == GLYPHS_UNICODE

    def test_ascii_only_overrides_a_capable_stream(self) -> None:
        buffer = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        presenter = Presenter(Console(file=buffer, width=100), ascii_only=True)
        assert presenter.unicode is False
        assert presenter.glyphs == GLYPHS_ASCII

    def test_the_spinner_never_crashes_on_a_cp1252_stream(self) -> None:
        """Regression: rich's default spinner is braille and raises on cp1252.

        It raised instead of degrading, which aborted any redirected run on a
        Windows console -- including the scripted Wireshark scenario.
        """
        buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        presenter = Presenter(Console(file=buffer, width=100))
        with presenter.thinking("connecting"):
            pass  # entering and leaving the status is what used to raise

    def test_every_status_line_survives_a_cp1252_stream(self) -> None:
        buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        presenter = Presenter(Console(file=buffer, width=100))
        presenter.info("information")
        presenter.warn("careful")
        presenter.error("broken")
        presenter.success("done")
        presenter.rule("a section")
        presenter.banner("model", "provider", "session", "logs/x.jsonl")
        presenter.servers([status()])
        buffer.flush()


# ---------------------------------------------------------------- accessibility


class TestColourIsNeverTheOnlySignal:
    def test_success_and_failure_are_distinguishable_without_colour(self) -> None:
        presenter, buffer = build(no_color=True)
        ok = ToolCallOutcome(tool="t", server="s", ok=True, text="fine", elapsed_ms=3, request_id="abc")
        bad = ToolCallOutcome(tool="t", server="s", ok=False, text="nope", elapsed_ms=4, request_id="def")
        presenter.tool_result(ok)
        presenter.tool_result(bad)
        output = buffer.getvalue()
        assert "ok" in output and "failed" in output
        assert presenter.glyphs["ok"] in output
        assert presenter.glyphs["error"] in output

    def test_warnings_and_errors_are_labelled_in_words(self) -> None:
        presenter, buffer = build(no_color=True)
        presenter.warn("disk almost full")
        presenter.error("could not connect")
        output = buffer.getvalue()
        assert "warning" in output and "disk almost full" in output
        assert "error" in output and "could not connect" in output

    def test_server_state_is_a_word_not_only_a_colour(self) -> None:
        presenter, buffer = build(no_color=True)
        connected = status("alpha")
        connected.connected = True
        connected.tool_count = 3
        connected.protocol_version = "2025-06-18"
        failed = status("beta")
        failed.error = "timed out during the handshake"
        presenter.servers([connected, failed])
        output = buffer.getvalue()
        assert "connected" in output
        assert "failed" in output
        assert "timed out" in output

    def test_no_color_env_var_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = io.StringIO()
        presenter = Presenter(Console(file=buffer, width=100, force_terminal=True))
        presenter.success("done")
        assert "\x1b[" not in buffer.getvalue()


# ------------------------------------------------------------ progressive disclosure


class TestProgressiveDisclosure:
    def test_compact_mode_abbreviates_a_long_result(self) -> None:
        presenter, buffer = build()
        outcome = ToolCallOutcome(
            tool="t", server="s", ok=True, text="x" * 500, elapsed_ms=1, request_id="abc"
        )
        presenter.tool_result(outcome)
        assert "..." in buffer.getvalue()
        assert "x" * 500 not in buffer.getvalue()

    def test_verbose_mode_shows_the_whole_result(self) -> None:
        presenter, buffer = build(verbose=True)
        outcome = ToolCallOutcome(
            tool="t", server="s", ok=True, text="y" * 300, elapsed_ms=1, request_id="abc"
        )
        presenter.tool_result(outcome)
        # The payload wraps across several lines, so count the characters rather
        # than looking for one contiguous run.
        assert buffer.getvalue().count("y") == 300
        assert "..." not in buffer.getvalue()

    def test_verbose_can_be_toggled_at_runtime(self) -> None:
        presenter, _ = build()
        assert presenter.verbose is False
        presenter.set_verbose(True)
        assert presenter.verbose is True


# -------------------------------------------------------------------- feedback


class TestFeedback:
    def test_a_tool_call_is_announced_with_its_server(self) -> None:
        presenter, buffer = build(no_color=True)
        presenter.tool_call("recommend_animals", "adoptamatch", {"limit": 3})
        output = buffer.getvalue()
        assert "adoptamatch" in output and "recommend_animals" in output
        assert '"limit": 3' in output

    def test_a_result_reports_duration_and_correlation_id(self) -> None:
        presenter, buffer = build(no_color=True)
        outcome = ToolCallOutcome(
            tool="t", server="s", ok=True, text="fine", elapsed_ms=82, request_id="c6783cd2b18a"
        )
        presenter.tool_result(outcome)
        output = buffer.getvalue()
        assert "82 ms" in output
        assert "c6783cd2b18a" in output

    def test_the_turn_footer_is_suppressed_for_a_trivial_turn(self) -> None:
        presenter, buffer = build()
        presenter.turn_summary(tool_calls=0, elapsed_ms=120)
        assert buffer.getvalue().strip() == ""

    def test_the_turn_footer_reports_tool_calls_when_there_were_any(self) -> None:
        presenter, buffer = build(no_color=True)
        presenter.turn_summary(tool_calls=2, elapsed_ms=3400)
        output = buffer.getvalue()
        assert "3.4s" in output and "2 tool calls" in output


# ------------------------------------------------------- recognition over recall


class TestRecognitionOverRecall:
    def test_a_qualified_tool_shows_its_original_name(self) -> None:
        presenter, buffer = build(no_color=True)
        tools = [
            ToolRef(
                server="alpha",
                original_name="ping",
                exposed_name="alpha__ping",
                description="Greet from alpha.",
                input_schema={"type": "object"},
            )
        ]
        presenter.tools(tools)
        output = buffer.getvalue()
        assert "alpha__ping" in output
        assert "ping" in output
        assert "collided" in output

    def test_an_empty_catalogue_points_at_the_next_step(self) -> None:
        presenter, buffer = build(no_color=True)
        presenter.tools([])
        assert "/servers" in buffer.getvalue()

    def test_the_log_panel_names_both_log_files(self) -> None:
        presenter, buffer = build(no_color=True)
        presenter.logs(
            {
                "session_id": "s1",
                "path": "logs/s1.jsonl",
                "protocol_path": "logs/s1.wire.jsonl",
                "events": 4,
                "tool_calls": 2,
                "errors": 0,
                "frames": {"request": 3, "response": 3},
            },
            [],
        )
        output = buffer.getvalue()
        assert "s1.jsonl" in output
        assert "s1.wire.jsonl" in output
        assert "request" in output
