# Network analysis of the MCP traffic (Wireshark)

Requirement 8 of the assignment: capture every interaction between the host and
the remote server, and say which JSON-RPC messages are **synchronisation**, which
are **requests**, and which are **responses**. Requirement 10: explain what
happens at the link, network, transport and application layers.

This is a **fillable guide**, not a report. Everything under `── evidence ──` is a
placeholder for what *you* captured on *your* machine. Nothing here invents a
packet, an address or a timing.

---

## 0. What is actually on the wire, and what is not

Read this first; it determines what can be captured at all.

| Server | Transport | On the network? | What Wireshark can see |
| --- | --- | --- | --- |
| `adoptamatch` | stdio | **No** | Nothing. The host writes JSON-RPC to the subprocess's stdin and reads its stdout. Those are OS pipes: no socket, no IP, no port. |
| `filesystem` | stdio | **No** | Nothing, same reason. |
| `git` | stdio | **No** | Nothing, same reason. |
| `pet_care_remote` (local) | Streamable HTTP, `http://127.0.0.1:8080/mcp` | **Yes**, on loopback | Everything, in cleartext: TCP, HTTP and the JSON-RPC bodies. |
| `pet_care_remote` (deployed) | Streamable HTTP, `https://…/mcp` | **Yes**, on the real interface | TCP, DNS, the TLS handshake, and encrypted records. **Not** the JSON-RPC bodies, unless you decrypt with your own session keys (§ 8). |

**The honest headline for the report:** three of the four servers produce no
network traffic whatsoever. That is not a limitation of the capture; it is what
the stdio transport *is*. The remote server exists precisely so there is
something to capture.

### Where MCP sits in the stack

MCP is an **application-layer** protocol carrying JSON-RPC 2.0 messages. It does
not define its own transport. Over Streamable HTTP the stack is:

```
  JSON-RPC 2.0 (MCP)          ← application payload
  HTTP/1.1                    ← application protocol (POST /mcp, DELETE /mcp)
  TLS 1.3                     ← only when the URL is https://
  TCP                         ← reliable, ordered, connection-oriented
  IPv4 / IPv6                 ← network
  Ethernet / Wi-Fi / loopback ← link
```

So **TCP** is the transport-layer protocol underneath the remote server, and it
is the right choice: MCP needs every byte of a JSON-RPC message delivered, in
order, exactly once, and the request/response pattern gains nothing from
datagrams. Over stdio there is no transport-layer protocol at all, because there
is no network.

---

## 1. The classification the requirement asks for

JSON-RPC 2.0 defines four message shapes. Telling them apart needs only two
questions: *is there a `method`?* and *is there an `id`?*

| Kind | Shape | Example in this project | Requirement's wording |
| --- | --- | --- | --- |
| **Request** | `method` **and** `id` | `initialize`, `tools/list`, `tools/call`, `ping` | *solicitud / petición* |
| **Notification** | `method`, **no** `id` | `notifications/initialized` | *sincronización* (fire-and-forget) |
| **Response** | `id` + `result` | the reply to any of the above | *respuesta* |
| **Error** | `id` + `error` with a numeric `code` | e.g. `-32601 Method not found` | *respuesta* (a failed one) |

MCP then splits the requests into two groups, and this is the distinction the
report should lead with:

| Group | Methods | Role |
| --- | --- | --- |
| **Synchronisation (lifecycle)** | `initialize` (request + response), `notifications/initialized` (notification), `ping` | Establish the session: agree a protocol revision, exchange capabilities, identify both peers. Happens once, at the start. |
| **Ordinary calls** | `tools/list`, `tools/call` | Do the actual work. Happen many times. |

The host **tags every frame with exactly this classification** as it writes it,
in `logs/session-<uuid>.wire.jsonl`. That is only possible because the host
implements the protocol itself rather than delegating to an SDK: it is the code
writing and reading the bytes.

```bash
# every frame of the session, classified
python -c "import json,sys;[print(r['direction'],r['kind'],'SYNC' if r['lifecycle'] else 'call',r['method'],r['id'],r['server']) for r in map(json.loads,open(sys.argv[1],encoding='utf-8'))]" logs/session-<uuid>.wire.jsonl

# counts by kind, which is the table the report needs
python -c "
import json,sys,collections
rows=[json.loads(l) for l in open(sys.argv[1],encoding='utf-8')]
c=collections.Counter((r['direction'],r['kind'],'synchronisation' if r['lifecycle'] else 'call') for r in rows)
[print(k,v) for k,v in sorted(c.items())]" logs/session-<uuid>.wire.jsonl
```

A real run of the Filesystem + Git scenario across four servers produced:

```
('in',  'response',     'call')            12
('in',  'response',     'synchronisation')  4
('out', 'notification', 'synchronisation')  4
('out', 'request',      'call')            12
('out', 'request',      'synchronisation')  4
```

Four servers × (one `initialize` request + its response + one
`notifications/initialized`) = the twelve synchronisation frames; everything else
is `tools/list` and `tools/call` with their replies.

── evidence ──
- [ ] The same table, from **your** capture session.
- [ ] One captured example of each of the four kinds, quoted from the packet bytes.

---

## 2. Prepare a clean session

A capture is only useful if it can be correlated with a log, so start both
together.

```bash
cd adoptamatch-chatbot

# 1. archive the previous session's logs
mkdir -p logs/archive && mv logs/session-* logs/archive/ 2>/dev/null

# 2. start the remote server in its own terminal
cd remote_server && HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp

# 3. confirm it answers before capturing anything
curl http://127.0.0.1:8080/healthz
```

For the cleanest capture, temporarily enable **only** the remote server in
`config/servers.toml`, so every line in the log corresponds to something on the
wire:

```toml
[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
enabled = true
url = "http://127.0.0.1:8080/mcp"
```

── evidence ──
- [ ] `logs/` contains exactly one session before you start typing.
- [ ] Screenshot of the start-up table showing `pet_care_remote` as connected.

---

## 3. Choose the interface

| Where the server runs | Interface to capture on |
| --- | --- |
| Same machine (`127.0.0.1`) | **Windows:** `Adapter for loopback traffic capture` (install Npcap with "Support loopback traffic capture" ticked). **Linux:** `lo`. **macOS:** `lo0`. |
| A deployed URL | Whichever interface carries your default route: `Wi-Fi` or `Ethernet`. |

```bash
# Windows (PowerShell)
Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select-Object InterfaceAlias, NextHop

# Linux / macOS
ip route get 1.1.1.1        # or: route -n get default
```

Wireshark shows a live sparkline next to each interface: pick the one that moves
when you run `curl http://127.0.0.1:8080/healthz`.

── evidence ──
```
Interface used:            ________
Why it is the right one:   ________
```

---

## 4. Filters

A **capture filter** (BPF, set before starting) limits what is recorded; a
**display filter** limits what is shown afterwards. Use a narrow capture filter to
keep the file small, then narrow further while reading.

### Local server, plaintext HTTP

| Purpose | Filter | Kind |
| --- | --- | --- |
| Record only this conversation | `tcp port 8080 and host 127.0.0.1` | capture |
| Show only it | `tcp.port == 8080` | display |
| Only the MCP posts | `http.request.method == "POST" && http.request.uri contains "/mcp"` | display |
| Only the replies | `http.response` | display |
| **The synchronisation messages** | `frame contains "initialize"` | display |
| **The initialized notification** | `frame contains "notifications/initialized"` | display |
| **The ordinary calls** | `frame contains "tools/call"` | display |
| The session termination | `http.request.method == "DELETE"` | display |
| Handshake and teardown | `tcp.flags.syn == 1 \|\| tcp.flags.fin == 1 \|\| tcp.flags.reset == 1` | display |
| Anything retransmitted | `tcp.analysis.retransmission` | display |

### Deployed server, HTTPS

| Purpose | Filter | Kind |
| --- | --- | --- |
| Record only that host | `host <your-service-host> and tcp port 443` | capture |
| The name lookup | `dns.qry.name contains "<your-service-host>"` | display |
| TLS handshake | `tls.handshake.type == 1` (ClientHello) / `== 2` (ServerHello) | display |
| The name you asked for | `tls.handshake.extensions_server_name` | display |
| Encrypted application data | `tls.record.content_type == 23` | display |

`Follow ▸ HTTP Stream` on any packet shows the whole exchange in order. It is the
single most useful view for the report.

── evidence ──
```
Capture filter used:  ________
Display filters, one per figure: ________
```

---

## 5. Provoke each message type

Run these one at a time and note the wall-clock time of each.

| Action | JSON-RPC on the wire | Class |
| --- | --- | --- |
| Start the chatbot | `initialize` request → response | synchronisation |
| … immediately after | `notifications/initialized` (no reply, HTTP `202 Accepted`) | synchronisation |
| … then | `tools/list` request → response | call |
| Ask *"What does a day of care look like for an adult, medium-energy dog?"* | `tools/call` (`get_daily_care_checklist`) → response | call |
| Ask *"How much water should an 18 kg dog drink?"* | `tools/call` (`estimate_daily_water_ml`) → response | call |
| Ask for a nonsense tool, or edit one call to a bad method | `error` reply, e.g. `-32601` | response (failed) |
| `/exit` | HTTP `DELETE /mcp`, then the TCP teardown | session end |

The `202 Accepted` with an empty body is worth a sentence in the report: it is the
HTTP-level expression of a JSON-RPC notification. There is no `id`, so there is
nothing to answer, and the transport says "accepted, no content" instead of
inventing a response.

── evidence ──
```
initialize                 frames ____ (request) / ____ (response)
notifications/initialized  frame  ____   HTTP status ____
tools/list                 frames ____ / ____
tools/call  #1             frames ____ / ____
tools/call  #2             frames ____ / ____
error reply (if captured)  frame  ____   code ____
DELETE /mcp                frame  ____
```

---

## 6. Correlate the capture with the logs

This is what makes the analysis *yours* rather than a generic packet dump. Three
identifiers line up:

| Identifier | Where it comes from | Where to find it |
| --- | --- | --- |
| JSON-RPC `id` | Assigned by the host's own client, monotonically from 1 | On the wire, and in the `id` field of the wire log |
| `request_id` | Assigned by the host for its own audit trail | The host-level log only |
| Frame number | Wireshark | The capture |

Because the host allocates JSON-RPC ids itself, the *n*-th request it sent to a
server carries id *n* — so a capture reads in order with no guesswork.

```bash
# the wire log: every frame, classified, with its id
python -c "import json,sys;[print(r['timestamp'][11:23],r['direction'],r['kind'],r['method'],r['id']) for r in map(json.loads,open(sys.argv[1],encoding='utf-8')) if r['server']=='pet_care_remote']" logs/session-<uuid>.wire.jsonl

# the host log: durations and request ids
python -c "import json,sys;[print(r['timestamp'][11:23],r['direction'],r['method'],r.get('tool',''),r['elapsed_ms'],r['request_id']) for r in map(json.loads,open(sys.argv[1],encoding='utf-8'))]" logs/session-<uuid>.jsonl
```

Fill in one row per message:

| JSON-RPC `id` | method | class | log time (UTC) | `elapsed_ms` | request frame | response frame | bytes out | bytes in | Δt in Wireshark |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `initialize` | synchronisation | | | | | | | |
| — | `notifications/initialized` | synchronisation | | | | | | | |
| 2 | `tools/list` | call | | | | | | | |
| 3 | `tools/call` | call | | | | | | | |

Notes for reading the columns:

- The log timestamps are UTC; Wireshark shows local time by default. Switch it
  with `View ▸ Time Display Format ▸ UTC Date and Time of Day` so the two match
  without arithmetic.
- For Δt use `View ▸ Time Display Format ▸ Seconds Since Previous Displayed
  Packet`, or right-click the request and `Set/Unset Time Reference`.
- `elapsed_ms` is always slightly larger than the packet-level round trip: it
  measures from just before the frame is written to just after the reply is
  parsed and validated. Say what the difference consists of.

── evidence ──
- [ ] The completed table, with at least three `tools/call` rows.
- [ ] One screenshot of `Follow ▸ HTTP Stream` next to the matching wire-log lines.

---

## 7. Layer-by-layer analysis

Fill each section from **your** capture. Expand the packet detail pane and read
the values off it.

### 7.1 Link layer

- The frame type: Ethernet II, Wi-Fi (802.11), or the loopback pseudo-header
  (`Null/Loopback` on Windows and macOS, `Linux cooked capture` on Linux).
- On a real interface: source and destination MAC addresses, and which is your
  machine and which is the default gateway. Note that the destination MAC is the
  **gateway's**, not the remote server's — beyond the local segment, delivery is
  by IP, not by MAC.
- On loopback: there are no real MAC addresses; the frame carries only a family
  identifier. Say so rather than inventing one.
- Frame length, and the interface MTU.

── evidence ──
```
Frame type:            ________
Source MAC:            ________   (this machine / gateway?)
Destination MAC:       ________
Frame length:          ____ bytes
Interface MTU:         ____ bytes
```

### 7.2 Network layer

- IPv4 or IPv6? Cloud services often resolve to both; note which one the client
  actually used.
- Source and destination addresses.
- TTL / Hop Limit on packets you send versus packets you receive — the difference
  is a rough hop count.
- Any fragmentation (`ip.flags.mf == 1`)? Usually none: TCP sizes its segments to
  the MSS precisely to avoid IP fragmentation.
- For a deployed server: the DNS exchange that produced the address, and its TTL.

── evidence ──
```
IP version:            ____
Source IP:             ________
Destination IP:        ________
TTL sent / received:   ____ / ____
Fragmented packets:    ____
DNS answer(s):         ________
```

### 7.3 Transport layer (TCP)

The layer the assignment is really about, so be thorough.

- **Ports.** An ephemeral source port chosen by the OS; destination `8080`
  locally or `443` deployed.
- **Three-way handshake.** Frame numbers of `SYN`, `SYN, ACK`, `ACK`; the
  advertised window sizes; the options in the `SYN` (MSS, window scale, SACK
  permitted, timestamps).
- **Data transfer.** Which segments carry the `POST /mcp` body and which carry
  the response. The `PSH` flag marks the end of an application message.
- **Acknowledgements.** Whether the ACK is piggybacked on a data segment or sent
  alone, and how `Seq`/`Ack` advance by exactly the payload length.
- **Connection reuse.** Streamable HTTP keeps one connection alive across several
  MCP messages. Confirm it: how many TCP connections were opened for how many
  JSON-RPC frames? (`Statistics ▸ Conversations ▸ TCP`.) This is a genuinely
  interesting number, because it is the difference between HTTP/1.1 keep-alive
  and a connection per call.
- **Teardown.** `FIN, ACK` / `ACK` in both directions, or an `RST`. Which side
  closed first, and does it line up with the `DELETE /mcp` the host sends at exit?
- **Anomalies.** `tcp.analysis.retransmission`, `duplicate_ack`, `zero_window`. On
  loopback there will usually be none: say so explicitly and explain why (no
  physical medium, no loss, no congestion) rather than leaving the section empty.

── evidence ──
```
Source / destination port:        ______ / ______
SYN / SYN-ACK / ACK frames:       ____ / ____ / ____
MSS advertised:                   ____ bytes
Window scale factor:              ____
TCP connections opened:           ____
JSON-RPC frames carried on them:  ____
Teardown: FIN or RST, opened by:  ________
Retransmissions / dup ACKs / zero windows: ____ / ____ / ____
```

### 7.4 Application layer

**Over plain HTTP — the bodies are readable.**

- The request line: `POST /mcp HTTP/1.1`.
- Request headers worth naming: `Content-Type: application/json`;
  `Accept: application/json, text/event-stream` (Streamable HTTP allows either
  form of reply); `Mcp-Session-Id` on every message after the handshake;
  `MCP-Protocol-Version` carrying the revision agreed in `initialize`.
- The response status and `Content-Type`. `200` with a JSON body for a request;
  `202` with no body for a notification; `204` for the `DELETE`.
- The JSON-RPC envelope itself, classified with the table in § 1.

**Over HTTPS — the bodies are not readable.**

State this plainly: TLS encrypts the whole HTTP layer, so Wireshark shows
`Application Data` records of a given length and nothing more. What *is* visible:

- the TCP connection and its timing;
- the TLS handshake: `ClientHello` with the SNI extension naming the host and the
  offered cipher suites, `ServerHello` with the chosen suite; on TLS 1.2 the
  certificate, on TLS 1.3 the certificate is itself encrypted;
- the **size, direction and timing** of each encrypted record — enough to infer
  *that* a request happened and roughly how large the answer was.

The plaintext of those same messages is in `logs/session-<uuid>.wire.jsonl`,
recorded by the host **before** encryption. Correlating the two is the honest way
to say what was inside an encrypted record.

── evidence ──
```
Request line:               ________
Mcp-Session-Id present?:    ________
MCP-Protocol-Version:       ________
Response status / type:     ________
JSON-RPC methods captured:  ________
TLS version negotiated:     ________
SNI in ClientHello:         ________
Cipher suite chosen:        ________
```

---

## 8. Optional: decrypting your own TLS session

Only against **your own** server, on **your own** machine. It is a legitimate,
reproducible technique: the client writes the session keys it just negotiated to a
file, and Wireshark reads that file.

CPython's `ssl` module honours the `SSLKEYLOGFILE` environment variable, and the
HTTP client this host is built on uses that module.

```bash
export SSLKEYLOGFILE="$PWD/logs/tls-keys.log"     # PowerShell: $env:SSLKEYLOGFILE="$PWD\logs\tls-keys.log"
uv run adoptamatch-chatbot                        # against the https:// URL, capturing at the same time

# Wireshark ▸ Edit ▸ Preferences ▸ Protocols ▸ TLS
#   "(Pre)-Master-Secret log filename" → logs/tls-keys.log
```

Wireshark then dissects the decrypted HTTP and the JSON-RPC inside it, and a
`Decrypted TLS` tab appears in the packet detail pane.

**`logs/tls-keys.log` is a secret.** It can decrypt that capture forever. It is
git-ignored with the rest of `logs/`; delete it once the report is written, and
never attach it to a submission.

If you do not decrypt, do not pretend you did: show the encrypted records beside
the wire log and say which is which.

── evidence ──
- [ ] Decryption attempted? yes / no
- [ ] If yes: screenshot of a decrypted `tools/call` frame.
- [ ] If no: screenshot of the `Application Data` records plus the matching wire-log lines.

---

## 9. Saving the evidence

```
Wireshark ▸ File ▸ Save As ▸ mcp-capture.pcapng
Trim it first if large: File ▸ Export Specified Packets ▸ Displayed
```

Keep together, in one folder:

- `mcp-capture.pcapng`, filtered down to the MCP conversation;
- `logs/session-<uuid>.jsonl` and `logs/session-<uuid>.wire.jsonl` from the same
  session;
- the screenshots referenced by each `── evidence ──` block;
- a one-line note of the interface, the filter and the date.

`*.pcapng` is git-ignored: attach it to the report rather than committing it.

---

## 10. What to conclude (write this yourself, from your numbers)

Prompts, not answers:

1. How many TCP connections carried how many JSON-RPC frames, and what does that
   say about Streamable HTTP versus opening a connection per call?
2. How does `elapsed_ms` in the host log compare with the packet-level round trip,
   and what accounts for the difference?
3. What could an observer on the network learn about your session if it were
   HTTPS — and what could they *not* learn?
4. Why do three of your four servers produce no packets at all, and what does
   that imply about where the security boundary of an MCP host really sits?
5. Was anything retransmitted or reordered? If not, why not — and what would you
   expect over a lossy link instead of loopback?
6. The synchronisation messages happen once and the calls happen many times. What
   would change if the host reconnected for every tool call instead of keeping the
   session?
