# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A terminal chat client for Meshtastic over WiFi (TCP API to a `meshtastic.local` device).

## Setup / running

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Two separate processes, always run in this order:

```bash
python3 mesh_logger.py   # 1. background daemon — start first, leave running
python3 mesh_chat.py     # 2. interactive client — start/stop freely
```

There is no build step or linter. Tests (pure stdlib, no device needed):

```bash
python3 -m unittest test_mesh_common -v
```

Run them after any change to the log line format or parser in `mesh_common.py`.

## Architecture

The Meshtastic device only accepts **one TCP connection at a time**. This single constraint
shapes the entire codebase, which is split into two processes that never both hold the device
connection:

- **`mesh_logger.py`** — the only process that opens `meshtastic.tcp_interface.TCPInterface` to
  the device. Runs continuously (deployed via `mesh-logger.service`, a systemd unit). Owns
  reconnect logic (both on `connection.lost` and via a silence watchdog: no packets of any kind
  for `SILENCE_TIMEOUT` → heartbeat probe → forced reconnect on failure), appends every text
  message to a daily log file under `logs/`, and exposes a Unix socket (`SOCKET_PATH` in
  `mesh_common.py`, default `/tmp/mesh_chat.sock`, chmod 0600) as an IPC broker.
- **`mesh_chat.py`** — the interactive prompt_toolkit TUI. Never touches the device directly.
  Talks to the logger exclusively over the Unix socket: sending messages, DMs, and node queries
  are all RPC calls (`send`, `dm`, `nodes`, `whoami`) over a newline-delimited JSON protocol.
  Can be started and stopped freely, and multiple instances can connect to the same logger
  simultaneously. If the logger goes away (e.g. `systemctl restart mesh-logger`), the client
  auto-reconnects to the socket and replays messages logged during the gap from `logs/`.
- **`mesh_common.py`** — shared source of truth for both processes: the daily log file naming
  scheme (`logs/chat-YYYY-MM-DD.log`, computed from the current date so rotation is automatic —
  no explicit rollover logic needed), and the log line format/parser (`format_message_line`,
  `format_quote_line`, `parse_log_line`). Because both processes read/write the exact same text
  format, keeping this module as the single formatter+parser prevents drift.

### IPC protocol (mesh_logger.py ⇄ mesh_chat.py)

Newline-delimited JSON over the Unix socket, in `mesh_logger.py`'s `_handle_client`. Two message
shapes flow back to a client:
- **Responses**: `{"req_id": N, "ok": bool, ...}` — always keyed by `req_id`, matched against a
  pending `asyncio.Future` in the client's `IPCClient._pending` dict.
- **Events** (unsolicited, pushed by the logger): `{"event": "delivery", ...}` for ACK/NAK after
  a send, and `{"event": "message", "lines": [...]}` broadcast to every connected client the
  instant a new message is logged. For received messages the event also carries `packet_id`,
  `from_id`, `is_dm`, `channel_index` — the client tracks the latest one as the `/reply` target.
  There is no polling — the logger pushes live updates directly, so `mesh_chat.py` only reads
  history once at startup (last 50 messages, walking `logs/` files newest-first via
  `list_log_files()`) and then relies entirely on pushed events.
- The `send`/`dm` commands accept an optional `reply_id` (a packet id) which is passed to
  `sendText(replyId=...)` — requires a meshtastic lib version whose `sendText` has that
  parameter; the logger checks via `inspect.signature` and returns a clear error otherwise.
  For sent messages the logger includes `is_dm` and (for DMs) `to_id` in the event metadata,
  so `/reply` can route outgoing DMs correctly.
- If the device is disconnected, `send`/`dm` don't error out: the logger appends the request
  (with its writer/lock so a later delivery event can still reach the same client) to `_outbox`
  (bounded, `OUTBOX_MAX`) and responds `{"ok": true, "queued": true}`. `_flush_outbox()` runs
  automatically from `_do_connect()` on every successful (re)connect, actually transmitting each
  queued item via the same `_send_message()` helper the live path uses.
- `cmd == "trace"` runs `_do_traceroute()`, a from-scratch traceroute (not the library's built-in
  `sendTraceRoute`, which blocks and prints straight to stdout instead of returning structured
  data) built on `sendData(portNum=TRACEROUTE_APP)` + a custom `onResponse`. Response packet
  field semantics are non-obvious and easy to get backwards: `p["to"]` is *us* (the original
  requester) and `p["from"]` is the *traced node* — mirrors meshtastic's own
  `onResponseTraceRoute`. `towards` = [us, ...route hops with SNR, traced node]; `back` = [traced
  node, ...routeBack hops, us] (empty if the far end never sent a return path).

### Reply/quote handling

Meshtastic's real "Reply" feature sets `decoded.replyId` on a packet, referencing the original
message's packet `id`. `mesh_logger.py` keeps an in-memory `_store` (bounded deque, `STORE_SIZE`
= 300) of recent packet IDs → sender/text, populated both from `on_receive` (others' messages) and
from the socket handler when *this* node sends a message. When a reply's `replyId` resolves in
`_store`, `_write_message` prepends a quote line (`format_quote_line`) before the message line.
The client's `/reply` command sends a real reply: it keeps a deque of the last 20 pushed
"message" events (with their `packet_id`), `/reply` alone lists them numbered (#1 = newest),
`/reply #N <text>` targets one, `/reply <text>` targets #1. Only messages received while the
client was running are replyable (log files carry no packet ids). Switching channels via
`/ch` drops targets from other channels.
Note: many community "ping bots" embed the target's hex node ID as plain text in their reply
(e.g. `🤖 Pong !1ba60314`) instead of using the real `replyId` field — these will never show a
quote line, which is expected, not a bug.

Tapback reactions (meshtastic sets `decoded.emoji` truthy) are just a reply whose text is the
emoji itself, so they get the same quote-line treatment in the log — no format change needed.
`mesh_chat.py` additionally compacts a (quote, message) pair into one line when the message text
looks like nothing but emoji (`_is_reaction_text`, a regex heuristic — the log format has no
"is this a reaction" bit to read back after a restart, so both live and history rendering use the
same heuristic for consistency rather than trusting the logger's more precise live `is_emoji` meta).

### Log line format

Two line kinds, parsed by `parse_log_line` in `mesh_common.py`:
```
  ┆ <name> (<short>): <quoted text>                          # quote line (optional, precedes a reply)
<channel> [HH:MM:SS] <DM tag><name> (<short>): <text> | <hops>   # message line
```
`<channel>` is the channel's display name (`Channel.settings.name`, or `"Primary"` for channel 0
when unnamed). `<DM tag>` is empty for broadcast, `DM ` for an incoming DM, `DM → ` for an
outgoing DM. An optional ` SNR:<float>` suffix follows `<hops>` when the device reported
`rxSnr` on the packet (absent for locally-sent messages and old log lines — `ParsedLine.snr` is
`None` in both cases). This grammar is deliberately simple/regex-friendly since `mesh_chat.py`
re-renders colored terminal output entirely by parsing these plain-text lines (from history file
reads and from pushed "message" events) — it never has direct access to raw packet objects.

### Channels

`mesh_logger.py` logs text messages from **every** channel (it doesn't filter by `channel`
index), and resolves index → name via `_channel_name()` (reads `interface.localNode.channels`).
`mesh_chat.py` run with no flags shows all channels at once (log lines rendered with the
`<channel>` prefix visible) and sends to channel 0 (Primary). Run with `--channel <name>` (or
the legacy shorthand `--<name>`, e.g. `mesh_chat.py --ping`), it resolves that name to an index
via the `channels` IPC command, restricts history/live display to that channel only (prefix
hidden, since it's implied), and sends/DMs go out on that channel's index. `--history N` sets
how much history is shown at startup (default 50). The `send`/`dm` IPC commands both take an optional
`channel` field (channel index, default 0) for this purpose.

### Node name resolution

`mesh_chat.py` additionally resolves `!hex (???)` fallback names (nodes not yet known to the
mesh) via the public OneMesh API (`https://map.onemesh.ru/api/v1/nodes/{decimal_node_id}`),
triggered by the `/updatenames` command. Results are cached in `node_names_cache.json` so they
persist across restarts. This resolution happens only in the display layer — it does not touch
`logs/`, so historical log lines keep whatever name was known to the device at write time.

### Deploying changes

`mesh_chat.py` picks up new code on its next manual run — no restart needed. `mesh_logger.py`
runs continuously under systemd, so code changes require an explicit restart:
```bash
git pull
sudo systemctl restart mesh-logger
```
