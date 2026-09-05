# AdoptaMatch Chatbot — a console MCP host

A terminal chatbot that acts as an **MCP host**: it talks to one language model,
connects to several [Model Context Protocol](https://modelcontextprotocol.io/)
servers at once over two different transports, routes the model's tool calls to
the right server, and writes every request and response to a persistent audit log.

**The MCP protocol is implemented here, directly over JSON-RPC 2.0 — no MCP SDK.**
Framing, both transports and the session lifecycle live in
[`mcp_wire/`](src/adoptamatch_chatbot/mcp_wire); the official SDK is a *test-only*
dependency, used as a conformance oracle to prove wire compatibility in both
directions.

The business case is AdoptaMatch: an assistant for an animal shelter that searches
animals, explains how well each one fits a household, compares candidates and
registers adoptions. The shelter data lives in a separate public MCP server,
[`adoptamatch-mcp`](../adoptamatch-mcp); this repository is the host.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The protocol, by hand](#the-protocol-by-hand)
- [Supported MCP transports](#supported-mcp-transports)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment variables](#environment-variables)
- [Server configuration](#server-configuration)
- [Running the chatbot](#running-the-chatbot)
- [Console commands](#console-commands)
- [Adding a new MCP server](#adding-a-new-mcp-server)
- [Logs](#logs)
- [Demo scenarios](#demo-scenarios)
- [Tests](#tests)
- [The interface](#the-interface)
- [Remote server and deployment](#remote-server-and-deployment)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)

---

## What it does

One user turn, end to end:

1. read the line typed in the console;
2. append it to the session history;
3. send the system prompt, the **whole** history and every discovered tool schema
   to the LLM;
4. if the model asks for tools, resolve each name to the server that owns it;
5. execute them through the MCP client, with a per-server timeout;
6. log request, response or error, and duration, under one shared `request_id`;
7. append the results to the history and ask the model again;
8. repeat until the model stops calling tools (bounded by `MAX_TOOL_ITERATIONS`);
9. print the final answer, and loop until `/exit`.

Everything else in this repository exists to make those nine steps observable and
hard to break.

## Architecture

```text
                          ┌──────────────────────────┐
              you ───────▶│  cli.py  ·  app.py       │
                          │  conversation · presenter│
                          └───────┬──────────┬───────┘
                                  │          │
              tool schemas +      │          │  tools/call
              full history        │          │
                                  ▼          ▼
                      ┌────────────────┐  ┌────────────────────────────┐
                      │ llm/           │  │ mcp_host/manager.py        │
                      │  base.py       │  │  connect · discover ·      │
                      │  anthropic_... │  │  route · timeout · close   │
                      │  scripted.py   │  └───┬────────────────────────┘
                      └───────┬────────┘      │
                              │               ├── stdio ──▶ adoptamatch  (own, public repo)
                              │               ├── stdio ──▶ filesystem   (official reference)
                              │               ├── stdio ──▶ git          (official reference)
                              │               ├── stdio ──▶ classmate ×2 (placeholders)
                              │               └── HTTP ───▶ pet-care     (own, remote)
                              ▼                            │
                  Anthropic or Gemini API                  ▼
                     (LLM_PROVIDER picks one)   mcp_host/logger.py ──▶ logs/*.jsonl
```

Four boundaries, each of which can be tested on its own:

| Module | Responsibility |
| --- | --- |
| `config.py` | Load and **validate** `.env` and `servers.toml`; fail at start-up with an actionable message |
| `conversation.py` | The message history, trimmed turn-aware so a `tool_result` is never orphaned |
| `llm/base.py` | A provider-neutral interface: one `complete()` call, one `tool_result_message()` builder |
| `llm/anthropic_provider.py` | The Anthropic implementation (single turn; the host owns the loop) |
| `llm/gemini_provider.py` | The Gemini implementation — a free alternative selected with `LLM_PROVIDER=gemini` |
| `llm/scripted.py` | A deterministic stand-in used by every test and by `--offline` |
| `mcp_wire/messages.py` | JSON-RPC framing, request ids, the four message kinds |
| `mcp_wire/transports.py` | `StdioTransport` (pipes) and `StreamableHttpTransport` (TCP) |
| `mcp_wire/session.py` | The MCP lifecycle: handshake, `tools/list`, `tools/call` |
| `mcp_host/models.py` | `ServerConfig`, `ToolRef`, `ServerStatus`, `ToolCallOutcome` |
| `mcp_host/manager.py` | Connections, discovery, the tool→server map, calls, timeouts, shutdown |
| `mcp_host/logger.py` | JSONL logging with redaction, truncation and request/response correlation |
| `app.py` | The conversation loop and the console commands |
| `presentation.py` | All terminal rendering (`rich`), so the loop stays free of formatting |

> The MCP package is called `mcp_host`, not `mcp`, so that `from mcp.client import Client`
> inside this project unambiguously means the official SDK.

## The protocol, by hand

MCP is JSON-RPC 2.0 with a defined set of methods. This host implements it
directly rather than calling an SDK, which is the assignment's first optional
extra — and, as it turned out, the simpler design.

### The lifecycle, in the order it appears on the wire

| # | Message | Kind | Purpose |
| --- | --- | --- | --- |
| 1 | `initialize` | request | Propose a protocol revision, declare capabilities, identify the client |
| 2 | *(reply)* | response | The revision the server will use, its capabilities, its identity, optional `instructions` |
| 3 | `notifications/initialized` | notification | The client confirms. **No reply** — a notification has no `id` |
| 4 | `tools/list` | request | Discover the catalogue and its JSON Schemas |
| 5 | `tools/call` | request | Invoke one tool |

Messages 1–3 are *synchronisation*; 4 and 5 are ordinary calls. The wire log tags
every frame with exactly that distinction, which is what the network analysis
needs.

### What is where

| Module | Responsibility |
| --- | --- |
| `mcp_wire/messages.py` | Build and classify frames; allocate monotonic request ids; turn a JSON-RPC `error` into a Python exception |
| `mcp_wire/transports.py` | `StdioTransport`: spawn a subprocess, newline-delimited JSON, one reader task dispatching replies by id. `StreamableHttpTransport`: one `POST` per frame, `Mcp-Session-Id` handling, JSON *or* SSE reply bodies, `DELETE` on close |
| `mcp_wire/session.py` | The lifecycle above, plus `tools/list` pagination and result normalisation |

The servers use the same approach: `minimcp.py`, one self-contained module
vendored identically into `adoptamatch-mcp` and `pet_care_mcp`.

### How the claim is verified

Three tests enforce "no MCP SDK at runtime", and they are the first thing to run
if you doubt it:

```bash
uv run pytest -q tests/test_mcp_wire.py -k NoSdk
```

- no module under `src/` imports `mcp`;
- importing the package in a fresh interpreter never loads an `mcp` module;
- the declared runtime dependencies contain no MCP SDK.

Interoperability is then proved in **both directions**, because a hand-written
implementation that only talks to itself proves nothing:

| Direction | What it shows |
| --- | --- |
| Official SDK client → hand-written servers | `adoptamatch` over stdio and `pet-care` over Streamable HTTP both satisfy a reference client, in both of its negotiation modes |
| Hand-written client → official SDK servers | The fixture servers in `tests/servers/` are built with the SDK on purpose, so every test in `test_manager.py` is also an interop test |
| Hand-written client → real third-party servers | The reference Filesystem (`npx`) and Git (`uvx`) servers connect and run tools, verified by `--check` and by `scripts/demo_filesystem_git.py` |

## Supported MCP transports

| Transport | Used for | How the host connects |
| --- | --- | --- |
| **stdio** | Local servers | The host spawns the server as a subprocess and speaks JSON-RPC over its stdin/stdout. No network socket is involved. |
| **Streamable HTTP** | The remote `pet-care` server | The host opens an HTTP connection to a URL (usually ending in `/mcp`) and exchanges JSON-RPC over it, on TCP. |

SSE is deliberately not used: it is the deprecated remote transport and new
projects should not start with it.

**Protocol revision.** The client proposes `2025-06-18` in `initialize` and
accepts whatever the server answers with. `/servers` shows the negotiated revision
per server. There is no negotiation guesswork and no probing: the handshake is the
first thing on the wire, exactly as the specification prescribes.

## LLM provider

The assignment asks for a connection to an LLM "at the API level" — it does not
mandate a vendor. Two are implemented behind the same `LLMProvider` interface
(`llm/base.py`), chosen with `LLM_PROVIDER` in `.env`:

| Provider | `LLM_PROVIDER` | Cost | Get a key |
| --- | --- | --- | --- |
| Anthropic (Claude) | `anthropic` (default) | Paid; new accounts get $5 of trial credit | https://console.anthropic.com/ |
| Google Gemini | `gemini` | Free tier, no card required | https://aistudio.google.com/apikey |

Both support tool calling, which the whole host depends on to invoke MCP tools.
OpenAI's API is not offered here because, unlike Gemini's, it has no free tier at
all — so it would not solve the "no budget left" problem it might seem to.

Adding a third provider is one new class implementing `complete()` and
`tool_result_message()`, plus one branch in `cli.py::build_provider()` — see
`docs/architecture.md`.

## Requirements

- **Python 3.10+** (developed and tested on 3.12).
- **No MCP SDK.** Runtime dependencies are `anthropic`, `google-genai`, `httpx2`,
  `pydantic`, `python-dotenv` and `rich`; a test asserts that none of them is an
  MCP SDK.
- **[uv](https://docs.astral.sh/uv/)** for dependencies and the lockfile.
- **Node.js 18+** — only for the official Filesystem server, launched via `npx`.
- **Git** — only for the official Git server, launched via `uvx`.
- An **Anthropic or Gemini API key** (see "LLM provider" above), unless you run
  with `--offline`.
- The sibling repository [`adoptamatch-mcp`](../adoptamatch-mcp) cloned next to
  this one, if you want the shelter tools.

### Windows and Linux

The same configuration file works on both. The MCP SDK resolves `npx` to `npx.cmd`
and `uvx` to `uvx.exe` on Windows automatically, so no `cmd /c` wrapper is needed.
Paths in `servers.toml` are resolved relative to that file, so they stay portable.

Windows specifics worth knowing:

- Use PowerShell or Git Bash; both work.
- If `npx` or `uvx` are not found at all, install Node.js and uv respectively and
  open a new terminal so `PATH` is refreshed.
- The first `uvx mcp-server-git` run downloads the package; give it a generous
  `connect_timeout_seconds` (the example config uses 120).

## Installation

```bash
git clone <PRIVATE_REPOSITORY_URL> adoptamatch-chatbot
cd adoptamatch-chatbot
uv sync

cp .env.example .env                              # then edit .env
cp config/servers.example.toml config/servers.toml  # then edit the paths
```

If you want the shelter tools, clone the public server next to this repository so
the default relative path in `servers.toml` resolves:

```bash
cd ..
git clone https://github.com/FabianMoraeles/adoptamatch-mcp.git
cd adoptamatch-mcp && uv sync && cd ../adoptamatch-chatbot
```

For the Filesystem and Git scenario, create the scoped demo areas once:

```bash
mkdir -p demo_workspace
git init -b main demo_workspace/demo-repo
git -C demo_workspace/demo-repo config user.name  "AdoptaMatch Demo"
git -C demo_workspace/demo-repo config user.email "demo@example.invalid"
```

Verify the whole wiring without spending a token:

```bash
uv run adoptamatch-chatbot --offline --check
```

That connects every enabled server, lists the discovered tools and exits.

## Environment variables

`.env` is git-ignored. Copy `.env.example` and fill it in.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | no | `anthropic` | `anthropic` or `gemini`. See "LLM provider" above. |
| `ANTHROPIC_API_KEY` | if provider is `anthropic` (not with `--offline`) | — | Your API key. Never committed. |
| `ANTHROPIC_MODEL` | no | `claude-opus-5` | Model id. Kept here, never hard-coded in the source. |
| `GEMINI_API_KEY` | if provider is `gemini` (not with `--offline`) | — | Your free API key. Never committed. |
| `GEMINI_MODEL` | no | `gemini-flash-latest` | Model id; the `-latest` alias tracks Google's current free-tier Flash model. |
| `MCP_SERVERS_CONFIG` | no | `config/servers.toml` | Path to the server inventory. |
| `LOG_DIR` | no | `logs` | Where the JSONL session logs are written. |
| `LOG_LEVEL` | no | `INFO` | Python logging level for the host itself. |
| `MAX_TOOL_ITERATIONS` | no | `8` | Tool rounds allowed inside one user turn. |

Missing or malformed values are reported at start-up with a message that says
what to fix, and the process exits with status 2 — never a traceback.

## Server configuration

`config/servers.toml` is a declarative inventory. Adding a server is an edit to
that file, never a code change. Full documentation of every key lives in the
comments of [`config/servers.example.toml`](config/servers.example.toml).

```toml
[[servers]]
name = "adoptamatch"                  # unique; used in logs and to qualify tool names
transport = "stdio"                   # "stdio" | "streamable-http"
enabled = true
description = "Own server: shelter search, matching, adoption"
command = "uv"
args = ["run", "adoptamatch-mcp"]
cwd = "../../adoptamatch-mcp"         # resolved RELATIVE TO THIS FILE
timeout_seconds = 30                  # per tools/call
connect_timeout_seconds = 60          # ceiling on one connection attempt

[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
url = "http://127.0.0.1:8080/mcp"
mode = "legacy"
```

The file ships with six entries: the own local server, the two official reference
servers, the own remote server, and two **disabled placeholders** for the
classmates' servers.

### Tool names and collisions

The host builds one map from *the name the model sees* to *the server that owns
it*. A tool whose name is unique across all servers keeps its bare name. When two
servers publish the same name, **every** colliding tool is qualified as
`<server>__<tool>` — a double underscore, because the API's tool-name pattern does
not accept a dot. `/tools` shows both names.

## Running the chatbot

```bash
uv run adoptamatch-chatbot                 # normal run
uv run adoptamatch-chatbot --offline       # no API key; scripted stand-in
uv run adoptamatch-chatbot --check         # connect, list tools, exit
uv run adoptamatch-chatbot --model claude-sonnet-5
uv run adoptamatch-chatbot --servers path/to/other.toml
uv run adoptamatch-chatbot --no-protocol-log
```

`--offline` replaces the model with a small keyword router. It is **not** a
language model: it exists so the MCP plumbing — discovery, routing, logging, the
tool loop, clean shutdown — can be demonstrated and tested with no key and no
cost. Anything it says is labelled `Offline mode:`.

## Console commands

| Command | What it does |
| --- | --- |
| `/help` | List the commands and show the current context size. |
| `/servers` | Every configured server: transport, state, negotiated protocol, tool count, target or error. |
| `/tools` | Every discovered tool, the server that owns it, and its original name if it was qualified. |
| `/logs` | The current log paths, a summary (events, tool calls, errors) and the last ten events. |
| `/clear` | Erase the conversation context after confirmation. MCP connections stay up. |
| `/exit` | Close every client, stream and subprocess, then quit. |

Ctrl-D and Ctrl-C also leave cleanly: shutdown lives in a `finally` block.

## Adding a new MCP server

1. **Read its code first.** A stdio server runs as a subprocess with your
   privileges on your machine. Check what it touches and what it depends on.
2. Test it in isolation with MCP Inspector:
   `npx @modelcontextprotocol/inspector <command> <args>`.
3. Add a `[[servers]]` entry to `config/servers.toml` with `enabled = false`.
4. Run `uv run adoptamatch-chatbot --offline --check`; the entry is listed as
   `disabled`.
5. Flip `enabled = true` and re-run `--check`. Confirm it reports `connected` and
   the expected number of tools.
6. Check `/tools` for name collisions with servers you already have.
7. Note what it does in [`docs/server-specifications.md`](docs/server-specifications.md).

If it fails to connect, the failure is isolated: the entry shows as `failed` with
the reason, and everything else keeps working.

## Logs

Every session writes to `LOG_DIR` (default `logs/`, git-ignored):

| File | Contents |
| --- | --- |
| `session-<uuid>.jsonl` | The host-level log: one JSON object per event. |
| `session-<uuid>.wire.jsonl` | **Every JSON-RPC frame, both directions**, tagged with its kind (`request` / `notification` / `response` / `error`) and whether it is part of the lifecycle handshake. Complete, because the host writes those bytes itself. |
| `session-<uuid>.<server>.stderr.log` | Whatever each stdio subprocess wrote to stderr. Reference servers are chatty; this keeps the console clean and the noise recoverable. |

A request and its response share a `request_id`. A real pair, copied from a run of
the Filesystem + Git scenario:

```json
{"timestamp":"2026-09-04T00:29:54.173+00:00","session_id":"6560c81e-2bff-4fac-afa3-cc8be8d9a497","server":"git","transport":"stdio","direction":"request","method":"tools/call","request_id":"aca2c7b07213","elapsed_ms":0,"status":"ok","tool":"git_commit","params":{"repo_path":"demo_workspace/demo-repo","message":"docs: add shelter notes via MCP"}}
{"timestamp":"2026-09-04T00:29:54.256+00:00","session_id":"6560c81e-2bff-4fac-afa3-cc8be8d9a497","server":"git","transport":"stdio","direction":"response","method":"tools/call","request_id":"aca2c7b07213","elapsed_ms":82,"status":"ok","tool":"git_commit","result":"Changes committed successfully with hash 229aa230afca2aec5b1c967ed9fd4b99e46fca45"}
```

Fields: `timestamp` (ISO-8601 UTC), `session_id`, `server`, `transport`,
`direction` (`request` | `response` | `error` | `notification` | `lifecycle`),
`method`, `request_id`, `elapsed_ms`, `status`, and, when they apply, `tool`,
`params`, `result` and `error`.

**Redaction is automatic.** Any key whose name contains `api_key`, `token`,
`authorization`, `secret`, `password`, `credential` or `private_key` has its value
replaced with `[REDACTED]`, at any depth, and any value that *looks* like a
credential (`sk-…`, `ghp_…`) is scrubbed even under an innocent key. Payloads
larger than 4000 characters are truncated in the log — the model still receives
the full result.

Quick analysis from the shell:

```bash
# every tool call and how long it took
python -c "import json,sys;[print(r['timestamp'],r['server'],r.get('tool'),r['elapsed_ms']) for r in map(json.loads,open(sys.argv[1],encoding='utf-8')) if r['method']=='tools/call']" logs/session-<uuid>.jsonl

# only the failures
grep '"status":"error"' logs/session-<uuid>.jsonl
```

## Demo scenarios

The prompts to type live in [`config/demo-prompts.md`](config/demo-prompts.md).
In short:

1. **General knowledge** — a question with no tool call, proving the LLM link.
2. **Context retention** — two related questions where the second only makes sense
   given the first.
3. **AdoptaMatch** — describe a household, get explained recommendations, compare
   two candidates, register an adoption, then try to adopt the same animal twice.
4. **Filesystem + Git** — create a directory and a `README.md` through the
   Filesystem server, then stage and commit it through the Git server, and show in
   `/logs` which server executed each step.
5. **Remote server** — ask a pet-care question answered over Streamable HTTP,
   while Wireshark captures the exchange.

Scenario 4 also exists as a deterministic script, for use as a demo contingency
when the API is unreachable:

```bash
uv run python scripts/demo_filesystem_git.py
```

It performs the same MCP calls through the same manager with the model replaced by
a fixed plan, and prints the resulting log.

## Tests

```bash
uv run pytest -q          # 88 tests
uv run ruff check .
uv run ruff format --check .

cd remote_server && uv run pytest -q   # 18 tests, incl. a real HTTP round-trip
```

**No test ever calls a language-model API** — every model turn comes from
`ScriptedProvider`, so the suite is deterministic and free. The MCP side is real:
the fixture servers in `tests/servers/` are launched as actual subprocesses and
spoken to over stdio.

Coverage, by area:

| Area | Examples |
| --- | --- |
| The protocol itself | Frame construction, the four message kinds, monotonic ids, every reserved JSON-RPC error code, lifecycle classification |
| No MCP SDK at runtime | No module imports one, importing the package never loads one, the declared runtime dependencies contain none |
| Interoperability | Hand-written client against official SDK servers over both transports; official SDK client against the hand-written servers |
| Transports | Handshake, frame hook in both directions, timeout, unstartable server, server that exits immediately, non-JSON noise on stdout, double close |
| Configuration | missing file, invalid TOML, unknown key, duplicate name, transport/field mismatch, missing `cwd`, bad timeout, the shipped example file itself |
| Discovery | tools merged from several servers, handshake details recorded |
| Collisions | colliding names qualified, unique names left bare, each qualified call reaching the right server |
| Routing | unqualified call to its only owner, unknown tool reported with the available list |
| Failure isolation | a server that cannot start, a tool that times out, a disabled server |
| Serialisation | structured content preferred, text fallback, error results |
| The loop | no tool, one tool, parallel tools in one message, several sequential rounds, the iteration cap |
| Context | a follow-up question that still sees the first turn; tool results still in the payload one turn later; trimming that never orphans a `tool_result` |
| Logging | required fields, request/response correlation, counters, redaction, truncation |
| Shutdown | `aclose()` releases everything and is idempotent |

## The interface

The terminal UI is a designed artefact, not an accident — the assignment's second
optional extra. The full rationale is in [`docs/ui-design.md`](docs/ui-design.md);
the rules it follows:

- **Hierarchy.** The answer is what you read, so it gets the plain foreground and
  the most space. Tool activity is indented and dimmed so it recedes. Errors are
  the only thing allowed to be loud.
- **Semantic colour.** Six roles, six meanings: cyan for identity and commands,
  magenta for machine activity, green for success, red for failure, yellow for
  warnings, dim for metadata. Magenta sits far from the green/red pair, which is
  the one colour-blind readers most often confuse.
- **Colour is never the only signal.** Every state also carries a glyph and a
  word, so `✓ ok` and `✗ failed` stay distinguishable with no colour at all.
- **Feedback.** Spinners name what they are waiting for; every tool call is
  announced before it runs; every result reports its duration and its correlation
  id.
- **Progressive disclosure.** Compact by default, `/verbose` for full payloads.
- **Accessibility.** `--no-color`, the `NO_COLOR` environment variable, `--ascii`
  for terminals that cannot render the glyphs, and a width-adaptive layout.

```bash
uv run adoptamatch-chatbot --offline --verbose   # full arguments and results
uv run adoptamatch-chatbot --offline --no-color  # semantics survive without colour
uv run adoptamatch-chatbot --offline --ascii     # ASCII glyph set
```

## Remote server and deployment

`remote_server/` is a self-contained project: **pet-care-mcp**, a small MCP server
over Streamable HTTP with two tools (`get_daily_care_checklist`,
`estimate_daily_water_ml`) and a plain `GET /healthz` route that is deliberately
*not* an MCP tool. See [`remote_server/README.md`](remote_server/README.md).

Run it locally:

```bash
cd remote_server
uv sync
HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp
```

Then set `enabled = true` on the `pet_care_remote` entry in `config/servers.toml`.

Deploying it to Google Cloud Run is documented step by step, with the cost and
risk stated up front, in [`docs/deployment.md`](docs/deployment.md). **Nothing is
deployed automatically.** Once you have a URL, put it in `servers.toml` — never in
the source.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ANTHROPIC_API_KEY is not set` | Copy `.env.example` to `.env` and fill it in, set `LLM_PROVIDER=gemini` for a free alternative, or run `--offline`. |
| `GEMINI_API_KEY is not set` | Add a free key from https://aistudio.google.com/apikey to `.env`, or run `--offline`. |
| `credit balance is too low` (Anthropic) | The trial credit is spent. Add credits, switch to `LLM_PROVIDER=gemini`, or run `--offline`. |
| `Gemini's free-tier rate limit was reached` | Free-tier requests/minute or requests/day were exceeded. Wait, or switch to `LLM_PROVIDER=anthropic`. |
| `MCP server configuration not found` | Copy `config/servers.example.toml` to `config/servers.toml`. |
| A server shows `failed` with `timed out … during the handshake` | It never answered `initialize`. Read `logs/session-<id>.<server>.stderr.log` — the real error is almost always there. |
| `filesystem` fails to start | Node.js is missing, or the scoped directory does not exist. Run `mkdir demo_workspace`. |
| `git` fails to start | `uvx` is missing, or `demo_workspace/demo-repo` is not a repository. Run `git init demo_workspace/demo-repo`. |
| `git_*` returns "outside the allowed repository" | Pass `repo_path` as the path the server was launched with (`demo_workspace/demo-repo`), not `.`. |
| `adoptamatch` fails to start | The sibling repository is missing or not installed. Clone it next to this one and run `uv sync` there. |
| The remote server shows `failed` | It is not running, or the port differs. Check `curl http://127.0.0.1:8080/healthz`. |
| A tool result looks truncated in the log | Only in the log: payloads over 4000 characters are capped there. The model received the full result. |
| Unreadable server output on the console | It should not appear — stdio servers' stderr goes to `logs/session-<id>.<server>.stderr.log`. Look there for the real error. |
| `git_add` fails with `Filename too long` (Windows) | The clone sits under a very deep path and Git hits `MAX_PATH`. Move the repository closer to the drive root, or enable long paths: `git config --system core.longpaths true` (needs an elevated shell). |

## Security notes

- **Secrets never enter Git.** `.env`, `config/servers.toml`, `*.key`, `*.pem` and
  every log file are git-ignored. The logger redacts credential-shaped values
  before writing.
- **The Filesystem server is scoped to `./demo_workspace`.** Never point it at your
  home directory or a drive root: whatever scope it is given, the model can read
  and write inside it.
- **The Git server is scoped to `./demo_workspace/demo-repo`**, a throw-away
  repository. It is not pointed at this project's own history.
- **Classmates' servers ship disabled** and must be read before being enabled. A
  stdio MCP server is an arbitrary program running with your privileges.
- **`register_adoption` is irreversible** through MCP. The system prompt requires
  explicit user confirmation and an e-mail before it is called.
- **The remote server gives no medical advice** and returns a disclaimer with every
  result.
- **Adopter data is demonstration data.** The shelter database stores names and
  e-mails in plain text in a local SQLite file, with no authentication. Do not put
  real personal data in it.
- **`--offline` output is not a model answer** and is labelled as such, so it can
  never be mistaken for one in a demo.

## Repository layout

```text
adoptamatch-chatbot/
├── src/adoptamatch_chatbot/
│   ├── cli.py                  entry point: parse, validate, connect, run, close
│   ├── app.py                  conversation loop and console commands
│   ├── config.py               .env + servers.toml loading and validation
│   ├── conversation.py         session history
│   ├── presentation.py         all terminal rendering
│   ├── llm/
│   │   ├── base.py             provider-neutral interface
│   │   ├── anthropic_provider.py
│   │   ├── gemini_provider.py  free alternative (LLM_PROVIDER=gemini)
│   │   └── scripted.py         test double and --offline router
│   ├── mcp_wire/            hand-written MCP client, no SDK
│   │   ├── messages.py      JSON-RPC framing and classification
│   │   ├── transports.py    stdio (pipes) and Streamable HTTP (TCP)
│   │   └── session.py       the MCP lifecycle
│   └── mcp_host/
│       ├── models.py           ServerConfig, ToolRef, ServerStatus, ToolCallOutcome
│       ├── manager.py          connections, discovery, routing, timeouts, shutdown
│       └── logger.py           JSONL logging with redaction
├── remote_server/              pet-care-mcp (Streamable HTTP) + Dockerfile + tests
├── config/
│   ├── servers.example.toml    documented inventory to copy
│   └── demo-prompts.md         the prompts to type during the demo
├── docs/
│   ├── rubric-map.md           every requirement, where it is, how to verify it
│   ├── architecture.md
│   ├── ui-design.md             the interface rationale
│   ├── server-specifications.md
│   ├── classmate-servers.md
│   ├── deployment.md
│   ├── wireshark-analysis.md
│   ├── report-outline.md
│   └── presentation-outline.md
├── scripts/demo_filesystem_git.py
├── tests/                      host tests + fixture MCP servers
├── logs/                        session logs (git-ignored)
├── .env.example
├── pyproject.toml
└── uv.lock
```

## Related repository

[`adoptamatch-mcp`](https://github.com/FabianMoraeles/adoptamatch-mcp) — the public
MCP server with the shelter tools and the explainable matching algorithm.
