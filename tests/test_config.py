"""Configuration loading and validation.

A configuration mistake must fail at start-up with a message that says what to
fix -- never halfway through a demo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adoptforme_chatbot.config import ConfigError, load_config, load_server_configs

VALID = """
[[servers]]
name = "adoptforme"
transport = "stdio"
command = "uv"
args = ["run", "adoptforme-mcp"]

[[servers]]
name = "remote"
transport = "streamable-http"
url = "http://127.0.0.1:8080/mcp"
enabled = false
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "servers.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------- servers.toml


def test_valid_file_loads_both_transports(tmp_path: Path) -> None:
    configs = load_server_configs(write(tmp_path, VALID))
    assert [c.name for c in configs] == ["adoptforme", "remote"]
    assert configs[0].transport == "stdio" and configs[0].enabled is True
    assert configs[1].transport == "streamable-http" and configs[1].enabled is False
    assert configs[0].timeout_seconds == 60.0  # documented default


def test_missing_file_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="servers.example.toml"):
        load_server_configs(tmp_path / "nope.toml")


def test_invalid_toml_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_server_configs(write(tmp_path, "[[servers]\nname = broken"))


def test_empty_inventory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least one"):
        load_server_configs(write(tmp_path, "title = 'no servers here'"))


def test_unknown_key_lists_the_valid_ones(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\ncolour = "blue"\n'
    with pytest.raises(ConfigError, match="unknown key"):
        load_server_configs(write(tmp_path, body))


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    body = (
        '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\n\n'
        '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\n'
    )
    with pytest.raises(ConfigError, match="Duplicate server name"):
        load_server_configs(write(tmp_path, body))


def test_stdio_without_command_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires a 'command'"):
        load_server_configs(write(tmp_path, '[[servers]]\nname = "x"\ntransport = "stdio"\n'))


def test_http_without_url_is_rejected(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "streamable-http"\n'
    with pytest.raises(ConfigError, match="requires a 'url'"):
        load_server_configs(write(tmp_path, body))


def test_http_with_a_non_http_url_is_rejected(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "streamable-http"\nurl = "ftp://host/mcp"\n'
    with pytest.raises(ConfigError, match="http://"):
        load_server_configs(write(tmp_path, body))


def test_mixing_transports_is_rejected(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\nurl = "http://127.0.0.1/mcp"\n'
    with pytest.raises(ConfigError, match="meaningless for transport 'stdio'"):
        load_server_configs(write(tmp_path, body))


def test_missing_cwd_is_caught_before_launch(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\ncwd = "no/such/dir"\n'
    with pytest.raises(ConfigError, match="does not exist"):
        load_server_configs(write(tmp_path, body))


def test_non_positive_timeout_is_rejected(tmp_path: Path) -> None:
    body = '[[servers]]\nname = "x"\ntransport = "stdio"\ncommand = "uv"\ntimeout_seconds = 0\n'
    with pytest.raises(ConfigError, match="timeout_seconds must be positive"):
        load_server_configs(write(tmp_path, body))


def test_shipped_example_file_is_valid() -> None:
    """The file a new user copies must itself parse and validate."""
    example = Path(__file__).resolve().parents[1] / "config" / "servers.example.toml"
    configs = load_server_configs(example)
    names = {config.name for config in configs}
    assert {"adoptforme", "filesystem", "git", "pet_care_remote"} <= names
    assert {"classmate_server_1", "classmate_server_2"} <= names
    for config in configs:
        if config.name.startswith("classmate"):
            assert config.enabled is False, "classmate placeholders must ship disabled"


# ------------------------------------------------------------------ .env side


def test_missing_api_key_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="--offline"):
        load_config(env_file=tmp_path / "absent.env", servers_config=write(tmp_path, VALID))


def test_offline_mode_does_not_require_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    config = load_config(
        env_file=tmp_path / "absent.env",
        servers_config=write(tmp_path, VALID),
        require_api_key=False,
    )
    assert config.model == "claude-opus-5"  # documented default
    assert len(config.servers) == 2


def test_env_file_is_read_but_never_overrides_the_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=from-file\nANTHROPIC_MODEL=from-file-model\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "from-shell-model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = load_config(env_file=tmp_path / ".env", servers_config=write(tmp_path, VALID))
    assert config.anthropic_api_key == "from-file"
    assert config.model == "from-shell-model"


def test_non_integer_iteration_cap_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "many")
    with pytest.raises(ConfigError, match="MAX_TOOL_ITERATIONS"):
        load_config(env_file=tmp_path / "absent.env", servers_config=write(tmp_path, VALID))
