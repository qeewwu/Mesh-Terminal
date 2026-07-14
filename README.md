# Mesh-Terminal

A terminal chat client for [Meshtastic](https://meshtastic.org/) mesh radio networks — over WiFi, USB serial, or Bluetooth LE.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

*(Русская версия: [README.ru.md](README.ru.md))*

> **Note:** the client's own messages and prompts default to Russian — see the screenshots-in-text below for what that actually looks like. Set `MESH_LANG=en` in `.env` for an English interface instead (see [Configuration](#configuration)). Slash-command *names* (`/dm`, `/reply`, ...) are plain ASCII either way.

## About

Mesh keeps a permanent, searchable log of your mesh traffic and gives you a fast terminal UI for reading and writing to it — channels, direct messages, real Meshtastic replies with quotes, reactions, node diagnostics, and remote device settings, all from the keyboard.

It's built as two small, independent programs instead of one:

- **`mesh_logger.py`** — a background daemon that holds the connection to your Meshtastic device, writes every message to a daily log file, and keeps running whether or not you have a terminal open.
- **`mesh_chat.py`** — the interactive client you actually type into. It never touches the device directly; it talks to the logger over a local socket, so you can start and stop it as often as you like — close your laptop, reopen it tomorrow, your history is still there.

```
                    device link          Unix socket
Meshtastic ◄─────────────── mesh_logger.py ◄─────────── mesh_chat.py
 device        (WiFi/USB/BLE)     │                      (start/stop freely,
                                  ▼                       run several at once)
                              logs/*.log
```

This split exists because a Meshtastic device only accepts **one connection at a time**, no matter the transport. By dedicating one long-running process to that connection, the interactive client is free to restart, crash, or simply not be running yet — nothing is lost, and several terminal sessions can share the same live feed.

## What it can do

- **Messaging** — all channels at once, or filtered to one; direct messages; real Meshtastic replies with quoted context (`/reply`, visible in official clients too); tapback reactions (`/react`)
- **History & search** — logs kept per day automatically, full-text search, per-sender history, message statistics
- **Network diagnostics** — node list with battery/SNR/hop count/online status, position and distance/bearing to a node, traceroute, ping (round-trip time)
- **Device control** — read and change your node's own settings remotely, reboot it, toggle an auto-reply ping-bot
- **Reliability** — messages sent while the device is offline queue up and go out on reconnect; both the logger↔device and client↔logger links auto-reconnect; delivery is confirmed with ✓/✗
- **Quality of life** — node names auto-resolved from the public [OneMesh](https://map.onemesh.ru/) map, muting for noisy senders, Tab-completion for commands/names/channels, interface language switchable between Russian and English (`MESH_LANG`)

## Chat

```
Primary [14:02:11] Вася Пупкин (VP): всем привет | 2
  ┆ [14:02:11] Вася Пупкин (VP): всем привет
Primary [14:02:34] 👍 Коля → [14:02:11] Вася Пупкин: «всем привет»
Primary [14:05:02] DM Петя (PT): встречаемся в 18:00 | 1
```

Run without any flags and every channel shows up at once, tagged with its name; sending goes to the Primary channel. Run with `--channel <name>` (e.g. `--channel ping`) and the session narrows to just that channel — the prefix disappears since it's implied, and your messages go out on it too.

Replying keeps the conversation threaded:

```
> /reply
─── На что можно ответить (#1 — самое свежее) ───
  #1 [14:05] Петя: встречаемся в 18:00
  #2 [14:02] Вася Пупкин: всем привет

> /reply #1 хорошо, буду
Отвечаю на: [14:05] Петя — встречаемся в 18:00
```

The reply routes itself based on what you're replying to — an incoming DM gets a DM back, a channel message gets a channel reply — so you never have to think about it. Only messages received since the client started are replyable (the on-disk log doesn't carry Meshtastic packet IDs).

## Nodes & network diagnostics

```
> /nodes
─── Видимые узлы ───
  Вася Пупкин (VP) 🔋82% SNR:6.5 · 2 хопа · Онлайн
  Петя (PT) 🔋54% SNR:-3.2 · 1 хоп · 12м назад

> /trace Вася
Трассирую до Вася Пупкин — может занять до 45с...
Маршрут туда: Я (ME) (?dB) → Relay (RL) (+5.0dB) → Вася Пупкин (VP) (+3.2dB)
Маршрут обратно: Вася Пупкин (VP) (?dB) → Relay (RL) (+2.8dB) → Я (ME) (+4.1dB)

> /pos Вася
📍 Вася Пупкин (VP): 55.75800, 37.61730, 180 м
   от меня: 4.2 км, азимут 132° (ЮВ)

> /ping Вася
🏓 Пингую Вася Пупкин (VP)...
🏓 Вася Пупкин (VP): доставлено за 2.3с, 2 хопа
```

`/nodes [online|names|hops]` sorts by whichever key you care about, with the other two breaking ties.

## History, search & stats

```
> /search разбудите
─── Поиск: «разбудите» ───
ping [18:44:02] Я (ME): разбудите меня если что | 0

> /stats
─── Статистика ───
  Всего сообщений: 8349 за 13 дн.
  Самый активный час: 18:00 (612 сообщений)
```

`/search` looks across the whole history on disk (respecting the active channel filter, if any); `/last <name>` narrows to one sender's recent messages; `/stats [day|node]` breaks the numbers down by day or by top talkers.

## Device settings

```
> /settings
─── Настройки узла ───
  role = CLIENT  Роль узла в сети (CLIENT, ROUTER, REPEATER, ...)
  hop_limit = 3  Максимум хопов для пакетов, отправленных с этого узла
  position_broadcast_secs = 900  Как часто рассылать позицию, секунды

> /settings hop_limit 4
✓ hop_limit = 4 (некоторые параметры требуют /reboot, чтобы вступить в силу)
```

A deliberately curated set of safe-to-expose settings — WiFi credentials, Bluetooth PINs, and security keys are intentionally excluded. Full parameter reference in [`SETTINGS.md`](SETTINGS.md). `/reboot` (with a required `/reboot confirm` inside a time window) applies settings that only take effect after a restart.

## Reliability

```
> hello from offline mode
⏳ В очереди: «hello from offline mode» — устройство офлайн, отправлю сразу при переподключении
```

If the device drops out — reboots, WiFi hiccups — nothing you send is lost: it queues and goes out the moment the connection is back. The client itself reconnects to the logger the same way if the logger process restarts, and replays whatever you missed.

Reconnection is otherwise automatic, but if it ever needs a nudge — or the device's IP changed — `/reconnect [host]` forces a fresh connection attempt immediately, regardless of what the logger currently thinks its state is:

```
> /reconnect
🔌 Forcing a reconnect to the device

> /reconnect 192.168.1.50
🔌 Reconnecting to 192.168.1.50...
```

## Quick start

**Requirements:** Python 3.10+, and a Meshtastic device reachable over WiFi, USB, or Bluetooth LE.

```bash
git clone <this repo>
cd Mesh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional — sensible defaults apply if you skip this
```

By default the client connects over WiFi to `meshtastic.local`. For USB or BLE, set `MESH_CONN_TYPE=usb` or `MESH_CONN_TYPE=ble` in `.env` (see [Configuration](#configuration) below) — on a machine with exactly one Meshtastic device attached, that's all you need.

Two processes, always start the logger first:

```bash
python3 mesh_logger.py   # background daemon — start first, leave running
python3 mesh_chat.py     # interactive client — start/stop freely
```

```bash
python3 mesh_chat.py                  # all channels, sends go to Primary
python3 mesh_chat.py --channel ping   # only the "ping" channel (short form: --ping)
python3 mesh_chat.py --history 200    # show more history on startup
```

The client shows the last 100 messages on startup (`--history` to change that), then live traffic. `Ctrl+C` / `Ctrl+D` to quit — the logger keeps running.

### Running the logger as a service

For a machine that's always on (a server, a Raspberry Pi), run the logger under systemd instead of a terminal:

```bash
sudo cp mesh-logger.service /etc/systemd/system/
# edit WorkingDirectory / ExecStart in the file to match your setup first
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-logger
systemctl status mesh-logger
journalctl -u mesh-logger -f
```

After pulling new code, `mesh_chat.py` picks it up on its next run automatically; the logger needs an explicit restart:

```bash
git pull
sudo systemctl restart mesh-logger
```

## Commands

| Command | Description |
|---|---|
| `/nodes [online\|names\|hops]` | List visible nodes: battery, SNR, hops, online status / last seen |
| `/who` | Info about your own node |
| `/dm <name> <text>` | Direct message (matches by name; quote names containing spaces) |
| `/reply` | List the last 20 receivable messages, numbered #1 (newest)…#20 |
| `/reply <text>` / `/reply #N <text>` | Reply with a real quote — to the latest message, or to #N |
| `/react <emoji>` / `/react #N <emoji>` | Send a tapback reaction |
| `/ch [name\|all]` | Show channels, or switch the session to one (or back to all) |
| `/send <channel> <text>` | One-off send to a channel without switching your session |
| `/search <text>` | Full-text search across all log history |
| `/last <name>` | Most recent messages from one sender (exact name match) |
| `/stats [day\|node]` | Message statistics: overview, by day, or by top senders |
| `/trace <name>` | Traceroute — SNR for each hop, both directions |
| `/ping <name>` | Round-trip time and hop count to a node |
| `/pos <name>` | Node position, distance and bearing from you |
| `/mute <name>` / `/unmute [name]` | Hide a sender from the live feed (search still finds them) |
| `/updatenames` | Force an immediate node-name resolution sweep via OneMesh |
| `/settings [key value]` | View or change local node settings — see [`SETTINGS.md`](SETTINGS.md) |
| `/botping 0\|1` | Enable/disable the auto-reply ping-bot |
| `/reboot` / `/reboot confirm` | Reboot the node (two-step confirmation) |
| `/reconnect [host]` | Force the logger to redial the device now, optionally at a new address (WiFi only) |
| `/clear` | Clear the screen |
| `/help` | Command help |

`Tab` completes commands, node names, and channel names.

## Configuration

All settings live in `.env` (copy from `.env.example`); every key is optional and falls back to a sensible default.

| Key | Default | Purpose |
|---|---|---|
| `MESH_CONN_TYPE` | `wifi` | `wifi` \| `usb` \| `ble` — how the logger connects to the device |
| `MESH_LANG` | `ru` | `ru` \| `en` — interface language (messages, bot replies, settings descriptions) |
| `MESH_HOST` | `meshtastic.local` | Device address for WiFi |
| `MESH_USB_PORT` | *(auto-detect)* | Serial port for USB, e.g. `/dev/ttyUSB0` |
| `MESH_BLE_ADDRESS` | *(auto-detect)* | MAC/UUID for BLE |
| `PING_CHANNEL` | `Ping` | Channel name the ping-bot and auto-ping canary use |
| `BOTPING_ENABLED` | `0` | Managed by `/botping 0\|1` — no need to edit by hand |
| `NODE_LONG_NAME` / `NODE_SHORT_NAME` / `NODE_ID` | *(empty)* | Self-populated by `/who` — no need to edit by hand |

## Under the hood

| Component | Technology | Role |
|---|---|---|
| Language | Python 3.10+, `asyncio` | Both processes |
| Device protocol | [`meshtastic`](https://pypi.org/project/meshtastic/) | TCP / Serial / BLE interfaces to the radio |
| Terminal UI | [`prompt_toolkit`](https://pypi.org/project/prompt-toolkit/) | Interactive prompt, Tab-completion, colored output |
| IPC | Unix domain socket, newline-delimited JSON | Logger ⇄ client communication |

## Project layout

```
mesh_common.py        shared constants and log line format/parser
mesh_logger.py         background daemon: device connection + logging + IPC server
mesh_chat.py           interactive client: IPC socket + history + TUI
test_mesh_common.py    tests for the log line format/parser
mesh-logger.service    example systemd unit for mesh_logger.py
SETTINGS.md            full reference for /settings parameters (ru: SETTINGS.ru.md)
node_names_cache.json  /mute state
onemesh_cache.json     node names resolved via OneMesh (written by mesh_logger.py)
logs/                  daily chat logs, chat-YYYY-MM-DD.log
```

## Tests

```bash
python3 -m unittest test_mesh_common -v
```

---

Curious how any of this actually works under the hood, or want to contribute? [`CLAUDE.md`](CLAUDE.md) has the full internals — architecture rationale, protocol details, and known trade-offs.
