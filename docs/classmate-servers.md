# Integrating a classmate's MCP server

Two entries are reserved in `config/servers.example.toml`, `classmate_server_1`
and `classmate_server_2`, both shipping `enabled = false`. A test asserts they stay
disabled until they are real. The example file stays generic on purpose — each
student's real, filled-in entries live only in their own git-ignored
`config/servers.toml`.

**Status: 1 of 2 integrated.** `classmate_server_1` is Camila Ramirez's
`academic-planner-mcp`, connected and demonstrated — see
[`server-specifications.md` §5](server-specifications.md#5-classmate_server_1-and-classmate_server_2)
for the tools, the risk review and the scenario run. `classmate_server_2` is still
open. **The requirement is not met until both are connected and demonstrated** —
do not mark it done before then.

## Before you run anything

A stdio MCP server is an arbitrary program that the host launches **as a
subprocess, with your user's privileges, on your machine**. A Streamable HTTP
server is safer to try but still receives whatever the model sends it.

Read the code first. Specifically:

- [ ] What does it read or write on disk? Is it scoped to a directory?
- [ ] Does it make network requests? To where?
- [ ] Does it run shell commands or `eval` anything?
- [ ] Does it need credentials? If so, which, and are they read from the
      environment (acceptable) or hard-coded (a problem to report to the author)?
- [ ] Does it write outside its own directory?
- [ ] What are its dependencies, and are they pinned?

Write the answers into `docs/server-specifications.md` § 5. "It looked fine" is not
an answer.

## Step by step

1. **Clone it somewhere outside this repository**, so its files never end up in
   this history.

   ```bash
   cd ..
   git clone <their-repo> classmate-one
   cd classmate-one && uv sync     # or npm install, per their README
   ```

2. **Test it in isolation, before the chatbot ever sees it.**

   ```bash
   npx @modelcontextprotocol/inspector uv run <their-entry-point>
   ```

   In Inspector: connect, open Tools, call one tool with realistic arguments.
   Record the tool names and their schemas.

3. **Add the entry, still disabled**, in `config/servers.toml`:

   ```toml
   [[servers]]
   name = "classmate_server_1"
   transport = "stdio"
   enabled = false
   description = "<author> — <what it does>"
   command = "uv"
   args = ["run", "<their-entry-point>"]
   cwd = "../../classmate-one"
   timeout_seconds = 60
   connect_timeout_seconds = 60
   ```

4. **Verify it is listed but not connected:**

   ```bash
   uv run adoptamatch-chatbot --offline --check
   ```

5. **Enable it and connect:**

   ```bash
   # flip enabled = true, then
   uv run adoptamatch-chatbot --offline --check
   ```

   Confirm `connected`, the tool count, and the negotiated protocol version.

6. **Check for name collisions** in `/tools`. If one of their tools has the same
   name as one of ours, both appear qualified as `<server>__<tool>`; note that in
   the report, because it is exactly the case the host was built to handle.

7. **Design a scenario.** At minimum one per server. Better: one scenario that uses
   both a classmate's server and ours in the same turn, since that is what a host
   is for. Add it to `config/demo-prompts.md`.

8. **Record the evidence.** Run the scenario, keep the `logs/session-*.jsonl` file,
   and note the date in `docs/server-specifications.md`.

## If it does not work

The host isolates the failure: the entry shows as `failed` with the reason and the
rest of the session is unaffected. Diagnose in this order:

| Symptom | Where to look |
| --- | --- |
| `timed out … in mode 'auto'` then also in `legacy` | The server never completed a handshake. Check its stderr file: `logs/session-<id>.<name>.stderr.log`. |
| Process exits immediately | Missing dependency or a wrong entry point. Run their command by hand in their directory. |
| Connects but exposes zero tools | Their server registered no tools, or registration failed at import time — again, the stderr file. |
| A tool call always errors | Compare the arguments you send with the schema in `/tools`; scoped servers often want paths relative to their own launch directory. |

Report real problems back to the author. Do not patch their code inside this
repository.
