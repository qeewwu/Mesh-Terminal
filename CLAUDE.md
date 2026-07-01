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

There is no build step, linter, or test suite in this repo.

## Architecture

The Meshtastic device only accepts **one TCP connection at a time**. This single constraint
shapes the entire codebase, which is split into two processes that never both hold the device
connection:

- **`mesh_logger.py`** — the only process that opens `meshtastic.tcp_interface.TCPInterface` to
  the device. Runs continuously (deployed via `mesh-logger.service`, a systemd unit). Owns
  reconnect logic, appends every text message to a daily log file under `logs/`, and exposes a
  Unix socket (`SOCKET_PATH` in `mesh_common.py`, default `/tmp/mesh_chat.sock`) as an IPC broker.
- **`mesh_chat.py`** — the interactive prompt_toolkit TUI. Never touches the device directly.
  Talks to the logger exclusively over the Unix socket: sending messages, DMs, and node queries
  are all RPC calls (`send`, `dm`, `nodes`, `whoami`) over a newline-delimited JSON protocol.
  Can be started and stopped freely, and multiple instances can connect to the same logger
  simultaneously.
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
  instant a new message is logged. There is no polling — the logger pushes live updates directly,
  so `mesh_chat.py` only reads history once at startup (last 50 messages, walking `logs/` files
  newest-first via `list_log_files()`) and then relies entirely on pushed events.

### Reply/quote handling

Meshtastic's real "Reply" feature sets `decoded.replyId` on a packet, referencing the original
message's packet `id`. `mesh_logger.py` keeps an in-memory `_store` (bounded deque, `STORE_SIZE`
= 300) of recent packet IDs → sender/text, populated both from `on_receive` (others' messages) and
from the socket handler when *this* node sends a message. When a reply's `replyId` resolves in
`_store`, `_write_message` prepends a quote line (`format_quote_line`) before the message line.
Note: many community "ping bots" embed the target's hex node ID as plain text in their reply
(e.g. `🤖 Pong !1ba60314`) instead of using the real `replyId` field — these will never show a
quote line, which is expected, not a bug.

### Log line format

Two line kinds, parsed by `parse_log_line` in `mesh_common.py`:
```
  ┆ <name> (<short>): <quoted text>              # quote line (optional, precedes a reply)
[HH:MM:SS] <DM tag><name> (<short>): <text> | <hops>   # message line
```
`<DM tag>` is empty for broadcast, `DM ` for an incoming DM, `DM → ` for an outgoing DM. This
grammar is deliberately simple/regex-friendly since `mesh_chat.py` re-renders colored terminal
output entirely by parsing these plain-text lines (from history file reads and from pushed
"message" events) — it never has direct access to raw packet objects.

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
