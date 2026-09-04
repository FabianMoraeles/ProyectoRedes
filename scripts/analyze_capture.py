"""Correlate a Wireshark capture with the host's wire log.

This produces the two tables the network analysis needs, from real data:

1. **The JSON-RPC classification** the assignment asks for -- which captured
   messages are synchronisation, which are requests and which are responses --
   read out of the packet bytes themselves, not out of the log.
2. **The correlation table**: frame number, timestamp, TCP ports, HTTP status,
   JSON-RPC method and id, next to the same message as the host recorded it.

It also prints the transport-layer facts worth quoting: how many TCP connections
carried how many JSON-RPC frames, the handshake and teardown frames, and any
retransmissions.

Requires ``tshark`` (installed with Wireshark). On Windows it is found
automatically under ``C:\\Program Files\\Wireshark``.

Usage::

    uv run python scripts/analyze_capture.py captures/mcp-capture.pcapng
    uv run python scripts/analyze_capture.py captures/mcp-capture.pcapng
        --wire-log logs/session-<uuid>.wire.jsonl

Over HTTPS the bodies are encrypted, so the JSON-RPC columns will be empty unless
the capture was decrypted with ``SSLKEYLOGFILE``. That is expected, and the script
says so rather than pretending otherwise.
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adoptamatch_chatbot.mcp_wire.messages import LIFECYCLE_METHODS, classify
from adoptamatch_chatbot.presentation import Presenter

WINDOWS_TSHARK = Path(r"C:\Program Files\Wireshark\tshark.exe")

#: Fields pulled from every HTTP packet in the capture.
FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "ip.src",
    "tcp.srcport",
    "ip.dst",
    "tcp.dstport",
    "frame.len",
    "http.request.method",
    "http.request.uri",
    "http.response.code",
    "http.file_data",
)


def find_tshark() -> str:
    """Locate tshark, preferring whatever is on PATH."""
    found = shutil.which("tshark")
    if found:
        return found
    if WINDOWS_TSHARK.exists():
        return str(WINDOWS_TSHARK)
    raise SystemExit("tshark was not found. Install Wireshark (which bundles it), or add it to PATH.")


def run_tshark(tshark: str, capture: Path, display_filter: str, fields: tuple[str, ...]) -> list[list[str]]:
    command = [tshark, "-r", str(capture), "-Y", display_filter, "-T", "fields"]
    for field in fields:
        command += ["-e", field]
    command += ["-E", "separator=\t", "-E", "occurrence=f"]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"tshark failed:\n{completed.stderr.strip()}")
    rows = []
    for line in completed.stdout.splitlines():
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def decode_body(hex_body: str) -> dict[str, Any] | None:
    """Turn tshark's hex-encoded HTTP body into a JSON-RPC message, if it is one."""
    if not hex_body:
        return None
    cleaned = hex_body.replace(":", "").strip()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def load_wire_log(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        # Sort by modification time, not by name: the file name carries a UUID,
        # so an alphabetical sort would pick an arbitrary session rather than the
        # most recent one.
        candidates = sorted(glob.glob("logs/*.wire.jsonl"), key=lambda name: Path(name).stat().st_mtime)
        if not candidates:
            return []
        path = Path(candidates[-1])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path, help="The .pcapng file to read.")
    parser.add_argument(
        "--wire-log",
        type=Path,
        default=None,
        help="The session wire log. Defaults to the most recent logs/*.wire.jsonl.",
    )
    args = parser.parse_args()

    if not args.capture.exists():
        raise SystemExit(f"No such capture: {args.capture}")

    ui = Presenter()
    tshark = find_tshark()

    # ------------------------------------------------------------- transport layer
    ui.rule("transport layer (TCP)")
    syn = run_tshark(
        tshark,
        args.capture,
        "tcp.flags.syn==1",
        ("frame.number", "ip.src", "tcp.srcport", "ip.dst", "tcp.dstport", "tcp.flags.str"),
    )
    fin = run_tshark(
        tshark,
        args.capture,
        "tcp.flags.fin==1 || tcp.flags.reset==1",
        ("frame.number", "ip.src", "tcp.srcport", "tcp.flags.str"),
    )
    retransmissions = run_tshark(tshark, args.capture, "tcp.analysis.retransmission", ("frame.number",))
    all_packets = run_tshark(tshark, args.capture, "tcp", ("frame.number",))
    streams = run_tshark(tshark, args.capture, "tcp.flags.syn==1 && tcp.flags.ack==0", ("tcp.stream",))

    ui.info(f"packets in the capture:        {len(all_packets)}")
    ui.info(f"TCP connections opened:        {len({row[0] for row in streams})}")
    ui.info(f"SYN / SYN-ACK frames:          {', '.join(row[0] for row in syn) or '(none)'}")
    ui.info(f"FIN / RST frames:              {', '.join(row[0] for row in fin) or '(none)'}")
    ui.info(f"retransmissions:               {len(retransmissions)}")
    if syn:
        ui.info(f"client -> server:              {syn[0][1]}:{syn[0][2]} -> {syn[0][3]}:{syn[0][4]}")

    # ------------------------------------------------------------ application layer
    ui.rule("application layer (HTTP + JSON-RPC)")
    http_rows = run_tshark(tshark, args.capture, "http", FIELDS)
    if not http_rows:
        ui.warn(
            "No plaintext HTTP in this capture. Over HTTPS the bodies are encrypted: "
            "use the wire log for the JSON-RPC, or decrypt with SSLKEYLOGFILE "
            "(docs/wireshark-analysis.md section 8)."
        )

    captured: list[dict[str, Any]] = []
    for row in http_rows:
        row = row + [""] * (len(FIELDS) - len(row))
        number, epoch, src, sport, dst, dport, length, method, uri, status, body = row[: len(FIELDS)]
        message = decode_body(body)
        kind = classify(message) if message else ""
        rpc_method = (message or {}).get("method")
        rpc_id = (message or {}).get("id")
        captured.append(
            {
                "frame": number,
                "time": datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                if epoch
                else "",
                "src": f"{src}:{sport}",
                "dst": f"{dst}:{dport}",
                "bytes": length,
                "http": f"{method} {uri}" if method else f"HTTP {status}",
                "kind": kind,
                "method": rpc_method or "",
                "id": "" if rpc_id is None else rpc_id,
                "lifecycle": rpc_method in LIFECYCLE_METHODS if rpc_method else None,
            }
        )

    # A reply carries no method; name it after the request it answers.
    open_requests: dict[Any, str] = {}
    for entry in captured:
        if entry["kind"] == "request":
            open_requests[entry["id"]] = entry["method"]
        elif entry["kind"] in ("response", "error"):
            answered = open_requests.pop(entry["id"], "")
            entry["method"] = answered
            entry["lifecycle"] = answered in LIFECYCLE_METHODS if answered else False

    header = (
        f"  {'frame':>5}  {'time':<12}  {'src -> dst':<32}  {'bytes':>5}  "
        f"{'http':<14}  {'kind':<12}  {'class':<15}  method / id"
    )
    ui.info(header)
    ui.info("  " + "-" * (len(header) - 2))
    for entry in captured:
        group = "" if entry["lifecycle"] is None else ("synchronisation" if entry["lifecycle"] else "call")
        identifier = f"{entry['method']} id={entry['id']}" if entry["method"] else ""
        ui.info(
            f"  {entry['frame']:>5}  {entry['time']:<12}  "
            f"{entry['src'] + ' -> ' + entry['dst']:<32}  {entry['bytes']:>5}  "
            f"{entry['http']:<14}  {entry['kind']:<12}  {group:<15}  {identifier}"
        )

    # ---------------------------------------------------------------- the summary
    ui.rule("JSON-RPC classification, from the packet bytes")
    counts: dict[tuple[str, str], int] = {}
    for entry in captured:
        if not entry["kind"]:
            continue
        group = "synchronisation" if entry["lifecycle"] else "call"
        counts[(entry["kind"], group)] = counts.get((entry["kind"], group), 0) + 1
    for (kind, group), count in sorted(counts.items()):
        ui.info(f"  {kind:<13} {group:<16} {count}")
    total = sum(counts.values())
    ui.info(f"  {'total':<13} {'':<16} {total}")

    # -------------------------------------------------------------- cross-check
    wire = load_wire_log(args.wire_log)
    ui.rule("cross-check against the host's wire log")
    if not wire:
        ui.warn("No wire log found. Pass --wire-log, or run a session first.")
        return
    logged = [row for row in wire if row.get("kind") in ("request", "notification", "response", "error")]
    ui.info(f"frames in the wire log:        {len(logged)}")
    ui.info(f"JSON-RPC messages in capture:  {total}")
    if total == len(logged):
        ui.success("The capture and the log agree: every logged message is on the wire.")
    elif total == 0:
        ui.info("Nothing to compare: the capture carries no plaintext JSON-RPC (TLS?).")
    else:
        ui.warn(
            "Counts differ. Usual causes: the capture started late or stopped early, other "
            "sessions are in the log, or the filter excluded part of the conversation."
        )


if __name__ == "__main__":
    main()
