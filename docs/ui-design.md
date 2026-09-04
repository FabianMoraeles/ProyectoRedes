# Terminal interface: design rationale

The chatbot lives in a terminal, so its interface is text. That is a constraint,
not an excuse: everything a graphical interface has to get right — hierarchy,
feedback, error recovery, accessibility — applies here too, and a terminal makes
the mistakes more visible because there is nowhere to hide a bad layout.

This document explains *why* the interface looks the way it does. The
implementation is [`src/adoptamatch_chatbot/presentation.py`](../src/adoptamatch_chatbot/presentation.py),
and it is the only module allowed to write to the console.

---

## 1. The problem this interface has to solve

A single turn produces four very different kinds of output, interleaved:

| What | Who produces it | How much attention it deserves |
| --- | --- | --- |
| The line you typed | you | reference only, you just wrote it |
| The model's narration ("let me look that up") | the LLM | low — it is a courtesy, not content |
| Tool activity: name, arguments, result, timing | the host | low while it works, **high when it fails** |
| The final answer | the LLM, grounded in tool results | **this is what you are here for** |

Put them all at the same weight and the transcript becomes a wall of text where
the answer is indistinguishable from the plumbing. Every decision below follows
from ranking them.

## 2. Visual hierarchy

**The answer wins.** It is rendered as Markdown, at the plain foreground colour,
with blank lines around it and a two-space indent that lines it up with nothing
else on screen. It is the only thing that gets that treatment.

**Machinery recedes.** Tool calls and results are indented further (2 and 4
spaces), rendered in `dim`, and abbreviated. They form a visually quieter block
between the question and the answer — present, skimmable, not competing.

**Errors are allowed to be loud.** They are the one case where interrupting the
reader is correct, so they get bold red plus the word `error`.

**Tables carry structure, not decoration.** `/servers`, `/tools` and `/logs` use
minimal box drawing (`SIMPLE`), a left-aligned title, and bold headers only. Heavy
borders around every cell would add ink without adding information.

## 3. Colour: psychology, and never on its own

Colour is used **semantically**, never decoratively. Six roles, six meanings:

| Role | Colour | Why this hue |
| --- | --- | --- |
| Identity / brand / commands | cyan | Cool, technical, calm. High contrast on both light and dark terminals, and it is not one of the alarm colours, so it can be used constantly without fatigue. |
| Tool activity | magenta | Reads as "machine", and is far from both green and red — critical for colour-blind readers, since the success/failure pair is the one most often confused. |
| Success | green + `✓` + the word `ok` | Conventional. |
| Failure | bold red + `✗` + the word `failed` | Conventional, and the only bold-red in the interface, so it cannot be confused with anything else. |
| Warning | yellow + `!` + the word `warning` | Between neutral and alarming, which is exactly what a warning is. |
| Metadata (timings, ids, paths) | `dim` | Present for when you need it, invisible when you do not. |

### The rule that makes it accessible

> **Colour is never the only carrier of meaning.**

Every state also has a **glyph** and a **word**. `✓ ok` and `✗ failed` are
distinguishable with no colour at all — which matters for:

- readers with deuteranopia or protanopia (the red/green pair);
- monochrome terminals and low-colour SSH sessions;
- output piped into a file, which is exactly what happens when a transcript is
  pasted into a report;
- `--no-color`, and the `NO_COLOR` environment variable
  ([no-color.org](https://no-color.org/)), both of which are honoured.

## 4. A fixed glyph vocabulary

Each line kind is introduced by the same symbol, in the same column, every time.
That is what lets you skim a long transcript without reading it.

| Glyph | ASCII | Meaning |
| --- | --- | --- |
| `›` | `>` | the prompt: your turn to type |
| `▸` | `\|>` | a tool call about to run |
| `✓` | `+` | the tool succeeded |
| `✗` | `x` | the tool failed |
| `!` | `!` | warning |
| `·` | `-` | neutral information |
| `•` | `*` | a bullet in the banner |

The ASCII column is not a fallback of last resort: the encoding of the output
stream is probed at start-up, and `--ascii` forces it. A Windows console in a
legacy code page therefore gets a clean interface rather than mojibake — the
failure mode that makes a demo look broken when it is not.

## 5. Feedback: never leave the user guessing

Nielsen's first heuristic is visibility of system status. Four places apply it:

1. **Start-up** prints a panel naming the model, the provider, the session id and
   the log path, then the server table. Before you type anything, you know what
   is connected and what is not.
2. **A spinner says what it is doing** — `asking the model...`, `starting MCP
   servers...`, `closing MCP servers...` — not a generic "loading". A long pause
   is only alarming when you cannot tell what is causing it.
3. **A tool call is announced before it runs**, with the server and the tool name.
   A slow call is then explainable rather than mysterious.
4. **Every result carries its duration and its correlation id** (`ok  82 ms  req
   c6783cd2`). That id is the same one in the JSONL log, so the screen and the
   evidence file can always be lined up.

A quiet footer after each turn reports elapsed time and tool-call count — visible
if you look for it, invisible otherwise. It is suppressed for fast, tool-free
turns, because a footer on every trivial answer is noise.

## 6. Recognition over recall

- The banner lists the three commands you need first, rather than expecting you
  to remember them or type `/help` to find out.
- `/help` shows every command with a one-line description.
- `/tools` shows the name the *model* sees, the server that owns it, and — when a
  name collided — the original name too, so a qualified `git__status` is
  self-explaining rather than cryptic.
- The server table names the transport and the negotiated protocol revision.
  Nothing about the connection is hidden behind a debug flag.

## 7. Error recovery

Every user-facing error says what is wrong **and** what to do:

```
error ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key,
      or export it in the shell. Run with --offline to try the chatbot without
      an API key.
```

Design commitments behind that:

- Configuration is validated **before** anything connects, so a typo fails in the
  first second rather than halfway through a demo, and exits with status 2 and no
  traceback.
- A failed server is reported in the table with its reason and the session
  continues — one broken server never costs you the other five.
- A tool failure comes back as a result the model can read and react to, so the
  conversation recovers instead of ending.
- `/clear` asks for confirmation, because losing context by mistyping is
  irreversible within a session.

## 8. Progressive disclosure

Compact by default: arguments are capped at 110 characters and results at 160, on
one line. That keeps a multi-tool turn readable on one screen.

`/verbose` (or `--verbose`) switches to full arguments and full results, for
debugging or for a demo where the payload *is* the point. Same session, no
restart. This is the "beginner sees a clean surface, expert reaches the detail"
pattern, and it avoids the two usual failures: hiding information behind a flag
nobody finds, or drowning the default view to satisfy the rare case.

## 9. Consistency and layout discipline

- One indent scale: `0` for status lines, `2` for the answer and tool headlines,
  `4` for payloads. Nothing is aligned by eye.
- One capitalisation convention: sentence case everywhere, including table
  headers.
- One vocabulary: a *tool* is always a tool, a *server* always a server, a
  *request id* always a request id — on screen, in the logs and in the docs.
- Layout adapts to the terminal width; nothing is hard-wrapped at 80 columns.

## 10. What was deliberately left out

- **Streaming the model's answer token by token.** It looks impressive and it
  fights with the tool-activity block for the same screen region. Since a turn
  can interleave several tool rounds, a stable "narration → tools → answer" order
  is easier to follow than text that arrives in fragments around it.
- **A full-screen TUI** (panes, a scrollback widget, mouse support). It would
  break the one property that makes this program good evidence: the transcript is
  plain text you can copy, pipe, and paste into a report.
- **Colour themes.** One well-chosen semantic palette that works on light and dark
  backgrounds beats several that need configuring.
- **Emoji as status markers.** Width is inconsistent across terminals, they break
  column alignment, and screen readers announce them unhelpfully.

## 11. Try it

```bash
uv run adoptamatch-chatbot --offline          # the default, compact
uv run adoptamatch-chatbot --offline --verbose  # full arguments and results
uv run adoptamatch-chatbot --offline --no-color # semantics survive without colour
uv run adoptamatch-chatbot --offline --ascii    # ASCII glyph set
NO_COLOR=1 uv run adoptamatch-chatbot --offline # the environment convention
```

Running the same scenario under `--no-color` and confirming it is still fully
readable is the quickest way to check that the accessibility rule in §3 still
holds after a change.
