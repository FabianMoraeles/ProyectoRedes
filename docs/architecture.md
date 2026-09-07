# Architecture

## The one-sentence version

A console program holds a conversation with a language model, and whenever the
model asks for a tool, the program finds which of several MCP servers owns that
tool, calls it, logs everything, and feeds the result back.

## Roles

MCP separates three roles. Being precise about them explains most of the design.

| Role | Who plays it here | Responsibility |
| --- | --- | --- |
| **Host** | `adoptamatch-chatbot` | Owns the conversation, the model, the security boundary and the log. Decides *which* servers exist and *what* the model is allowed to see. |
| **Client** | One `mcp_wire.ClientSession` per server, inside `MCPManager` | Speaks JSON-RPC to exactly one server. Owns the handshake, the session id and the transport. |
| **Server** | `adoptamatch`, `filesystem`, `git`, `pet-care`, classmates' | Exposes tools. Knows nothing about the model or the other servers. |

One host, several clients, several servers. A server never learns that other
servers exist, and the model never learns which server it is talking to except
through the name the host prefixes onto each tool description.

## Component diagram

```text
┌──────────────────────────────── HOST (this repository) ──────────────────────────────┐
│                                                                                       │
│  cli.py                                                                               │
│    parse args → load+validate config → open log → connect servers → run loop → close  │
│      │                                                                                │
│      ▼                                                                                │
│  app.py  ChatApp                                                                      │
│    ┌────────────────────────────────────────────────────────────────────┐             │
│    │ 1 read line                                                        │             │
│    │ 2 conversation.add_user()                                          │             │
│    │ 3 provider.complete(system, history, tool_specs) ──────────────┐   │             │
│    │ 4 for each tool_call: manager.call_tool(name, args) ───────┐   │   │             │
│    │ 5 conversation.add_raw(provider.tool_result_message(...))  │   │   │             │
│    │ 6 loop until no tool_call, max MAX_TOOL_ITERATIONS         │   │   │             │
│    │ 7 print                                                    │   │   │             │
│    └────────────────────────────────────────────────────────────┼───┼───┘             │
│                                                                 │   │                 │
│  conversation.py  ── the full history, resent every turn ───────┘   │                 │
│  presentation.py  ── all rendering                                  │                 │
│                                                                     │                 │
│  llm/base.py        LLMProvider protocol ───────────────────────────┘                 │
│  llm/anthropic_provider.py   real, paid                                               │
│  llm/gemini_provider.py      real, free tier (LLM_PROVIDER=gemini)                    │
│  llm/scripted.py             tests and --offline                                      │
│                                                                                       │
│  mcp_host/manager.py  MCPManager                                                      │
│    per server: mcp_wire.ClientSession → handshake → tools/list                        │
│    tool map:  exposed_name → ToolRef(server, original_name, schema)                   │
│    call_tool: log request → client.call_tool(timeout) → log response/error            │
│                                                                                       │
│  mcp_host/logger.py  InteractionLogger → logs/session-<uuid>.jsonl                    │
└───────────────────────────────────────────────────────────────────────────────────────┘
        │ stdio (pipes)                                   │ Streamable HTTP (TCP)
        ▼                                                 ▼
  adoptamatch · filesystem · git · classmate ×2      pet-care (local or deployed)
```

## Key decisions, and why

### The host owns the agentic loop

The Anthropic SDK ships a tool runner that would drive the loop automatically. It
is not used here, deliberately: the assignment is about being an MCP host, and the
loop is where routing, logging and timeout policy live. Writing it explicitly makes
those visible and testable. The provider class is reduced to a single-turn adapter.

### Two interchangeable LLM providers, picked by `LLM_PROVIDER`

The assignment requires "a connection to an LLM at the API level" (requirement
1) but names no vendor; the brief only *suggests* Anthropic because new accounts
get $5 of trial credit. That credit is not renewable, so a second, genuinely
free option was added: Google's Gemini API, which needs no billing setup and
still supports tool calling. `llm/anthropic_provider.py` and
`llm/gemini_provider.py` are both single-turn adapters behind the same
`LLMProvider` Protocol (`llm/base.py`); `cli.py::build_provider()` is the one
place that picks between them, so nothing in `app.py`, the MCP manager or the
logger changes based on which model is answering. OpenAI's API was considered
and rejected for this role: it has no free tier at all, so it does not solve
the problem a free option is meant to solve.

The two SDKs disagree on how a tool call is represented on the wire (Anthropic
gives every `tool_use` block a persistent id to pair with its `tool_result`;
Gemini's `function_call` carries only a name), so `GeminiProvider` mints its own
id per call and keeps a local id→name map to translate the reply back. That
translation is the only place a provider difference leaks past the `LLMProvider`
interface.

### MCP is implemented directly over JSON-RPC, with no MCP SDK

This is the assignment's first extra, and it also turned out to be the simpler
design. `mcp_wire` builds and parses every frame itself, against the JSON-RPC 2.0
and MCP specifications. Three consequences worth stating:

* **The wire log is complete.** Because the host writes the bytes, it can record
  every frame in both directions, classified. An SDK's message hook only exposes
  what a server *initiates*.
* **The lifecycle is explicit.** `initialize`, `notifications/initialized`,
  `tools/list`, `tools/call` is a few lines of readable code, which is exactly what
  the network report has to describe.
* **Interoperability is proved, not assumed.** The official SDK is a test-only
  dependency used as a conformance oracle, in both directions: its client drives
  the hand-written servers, and the hand-written client drives its servers as well
  as the reference Filesystem (`npx`) and Git (`uvx`) servers.

The servers use the same approach: `minimcp.py`, a single self-contained module
vendored identically into `adoptamatch-mcp` and `pet_care_mcp`.

### One session per server, closed independently

Each server gets its own `ClientSession` and its own subprocess or connection
pool, and they are torn down one at a time. A server that hangs or crashes on
shutdown cannot prevent the others from closing. The same isolation applies at
connect time: a failure is recorded on that server's status and the loop moves on.

### Qualify tool names only on a collision

Prefixing every tool with its server would make names longer and prompts worse for
no benefit in the common case. The manager counts occurrences first, and qualifies
as `<server>__<tool>` only the tools that actually collide. `/tools` shows both the
exposed name and the original.

The separator is a double underscore rather than a dot because the Claude API's
tool-name pattern accepts `[a-zA-Z0-9_-]` only.

### One handshake, bounded by a connect timeout

There is no negotiation guesswork: the client sends `initialize` first, exactly as
the specification prescribes, so a server either completes the handshake or fails
for a reason worth reporting. Each attempt is bounded by `connect_timeout_seconds`,
so a server that never answers cannot stall start-up.

This is worth a paragraph in the report, because it replaced a real problem. An
earlier version of this host used the official SDK's client, whose default
negotiation probes a newer `server/discover` method before falling back to
`initialize`. Two of the official reference servers neither answer nor reject that
probe, so connecting to the Filesystem server took 72 seconds and then failed.
Writing the client by hand removed the failure mode entirely rather than working
around it.

### The history is the context

There is no memory system, no summarisation and no retrieval. Context retention is
the plain fact that the whole message list — user turns, assistant turns including
their `tool_use` blocks, and every `tool_result` — is sent again on every request.
The only cleverness is in trimming: when the list exceeds its cap, the trim skips
forward past any leading `tool_result`, because a `tool_result` without the
`tool_use` it answers is rejected by the API.

### Errors become results, not exceptions

A tool that fails, times out, or does not exist produces a `tool_result` with
`is_error: true` containing the reason. The model reads it and can correct itself.
An exception would end the turn and tell the user nothing useful.

### Three logs, one session

* The **host-level** JSONL log: one event per request and one per reply,
  correlated by `request_id`, with durations. This is the audit trail.
* The **wire log**: every JSON-RPC frame in both directions, tagged with its kind
  (`request` / `notification` / `response` / `error`) and whether it belongs to the
  lifecycle handshake. This is what the network analysis correlates against a
  packet capture. It is complete because the host writes those bytes itself.
* One **stderr file per stdio server**, which keeps the console readable and a
  failure recoverable.

### Configuration is data

Server inventory lives in TOML, secrets in the environment. The model id comes from
`ANTHROPIC_MODEL`, never from a literal in the code. A deployed server's URL goes
in `servers.toml`, never in a constant. Adding a classmate's server is an edit to a
file, and the tests assert that the shipped example file parses and that the
classmate placeholders ship disabled.

### One host, two presentation layers

`adoptamatch_chatbot.web` (a browser chat, `adoptamatch-chatbot-web`) exists
alongside the terminal, not instead of it. `ChatApp.handle_message` was already
written against a narrow six-method surface of `Presenter` (`thinking`,
`tool_call`, `tool_result`, `assistant`, `warn`, `error`), called live and
unawaited as the turn progresses -- so adding a second presentation layer meant
implementing that same surface once more (`web/presenter.py`, `WebPresenter`),
each method pushing a JSON-serialisable event onto an `asyncio.Queue` instead of
printing. A task in `web/server.py` drains that queue onto a WebSocket
concurrently with `handle_message` running, so a tool call and its result reach
the browser as they happen. `ChatApp`, `MCPManager`, `Conversation` and every
`LLMProvider` are untouched -- not one line of already-tested conversation logic
changed to support this, the same guarantee the `LLMProvider` Protocol makes for
swapping Anthropic and Gemini one layer down.

Two consequences of that boundary: `/clear`'s confirmation prompt (`input()` in
the terminal) has no equivalent over a socket, so the web server never calls
`ChatApp.handle_command` at all -- it implements the handful of slash commands
directly against `MCPManager`/`InteractionLogger` and lets the browser confirm
destructive ones with a native `confirm()` before sending. And each WebSocket
connection builds its own `Session` (one `ChatApp`, one `MCPManager`, one
`InteractionLogger`), so two browser tabs are as isolated from each other as two
terminal windows would be -- separate MCP subprocesses, separate session logs.

## Data flow of one tool call

```text
model                host                          client            server
  │  tool_use          │                              │                 │
  ├───────────────────▶│                              │                 │
  │                    │ resolve exposed_name→ToolRef │                 │
  │                    │ log(request, request_id)     │                 │
  │                    ├─────────────────────────────▶│ tools/call      │
  │                    │                              ├────────────────▶│
  │                    │                              │◀────────────────┤
  │                    │◀─────────────────────────────┤ CallToolResult  │
  │                    │ serialise: structured_content│                 │
  │                    │            else text blocks  │                 │
  │                    │ log(response|error, same id, elapsed_ms)       │
  │  tool_result       │                              │                 │
  │◀───────────────────┤                              │                 │
```

## Failure modes and what happens

| Failure | Behaviour |
| --- | --- |
| A server will not start | Status `failed` with the reason; the session continues without its tools. |
| A server hangs during the handshake | `connect_timeout_seconds` fires; the server is marked failed with the reason. |
| A tool exceeds `timeout_seconds` | The call returns `is_error`; the session continues. |
| A tool raises | The server returns `isError: true`; the host relays the message. |
| The model names a tool that does not exist | The host answers with the list of tools that do. |
| The model loops on tools | `MAX_TOOL_ITERATIONS` stops the turn and says so. |
| The LLM API fails | The error is printed; the session stays open. |
| Ctrl-C or Ctrl-D | Shutdown runs from a `finally` block; every subprocess is closed. |

## What is deliberately *not* here

- No SSE transport — it is the deprecated remote transport.
- No streaming of model output — it would complicate the loop without demonstrating
  anything about MCP.
- No persistence of conversations across runs — the log is the record.
