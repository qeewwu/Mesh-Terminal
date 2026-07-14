# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A terminal chat client for Meshtastic. Connects over WiFi (TCP API, default), USB serial, or
Bluetooth LE — see `MESH_CONN_TYPE` in `.env`.

## Documentation split

`README.md` (English, canonical) and `README.ru.md` (Russian, kept as a faithful translation —
same structure, same sections, same information, just the language) are user-facing: what the
project is, what it does, how to install/run/use it, the command and config reference. No
implementation rationale belongs there — no "why", no protocol internals, no history of bugs
found and fixed. That's all here, in `CLAUDE.md`, instead.

When a user-visible feature, command, or config key changes, update **both** README files
together, keeping their structure identical — don't let one drift ahead of the other. If you're
tempted to explain *why* something works a certain way in a README, that explanation belongs in
this file, with at most a pointer from the README ("see CLAUDE.md" / "see SETTINGS.md").

## Setup / running

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional — sensible defaults apply if you skip this
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

The Meshtastic device only accepts **one connection at a time**, regardless of transport (WiFi
TCP, USB serial, or BLE all share this limit at the firmware level). This single constraint
shapes the entire codebase, which is split into two processes that never both hold the device
connection:

- **`mesh_logger.py`** — the only process that opens a connection to the device, via
  `meshtastic.tcp_interface.TCPInterface`, `meshtastic.serial_interface.SerialInterface`, or
  `meshtastic.ble_interface.BLEInterface` depending on `CONN_TYPE` (`mesh_common.py`, from
  `MESH_CONN_TYPE` in `.env`: `wifi` (default) | `usb` | `ble`). `_open_interface()` is the single
  place that branches on `CONN_TYPE` and constructs the right interface; everything else in the
  file (reconnect loop, watchdog, `close()`) treats `_interface` uniformly since all three
  interface classes share the same base API. `HOST`/`MESH_HOST` (wifi), `USB_PORT`/`MESH_USB_PORT`
  (usb), and `BLE_ADDRESS`/`MESH_BLE_ADDRESS` (ble) are all optional except `HOST` (which has a
  hardcoded fallback of `meshtastic.local`) — leaving `USB_PORT`/`BLE_ADDRESS` unset lets the
  meshtastic lib auto-detect the sole attached serial device / do a BLE scan and connect if
  exactly one Meshtastic device is found, so `usb`/`ble` work with zero extra `.env` config on a
  machine with only one device attached. `CONN_TARGET` is a display-only string (which of
  `HOST`/`USB_PORT`/`BLE_ADDRESS` is relevant for the configured `CONN_TYPE`, or a fallback label
  when unset) used only in log messages — it doesn't affect how `_open_interface()` connects.
  Runs continuously (deployed via `mesh-logger.service`, a systemd unit). Owns
  reconnect logic (both on `connection.lost` and via a silence watchdog: no packets of any kind
  for `SILENCE_TIMEOUT` → heartbeat probe → forced reconnect on failure), appends every text
  message to a daily log file under `logs/`, and exposes a Unix socket (`SOCKET_PATH` in
  `mesh_common.py`, default `/tmp/mesh_chat.sock`, chmod 0600) as an IPC broker.
  In the reconnect loop, `_do_connect()`/`close()` run via `asyncio.to_thread` — the
  interface constructor blocks for seconds (DNS + connect + config wait for TCP; port open +
  handshake for USB/BLE), and running it on the event loop would freeze all IPC clients for the
  duration.
  `_watchdog()` also pings systemd's own watchdog (`sd_notify("WATCHDOG=1")`, `_sd_notify()`
  writes directly to the `NOTIFY_SOCKET` unix datagram socket — no extra dependency) every
  `WATCHDOG_PING_SECONDS`, so a wedged event loop gets restarted by systemd even though the
  process itself never crashed; `mesh-logger.service` needs `Type=notify` + `WatchdogSec` for
  this to do anything (a no-op otherwise, since `NOTIFY_SOCKET` is unset outside systemd).
- **`mesh_chat.py`** — the interactive prompt_toolkit TUI. Never touches the device directly.
  Talks to the logger exclusively over the Unix socket: sending messages, DMs, and node queries
  are all RPC calls (`send`, `dm`, `nodes`, `whoami`) over a newline-delimited JSON protocol.
  Can be started and stopped freely, and multiple instances can connect to the same logger
  simultaneously. If the logger goes away (e.g. `systemctl restart mesh-logger`), the client
  auto-reconnects to the socket and replays messages logged during the gap from `logs/`.
  Rendering at the history/live seams (startup buffer flush, post-reconnect replay) goes through
  `_render_if_new`, which skips message lines already in `_recent_lines` — a message logged
  between a file read and the corresponding live event would otherwise print twice. Plain
  `_render_unit` stays in use where re-showing is intended (`/ch` recent history, `/search`).
  `/dm` and `/trace` resolve names via `_pick_node`: exact name/`!hex`-id match beats prefix
  beats substring, and an ambiguous query lists the candidates instead of picking one silently.
  So do `/ping`, `/who <name>`, and `/ignore`/`/unignore` (see Ignoring below).
- **`mesh_common.py`** — shared source of truth for both processes: the daily log file naming
  scheme (`logs/chat-YYYY-MM-DD.log`, computed from the current date so rotation is automatic —
  no explicit rollover logic needed), and the log line format/parser (`format_message_line`,
  `format_quote_line`, `parse_log_line`). Because both processes read/write the exact same text
  format, keeping this module as the single formatter+parser prevents drift.

### `.env` (personal/environment config)

`parse_env_file()`/`update_env_file()` in `mesh_common.py` are a deliberately minimal stdlib
`KEY=VALUE` reader/writer (no `python-dotenv` — the project has no other third-party deps to
justify one). `_ENV` is parsed once at import time; `HOST` (device hostname, `MESH_HOST`) and
`CONN_TYPE` (`MESH_CONN_TYPE`) fall back to their old hardcoded defaults if `.env` is missing or a
key isn't set, so a fresh checkout with no `.env` at all still runs (and connects over wifi, same
as before this setting existed). `USB_PORT` (`MESH_USB_PORT`) and `BLE_ADDRESS`
(`MESH_BLE_ADDRESS`) fall back to `None` instead, which is a meaningful value to the meshtastic lib
(auto-detect), not just an unset placeholder. `.env.example` in the repo documents every key with a
placeholder/default; `.env` itself is gitignored. `update_env_file()` rewrites only the keys it's
given, preserving every other line (comments, unrelated vars) — `mesh_chat.py`'s `/who` calls it
with the freshly-learned `NODE_LONG_NAME`/`NODE_SHORT_NAME`/`NODE_ID` every time it runs, so `.env`
self-updates with the current node identity instead of needing manual upkeep.

### Interface language (`mesh_i18n.py`)

`LANG` (`MESH_LANG` in `.env`, `ru`|`en`, default `ru`) is read once at import in
`mesh_common.py` next to `HOST`/`CONN_TYPE` — same lifecycle as every other env key, so changing
it requires restarting both processes. `mesh_common.py` deliberately does **not** import
`mesh_i18n` (that would be circular, since `mesh_i18n` imports `LANG` from it) — this also keeps
the log-format module free of UI concerns.

`mesh_i18n.py` is a flat pair of dicts (`_RU`, `_EN`), not a gettext/`.po` setup — this is a
two-language personal project, not a framework, and a dict pair is easier to diff and keep in
sync than a translation-file pipeline. `t(key, **kwargs)` looks up the active-language template
and calls `.format(**kwargs)`; a key missing from the active table falls back to `_RU`, then to
the key itself — the TUI must never raise over a missing translation. `t_list(key)` is the same
for list-valued keys (currently only `help_lines`, kept as one full list per language rather than
atomized per line, since `/help`'s column alignment is something you want to eyeball as a whole
block per language). `plural(key, n)` renders `"{n} {word}"` from a per-key forms tuple — 2 forms
(singular/plural) for English, 3 for Russian (the real `n%100`/`n%10` grammatical rule, not the
`2 <= n <= 4` shortcut that used to be inlined three times in `mesh_chat.py` and once in
`mesh_logger.py`).

Convention carried over unchanged from the pre-i18n f-strings: templates hold the prompt_toolkit
HTML markup (`<ansired>`, literal `<b>`, pre-escaped `&lt;name&gt;`) and are trusted; every
interpolated *value* is `_safe()`-escaped by the caller before being passed as a kwarg to `t()`.

**The log line format is never localized** — `format_message_line`/`format_quote_line`/
`parse_log_line`, channel names, and the `DM `/`DM → ` tags stay exactly as they were before
`MESH_LANG` existed, regardless of which language the UI is in. `test_mesh_i18n.py`'s key/
placeholder-parity tests guard the string tables themselves; the log format's own invariant is
still guarded the usual way, by `test_mesh_common.py`'s round-trip tests never touching
`mesh_i18n`. The bare status glyphs (`🏓 ✓ ✗ ⏳`, the `───` rule characters) are deliberately *not*
looked up per-language.

`compass_point()` (`/who`'s bearing labels) is the one localized string that lives outside
`mesh_i18n.py`, in `mesh_common.py` itself, purely because of the import direction above — it
takes an optional `lang` parameter (defaulting to the module-level `LANG`) so tests can pin both
languages without touching `.env`.

Input parsing stays bilingual regardless of `MESH_LANG` — accepting input in either language costs
nothing and avoids surprising a user who's used to typing `да`/`нет`: `_parse_setting_value`'s
bool parser (`да`/`нет`/`вкл`/`выкл` alongside `yes`/`no`/`on`/`off`), `/ch`'s `all`/`*`/`все`, and
`/stats`'s `день`/`дни`/`узел`/`узлы` alongside `day`/`days`/`node`/`nodes`. Only the *output*
(prompts, confirmations, tab-completion candidates) follows `MESH_LANG`.

### IPC protocol (mesh_logger.py ⇄ mesh_chat.py)

Newline-delimited JSON over the Unix socket, in `mesh_logger.py`'s `_handle_client`. Two message
shapes flow back to a client:
- **Responses**: `{"req_id": N, "ok": bool, ...}` — always keyed by `req_id`, matched against a
  pending `asyncio.Future` in the client's `IPCClient._pending` dict.
- **Events** (unsolicited, pushed by the logger): `{"event": "delivery", ...}` for ACK/NAK after
  a send, `{"event": "message", "lines": [...]}` broadcast to every connected client the instant
  a new message is logged, and `{"event": "device", "status": "connected"|"disconnected", ...}`
  for the *device* link's own state (see "Device connection status" below — distinct from a
  client's own socket to the logger, which `IPCClient.on_disconnect` tracks separately). All
  three go through the same `_broadcast_event()` in `mesh_logger.py`; every per-client write
  (broadcast and response alike) is bounded by `CLIENT_SEND_TIMEOUT`: a client that stopped
  reading (e.g. a suspended terminal) fills its socket buffer and would otherwise hold its write
  lock in `drain()` forever, stalling broadcasts to everyone else — instead it gets disconnected.
  For received messages the event also carries `packet_id`, `from_id`, `is_dm`, `channel_index`
  — the client tracks the latest one as the `/reply` target. There is no polling — the logger
  pushes live updates directly, so `mesh_chat.py` only reads history once at startup (`HISTORY_SIZE`
  messages, currently 100 — see Channels below — walking `logs/` files newest-first via
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
  queued item via the same `_send_message()` helper the live path uses. The same queueing also
  covers the "link died but `connection.lost` hasn't fired yet" case: if `sendText` itself
  raises, the message is re-queued (bounded by `OUTBOX_RETRIES` per message, so a poison message
  can't loop forever) and a reconnect is triggered; a message that exhausts its retries reports
  failure to its client via a `delivery` event, since the client already got a "queued" response.
  The logger also starts fine with the device unreachable: `main()` no longer exits on a failed
  initial connect — it serves the IPC socket immediately (clients see "not connected", sends
  queue to the outbox) and leaves retrying to the reconnect loop.
- `cmd == "trace"` runs `_do_traceroute()`, a from-scratch traceroute (not the library's built-in
  `sendTraceRoute`, which blocks and prints straight to stdout instead of returning structured
  data) built on `sendData(portNum=TRACEROUTE_APP)` + a custom `onResponse`. Response packet
  field semantics are non-obvious and easy to get backwards: `p["to"]` is *us* (the original
  requester) and `p["from"]` is the *traced node* — mirrors meshtastic's own
  `onResponseTraceRoute`. `towards` = [us, ...route hops with SNR, traced node]; `back` = [traced
  node, ...routeBack hops, us] (empty if the far end never sent a return path).
- `send`/`dm` accept an optional `emoji: bool` field for sending tapback reactions (`/react`;
  see Reactions below). The `delivery` event also carries an optional `hops` field: `handler()`
  in `_send_message()` computes it from the ACK packet's `hopStart`/`hopLimit`, the same
  arithmetic `on_receive()` uses for incoming messages — this is what lets `/ping` report a hop
  count, and the generic "✓ Доставлено" line show it too.
- `cmd == "settings"` (`action: "list"|"set"`) reads/writes the local node's own device
  configuration — see "Local node settings" below.
- Both `IPCClient.connect()` (`open_unix_connection`) and the logger's `start_unix_server()` pass
  `limit=IPC_LINE_LIMIT` (`mesh_common.py`, 8 MiB) instead of accepting asyncio's 64 KiB default —
  see "IPC readline limit broke `/nodes` on large meshes" under Fixed bugs below.

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
`/ch` drops targets from other channels. When the session is unfiltered (multiple channels mixed
together), each listed entry shows which channel it's on (`_reply_label`, same magenta-prefix
convention as live rendering in `_print_msg`) — with channels interleaved, picking the wrong #N
because it wasn't obvious which channel it belonged to was an easy miss.

`#N` is resolved against `_last_reply_snapshot` — a copy of the list taken at the moment `/reply`
(or `/react`) last printed it with no arguments — not against the live `_replyables` deque at
send time. Traffic keeps flowing between looking at the list and typing the command, and on a busy
channel that can be several messages within seconds; worse, **our own outgoing send is itself a
pushed "message" event** and gets appended to `_replyables` too, so even a single successful
`/reply #N` shifts every number for the *next* one. Resolving live meant "#5" could silently point
at a completely different message by the time the command ran — no error, just the wrong target.
Bare `/reply <text>` (no explicit number) deliberately stays live (`_replyables[-1]`, not the
snapshot) — it means "reply to whatever's newest right now," which doesn't presuppose the list was
ever shown, unlike an explicit `#N` which only makes sense relative to a list the user actually
looked at.
Note: many community "ping bots" embed the target's hex node ID as plain text in their reply
(e.g. `🤖 Pong !1ba60314`) instead of using the real `replyId` field — these will never show a
quote line, which is expected, not a bug.

Tapback reactions (meshtastic sets `decoded.emoji` truthy) are just a reply whose text is the
emoji itself, so they get the same quote-line treatment in the log — no format change needed.
`mesh_chat.py` additionally compacts a (quote, message) pair into one line when the message text
looks like nothing but emoji (`_is_reaction_text`, a regex heuristic — the log format has no
"is this a reaction" bit to read back after a restart, so both live and history rendering use the
same heuristic for consistency rather than trusting the logger's more precise live `is_emoji` meta).

**Sending** a reaction (`/react #N <emoji>`, sharing `_replyables`/target-parsing with `/reply`
via `_parse_target_and_text`) needs `decoded.emoji = 1` on the outgoing packet — a field the
`Data` protobuf message has but that neither `sendText()` nor `sendData()` exposes as a keyword
argument. `_send_emoji_packet()` in `mesh_logger.py` builds the `MeshPacket` by hand (mirroring
what `sendData()` does internally) and calls the interface's private `_sendPacket()`/
`_addResponseHandler()`/`_generatePacketId()` directly — same precedent as `_do_traceroute()`
reaching past the public API when it doesn't expose a needed field. The reaction is then logged
through the normal `_write_message()` path with `reply_to` set, so it round-trips through
`_is_reaction_text()` exactly like a reaction received from someone else.

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

`!hex (???)` fallback names (nodes the mesh itself hasn't given us a NodeInfo for) get resolved
via the public OneMesh API (`https://map.onemesh.ru/api/v1/nodes/{decimal_node_id}`) — but the
resolution and caching live in **`mesh_logger.py`**, not the client. This used to be purely a
`mesh_chat.py` display-layer feature (cache never touched `logs/`, so historical lines kept
whatever name was known at write time) — moved because that meant resolution only ever ran while
some chat client happened to be connected. `mesh_logger.py` runs `_run_onemesh_update()`
continuously (`_periodic_onemesh_update()`: once at startup, then every `ONEMESH_UPDATE_INTERVAL`
= 30 min) and — this is the actual point of the move — feeds the result straight into
`_node_names()`, so **freshly-written log lines** get the resolved name instead of `!hex (???)`,
not just how a client happens to render them. Targets come from two sources: entries in
`_interface.nodes` with no `user` data at all, *and* `_unresolved_ids` — a set that `_node_names()`
itself populates every time it has to fall back to `!hex (???)`, covering senders the device has
only ever relayed a message from and never received a full NodeInfo for (so they never appear in
`_interface.nodes` at all, and the first loop alone can't find them — see Fixed bugs below). Either
way, an id already in `_onemesh_names` is skipped. Results persist in `onemesh_cache.json`
(`ONEMESH_CACHE_FILE` in `mesh_common.py`), a flat `{node_id: {long, short}}` map — logger-owned,
logger-written. `mesh_chat.py`'s `/updatenames` is now a thin `updatenames` IPC call that just
triggers an extra sweep on demand and reloads the shared cache to show the result immediately;
`_periodic_cache_reload()` re-reads that same file every `CACHE_RELOAD_INTERVAL` (5 min) so a
long-running client picks up names the logger resolved in the background, without needing a
restart — no network calls happen client-side.

### Ignoring (`/ignore`, `/unignore`)

Not an app-level filter — a real firmware feature. `node.setIgnored(nodeId)`/`removeIgnored(nodeId)`
(`_set_ignored_node()` in `mesh_logger.py`, an admin message like `writeConfig`/`reboot`, same
`_ensure_session_key()` race to close first) tells the device's own NodeDB to ignore that node id;
`is_ignored`/`is_muted` are real fields on the protobuf `NodeInfo` message, so `_nodes_payload()`
exposes `is_ignored` straight from `_interface.nodes` for `/ignore`/`/who`/`/nodes` to read. This
replaced an earlier app-level `/mute` (keyed by display name, filtering the client's own render of
already-logged lines) once it turned out the firmware already has a proper per-node ignore list —
no reason to reimplement a weaker version of it in `mesh_chat.py`.

Consequence worth knowing: because the device drops an ignored node's packets before
`mesh_logger.py` ever sees them, an ignored sender's messages stop being logged **at all**, not
just hidden from the live feed — unlike the old `/mute`, there's no falling back to `/search` for
them afterward. It's also node-id-keyed rather than name-keyed, so (unlike the old `/mute`)
`/ignore <name>` can only target a node currently visible in `/nodes` — resolves through
`_pick_node()`/`_enrich_nodes()` like every other by-name command, and needs a real `node_id` to
send the admin message, so it can't blindly ignore-by-name-only the way the old mute could.

### Node lookup and position (`/who <name>`)

`/who` with no argument is unchanged — self info plus the `.env` self-update (see `.env` above).
`/who <name>` is a separate branch (`_cmd_who`, dispatching to `_cmd_who_self()` for the no-arg
case) that looks up *any* visible node via the same `_enrich_nodes()`/`_pick_node()` pair every
other by-name command uses, and prints everything about it in one place: display name, hex id,
battery/SNR/hops, last-heard status, and position/distance-from-us if available. Deliberately
never touches `.env`: that self-update is specifically about *this* node's own identity persisting
across a logger restart, and writing another node's name into it would be nonsensical.
`_format_last_heard()` (the online/`Nм назад`/`Nч назад`/`?` label+color logic) was factored out of
`_cmd_nodes` so `/who` doesn't duplicate it.

The position half used to be a separate `/pos <node>` command, removed once `/who` covered the same
ground — no point maintaining two commands with overlapping by-name lookup and near-identical
output. `_nodes_payload()` in `mesh_logger.py` exposes `lat`/`lon`/`alt` from `node["position"]` —
already converted from the raw `latitudeI`/`longitudeI` integers to float degrees by the meshtastic
library's own `_fixupPosition()`, so no unit conversion happens on this side. `_cmd_who` reads the
target's position plus our own (found by matching `whoami_id` in the same `nodes` list) and
computes distance/bearing with `haversine_km()`/`bearing_deg()` in `mesh_common.py` (kept there,
not in `mesh_chat.py`, purely so they're plain stdlib math and can be unit-tested without
prompt_toolkit).

### RTT measurement (`/ping`)

`/ping <node>` sends its own dedicated `ping` IPC command (not `dm`) and measures wall time until
the matching `delivery` event arrives. It used to piggyback on `dm` with a literal `"🏓 ping"` text
— which worked for RTT, but meant a real chat message landed in the *target's* DM inbox just to
measure round-trip time, purely as a side effect of how the probe was implemented (see Fixed bugs
below). `_send_ping_packet()` in `mesh_logger.py` instead builds a bare `wantAck=True` packet on
`PRIVATE_APP` by hand (same `_sendPacket()`-by-hand precedent as `_send_emoji_packet`/
`_do_traceroute`) — the mesh-layer ACK/NAK that `/ping` actually measures fires for any unicast
packet with `wantAck=True` regardless of portnum, so there's no need for `TEXT_MESSAGE_APP` at all;
official Meshtastic apps don't render `PRIVATE_APP` packets in their chat UI. `_send_message()`'s
`ping=True` branch skips the text-size check, the `_write_message()`/`_store_msg()` local-log calls
(there's no text to log, and nothing was actually sent to anyone's visible chat), and outbox
queueing on a transient link failure (a ping delivered late after a reconnect wouldn't measure
anything meaningful, unlike a real queued message).

Since `send`/`dm`/`ping`'s `packet_id` comes back immediately in the request response while
delivery is a separate, asynchronously pushed event, `_cmd_ping()` registers
`_pending_pings[packet_id] = future` and `_handle_event()`'s `delivery` branch resolves that future
*instead of* printing the generic "✓ Доставлено" line when a match is found — `/ping` renders its
own line (with the elapsed time and hop count) so the same delivery doesn't get reported twice.

### Statistics and per-sender history (`/stats`, folded into `/who <name>`)

`/last <name>` used to be its own command; it's now the tail end of `/who <name>` instead (an
*exact*, case-insensitive match on `long_name`/`short_name` — unlike `/search`'s substring match,
the intent is "this specific sender", not "reminds me of" — matched against the resolved display
name `_pick_node()` returned, not the raw typed query, since that's what log lines are actually
written under). Both it and `/stats` read only from `logs/` (no device round-trip) and share the
walk-newest-first/limit/ordering logic in `_scan_units()` — the same helper `/search` uses,
parameterized by a match predicate. `/stats` computed via `_collect_stats()` runs in
`asyncio.to_thread` since walking every log file in a long-lived install can take a moment and
would otherwise stall the prompt; it defaults to the Primary channel (`_channel_filter or
"Primary"`) rather than every channel mixed together — an unfiltered aggregate conflated unrelated
groups' traffic into one meaningless total — but still respects an explicit `/ch` switch to some
other channel, since that's already a deliberate one-channel view.

### Local node settings (`/settings`)

Reads/writes go through the local node's own `localConfig`/`moduleConfig` (`node.writeConfig()`),
which is safe to read-modify-write in place because the interface's connection handshake already
streams the device's *entire* current config into those objects before `_do_connect()` returns —
setting one field and calling `writeConfig(section)` pushes back the real current values for
everything else in that section, not blank defaults. `SETTINGS_REGISTRY` in `mesh_logger.py` is a
deliberately small curated whitelist (not a generic protobuf-path get/set) — full rationale and
the parameter table are in `SETTINGS.md`, but the short version: it excludes `network` (WiFi
credentials; changing it while connected over that same wifi link could sever the connection —
still excluded even for a usb/ble session, to keep the registry's behavior independent of
`CONN_TYPE`) and `bluetooth`/`security` (keys/PINs, not something to expose over a plaintext local
socket).

MQTT (`mqtt_*` keys — bridges LoRa traffic to services like OneMesh over the internet) mostly
fits the same registry mechanism (`moduleConfig.mqtt`, `is_module=True`), plus a `"str"` kind in
`_parse_setting_value()`/`SETTINGS_REGISTRY` for free-text fields (address/username/password/root)
alongside the existing bool/int/enum. `mqtt_password` is the one field that's write-only in
effect: `_apply_setting()` and `_settings_snapshot()` both substitute `MQTT_PASSWORD_MASK` for the
real value on the way out, so a plaintext password set once is never echoed back over the IPC
socket or shown in `/settings`. `mqtt_uplink`/`mqtt_downlink` don't fit the registry's
(config-section, field) shape at all — `uplink_enabled`/`downlink_enabled` live on
`ChannelSettings` (per-channel, via `node.writeChannel()`), not `moduleConfig` — so they're
special-cased in `_apply_setting()` (alongside `owner_long`/`owner_short`) with a
`<channel_name>:on|off` value format, and `_settings_snapshot()` shows a summary across every
non-disabled channel (`_channel_links_summary()`) instead of a single scalar.

Every admin message (`writeConfig`, `setOwner`, `reboot`) needs a per-session passkey the
firmware hands out on request — but the library's own `node.ensureSessionKey()` only *fires*
that request; the reply lands later, asynchronously, via `_onAdminReceive()` on meshtastic's
pubsub thread. Sending the admin packet immediately after (as the library itself does inside
`setOwner()`, and as a first-time `/settings`/`/reboot` naturally would) races that reply: without
the key, the firmware just drops the packet — no exception, no NAK, nothing to catch. This was
the actual cause of `/reboot confirm` reporting success while the device never rebooted.
`_ensure_session_key()` in `mesh_logger.py` closes the race by blocking (via `time.sleep` — it
always runs inside the same `asyncio.to_thread` call as the admin action it precedes, same as
`_apply_setting`) until `_has_session_key()` sees the passkey cached or `SESSION_KEY_TIMEOUT`
elapses; on timeout it proceeds anyway rather than inventing a new error path; some setups may
not need a passkey at all, in which case this is a no-op wait that costs nothing.

### Rebooting the node (`/reboot`)

`cmd == "reboot"` calls the meshtastic library's `node.reboot(REBOOT_DELAY_SECS)` (an admin
message, same family as `writeConfig`) via `asyncio.to_thread` for the same blocking-socket-write
reason as everything else that talks to the device. This is the deliberately disruptive sibling
of `/settings` — some config sections (mainly `lora`) only take effect after a reboot, so
`/reboot` is also how a user applies those. `mesh_chat.py` gates it behind a two-step confirm
(`_cmd_reboot`): a bare `/reboot` only arms a `REBOOT_CONFIRM_WINDOW`-second window (module
global `_reboot_confirm_deadline`, checked against `_loop.time()`) and prints a warning; only
`/reboot confirm` inside that window actually sends the IPC request. Two separate commands
rather than a y/n prompt so the handler doesn't need access to the `PromptSession` object created
in `main()`. The reboot drops the TCP connection like any other disconnect — the logger's normal
reconnect loop (`on_connection_lost` → `_reconnect_loop`) picks it back up with no special-casing.

### Manual reconnect (`/reconnect`)

The automatic reconnect logic above (connection.lost → `_reconnect_loop`, plus the silence
watchdog) only ever triggers itself — there was no way for a user to say "reconnect now" from
`mesh_chat.py`. `/reconnect [host]` closes that gap: it must work **regardless of what the logger
currently believes its own state is** — connected-and-fine, or already mid-backoff for an earlier,
unrelated drop — and optionally redirect the connection to a different address (a device's DHCP
lease changing is the main reason to want this).

The implementation deliberately does *not* spawn a second, parallel connect attempt alongside
whatever `_reconnect_loop` might already be doing — two coroutines racing to assign `_interface`
would be a real bug. Instead, `_trigger_hard_reconnect(host)` signals the *same* loop task:

- `_hard_reconnect_event` (parallel to `_reconnect_event`) interrupts an in-progress backoff sleep.
  `_reconnect_loop`'s sleep is `await asyncio.wait_for(_hard_reconnect_event.wait(), timeout=delay)`
  instead of a bare `asyncio.sleep(delay)`, so setting the event wakes it immediately instead of
  waiting out however much of the exponential-backoff delay (up to 60s) was left.
- `_reconnect_event` is set too, covering the case where the loop is currently idle at the outer
  `await _reconnect_event.wait()` (nothing has told it anything is wrong yet) — without this, a
  hard reconnect while the logger thinks it's happily connected would do nothing.
- When `_hard_reconnect_event` is observed set at the top of an inner-loop iteration, `delay` is
  forced to `0` — a manual reconnect always tries immediately, never waits out backoff at all, not
  even the first attempt's normal `delays[0]` (3s).

`_hard_reconnect_host` overrides `HOST` in `_open_interface()`'s wifi branch only (checked via
`CONN_TYPE == "wifi"` in the IPC handler; USB/BLE targets aren't addressed by hostname, so a
`host` argument there returns `err_reconnect_wifi_only` instead of being silently ignored). It's
sticky for the rest of the process's life once set, and gets persisted to `.env`'s `MESH_HOST` in
`_do_connect()` on the first successful connect that used it — same precedent as `/who`
self-updating `NODE_LONG_NAME` — so a later logger restart also starts from the new address
instead of the stale one.

The IPC `reconnect` command itself doesn't block waiting for the outcome (a real reconnect can
take several seconds — DNS + TCP handshake, or a full BLE scan). It fires the trigger and returns
`{"ok": True}` immediately; `mesh_chat.py` reports the eventual result the same way `/reboot`
does — via the existing `"device"` event (see below), not a synchronous response.

A failed attempt needs its own signal, though — unlike `/reboot` (which always either lands or the
device comes back regardless), a hard reconnect can keep failing indefinitely (wrong host, device
still down), and the *plain* automatic retry loop is silent on failure by design (no point
printing something every 3–60s during a long outage the client already knows about via the earlier
`"disconnected"` event). But a manually triggered attempt is a one-off action the user is actively
waiting on, so it gets an explicit answer either way: `_reconnect_loop` tracks whether the
*current* attempt was hard-triggered (`was_hard`, set both when `_hard_reconnect_event` was already
set going in, and when it fires mid-sleep), and on a `was_hard` failure broadcasts
`{"status": "reconnect_failed", "error": ...}` (`_last_connect_error`, stashed by `_do_connect()`'s
except block since it only returns a bool). `mesh_chat.py` renders this and nothing else — no
special-casing needed for "attempt 2 of 5", the logger keeps retrying automatically regardless.

### Device connection status (`"device"` event)

Without this, `/reboot confirm` was a UX dead end: the IPC response only confirms the admin
packet was *sent*, not that the device actually rebooted and came back — that whole cycle
happens entirely between `mesh_logger.py` and the device, invisible to any client, since a
client's Unix socket to the logger doesn't drop when the *device* link does. `on_connection_lost`
broadcasts `{"event": "device", "status": "disconnected"}`; `_do_connect()` broadcasts
`{"status": "connected", "long_name": ..., "short_name": ..., "node_id": ...}` on every successful
(re)connect, not just after a reboot — so it also covers a plain WiFi drop or a manual `systemctl
restart mesh-logger` mid-connection. `mesh_chat.py` prints both unconditionally (no channel/mute
filtering — this is link status, not a chat message). Absent a live client, `/who` or `/nodes`
still work as a manual check: they round-trip through the logger and report "not connected" until
the device is actually back.

The `"connected"` branch also refreshes `IPCClient.whoami`/`whoami_id` directly from the event's
(unescaped) fields, not just the `_safe()`'d copies used for the printed message. Before this, that
cache was only ever populated in `main()` (once, at client startup) and in `_reconnect_logger()`
(when the client's own socket to the *logger* reconnects) — neither covers a device-only reconnect
while the client's logger socket stayed up the whole time (e.g. via `/reconnect`), and if the
device wasn't reachable yet at client startup, `whoami` was never populated at all, permanently
breaking `/who` and the "is this my own message" color check in `_render_line()` even after the
device came back. `node_id` was added to the broadcast payload specifically to make this possible
without an extra IPC round trip.

### `/nodes` includes relay-only senders too

`_nodes_payload()` used to be built purely from `_interface.nodes` — so a sender the device only
ever knows via relayed text (no NodeInfo, the same class `_unresolved_ids` tracks — see Node name
resolution above) never appeared in `/nodes` or `/who <name>` at all, even once its name was
resolved via OneMesh and already visible in the chat log. Fixed by appending an entry for every id
in `_onemesh_names` not already covered by the `_interface.nodes` scan — no telemetry to offer for
these (`battery`/`snr`/`hops_away`/`seconds_ago`/position all `None`), and `has_user: False` so the
client falls back to its own name cache exactly like it already does for any other OneMesh-resolved
`!hex (???)` sender.

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

`mesh-logger.service` uses `Type=notify` + `WatchdogSec=150` (see the sd_notify watchdog note
above) — after editing the unit file, `sudo systemctl daemon-reload` before restarting.

### Backing up logs (`ops/`)

Both `logs/*.log` (chat history) and `mesh-logger.py`'s own runtime output only ever exist on
whichever machine runs the logger — for a server/Raspberry Pi deployment, that's a single point of
loss if the disk dies. `ops/` holds two scripts to pull a copy onto another machine (a laptop, say)
on demand, with no new dependency (`rsync` + `journalctl`, nothing else) and deliberately no
scheduled automation — this project doesn't get touched often enough to justify a systemd
timer/cron entry running forever for data that's only ever needed right before a debugging session:

- **`ops/mesh-log-export.sh`** — run manually on the server, whenever you actually want the
  process's own `[info]`/`[error]`/`[warn]` lines included in a backup. `journalctl -u mesh-logger`
  is a binary journal, not a plain file, so this dumps entries since the last export (tracked via a
  `service-logs/.last_export` timestamp file, falling back to the last 7 days on a first run) into
  `service-logs/mesh-logger.log` — a plain-text file `ops/sync-logs.sh` can then pull like any other.
- **`ops/sync-logs.sh`** — run manually on the backup destination (e.g. your laptop) right before
  you need a local copy. Pulls `logs/` and `service-logs/` from the server via `rsync -avz`
  (additive, no `--delete` — a briefly unreachable server just leaves the last successful mirror in
  place, not a partial/wiped one) into `./backup/`. Remote host/path and local destination are all
  overridable via env vars (`MESH_BACKUP_REMOTE`/`MESH_BACKUP_REMOTE_DIR`/`MESH_BACKUP_DEST`) so it
  works whether or not you've set up an SSH config alias.

One-time, machine-specific setup that's *not* in `ops/` because it's not something to run
repeatedly: authorizing an SSH key on the server (or reusing whichever key already works — check
`~/.ssh/known_hosts` / shell history for the host you connect to manually), and an optional
`~/.ssh/config` alias so `sync-logs.sh` needs no env vars.

`logs/`, `service-logs/`, and `backup/` are all gitignored — runtime output, generated fresh on
whichever machine runs the relevant script, never checked in.

## Fixed bugs and design decisions (session log)

A running log of non-obvious bugs found and fixed, and judgment calls made along the way — kept
so the reasoning behind them doesn't have to be rediscovered from scratch next time. Grouped by
theme, not chronologically.

### `/ping` was writing a visible message into the target's DM inbox

`/ping` used to send a real `TEXT_MESSAGE_APP` DM (`"🏓 ping"`, via the normal `dm` IPC command) just
to get a `wantAck` round trip — but that's an actual chat message the target's own Meshtastic app
displays and notifies on, which is rude to send to a node you're merely curious about (e.g. probing
someone else's node found via `/nodes` or `/who`). The mesh-layer ACK/NAK `/ping` measures doesn't
care about portnum — any unicast packet with `wantAck=True` gets one — so the fix
(`_send_ping_packet()` on `PRIVATE_APP`, see RTT measurement above) removes the text message
entirely rather than trying to make it less objectionable (e.g. a shorter or quieter text would
still be a message the recipient didn't ask for).

### Broadcast sends spammed "delivery not confirmed" as if something was wrong

`_send_message()` sets `wantAck=True` on every send regardless of destination, but Meshtastic only
has a real per-recipient ACK for a **unicast** (DM) packet — a broadcast has no single node
responsible for acknowledging it, only an unreliable "implicit ack" (the sender hearing a neighbor
rebroadcast it, which may just not happen on a busy or lossy channel). The client used to print the
same `delivery_unconfirmed` warning for a timed-out broadcast as for a timed-out DM, but for a
broadcast that timeout is the *normal*, expected outcome, not a signal anything failed — and on a
noisy channel (e.g. a community "Ping" channel with constant traffic) it fired on nearly every send,
reading as a string of alarming failures for something that most likely went out fine. Fixed by
tagging the `delivery` event with `is_dm` (`mesh_logger.py`'s `emit_delivery`) and only printing the
"not confirmed" line when it's true; a broadcast that times out now says nothing, same as it would
if the implicit ack happened to arrive a moment too late to matter. `not_delivered` (a real NAK, `ok
is False`) and `delivered` (`ok is True`) still print either way — those aren't ambiguous.

### Send-path: oversized messages and a delivery-tracking race

`sendData()` (which `sendText()` wraps) raises `MeshInterface.MeshInterfaceError("Data payload
too big")` **synchronously**, before touching the link at all, once the UTF-8-encoded text
exceeds `mesh_pb2.Constants.DATA_PAYLOAD_LEN` (233 bytes). `_send_message()` used to catch this
with the same broad `except Exception` as an actual link failure — a too-long message would
needlessly trip `_reconnect_event.set()` and get queued to the outbox, retried against a
perfectly healthy connection, and still never succeed. Fixed with a pre-check (`MAX_PAYLOAD_BYTES`)
that returns a clear error immediately, plus a dedicated `except MeshInterface.MeshInterfaceError`
before the generic link-failure handler.

Separately, `handler()` (the ACK callback passed to `sendText`) reads `pid_holder["pid"]` to know
which packet a `delivery` event is for — but that dict used to only get populated *after*
`await asyncio.to_thread(...)` returned control to the event loop. A very fast ACK (small hop
count, e.g. over USB) could fire before that assignment ran, reading an empty `pid_holder` and
leaving `/ping`'s waiting future unresolved until timeout. For reactions (`_send_emoji_packet`),
fixed completely — `pid_holder["pid"]` is now set right after the locally-generated packet id is
assigned, strictly before the response handler is even registered, so the ACK cannot possibly
arrive first. For plain text (`_send_text_tracked`), only narrowed — the id is generated inside
the meshtastic library itself, so the assignment happens as early as the worker thread allows
(immediately after `sendText()` returns) but not any earlier.

### Log format: names are as untrusted as message text

`sanitize_text()` (collapses embedded `\n` into ` ⏎ `) already existed for message text, but
`long_name`/`short_name` — which come from the same untrusted source, another node's broadcast
NodeInfo — went into `format_message_line()`/`format_quote_line()` unsanitized. Two concrete
consequences, both fixed by extending sanitization to both name fields:
- A newline embedded in a name could forge extra fake log lines, the same class of attack
  `sanitize_text()` already existed to prevent for message text (see `test_forged_line_injection`).
- A `)` in `short_name` broke `parse_log_line`'s `_MSG_RE`/`_QUOTE_RE` outright — their `short`
  capture group is `[^)]*`, so a `)` inside the value itself desyncs where the regex thinks the
  name/short boundary is. `_sanitize_short_name()` replaces `)` with `⟩` at write time.

**Known, deliberately unfixed**: a node's own `long_name` can contain a well-formed
`(<short>): ` substring (matching literal parens, not just a stray `)`) that hijacks the parser's
non-greedy name/short split — it finds the *first* syntactically valid boundary while scanning
left to right, not necessarily the intended one. A node could name itself e.g.
`"Notice (ADMIN): "` so that every message it ever sends displays as if sent by "ADMIN". This is
purely a display-layer identity-spoofing issue (no new physical log line gets forged, nothing
crashes) — closing it fully means disallowing literal `(`/`)` in `long_name`, which conflicts
with the existing, intentionally-tested behavior in `test_name_with_parentheses` (a name like
"Bob (admin)" is expected to round-trip with its real parens intact). Verified against ~12k lines
of real production logs (`/Users/qeewwu/Desktop/logs/`, Jul 2026): zero occurrences of this
pattern, malicious or accidental — left as-is by explicit decision, revisit if it's ever actually
exploited. (Those same logs *did* independently confirm `sanitize_text()`'s value: 790 lines
(6.5%) failed to parse before it reached production around 05–06 Jul — multi-line ping-bot
replies and one human-pasted shell command forging extra "lines" — and zero after.)

### Client-side name resolution gaps

`/dm`, `/trace`, `/ping`, `/who <name>` (`/pos` at the time), the old `/mute` (see Ignoring above —
since replaced by the firmware-level `/ignore`), and tab-completion all used to match node names
against the raw `nodes` IPC payload — which only ever carries a name if the mesh's own NodeInfo
reported one. A node visible in `/nodes` under its OneMesh-resolved display name (e.g. "Вася")
was unmatchable by that exact name anywhere else — only by its raw `!hex` id. `/nodes` already
computed the fix inline (`display_long`/`display_short`); factored it out as `_enrich_nodes()`
and applied it everywhere a node list is matched or rendered, including `_update_node_completions`.

### `--channel` at startup when the device isn't connected yet

`mesh_chat.py --channel <name>` used to `sys.exit(1)` if the given channel name didn't resolve —
but `_channels_payload()` degrades to a single fake `"Primary"` entry whenever `_interface` is
`None`, indistinguishable from a genuine typo. A client started while the logger was still
mid-reconnect (or the device was simply off) would hard-exit on a channel that actually exists.
Fixed with a `_channel_resolved` flag: if the device wasn't connected at the "channels" lookup,
the given name is accepted provisionally (sending defaults to Primary meanwhile) and
`_try_resolve_pending_channel()` re-checks it for real off the next `device: connected` event —
falling back to "all channels" only if it's still not found once the device actually answers.

### Reconnect-to-logger race

`_reconnect_logger()` used to assign `client.on_disconnect` *after* `await client.connect()`
returned. A drop in that window (read_loop's `finally` runs with `on_disconnect` still `None`)
went completely unnoticed — `_reconnecting` never got reset, so nothing would ever retry, and the
client would sit silently disconnected until manually restarted. Fixed by assigning
`on_disconnect` before `connect()`, plus a defensive check right after the post-connect `whoami`
round trip (in case the connection dropped again during that handshake, while `_reconnecting` was
still `True` and would have swallowed a real disconnect event as a no-op).

### OneMesh resolution missed relayed-only senders

`_run_onemesh_update()` only ever scanned `_interface.nodes` for un-named targets — but a node the
device has merely *relayed* a text message from, without ever receiving that node's own NodeInfo
packet, never gets an entry in `_interface.nodes` at all. Such senders stayed `!hex (???)` forever,
and `/updatenames` reported "nothing to update" even while they were visibly unresolved in the chat
— there was no target list that included them. Fixed by having `_node_names()` record every id it
falls back to `!hex (???)` for into `_unresolved_ids`, and having `_run_onemesh_update()` treat that
set as extra targets alongside the `_interface.nodes`-without-`user` case (see Node name resolution
above).

### `/ch` showed too little history after switching

`/ch <channel>` used to re-print only the last 10 messages of that channel (`CH_SWITCH_HISTORY`),
far less than the 100 shown at client startup (`HISTORY_SIZE`) — jarring when switching into a
channel you haven't seen yet this session. There was no reason for the two to differ, so
`CH_SWITCH_HISTORY` was removed and `_print_initial_history()` is now called with its default
(`HISTORY_SIZE`) in both places.

### Command cleanup: `/botping`, `/autoping` removed; `/last` folded into `/who`

A review of every command's actual usefulness (not a bug fix, a judgment call): `/botping` (auto-
reply bot on the Ping channel) and `/autoping` (periodic liveness canary into the same channel)
were both removed outright — their entire purpose was broadcasting extra traffic onto the mesh,
which is the opposite of what you want on a shared, bandwidth-constrained LoRa network, and neither
saw real use. All the plumbing came out together: the `_botping_enabled`/`_autoping_interval_min`/
`_autoping_text_override` globals and their `.env` keys (`BOTPING_ENABLED`, `AUTOPING_INTERVAL_MIN`,
`AUTOPING_TEXT`), `PING_CHANNEL`/`PING_CHANNEL_NAME` and `_ping_channel_index()` (nothing else used
it), the `"botping"`/`"autoping"` IPC commands, and both `mesh_chat.py` command handlers. `/last`
wasn't removed for the same reason — it stayed useful — but its output was a strict subset of what
`/who <name>` already needed to show anyway (see Node lookup above), so it was folded in rather than
kept as a separate command with overlapping by-name lookup logic.

### `_store_msg` dict/deque desync

`_store` (dict) and `_store_order` (bounded deque, drives eviction) were meant to stay in
lockstep, one entry each per tracked `packet_id`. Re-inserting an *already-tracked* `packet_id`
(duplicate delivery, or a rare id collision) appended a second copy to the deque without a
matching second dict entry — when the older copy of that duplicate reached the front and got
evicted, it deleted the (still fresh) record out from under the newer copy, causing premature
loss of quote-line data well before `STORE_SIZE` messages had actually passed. Fixed: an update
to an already-tracked key now skips the `_store_order.append()` entirely.

### IPC readline limit broke `/nodes` on large meshes

`IPCClient.connect()` called `asyncio.open_unix_connection()` with no `limit=`, so its
`StreamReader` used the default 64 KiB cap on a single `readline()`. The `nodes` response is one
JSON line listing every known node — on a real ~369-node mesh that line ran to ~83 KB, already
over the limit. `reader.readline()` in `_read_loop()` then raised `asyncio.LimitOverrunError`,
which fell through the narrow `except Exception` around `json.loads` into the outer one wrapping
the whole loop — tearing down the *entire* IPC connection, not just failing the one request:
`finally` set `connected = False`, resolved every in-flight request as `{"ok": False, ...}`, and
fired `on_disconnect()`. `/reconnect` looked like a fix because it resets the *device's* node
table, so the next `/nodes` response was briefly small enough — coincidence, not a cure. Found by
Claude Cowork, reproduced live against the production server (confirmed by the client itself
disconnecting mid-`/nodes`, twice, before `/reconnect`). Fixed by passing a much larger shared
`IPC_LINE_LIMIT` (`mesh_common.py`) to both `open_unix_connection` and `start_unix_server` — the
logger's own `_handle_client` reads client requests the same way and was equally exposed, even
though incoming requests are small enough in practice that it wasn't observed failing.
Separately, `_cmd_nodes` printed the same "Нет данных об узлах" for every `ok: False`, so this
failure mode had no distinguishing error message on screen; it now surfaces `resp["error"]`.

### Offline send/dm outbox skipped the oversized-message check

`_send_message()` (used while the device is connected) rejects a message over
`MAX_PAYLOAD_BYTES` (233 bytes, `mesh_pb2.Constants.DATA_PAYLOAD_LEN`) before it ever reaches the
link. But `send`/`dm` while the device is *disconnected* go through a separate branch in
`_handle_client` that appends straight to `_outbox` with no size check at all, replying
`{"ok": true, "queued": true}`. The oversized message then sat in the outbox until the device
reconnected and `_flush_outbox()` tried and failed to send it — the user found out possibly hours
later, and only via a delivery-failure event instead of an immediate error. Fixed by adding the
same `MAX_PAYLOAD_BYTES` check ahead of the `_outbox.append()` in the offline branch.

### Invalid-utf8 text messages logged as empty strings

The meshtastic library's own `_onTextReceive` omits the `"text"` key from `decoded` entirely
(and just logs "Malformatted utf8 in text message: ...") when a packet's payload isn't valid
utf-8. `on_receive()` read it as `decoded.get("text", "")`, which can't tell "no key" apart from
"really is an empty string" — either way it wrote and broadcast a normal-looking message with
empty text, a permanent, meaningless line in the log for every affected packet. Fixed by checking
`"text" not in decoded` first and dropping the packet (with a `[warn]` line to stderr) instead of
falling through with an empty string.

All three found by Claude Cowork during a testing session (2026-07-15); verified against this
codebase and the first one reproduced live over Tailscale/SSH against the production server
before fixing.

### Tab-completed quoted names never matched

`_update_node_completions` wraps a node's long name in `"..."` when it contains a space (e.g.
`"Meshtastic 6914"`), so completing it inserts one shell-like token instead of several words —
the completer's own matching (`MeshCompleter.get_completions`) already stripped those quotes
before comparing. But `_find_node_in_list`, the shared matcher behind `_pick_node` (and so behind
`/who`, `/dm`, `/trace`, `/ping`, `/ignore`, `/unignore`), never did — it compared the literal
quoted string against unquoted node names and always came up empty, even for a name that would
otherwise match exactly. Tab completion was the only way to hit this (typing the same name by
hand without quotes worked fine, since this REPL isn't a shell and doesn't need quoting for a
multi-word argument — `args.split(None, 1)` already keeps the whole rest of the line together),
which is why it looked like an intermittent "node not found" rather than a deterministic bug.
Fixed by stripping one surrounding `"..."` pair in `_find_node_in_list` itself, matching what the
completer already assumed.

### Miscellaneous reliability

- `_broadcast_event()` pushed to clients sequentially, each bounded by `CLIENT_SEND_TIMEOUT` — one
  stuck client could delay live-message delivery to everyone *after* it in iteration order by up
  to that timeout, compounding with more stuck clients. Switched to `asyncio.gather` so all
  per-client pushes run concurrently.
- `_watchdog()` silently no-op'd forever, with zero indication why, if the installed `meshtastic`
  library predates `sendHeartbeat()` — silent-link detection would be permanently disabled.
  Added a one-time (not per-cycle) `[warn]` line to stderr.
- `/settings <key> <value>`'s default 10s IPC timeout was tight against the logger's own session-key
  wait (up to `SESSION_KEY_TIMEOUT`) plus a synchronous `writeConfig()` round trip — bumped to 20s
  (`SETTINGS_SET_TIMEOUT`) for the `set` action specifically.
- `/trace` didn't pass the session's active channel filter, unlike `/dm`/`/ping` — now does.
