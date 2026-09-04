# Network analysis of the MCP traffic (Wireshark)

This is a **fillable guide**, not a report. Everything below the line marked
`── evidence ──` in each section is a placeholder for what *you* captured on
*your* machine. Nothing here invents a packet, an address or a timing.

---

## 0. What is actually on the wire, and what is not

Read this first; it determines what can be captured at all.

| Server | Transport | On the network? | What Wireshark can see |
| --- | --- | --- | --- |
| `adoptamatch` | stdio | **No** | Nothing. The host writes JSON-RPC to the subprocess's stdin and reads its stdout. Those are OS pipes; no socket, no IP, no port. |
| `filesystem` | stdio | **No** | Nothing, same reason. |
| `git` | stdio | **No** | Nothing, same reason. |
| `pet_care_remote` (local) | Streamable HTTP over `http://127.0.0.1:8080/mcp` | **Yes**, on loopback | Everything, in cleartext: TCP, HTTP and the JSON-RPC bodies. |
| `pet_care_remote` (deployed) | Streamable HTTP over `https://…/mcp` | **Yes**, on the real interface | TCP, DNS, the TLS handshake and encrypted records. **Not** the JSON-RPC bodies, unless you decrypt with your own session keys (§7). |

**The honest headline for the report:** three of the four servers produce no
network traffic whatsoever. That is not a limitation of the capture, it is what
the stdio transport *is*. The remote server exists precisely so there is something
to capture.

### Which protocol carries MCP?

MCP is an **application-layer** protocol: JSON-RPC 2.0 messages. It does not define
its own transport. Over Streamable HTTP the stack is:

```
  JSON-RPC 2.0 (MCP)          ← application payload
  HTTP/1.1                    ← application protocol (POST /mcp, GET /mcp)
  TLS 1.3                     ← only when the URL is https://
  TCP                         ← reliable, ordered, connection-oriented
  IPv4 / IPv6                 ← network
  Ethernet / Wi-Fi / loopback ← link
```

So yes — **TCP** is the transport-layer protocol underneath the remote server, and
it is the right choice here: MCP needs every byte of a JSON-RPC message delivered,
in order, exactly once. UDP would give none of that, and the request/response
pattern gains nothing from datagrams. Over stdio there is no transport-layer
protocol at all, because there is no network involved.

---

## 1. Prepare a clean session

A capture is only useful if it can be correlated with a log, so start both at the
same time.

```bash
cd adoptamatch-chatbot

# 1. archive or delete the previous session's logs
mkdir -p logs/archive && mv logs/session-* logs/archive/ 2>/dev/null

# 2. start the remote server in its own terminal
cd remote_server && HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp

# 3. confirm it answers before you capture anything
curl http://127.0.0.1:8080/healthz
```

Enable only the remote server for the capture, so the log is short and every line
in it corresponds to something on the wire:

```toml
# config/servers.toml — temporarily
[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
enabled = true
url = "http://127.0.0.1:8080/mcp"
mode = "legacy"     # forces the classic `initialize` handshake, easier to point at
```

> `mode = "legacy"` matters for the capture. With `mode = "auto"` the first request
> on the wire is `server/discover`, not `initialize`. Both are valid MCP; the report
> should describe whichever one you actually captured.

── evidence ──
- [ ] `logs/` contains exactly one session before you start typing.
- [ ] Screenshot of the start-up table showing `pet_care_remote` as `connected`.

---

## 2. Choose the interface

| Where the server runs | Interface to capture on |
| --- | --- |
| Same machine (`127.0.0.1`) | **Windows:** `Adapter for loopback traffic capture` (installed with Npcap — tick "Support loopback traffic capture" during installation). **Linux:** `lo`. **macOS:** `lo0`. |
| A deployed URL | The interface that actually carries your default route: `Wi-Fi` or `Ethernet`. |

Find the right one before capturing:

```bash
# Windows (PowerShell)
Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select-Object InterfaceAlias, NextHop
ipconfig /all

# Linux / macOS
ip route get 1.1.1.1        # or: route -n get default
ip -brief address
```

Wireshark also lists a live sparkline next to each interface: pick the one that
moves when you run `curl http://127.0.0.1:8080/healthz`.

── evidence ──
- [ ] Name of the interface used: `________`
- [ ] Why it is the right one (route or sparkline): `________`

---

## 3. Capture filters and display filters

A **capture filter** (BPF, set before starting) reduces what is recorded. A
**display filter** (set afterwards) reduces what is shown. Use a narrow capture
filter so the file stays small, then narrow further while reading.

### Local server, plaintext HTTP

| Purpose | Filter | Kind |
| --- | --- | --- |
| Record only this server's traffic | `tcp port 8080 and host 127.0.0.1` | capture |
| Show only that conversation | `tcp.port == 8080` | display |
| Only the MCP POSTs | `http.request.method == "POST" && http.request.uri contains "/mcp"` | display |
| Only the responses | `http.response` | display |
| Find one JSON-RPC method by name | `frame contains "tools/call"` | display |
| The TCP handshake and teardown | `tcp.flags.syn == 1 \|\| tcp.flags.fin == 1 \|\| tcp.flags.reset == 1` | display |
| Anything retransmitted | `tcp.analysis.retransmission` | display |

### Deployed server, HTTPS

| Purpose | Filter | Kind |
| --- | --- | --- |
| Record only that host | `host <your-service-host> and tcp port 443` | capture |
| The name lookup | `dns.qry.name contains "<your-service-host>"` | display |
| The TLS handshake | `tls.handshake.type == 1` (ClientHello) / `== 2` (ServerHello) | display |
| The server name you asked for | `tls.handshake.extensions_server_name` | display |
| Encrypted application data | `tls.record.content_type == 23` | display |

`Follow ▸ TCP Stream` (or `HTTP Stream`) on any packet in the conversation shows
the whole exchange in order — this is the single most useful view for the report.

── evidence ──
- [ ] Capture filter used: `________`
- [ ] Display filters used, one per figure: `________`

---

## 4. Provoke the three message types

Each of these produces a specific JSON-RPC method. Run them one at a time, and note
the wall-clock time of each so you can line it up with the log.

| Action in the chatbot | JSON-RPC method(s) on the wire |
| --- | --- |
| Start the chatbot (connection) | `initialize`, then `notifications/initialized` — or `server/discover` if `mode = "auto"` |
| Start the chatbot (discovery) | `tools/list` |
| Ask *"What does a day of care look like for an adult, medium-energy dog?"* | `tools/call` with `name: "get_daily_care_checklist"` |
| Ask *"How much water should an 18 kg dog drink?"* | `tools/call` with `name: "estimate_daily_water_ml"` |
| `/exit` | HTTP `DELETE /mcp` (session termination), then the TCP teardown |

── evidence ──
- [ ] Frame number of `initialize` (or `server/discover`): `____`
- [ ] Frame number of `tools/list`: `____`
- [ ] Frame numbers of each `tools/call`: `____`

---

## 5. Correlate the capture with the host log

This is the part that makes the analysis *yours* rather than a generic packet
dump. The host log is the plaintext side of the same exchange.

```bash
# every event of the session, in order, with its correlation id
python -c "import json,sys;[print(r['timestamp'],r['direction'],r['method'],r.get('tool',''),r['elapsed_ms'],r['request_id']) for r in map(json.loads,open(sys.argv[1],encoding='utf-8'))]" logs/session-<uuid>.jsonl
```

Then, for each `tools/call`, fill in a row:

| `request_id` | tool | log timestamp (UTC) | `elapsed_ms` | request frame # | response frame # | bytes out | bytes in | Δt from Wireshark |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |

How to read each column:

- **log timestamp** is UTC; Wireshark shows local time by default. Switch it with
  `View ▸ Time Display Format ▸ UTC Date and Time of Day` so the two match without
  arithmetic.
- **Δt from Wireshark**: set `View ▸ Time Display Format ▸ Seconds Since Previous
  Displayed Packet`, or right-click the request and `Set/Unset Time Reference`.
- **`elapsed_ms` vs Δt**: the log measures from just before the SDK call to just
  after the result is parsed, so it is always slightly larger than the pure network
  round trip. Note the difference and say what it consists of (serialisation,
  validation, the server's own work).

── evidence ──
- [ ] The completed table above, with at least three `tools/call` rows.
- [ ] One screenshot of `Follow ▸ HTTP Stream` showing a matching `request_id`
      request and its response.

---

## 6. Layer-by-layer analysis

Fill each section from **your** capture. Expand the packet detail pane and read
the values off it; do not copy numbers from anywhere else.

### 6.1 Link layer

- Interface used and its frame type: Ethernet II, Wi-Fi (802.11), or the loopback
  pseudo-header (`Null/Loopback` on Windows and macOS, `Linux cooked capture` on
  Linux).
- On a real interface: source and destination MAC addresses, and which is your
  machine and which is the default gateway. Note that the destination MAC is the
  **gateway's**, not the remote server's — routing beyond the local segment is by
  IP, not MAC.
- On loopback: there are no real MAC addresses; the frame carries only a family
  identifier. Say so rather than inventing one.
- Frame size in bytes, and the MTU of the interface.

── evidence ──
```
Frame type:            ________
Source MAC:            ________     (this machine / gateway?)
Destination MAC:       ________
Frame length:          ____ bytes
Interface MTU:         ____ bytes
```

### 6.2 Network layer

- IPv4 or IPv6? (Cloud services often resolve to both; note which one the client
  actually used.)
- Source and destination addresses.
- TTL / Hop Limit on the packets *you* send versus the ones you receive — the
  difference is a rough hop count.
- Whether any packet was fragmented (`ip.flags.mf == 1`). Usually none: TCP sizes
  its segments to the MSS precisely to avoid IP fragmentation.
- For a deployed server: the DNS exchange that produced the address
  (`dns.qry.name contains "<host>"`), and the answer's TTL.

── evidence ──
```
IP version:            ____
Source IP:             ________
Destination IP:        ________
TTL sent / received:   ____ / ____
Fragmented packets:    ____
DNS answer(s):         ________
```

### 6.3 Transport layer (TCP)

This is the layer the assignment is really about, so be thorough.

- **Ports.** Ephemeral source port chosen by the OS; destination `8080` locally or
  `443` when deployed.
- **Three-way handshake.** Find the `SYN`, `SYN, ACK`, `ACK` and record their frame
  numbers. Note the advertised window sizes and the options in the `SYN`: MSS,
  window scale, SACK permitted, timestamps.
- **Data transfer.** Which segments carry the `POST /mcp` body and which carry the
  response. Look at the `PSH` flag: it marks the end of an application message.
- **Acknowledgements.** Whether the ACK is piggybacked on a data segment or sent on
  its own, and how the `Seq`/`Ack` numbers advance by exactly the payload length.
- **Connection reuse.** Streamable HTTP keeps one connection alive across several
  MCP messages. Confirm it: how many TCP connections were opened for how many
  `tools/call` messages? (`Statistics ▸ Conversations ▸ TCP`.)
- **Teardown.** `FIN, ACK` / `ACK` in both directions, or an `RST`. Which side
  closed first?
- **Anomalies.** `tcp.analysis.retransmission`, `tcp.analysis.duplicate_ack`,
  `tcp.analysis.zero_window`. On loopback there will usually be none; say that
  explicitly rather than leaving the section empty, and explain why (no physical
  medium, no loss, no congestion).

── evidence ──
```
Source port / destination port:   ______ / ______
SYN / SYN-ACK / ACK frames:       ____ / ____ / ____
MSS advertised:                   ____ bytes
Window scale factor:              ____
TCP connections opened:           ____
MCP messages carried on them:     ____
Teardown: FIN or RST, initiated by: ________
Retransmissions / dup ACKs / zero windows: ____ / ____ / ____
```

### 6.4 Application layer

**Over plain HTTP (local server) — the bodies are readable.**

- The request line: `POST /mcp HTTP/1.1`.
- Request headers to note: `Content-Type: application/json`, `Accept:
  application/json, text/event-stream` (Streamable HTTP can answer either way),
  `MCP-Protocol-Version`, and `Mcp-Session-Id` on every message after the first.
- The response status and `Content-Type`. A `202 Accepted` with no body is the
  server acknowledging a notification; a `200 OK` carries a JSON-RPC result.
- The JSON-RPC envelope itself. Classify what you captured:

  | Kind | How to recognise it | Example from your capture |
  | --- | --- | --- |
  | Request | has both `"method"` and `"id"` | `________` |
  | Response (success) | has `"id"` and `"result"` | `________` |
  | Response (error) | has `"id"` and `"error"` with a `code` | `________` |
  | Notification | has `"method"` but **no** `"id"` | `________` |

- Match the JSON-RPC `id` on the wire to the `request_id` in the host log. They are
  different identifiers — the JSON-RPC `id` is assigned by the SDK, the
  `request_id` by the host — so correlate them by method, tool name and timestamp,
  and say that in the report rather than pretending they are the same field.

**Over HTTPS (deployed server) — the bodies are not readable.**

State this plainly: TLS encrypts the entire HTTP layer, so Wireshark shows
`Application Data` records of a given length and nothing else. What *is* visible:

- the TCP connection and its timing;
- the TLS handshake: `ClientHello` (with the SNI extension naming the host, and the
  offered cipher suites and versions), `ServerHello` (the chosen suite), and the
  certificate on TLS 1.2 — on TLS 1.3 the certificate itself is encrypted;
- the **size and direction and timing** of each encrypted record, which is enough
  to infer *that* a request happened and roughly how big the answer was.

The plaintext of those same messages is in `logs/session-<uuid>.jsonl`, recorded by
the host **before** encryption. Correlating the two is the honest way to say what
was inside an encrypted record.

── evidence ──
```
Request line:               ________
Mcp-Session-Id present?:    ________
Response status / type:     ________
JSON-RPC methods captured:  ________
TLS version negotiated:     ________
SNI in ClientHello:         ________
Cipher suite chosen:        ________
```

---

## 7. Optional: decrypting your own TLS session

Only do this against **your own** server, on **your own** machine. It is a
legitimate, reproducible technique: the client writes the session keys it just
negotiated to a file, and Wireshark reads that file.

CPython's `ssl` module honours the `SSLKEYLOGFILE` environment variable, and the
HTTP client the MCP SDK uses is built on it.

```bash
# 1. point the client at a key log file
export SSLKEYLOGFILE="$PWD/logs/tls-keys.log"        # PowerShell: $env:SSLKEYLOGFILE="$PWD\logs\tls-keys.log"

# 2. run the chatbot against the HTTPS URL, capturing at the same time
uv run adoptamatch-chatbot

# 3. Wireshark ▸ Edit ▸ Preferences ▸ Protocols ▸ TLS
#    "(Pre)-Master-Secret log filename" → logs/tls-keys.log
```

Wireshark then dissects the decrypted HTTP and the JSON-RPC inside it, and a
`Decrypted TLS` tab appears in the packet detail pane.

**`logs/tls-keys.log` is a secret.** It can decrypt that capture forever. It is
git-ignored along with the rest of `logs/`; delete it once the report is written,
and never attach it to a submission.

If you do not decrypt, do not pretend you did: the report should show the encrypted
records and the host log side by side, and say which is which.

── evidence ──
- [ ] Decryption attempted? yes / no
- [ ] If yes: screenshot of a decrypted `tools/call` frame.
- [ ] If no: screenshot of the `Application Data` records plus the matching log lines.

---

## 8. Saving the evidence

```bash
# In Wireshark: File ▸ Save As ▸ mcp-capture.pcapng
# Trim it first if it is large: File ▸ Export Specified Packets ▸ Displayed
```

Keep together, in one folder:

- `mcp-capture.pcapng` — the capture, filtered down to the MCP conversation;
- `logs/session-<uuid>.jsonl` — the host log of the same session;
- the screenshots referenced by each `── evidence ──` block above;
- a one-line note of the interface, the filter and the date of the capture.

`*.pcapng` is git-ignored: attach it to the report rather than committing it.

---

## 9. What to conclude (write this yourself, from the numbers above)

Prompts, not answers:

1. How many TCP connections carried how many MCP messages, and what does that say
   about Streamable HTTP versus opening a connection per call?
2. How does the `elapsed_ms` in the host log compare with the packet-level round
   trip, and what accounts for the difference?
3. What could an observer on the network learn about your session if it were
   HTTPS — and what could they *not* learn?
4. Why do three of your four servers produce no packets at all, and what does that
   imply about where the security boundary of an MCP host really is?
5. Was anything retransmitted or reordered? If not, why not — and what would you
   expect to change over a lossy link instead of loopback?
