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
| **Client** | One `mcp.client.Client` per server, inside `MCPManager` | Speaks JSON-RPC to exactly one server. Owns the handshake, the session id and the transport. |
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
│  llm/anthropic_provider.py   real                                                     │
│  llm/scripted.py             tests and --offline                                      │
│                                                                                       │
│  mcp_host/manager.py  MCPManager                                                      │
│    per server: AsyncExitStack → Client → handshake → tools/list                       │
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

### One `AsyncExitStack` per server

Every server is entered into its own exit stack, and the stacks are unwound
independently. A server that hangs or crashes on shutdown cannot prevent the others
from closing. The same isolation applies at connect time: a failure is recorded on
that server's status and the loop moves on.

### Qualify tool names only on a collision

Prefixing every tool with its server would make names longer and prompts worse for
no benefit in the common case. The manager counts occurrences first, and qualifies
as `<server>__<tool>` only the tools that actually collide. `/tools` shows both the
exposed name and the original.

The separator is a double underscore rather than a dot because the Claude API's
tool-name pattern accepts `[a-zA-Z0-9_-]` only.

### Connect timeout plus a legacy retry

MCP 2.x negotiates by probing a modern `server/discover` method and falling back to
the classic `initialize` handshake. Two of the official reference servers do not
answer that probe at all, so an `auto` attempt would sit until the read timeout.
Each attempt is therefore bounded by `connect_timeout_seconds`, and on failure the
host retries once in `legacy` mode. Both attempts are in the log, and `/servers`
shows which mode actually won.

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

### Two logs, one session

The host-level JSONL log is decoded and correlated — it is what the report uses.
The protocol-level log captures only what a server *initiates* (notifications and
server-to-client requests), because responses to the host's own requests do not
pass through the SDK's message hook. Each stdio server's stderr goes to its own
file, which keeps the console readable and the failure recoverable.

### Configuration is data

Server inventory lives in TOML, secrets in the environment. The model id comes from
`ANTHROPIC_MODEL`, never from a literal in the code. A deployed server's URL goes
in `servers.toml`, never in a constant. Adding a classmate's server is an edit to a
file, and the tests assert that the shipped example file parses and that the
classmate placeholders ship disabled.

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
| A server hangs during negotiation | `connect_timeout_seconds` fires; a legacy retry follows. |
| A tool exceeds `timeout_seconds` | The call returns `is_error`; the session continues. |
| A tool raises | The server returns `isError: true`; the host relays the message. |
| The model names a tool that does not exist | The host answers with the list of tools that do. |
| The model loops on tools | `MAX_TOOL_ITERATIONS` stops the turn and says so. |
| The LLM API fails | The error is printed; the session stays open. |
| Ctrl-C or Ctrl-D | Shutdown runs from a `finally` block; every subprocess is closed. |

## What is deliberately *not* here

- No SSE transport — it is the deprecated remote transport.
- No hand-rolled JSON-RPC on the required path — the official SDK is used, as the
  assignment requires.
- No streaming of model output — it would complicate the loop without demonstrating
  anything about MCP.
- No persistence of conversations across runs — the log is the record.
