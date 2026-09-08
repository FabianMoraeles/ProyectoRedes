# Presentation outline

Target: 10–12 minutes plus questions. Roughly 60 % live demo, 40 % slides. Visual
evidence beats prose on every slide.

## Slide 1 — Title (15 s)

AdoptForMe: a console MCP host. Your name, course, date. One line on what it is:
"one language model, six MCP servers, two transports, one audit log."

## Slide 2 — The problem (45 s)

A model asked to recommend a pet will invent animals, forget which are available,
and answer differently every time. Show a made-up answer next to a tool-grounded
one. MCP is how the model gets facts instead of guesses.

## Slide 3 — Architecture (1 min)

The component diagram. Say the three roles out loud — host, client, server — and
point at who plays each. Highlight the two transports.

**Do not read the diagram aloud.** Name the three parts and move on.

## Slide 4 — What was built (45 s)

| | |
| --- | --- |
| Own local server | `adoptforme`, 5 tools, public repository |
| Own remote server | `pet-care`, 2 tools, Streamable HTTP |
| Official servers | Filesystem, Git — both scoped |
| Classmates' servers | [fill in] |
| Tests | 79 + 88 + 18, no API credits consumed |
| Extra A | MCP over direct JSON-RPC, no SDK |
| Extra B | An interface designed against HCI principles |

## Slides 5–9 — LIVE DEMO (5–6 min)

Follow `config/demo-prompts.md` exactly. Keep it open on a second screen.

1. `/servers` and `/tools` — the servers, two transports, 33 tools.
2. A general question — zero tool calls. Proof of the LLM link.
3. A household description — explained recommendations with reasons *and*
   concerns, then a follow-up question with no antecedent, to show context.
4. Duplicate adoption — refused by the database, not by the prompt.
5. Filesystem then Git — one turn, two servers, and `/logs` attributing each step.
6. The remote server — then switch to Wireshark, already capturing.

Leave `/logs` on screen at the end of each scenario. The log is the evidence.

## Slide 10 — The explainable score (45 s)

The weights table and one real breakdown. The point: deterministic, reproducible,
defensible to an adopter — and hard rules are applied *before* scoring, so an
unsafe match is never merely low-scoring.


## Slide 10b — The protocol by hand, and the interface (1 min)

Two slides' worth of material compressed into one, because both are optional
extras and both are visible in the demo.

- **No MCP SDK.** One diagram: the four message kinds, and the five-step
  lifecycle. Then the honest proof: the official SDK client drives your servers,
  and your client drives the official servers plus the reference Filesystem and
  Git servers. Mention the test that fails if anyone ever imports the SDK.
- **The interface.** Show the same scenario twice, once normally and once with
  `--no-color`. Everything still readable: that is the accessibility rule, not a
  slogan. Mention `/verbose` as progressive disclosure.

## Slide 11 — Network analysis (1.5 min)

- The headline: stdio produces **no** network traffic; that is what the transport
  is. The remote server exists so there is something to capture.
- One screenshot of the TCP three-way handshake.
- One screenshot of `Follow HTTP Stream` with a JSON-RPC `tools/call` visible.
- The correlation table: log `request_id` ↔ frame number ↔ timing.
- If HTTPS: the encrypted records next to the host's plaintext log, and say
  clearly which is which.

## Slide 12 — Difficulties and solutions (1 min)

Pick three, with the fix in one line each. Suggested:

- Two reference servers hang on the SDK's modern negotiation probe → writing the
  client by hand removed the failure mode instead of working around it.
- Closing the HTTP transport cancelled the shutdown → work out whose cancel scope
  it is, then swallow it on the shutdown path only.
- Scoped servers express their scope differently → read the error, pass the path
  the server was launched with.

Say what you tried first. A debugging story convinces more than a clean result.

## Slide 13 — Lessons learned (45 s)

Three, honestly:

1. The host, not the model, is the security boundary — a stdio server is an
   arbitrary program running with your privileges.
2. A protocol is only as good as its failure handling; most of the host code is
   isolation, timeouts and logging.
3. Deterministic explanations beat a confident paragraph, in a domain where a bad
   match goes home with a family.

## Slide 14 — Closing (15 s)

Repository links, and an offer to show the code or the log.

---

## Preparation checklist

- [ ] Terminal font at 16 pt or larger; window at least 120 columns.
- [ ] `logs/` cleared before starting.
- [ ] Remote server already running; `curl /healthz` verified.
- [ ] Wireshark already capturing with the filter applied.
- [ ] `demo_workspace/demo-repo` reset to a known commit.
- [ ] `config/demo-prompts.md` open on a second screen.
- [ ] Screenshots of every scenario already taken, in case the live run fails.
- [ ] `uv run python scripts/demo_filesystem_git.py` verified as the contingency.
- [ ] Answers ready for: "why not the SDK's tool runner?", "what happens if a
      server crashes?", "why does stdio show nothing in Wireshark?", "how do you
      prevent a tool-name collision?", "how do you know your JSON-RPC is correct
      if you wrote it yourself?".
