# Demo script

Prompts to type during the live demonstration, in order, with what to point out
after each one. Times are rough; the whole run is about eight minutes.

Before starting:

```bash
rm -f logs/*.jsonl logs/*.log          # a clean log makes the demo legible
git -C demo_workspace/demo-repo log --oneline   # note the starting point
uv run adoptamatch-chatbot
```

Show the start-up table: six configured servers, four connected, the transport and
negotiated protocol version of each, and the tool count.

---

## 0 — Orientation (30 s)

```
/servers
/tools
/verbose
```

Point out: two transports side by side (`stdio` and `streamable-http`), the
negotiated protocol revision per server, and — in `/tools` — that every tool name
is unique here, so none needed qualifying. Mention that a collision would be
resolved as `<server>__<tool>`.

`/verbose` switches the interface from compact to full payloads; leave it on if
the audience wants to see the JSON, off if they want to follow the conversation.

---

## 1 — The LLM link, with no tools (30 s)

```
Explain in two sentences what the Model Context Protocol is and why a host needs it.
```

Point out: the model answered directly. `/logs` shows **zero** tool calls for this
turn — proof that the model is reached through its API and that the host does not
force a tool call on every turn.

---

## 2 — Own local server: explained matching (2 min)

```
I live in an apartment with my two kids and one cat. I can walk a dog about 45 minutes a day and I have never had a pet before. Which animals would suit us?
```

Point out:

- one `recommend_animals` call, routed to `adoptamatch`, timed in the console;
- the answer names real animals with real ids — all of it from the tool result;
- the *exclusions*: animals removed by a hard rule before scoring, with the reason;
- the *concerns*, not just the reasons.

Follow up, to show context retention:

```
Why is the first one better than the second?
```

Point out: the question names no animal. It resolves because the whole history —
including the previous tool result — is sent again.

Then:

```
Compare those two side by side.
```

```
Tell me everything about A012.
```

---

## 3 — Own local server: the irreversible tool (1 min)

```
I want to adopt A012. My name is Ana Perez and my email is ana.perez@example.com.
```

Point out: the model confirms the animal by name and id before calling
`register_adoption`, as the system prompt requires.

Then, deliberately:

```
Now register the same animal for Luis Gomez, luis@example.com.
```

Point out: the tool returns `isError: true` with "not available for adoption;
current status: adopted". The guard is in the database (a `UNIQUE` constraint plus
a status check inside one transaction), not in the prompt — and `/logs` shows the
error with the same `request_id` as its request.

---

## 4 — Official reference servers: Filesystem and Git (2 min)

```
Using the filesystem tools, create a directory demo-repo/notes and write a README.md inside it describing this demo. Then, with the git tools, check the status of demo_workspace/demo-repo, stage notes/README.md and commit it with the message "docs: add shelter notes via MCP". Finally show me the last two commits.
```

Point out:

- the console shows each call prefixed with the server that executed it;
- `/logs` attributes each step to `filesystem` or `git`, with durations;
- both servers are **scoped**: `list_allowed_directories` returns only
  `demo_workspace`, and the Git server rejects any repository outside
  `demo_workspace/demo-repo`.

Verify outside the chatbot:

```bash
git -C demo_workspace/demo-repo log --oneline -2
```

**Contingency.** If the API is unreachable, run the same scenario deterministically:

```bash
uv run python scripts/demo_filesystem_git.py
```

---

## 5 — Own remote server over Streamable HTTP (2 min)

Start the remote server in a second terminal first (or point the entry at the
deployed URL), and start the Wireshark capture with the filter from
`docs/wireshark-analysis.md`.

```
My new dog is an adult with medium energy and weighs 18 kg. What does a normal day of care look like, and roughly how much water should he drink?
```

Point out:

- both tool calls go to `pet_care_remote`, whose transport column says
  `streamable-http`;
- the answer carries the server's disclaimer — this is guidance, not veterinary
  advice;
- switch to Wireshark: the TCP handshake, the `POST /mcp` exchanges and their
  timing line up with the `request_id` entries in `/logs`.

---

## 6 — Closing (30 s)

```
/logs
/exit
```

Point out: the session summary, the log path, and that `/exit` closes every client,
stream and subprocess. Then show the log file itself:

```bash
wc -l logs/session-*.jsonl
grep '"method":"tools/call"' logs/session-*.jsonl | head
```

---

## If something goes wrong

| Problem | What to do on stage |
| --- | --- |
| A server shows `failed` | Say so, show `/servers` — it names the reason — and continue; the rest of the session works. That isolation is a feature, not an excuse. |
| The API is unreachable | Switch to `--offline` for the MCP half, and run `scripts/demo_filesystem_git.py` for scenario 4. |
| The remote server is down | `curl http://127.0.0.1:8080/healthz`, restart it, `/exit` and reconnect. |
| A tool call is slow | The console prints the elapsed time; use it to talk about the per-server timeout. |
