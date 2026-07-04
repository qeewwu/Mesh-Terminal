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
  In the reconnect loop, `_do_connect()`/`close()` run via `asyncio.to_thread` — the
  `TCPInterface` constructor blocks for seconds (DNS + connect + config wait), and running it on
  the event loop would freeze all IPC clients for the duration.
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
  instant a new message is logged. Every per-client write (broadcast and response alike) is
  bounded by `CLIENT_SEND_TIMEOUT`: a client that stopped reading (e.g. a suspended terminal)
  fills its socket buffer and would otherwise hold its write lock in `drain()` forever, stalling
  broadcasts to everyone else — instead it gets disconnected. For received messages the event also carries `packet_id`,
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
  ┆ [HH:MM:SS] <name> (<short>): <quoted text>                    # quote line (optional, precedes a reply)
<channel> [HH:MM:SS] <DM tag><name> (<short>): <text> | <hops>   # message line
```
`<channel>` is the channel's display name (`Channel.settings.name`, or `"Primary"` for channel 0
when unnamed). `<DM tag>` is empty for broadcast, `DM ` for an incoming DM, `DM → ` for an
outgoing DM. An optional ` SNR:<float>` suffix follows `<hops>` when the device reported
`rxSnr` on the packet (absent for locally-sent messages and old log lines — `ParsedLine.snr` is
`None` in both cases). This grammar is deliberately simple/regex-friendly since `mesh_chat.py`
re-renders colored terminal output entirely by parsing these plain-text lines (from history file
reads and from pushed "message" events) — it never has direct access to raw packet objects.

`<DM tag><name>` is inherently ambiguous when the name could extend the tag (a node named
"DM Master", or "→ X" after an incoming-DM tag) — the parser cannot recover this, so
`_escape_name` in `mesh_common.py` fixes it at write time by swapping the ambiguous space for a
non-breaking one (U+00A0, visually identical). It also substitutes `?` for an empty long name
(an empty name makes the line unparseable, and the message would silently vanish from clients);
`_node_names` in `mesh_logger.py` guards the same case at the source with `or`-fallbacks, since
firmware can report `longName`/`shortName` keys holding empty strings.

The quote line's `[HH:MM:SS]` prefix is the *original* message's time (when it was first logged),
not the time of the reply — `_QUOTE_RE` in `mesh_common.py` makes this group optional so old log
lines written before this existed (no time in the quote) still parse fine, just with
`ParsedLine.time_str == ""`. `mesh_logger.py`'s `MsgRecord` (the `_store` value type) carries a
`time_str` field for exactly this: `on_receive` and `_send_message` compute the timestamp once,
store it in `MsgRecord`, and pass the same string into `_write_message(time_str=...)` so the
message line and any later reply's quote never drift from each other or from real time.

### Channels

`mesh_logger.py` logs text messages from **every** channel (it doesn't filter by `channel`
index), and resolves index → name via `_channel_name()` (reads `interface.localNode.channels`).
`mesh_chat.py` run with no flags shows all channels at once (log lines rendered with the
`<channel>` prefix visible) and sends to channel 0 (Primary). Run with `--channel <name>` (or
the legacy shorthand `--<name>`, e.g. `mesh_chat.py --ping`), it resolves that name to an index
via the `channels` IPC command, restricts history/live display to that channel only (prefix
hidden, since it's implied), and sends/DMs go out on that channel's index. `--history N` sets
how much history is shown at startup (default `HISTORY_SIZE`, currently 100). The `send`/`dm`
IPC commands both take an optional `channel` field (channel index, default 0) for this purpose.
`/send <channel> <text>` (`_cmd_send`) is a one-off send to an arbitrary channel by name — it
looks up the channel via the same `channels` IPC command `/ch` uses, but unlike `/ch` it never
touches `_channel_filter`/`_channel_index`, so the session's current channel, history filter,
and `/reply` targets are unaffected.

Incoming DMs arrive on channel index 0, so a session filtered with `--channel <name>` (or `/ch`)
does **not** display them — this is a deliberate decision, not a bug: a filtered session is meant
to be a clean view of that one channel. Run an unfiltered client (multiple clients can share the
logger) to watch DMs; nothing is lost — DMs are always in `logs/` regardless of any client's filter.

### Node name resolution

`mesh_chat.py` additionally resolves `!hex (???)` fallback names (nodes not yet known to the
mesh) via the public OneMesh API (`https://map.onemesh.ru/api/v1/nodes/{decimal_node_id}`),
triggered by the `/updatenames` command. Results are cached in `node_names_cache.json` so they
persist across restarts. This resolution happens only in the display layer — it does not touch
`logs/`, so historical log lines keep whatever name was known to the device at write time.

### `/nodes` sorting and online status

`mesh_logger.py`'s `_nodes_payload()` includes `hops_away` (from the protobuf `NodeInfo.hops_away`,
appears as `hopsAway` in the `MessageToDict`-converted node dict — absent for nodes the firmware
hasn't reported it for). `mesh_chat.py`'s `_cmd_nodes()` first resolves display names (same
OneMesh-cache logic as before) into each node dict as `display_long`/`display_short`, *then* sorts
via `_node_sort_key(n, mode)`. Sorting happens after name resolution specifically so the
alphabetical order matches what's on screen, not raw `!hex` node IDs for unresolved nodes.

`/nodes [online|names|hops]` (`NODE_SORT_MODES`) picks which key is primary — the other two
still apply as tiebreakers, just demoted:
- `online` (default): online status desc → name → hops
- `names`: name → online status desc → hops
- `hops`: hops asc (`None` last) → online status desc → name

A node counts as "online" when `seconds_ago < ONLINE_THRESHOLD_SECONDS` (15 minutes) — this is a
display-layer heuristic in `mesh_chat.py`, not something the device/logger reports.

### Deploying changes

`mesh_chat.py` picks up new code on its next manual run — no restart needed. `mesh_logger.py`
runs continuously under systemd, so code changes require an explicit restart:
```bash
git pull
sudo systemctl restart mesh-logger
```
