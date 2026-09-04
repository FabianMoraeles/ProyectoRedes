# Report outline

An editable skeleton. Every `[fill in]` is something only you can write — a real
observation, a real number, a real difficulty. Do not invent any of them.

Suggested length: 12–18 pages including figures.

---

## 1. Introduction and objectives (1 page)

- What MCP is, in three sentences: a protocol that lets a model use tools exposed
  by independent servers, with a host in the middle that owns the conversation and
  the security boundary.
- Why the *host* is the interesting part of this assignment: it is where routing,
  isolation, logging and transport choice actually live.
- The business case: AdoptaMatch, an assistant for an animal shelter.
- Objectives, mapped one-to-one to the rubric. Say up front which are done and
  which are not.

## 2. Architecture (2–3 pages)

- The host / client / server split, and who plays each role here.
- The component diagram from `docs/architecture.md`.
- The nine steps of one user turn.
- Two transports side by side: stdio for local servers, Streamable HTTP for the
  remote one. Why not SSE.
- Design decisions worth defending, one short paragraph each:
  - the host owns the agentic loop rather than the SDK's tool runner, because
    routing and logging live there;
  - one `AsyncExitStack` per server, so one failure is isolated;
  - tool names qualified only on a real collision;
  - errors turned into `tool_result` payloads instead of exceptions;
  - configuration as data, secrets in the environment.

**Figure 1** — component diagram.
**Figure 2** — data flow of a single tool call.

## 3. Own servers (2–3 pages)

### 3.1 `adoptamatch` (local, stdio, public repository)

- Data model, and why the compatibility flags are three-state.
- The two-stage algorithm: hard rules, then the weighted score. Include the
  weights table and the size-fit table.
- One worked example: a real household, a real recommendation, its score
  breakdown, its reasons and its concerns. Copy it from a tool result.
- Why the explanations cannot contradict the score — they are generated from the
  same components, and a test asserts it.
- Transactional adoption and the duplicate-adoption guard.

### 3.2 `pet-care` (remote, Streamable HTTP)

- Both tool specifications with parameters, constraints and an example response.
- Why `GET /healthz` is a plain HTTP route and not an MCP tool.
- The container: non-root, reads `PORT`, no secrets.
- Deployment: [fill in — local only, LAN, or a deployed URL], and why.

**Figure 3** — a `recommend_animals` result with its breakdown.

## 4. Integrations (2 pages)

### 4.1 Official reference servers

- Filesystem, scoped to `demo_workspace`; Git, scoped to
  `demo_workspace/demo-repo`.
- The reproducible scenario, step by step, with the log lines that prove which
  server executed each step.
- Two concrete findings worth reporting:
  - both are handshake-era and do not answer the modern `server/discover` probe,
    which is why the host bounds each attempt and retries in `legacy` mode;
  - the Git server's `repo_path` must be the path it was launched with, not `.`.

### 4.2 Classmates' servers

[fill in — not integrated at the time of writing. For each server: author,
repository, purpose, transport, tools, dependencies, risks noticed while reading
the code, whether any tool name collided with ours, and the scenario demonstrated.
Use the checklist in `docs/classmate-servers.md` and the table in
`docs/server-specifications.md` § 5.]

**Figure 4** — `/servers` and `/tools` with every server connected.

## 5. Demonstrated scenarios (1–2 pages)

For each of the five scenarios in `config/demo-prompts.md`: the prompt, what the
host did, and the log excerpt that proves it.

- A general question with zero tool calls — the LLM link.
- Two related questions — context retention. Quote both, and say explicitly that
  the second resolves only because the whole history is resent.
- The shelter flow, including the rejected duplicate adoption.
- Filesystem then Git, with the commit hash as evidence.
- The remote server, captured in Wireshark.

## 6. Network analysis (3–4 pages)

Follow `docs/wireshark-analysis.md`, and lead with the honest headline: three of
the four servers use stdio and therefore produce **no** network traffic at all.
The remote server exists so there is something to capture.

- Capture conditions: [fill in — interface, filter, date, duration, packet count].
- **Link layer**: [fill in — frame type, MAC addresses or the loopback
  pseudo-header, frame sizes, MTU].
- **Network layer**: [fill in — IP version, addresses, TTL, fragmentation, DNS if
  applicable].
- **Transport layer**: [fill in — ports, the three-way handshake with frame
  numbers, MSS and window scaling, how many TCP connections carried how many MCP
  messages, the teardown, and any retransmissions — or an explicit statement that
  there were none, and why].
- **Application layer**: [fill in — the HTTP request line and headers, the
  `Mcp-Session-Id`, and the JSON-RPC classification: request / success response /
  error response / notification, with one captured example of each].
- Correlation table: `request_id` ↔ frame numbers ↔ timings.
- If HTTPS: say plainly that TLS encrypts the application payload, show what *is*
  visible (SNI, cipher suite, record sizes and timing), and explain that the
  plaintext comes from the host's own log, recorded before encryption — or from a
  decryption with your own `SSLKEYLOGFILE`, if you did that.

**Figure 5** — the TCP three-way handshake.
**Figure 6** — `Follow HTTP Stream` on a `tools/call`.
**Figure 7** — the correlation table.

## 7. Difficulties and solutions (1–2 pages)

Write these from what actually happened. Several are already documented in the
code and are yours to reuse if they were yours:

| Difficulty | How it showed up | Solution |
| --- | --- | --- |
| The MCP Python SDK is at 2.x, where `FastMCP` became `MCPServer` and the client gained a `Client` facade | Any 1.x example fails at import | Read the installed package instead of relying on remembered APIs |
| Two reference servers hang on the `server/discover` probe | The Filesystem server took 72 s and then failed to connect | Bound each attempt with `connect_timeout_seconds` and retry once in `legacy` mode; set `mode = "legacy"` explicitly for known handshake-era servers |
| The Git server logged a 31-error validation warning on every start | Unreadable console | Same root cause; plus each stdio server's stderr now goes to its own log file |
| Closing a Streamable HTTP client raised `CancelledError` | Teardown aborted and the closing log lines were lost | The transport's own task group cancels the final `DELETE`; swallow it on the shutdown path only, with a comment saying why |
| The Git server rejected `repo_path: "."` | "outside the allowed repository" | Pass the path the server was launched with |
| SQLite connections are thread-bound | Would break depending on how the SDK schedules tool functions | One short-lived connection per tool call |
| `sqlite3.Connection.execute` is read-only | The rollback test could not monkeypatch it | Inject the failure with a delegating proxy object |

[fill in — your own, with what you tried first and why it did not work.]

## 8. Conclusions and lessons learned (1 page)

Prompts, not answers:

- Where does the real security boundary of an MCP host sit, given that a stdio
  server is an arbitrary program running with your privileges?
- What did supporting two transports actually cost in complexity, and what did the
  packet capture teach you that reading the code did not?
- What would you change if the host had to support twenty servers instead of six?
- What is the honest limit of the compatibility score, and how would you validate
  it against real adoption outcomes?

## 9. References

- Model Context Protocol specification — <https://modelcontextprotocol.io/>
- MCP Python SDK — <https://github.com/modelcontextprotocol/python-sdk> (version
  used: `mcp 2.1.1`)
- Official reference servers — <https://github.com/modelcontextprotocol/servers>
  (`@modelcontextprotocol/server-filesystem`, `mcp-server-git`)
- Anthropic Python SDK — <https://github.com/anthropics/anthropic-sdk-python>
  (version used: `anthropic 1.3.0`)
- Wireshark User's Guide — <https://www.wireshark.org/docs/wsug_html_chunked/>
- [fill in — every other source you used, including anything an AI assistant
  produced for you, and what you changed about it.]

## Appendices

- A: the full `servers.toml` used for the demo.
- B: one complete session log (`logs/session-<uuid>.jsonl`).
- C: the capture file `mcp-capture.pcapng`.
- D: test output (`uv run pytest -q` in both repositories).
