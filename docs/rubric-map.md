# Requirement map

Every numbered requirement of *Proyecto 1 — Uso de un protocolo existente*, with
where it is implemented and how to verify it. Status is honest: three items are
not finished, and they say so.

Legend: **done** — implemented and verified · **pending** — needs an action only
the student can take · **blocked** — waiting on something external.

---

## Chatbot behaviour (15 %)

### 1. Connect to an LLM through its API — 5 % · **done (needs a key to run)**

| | |
| --- | --- |
| Where | [`llm/anthropic_provider.py`](../src/adoptamatch_chatbot/llm/anthropic_provider.py), behind the provider-neutral interface in [`llm/base.py`](../src/adoptamatch_chatbot/llm/base.py) |
| Model | From `ANTHROPIC_MODEL`, never hard-coded. Default `claude-opus-5`. |
| Verify | Set `ANTHROPIC_API_KEY` in `.env`, run `uv run adoptamatch-chatbot`, ask a general-knowledge question. `/logs` shows **zero** tool calls for that turn — proof the answer came from the model, not from a tool. |
| Tests | `tests/test_app.py::test_a_plain_question_never_touches_a_tool`, plus every error path in `anthropic_provider.py`. |
| Caveat | The code path has not been exercised against the live API in this workspace, because no API key is configured here. Everything else is tested with a scripted stand-in so the suite costs nothing. |

### 2. Keep context within a session — 5 % · **done**

| | |
| --- | --- |
| Where | [`conversation.py`](../src/adoptamatch_chatbot/conversation.py). The whole message list — user turns, assistant turns including their `tool_use` blocks, and every `tool_result` — is resent on every request. |
| Verify | Ask "Tell me about Luna", then "And how much exercise does she need?". The second question names no animal and still resolves. |
| Tests | `test_the_second_question_still_sees_the_first`, `test_tool_results_stay_in_context_for_the_next_question`, `test_history_trimming_never_orphans_a_tool_result`. |

### 3. Keep and show a log of every MCP interaction — 5 % · **done**

| | |
| --- | --- |
| Where | [`mcp_host/logger.py`](../src/adoptamatch_chatbot/mcp_host/logger.py) |
| Files | `logs/session-<uuid>.jsonl` (host level, request/response correlated by `request_id`), `logs/session-<uuid>.wire.jsonl` (**every JSON-RPC frame in both directions**, classified), `logs/session-<uuid>.<server>.stderr.log` |
| Show | The `/logs` command prints the paths, a summary, the JSON-RPC frame counts by kind, and the last ten events. |
| Verify | `uv run python scripts/demo_filesystem_git.py`, then read `logs/`. |
| Tests | All of `tests/test_logger.py`, including secret redaction and truncation. |

## MCP servers, part one (30 %)

### 4. Official Filesystem and Git servers — 15 % · **done**

| | |
| --- | --- |
| Configured in | [`config/servers.example.toml`](../config/servers.example.toml) — Filesystem scoped to `./demo_workspace`, Git scoped to `./demo_workspace/demo-repo` |
| Scenario | Create a directory and a `README.md` through Filesystem, then `git_status` / `git_add` / `git_commit` / `git_log` through Git, in one turn. Prompt in [`config/demo-prompts.md`](../config/demo-prompts.md) § 4. |
| Deterministic replay | `uv run python scripts/demo_filesystem_git.py` runs the same MCP calls with the model replaced by a fixed plan — the demo contingency. |
| Evidence | The session log attributes each step to `filesystem` or `git` with its duration, and the commit hash is verifiable with `git -C demo_workspace/demo-repo log`. |

### 5. Own local MCP server, non-trivial, public repository — 15 % · **done (one action pending)**

| | |
| --- | --- |
| Repository | `adoptamatch-mcp` — separate, independent, to be made **public** |
| Tools | `search_animals`, `get_animal_details`, `recommend_animals`, `compare_animals`, `register_adoption` |
| Why not trivial | Hard household safety rules applied before scoring; a deterministic, explainable 0–100 score over six weighted dimensions whose written justifications are generated from the same components as the number; three-state compatibility flags where "unknown" never excludes but always costs points and raises a concern; a transactional adoption guarded by a `UNIQUE` constraint. |
| Specification | That repository's README, and [`server-specifications.md`](server-specifications.md) § 1. |
| Tests | 79 in `adoptamatch-mcp`. |
| **Pending** | Confirm the chosen functionality with the professor, as § 3.1 requires, and publish the repository. |

## MCP servers, part two (45 %)

### 6. Two classmates' servers — 15 % · **blocked**

Two placeholder entries ship **disabled** in `servers.example.toml`, and a test
asserts they stay disabled until they are real. The integration checklist —
including reading a classmate's code before running it — is in
[`classmate-servers.md`](classmate-servers.md), and the table to fill in is in
[`server-specifications.md`](server-specifications.md) § 5.

This cannot be completed until the class publishes its repositories.

### 7. Own remote MCP server on a cloud service — 15 % · **done locally, deployment pending**

| | |
| --- | --- |
| Server | `pet-care-mcp` in [`remote_server/`](../remote_server) — Streamable HTTP, `POST/GET/DELETE /mcp`, plus a plain `GET /healthz` that is deliberately **not** an MCP tool |
| Container | `remote_server/Dockerfile`: small, non-root, reads `HOST` and `PORT` |
| Verified | 18 tests, including a real HTTP round trip over TCP and an interoperability check against the official SDK client |
| **Pending** | The assignment requires it to run **on a cloud service**. Everything is prepared; deploying needs your account and your authorisation. Step-by-step guide, with cost and risk stated first, in [`deployment.md`](deployment.md). Once deployed, put the URL in `servers.toml` — never in the source. |

### 8. Wireshark analysis of the remote exchange — 15 % · **pending your capture**

| | |
| --- | --- |
| Guide | [`wireshark-analysis.md`](wireshark-analysis.md) — a fillable walkthrough, with placeholders instead of invented observations |
| The classification the requirement asks for | Synchronisation (`initialize`, `notifications/initialized`, `ping`), requests (`tools/list`, `tools/call`) and responses (`result` / `error`) — the host's wire log tags every frame with exactly that, so the capture and the log can be lined up frame by frame |
| Head start | `logs/session-<uuid>.wire.jsonl` already contains the plaintext of every message, classified, with ids. Filter it with `jq`/Python and match it against the capture. |
| **Pending** | The capture has to be taken on your machine, on your network. |

## Report (10 %)

### 9. Specification, parameters and endpoints of the own servers — **done**

[`server-specifications.md`](server-specifications.md) gives both servers in full:
parameters with types and constraints, outputs, errors, endpoints, and worked
examples. The public repository's README repeats the AdoptaMatch half for people
who only clone that one.

### 10. What happens at the link, network, transport and application layers — **template ready**

[`wireshark-analysis.md`](wireshark-analysis.md) § 6 has one section per layer,
each with the fields to fill in from your own capture, and § 0 states plainly the
thing most reports get wrong: three of the four servers use stdio and therefore
generate **no network traffic at all**.

### 11. Conclusions and commentary — **outline ready**

[`report-outline.md`](report-outline.md) §§ 7–8, with the real difficulties
already recorded (they are in the code comments and the commit messages) and
prompts for the conclusions only you can write.

## Documentation and version control (15 %)

| Requirement | Status |
| --- | --- |
| Private repository | The chatbot repository is local and unpublished. **Pending**: create it as private and grant access to the teaching accounts. |
| Public, independent repository for the local MCP server | `adoptamatch-mcp` is a separate repository with its own history, licence and README. **Pending**: publish it. |
| README in English, install-and-run from a clean clone | Done, and verified: both repositories were cloned into an empty directory and brought up following only the README. |
| Code documented | Every module has a docstring explaining *why*, not only *what*; every non-obvious decision carries a comment. |
| Commit messages in English | Done. |
| **Gradual version control over the five weeks** | ⚠️ **Partially at risk.** The history is real and incremental — sixteen commits in a sensible order — but it was created over a short period, and the rubric explicitly penalises "most commits made in the days before delivery". Nothing can honestly change that retroactively: commit dates must not be forged. Keep committing your own increments from here (the Wireshark analysis, the classmates' servers, the deployment, the report) so the history keeps growing on real dates. |

## Presentation (15 %)

[`presentation-outline.md`](presentation-outline.md) covers the three things § 3.3
requires — features implemented, difficulties and how they were solved, lessons
learned — with a demo script and a contingency plan.

## Extras

### Extra A — MCP over direct JSON-RPC, with no MCP SDK (+15 %, binary) · **done**

| | |
| --- | --- |
| Client | [`mcp_wire/`](../src/adoptamatch_chatbot/mcp_wire) — framing, both transports, the session lifecycle |
| Servers | `minimcp.py`, vendored identically into `adoptamatch-mcp` and `pet_care_mcp` |
| Runtime dependencies | No MCP SDK in any of the three projects. Enforced by tests: no module imports one, importing the package never loads one, and the declared runtime dependencies contain none. |
| Wire compatibility | Proved in both directions. The official SDK client drives both hand-written servers (stdio and Streamable HTTP, in both of its negotiation modes); the hand-written client drives official SDK servers, the reference Filesystem server (`npx`) and the reference Git server (`uvx`). The SDK is a **test-only** dependency, used as a conformance oracle. |
| Coverage of the required functionality | All of it: the host, both own servers, and the integrations with the two official reference servers, all run on the hand-written implementation. |
| Verify | `uv run pytest -q tests/test_mcp_wire.py` and `uv run adoptamatch-chatbot --offline --check`. |

### Extra B — A considered user interface (+15 %) · **done**

| | |
| --- | --- |
| Implementation | [`presentation.py`](../src/adoptamatch_chatbot/presentation.py) — the only module that writes to the console |
| Rationale | [`ui-design.md`](ui-design.md): visual hierarchy, semantic colour, the rule that colour is never the only signal, a fixed glyph vocabulary with an ASCII fallback, feedback and system status, recognition over recall, actionable error recovery, progressive disclosure via `/verbose`, and what was deliberately left out |
| Accessibility | `--no-color`, `NO_COLOR`, `--ascii`, width-adaptive layout, and every state carried by a glyph and a word as well as a colour |
| Verify | Run the same scenario with and without `--no-color`; nothing becomes ambiguous. |

---

## One-command verification

```bash
# public server: 79 tests
cd adoptamatch-mcp        && uv run pytest -q && uv run ruff check .

# host: 88 tests, including the no-MCP-SDK guarantee
cd ../adoptamatch-chatbot && uv run pytest -q && uv run ruff check .

# remote server: 18 tests, including a real HTTP round trip
cd remote_server          && uv run pytest -q && uv run ruff check .

# everything connected, end to end
cd .. && uv run adoptamatch-chatbot --offline --check
uv run python scripts/demo_filesystem_git.py
```
