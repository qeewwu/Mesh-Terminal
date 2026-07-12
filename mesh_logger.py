#!/usr/bin/env python3
"""Lightweight always-on Meshtastic logger and IPC broker.

Holds the single connection to the Meshtastic device (WiFi/TCP, USB serial, or
BLE — see MESH_CONN_TYPE in .env), appends every text message to a daily log
file under logs/, and exposes a Unix socket so other processes (e.g.
mesh_chat.py) can send messages and query node info without needing their own
connection to the device.
"""

import asyncio
import collections
import contextlib
import datetime
import inspect
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from typing import NamedTuple

import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.protobuf import config_pb2
from meshtastic.util import to_node_num
from pubsub import pub

from mesh_common import (
    ACK_TIMEOUT_SECONDS,
    BLE_ADDRESS,
    BROADCAST_ADDR,
    CONN_TYPE,
    ENV_FILE,
    HOST,
    LOG_DIR,
    ONEMESH_CACHE_FILE,
    PING_CHANNEL_NAME,
    SOCKET_PATH,
    USB_PORT,
    current_log_file,
    format_message_line,
    format_quote_line,
    parse_env_file,
    update_env_file,
)

STORE_SIZE = 300
# no mesh traffic at all for this long → probe the link with a heartbeat
# (a silently dead WiFi/USB/BLE link doesn't always fire connection.lost)
SILENCE_TIMEOUT = 600
OUTBOX_MAX = 50  # queued sends while the device is disconnected
OUTBOX_RETRIES = 3  # per-message re-queue limit when sendText itself fails
# a client that stopped reading (suspended terminal) fills its socket buffer;
# past this timeout it is disconnected so it can't stall pushes to everyone else
CLIENT_SEND_TIMEOUT = 5
TRACE_TIMEOUT = 40  # a traceroute is a full round trip across the mesh, can be slow
WATCHDOG_PING_SECONDS = 60  # cadence for the watchdog loop; also used for sd_notify(WATCHDOG=1)
REBOOT_DELAY_SECS = 5  # gives our own ack response a moment to flush before the device reboots
SESSION_KEY_TIMEOUT = 5.0  # how long to wait for the admin session passkey handshake
ONEMESH_API = "https://map.onemesh.ru/api/v1/nodes/{}"
ONEMESH_DELAY = 0.25  # between OneMesh lookups, so a full sweep doesn't hammer the API
ONEMESH_UPDATE_INTERVAL = 30 * 60  # background resolution sweep: at startup, then every 30 min
AUTOPING_INTERVAL = 2 * 60 * 60  # health-check broadcast into the Ping channel

_interface = None
_loop: asyncio.AbstractEventLoop | None = None
_reconnect_event: asyncio.Event | None = None
_last_rx: float = 0.0  # time.monotonic() of the last packet of any kind
# /botping toggle (mesh_chat.py "botping" IPC cmd) — persisted to .env
# (BOTPING_ENABLED) so it survives a logger restart; loaded once at import,
# same as HOST/PING_CHANNEL_NAME.
_botping_enabled: bool = parse_env_file(ENV_FILE).get("BOTPING_ENABLED", "0") == "1"
BOTPING_MARKER = "🤖"  # prefix on our own bot replies — see _maybe_botping_reply


class MsgRecord(NamedTuple):
    long_name: str
    short_name: str
    text: str
    time_str: str = ""


# имена и текст пакетов приходят с чужих узлов — не даём escape-последовательностям
# попадать в stdout/journald (в лог-файлы пишется как есть, их чистит клиент)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _console(s: str) -> str:
    return _CTRL_RE.sub(" ", s)


_store: dict[int, MsgRecord] = {}
_store_order: collections.deque[int] = collections.deque(maxlen=STORE_SIZE)

# Connected IPC clients, for pushing live message events with no polling delay
_clients: dict[asyncio.StreamWriter, asyncio.Lock] = {}

# Sends attempted while the device was disconnected; flushed once it reconnects
_outbox: collections.deque[dict] = collections.deque()

# node_id -> (long_name, short_name) resolved via OneMesh for nodes the mesh
# itself hasn't given us a NodeInfo for yet. Persisted to ONEMESH_CACHE_FILE.
# Feeding this into _node_names() (not just a display-layer lookup, unlike the
# old mesh_chat.py version of this) is the whole point of living here: it's
# what actually lands in freshly-written log lines instead of `!hex (???)`.
_onemesh_names: dict[int, tuple[str, str]] = {}


def _load_onemesh_cache() -> None:
    global _onemesh_names
    if not ONEMESH_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(ONEMESH_CACHE_FILE.read_text(encoding="utf-8"))
        _onemesh_names = {int(k): (v["long"], v["short"]) for k, v in raw.items()}
    except Exception:
        _onemesh_names = {}


def _save_onemesh_cache() -> None:
    try:
        raw = {str(k): {"long": v[0], "short": v[1]} for k, v in _onemesh_names.items()}
        ONEMESH_CACHE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception:
        pass


def _fetch_onemesh_name(node_id: int) -> tuple[str, str] | None:
    req = urllib.request.Request(
        ONEMESH_API.format(node_id),
        headers={"User-Agent": "mesh-logger/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    node = data.get("node")
    if not node:
        return None
    long_name = node.get("long_name") or f"!{node_id:08x}"
    short_name = node.get("short_name") or "???"
    return long_name, short_name


def _run_onemesh_update() -> dict:
    """Synchronous — scans the live NodeInfo table for nodes the mesh hasn't
    told us a name for, and asks OneMesh for each not already cached. Call via
    asyncio.to_thread (blocking network I/O). Runs at startup and every
    ONEMESH_UPDATE_INTERVAL automatically (_periodic_onemesh_update), and once
    on demand via the "updatenames" IPC command (mesh_chat.py's /updatenames)."""
    if not _interface or not _interface.nodes:
        return {"resolved": 0, "targets": 0}
    targets = []
    for nid, node in list(_interface.nodes.items()):
        num = nid if isinstance(nid, int) else int(str(nid).lstrip("!"), 16)
        if not node.get("user") and num not in _onemesh_names:
            targets.append(num)

    resolved = 0
    for nid in targets:
        result = _fetch_onemesh_name(nid)
        if result:
            _onemesh_names[nid] = result
            resolved += 1
        time.sleep(ONEMESH_DELAY)

    if resolved:
        _save_onemesh_cache()
    return {"resolved": resolved, "targets": len(targets)}


async def _periodic_onemesh_update() -> None:
    while True:
        result = await asyncio.to_thread(_run_onemesh_update)
        if result["resolved"]:
            print(f"[info] onemesh: resolved {result['resolved']}/{result['targets']} name(s)")
        await asyncio.sleep(ONEMESH_UPDATE_INTERVAL)


def _store_msg(packet_id: int, record: MsgRecord) -> None:
    if len(_store_order) == STORE_SIZE and _store_order[0] in _store:
        del _store[_store_order[0]]
    _store[packet_id] = record
    _store_order.append(packet_id)


def _node_names(node_id: int) -> tuple[str, str]:
    hex_id = f"!{node_id:08x}"
    if _interface and _interface.nodes:
        node = _interface.nodes.get(node_id) or _interface.nodes.get(hex_id)
        if node and "user" in node:
            u = node["user"]
            # both fields present is the common case for a node the mesh
            # already knows about — prefer it over a (possibly stale) OneMesh
            # cache entry; an incomplete pair falls through to OneMesh instead
            ln, sn = u.get("longName"), u.get("shortName")
            if ln and sn:
                return ln, sn
    cached = _onemesh_names.get(node_id)
    if cached:
        return cached
    return hex_id, "???"


def _channel_name(index: int) -> str:
    try:
        channels = _interface.localNode.channels
        if channels and 0 <= index < len(channels):
            name = channels[index].settings.name
            if name:
                return name
    except Exception:
        pass
    return "Primary" if index == 0 else f"Channel{index}"


def _channels_payload() -> list[dict]:
    out = []
    try:
        channels = _interface.localNode.channels or []
    except Exception:
        channels = []
    for ch in channels:
        role = ch.role
        if role == 0:  # DISABLED
            continue
        out.append({"index": ch.index, "name": _channel_name(ch.index)})
    if not out:
        out.append({"index": 0, "name": "Primary"})
    return out


def _ping_channel_index() -> int | None:
    """None if the device has no channel named PING_CHANNEL_NAME (bot simply
    never fires — channel_index never equals None) or channels aren't known yet."""
    try:
        channels = _interface.localNode.channels or []
    except Exception:
        return None
    for ch in channels:
        if ch.role != 0 and ch.settings.name.lower() == PING_CHANNEL_NAME.lower():
            return ch.index
    return None


def _set_botping(enabled: bool) -> None:
    global _botping_enabled
    _botping_enabled = enabled
    with contextlib.suppress(Exception):
        update_env_file(ENV_FILE, {"BOTPING_ENABLED": "1" if enabled else "0"})


async def _send_botping_reply(reply_id: int, channel_index: int, hops: int) -> None:
    word = "хоп" if hops == 1 else "хопа" if 2 <= hops <= 4 else "хопов"
    text = f"{BOTPING_MARKER} {hops} {word} от вас"
    await _send_message("send", text, channel_index, reply_id=reply_id)


def _maybe_botping_reply(from_id: int, packet_id: int, channel_index: int,
                          is_dm: bool, is_reply: bool, text: str, hops: int) -> None:
    """Called from on_receive() (meshtastic's callback thread) — only ever
    schedules work onto the event loop, never sends directly from here."""
    if not _botping_enabled or is_dm or not packet_id:
        return
    if channel_index != _ping_channel_index():
        return
    if _interface and from_id == _interface.myInfo.my_node_num:
        return  # никогда не должно сработать (свои пакеты не приходят через on_receive), но дёшево перестраховаться
    # без этой проверки два узла с включённым botping отвечали бы друг другу
    # бесконечно: сообщение с маркером — это уже чей-то ответ бота, не исходное
    if is_reply or text.strip().startswith(BOTPING_MARKER):
        return
    if _loop:
        _loop.call_soon_threadsafe(
            lambda: _loop.create_task(_send_botping_reply(packet_id, channel_index, hops)))


async def _periodic_autoping() -> None:
    """Health-check heartbeat into the Ping channel: unrelated to /botping (runs
    regardless of whether the reply-bot is toggled on) — the point is a visible
    "the mesh link is alive" canary, for us and for anyone/anything listening on
    that channel. Sleeps first so a logger restart doesn't immediately re-ping."""
    while True:
        await asyncio.sleep(AUTOPING_INTERVAL)
        if not _interface:
            continue
        idx = _ping_channel_index()
        if idx is None:
            continue
        try:
            await _send_message("send", "🏓 автопинг: проверка связи", idx)
        except Exception as e:
            print(f"[warn] autoping failed: {e}", file=sys.stderr)


# _write_message runs both on the meshtastic callback thread (on_receive) and
# the event loop thread (send/dm) — without a lock a quote+message pair could
# interleave with a concurrent write.
_log_lock = threading.Lock()


def _log(*lines: str) -> None:
    with _log_lock, open(current_log_file(), "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


async def _broadcast_event(event: dict) -> None:
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    for writer, lock in list(_clients.items()):

        async def _push():
            async with lock:
                writer.write(data)
                await writer.drain()

        try:
            # the timeout covers the lock too: a stuck client holds its lock in
            # drain(), and without a bound every later broadcast piles up on it
            await asyncio.wait_for(_push(), timeout=CLIENT_SEND_TIMEOUT)
        except Exception:
            _clients.pop(writer, None)
            with contextlib.suppress(Exception):
                writer.close()


def _broadcast_device_status(status: str, **fields) -> None:
    """Tells connected clients when the device link itself drops/comes back —
    e.g. after /reboot, or an ordinary connection.lost. Called from
    on_connection_lost (meshtastic's callback thread) and _do_connect() (runs
    via asyncio.to_thread), so always hops back onto the loop like _write_message
    does; a no-op if the loop isn't running yet (startup, before main() sets it)."""
    if not _loop:
        return
    event = {"event": "device", "status": status, **fields}
    _loop.call_soon_threadsafe(lambda: _loop.create_task(_broadcast_event(event)))


def _write_message(long_name: str, short_name: str, text: str, hops: int,
                    dm_tag: str = "", reply_to: MsgRecord | None = None,
                    channel_index: int = 0, meta: dict | None = None,
                    snr: float | None = None, time_str: str | None = None) -> None:
    # accepts an explicit time_str so callers can reuse the exact timestamp
    # they already stored in _store for this message (no clock-skew mismatch
    # between the message line and a later reply's quoted time)
    now = time_str or datetime.datetime.now().strftime("%H:%M:%S")
    lines = []
    if reply_to:
        lines.append(format_quote_line(reply_to.long_name, reply_to.short_name,
                                       reply_to.text, reply_to.time_str))
    lines.append(format_message_line(now, long_name, short_name, text, hops, dm_tag,
                                      channel_name=_channel_name(channel_index), snr=snr))
    _log(*lines)
    if _loop:
        # on_receive runs on meshtastic's background thread; schedule safely
        event = {"event": "message", "lines": lines}
        if meta:
            event.update(meta)
        _loop.call_soon_threadsafe(lambda: _loop.create_task(_broadcast_event(event)))


def _packet_id(sent) -> int:
    if sent is None:
        return 0
    if isinstance(sent, dict):
        return sent.get("id", 0)
    return getattr(sent, "id", 0)


# ── device pubsub callbacks ───────────────────────────────────────────────────

def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        text      = decoded.get("text", "")
        from_id   = packet.get("from", 0)
        to_id     = packet.get("to", BROADCAST_ADDR)
        packet_id = packet.get("id", 0)
        reply_id  = decoded.get("replyId", 0)
        is_emoji  = bool(decoded.get("emoji"))
        hop_limit = packet.get("hopLimit", 0)
        hop_start = packet.get("hopStart", hop_limit)
        hops      = hop_start - hop_limit
        is_dm     = (to_id != BROADCAST_ADDR)
        channel_index = packet.get("channel", 0)
        snr       = packet.get("rxSnr")

        long_name, short_name = _node_names(from_id)
        reply_to = _store.get(reply_id) if reply_id else None
        now = datetime.datetime.now().strftime("%H:%M:%S")
        if packet_id:
            _store_msg(packet_id, MsgRecord(long_name, short_name, text, now))

        # meta lets clients offer "/reply" on this message (packet id → replyId)
        # and render tapback reactions (emoji=True) compactly
        _write_message(long_name, short_name, text, hops,
                       dm_tag=("DM " if is_dm else ""), reply_to=reply_to,
                       channel_index=channel_index, snr=snr, time_str=now,
                       meta={"packet_id": packet_id, "from_id": from_id,
                             "is_dm": is_dm, "channel_index": channel_index,
                             "is_emoji": is_emoji})
        print(_console(f"[recv] {long_name} ({short_name}): {text}"))
        _maybe_botping_reply(from_id, packet_id, channel_index, is_dm,
                              bool(reply_id), text, hops)
    except Exception as e:
        print(f"[error] on_receive: {e}", file=sys.stderr)


def on_connection_lost(interface):
    print("[warn] connection lost, will reconnect", file=sys.stderr)
    _broadcast_device_status("disconnected")
    if _loop and _reconnect_event:
        _loop.call_soon_threadsafe(_reconnect_event.set)


def on_any_packet(packet, interface):
    # feeds the silence watchdog: nodeinfo/position/telemetry count as proof of life
    global _last_rx
    _last_rx = time.monotonic()


def _subscribe():
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_any_packet, "meshtastic.receive")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")


def _unsubscribe():
    for fn, topic in ((on_receive, "meshtastic.receive.text"),
                      (on_any_packet, "meshtastic.receive"),
                      (on_connection_lost, "meshtastic.connection.lost")):
        with contextlib.suppress(Exception):
            pub.unsubscribe(fn, topic)


# human-readable description of the configured target, for log messages —
# doesn't affect how _open_interface() actually connects
CONN_TARGET = {
    "wifi": HOST,
    "usb": USB_PORT or "auto-detected USB port",
    "ble": BLE_ADDRESS or "auto-detected BLE device",
}.get(CONN_TYPE, HOST)


def _open_interface():
    """Construct the interface for the configured MESH_CONN_TYPE. USB/BLE
    targets are optional (None) — the meshtastic lib then auto-detects the
    sole attached serial device / does a BLE scan and connects if exactly one
    Meshtastic device is found."""
    if CONN_TYPE == "usb":
        return meshtastic.serial_interface.SerialInterface(devPath=USB_PORT)
    if CONN_TYPE == "ble":
        return meshtastic.ble_interface.BLEInterface(address=BLE_ADDRESS)
    return meshtastic.tcp_interface.TCPInterface(hostname=HOST)


def _do_connect() -> bool:
    global _interface, _last_rx
    try:
        _interface = _open_interface()
        _subscribe()
        _last_rx = time.monotonic()
        my_id = _interface.myInfo.my_node_num
        ln, sn = _node_names(my_id)
        print(f"[info] connected via {CONN_TYPE} to {CONN_TARGET} as {ln} ({sn})")
        _broadcast_device_status("connected", long_name=ln, short_name=sn)
        if _loop and _outbox:
            _loop.call_soon_threadsafe(lambda: _loop.create_task(_flush_outbox()))
        return True
    except Exception as e:
        print(f"[error] connect failed: {e}", file=sys.stderr)
        return False


async def _reconnect_loop() -> None:
    delays = [3, 5, 10, 30, 60]
    attempt = 0
    while True:
        await _reconnect_event.wait()
        _reconnect_event.clear()
        while True:
            delay = delays[min(attempt, len(delays) - 1)]
            print(f"[info] reconnecting in {delay}s (attempt {attempt + 1})")
            await asyncio.sleep(delay)
            _unsubscribe()
            with contextlib.suppress(Exception):
                if _interface:
                    await asyncio.to_thread(_interface.close)
            # the interface constructor blocks for seconds (DNS+connect+config
            # wait for TCP, port open + handshake for USB/BLE) — in a thread
            # so IPC clients keep getting responses meanwhile
            if await asyncio.to_thread(_do_connect):
                # событие, взведённое во время попытки (watchdog, поздний
                # connection.lost старого интерфейса), относится к уже закрытому
                # соединению — без сброса свежий коннект тут же порвался бы.
                # Пропущенный реальный обрыв в этом окне подстрахует watchdog.
                _reconnect_event.clear()
                attempt = 0
                break
            attempt += 1


def _sd_notify(message: str) -> None:
    """Tell systemd (Type=notify) this process is ready/alive. A no-op outside
    systemd — NOTIFY_SOCKET is only set when the unit actually requests it."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # Linux abstract socket namespace
    with contextlib.suppress(Exception):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())


async def _watchdog() -> None:
    """Probe the device link when the mesh has been silent for too long, and
    ping systemd's watchdog every cycle so it can restart us if the event loop
    itself ever wedges (WatchdogSec in mesh-logger.service)."""
    global _last_rx
    while True:
        await asyncio.sleep(WATCHDOG_PING_SECONDS)
        _sd_notify("WATCHDOG=1")
        if time.monotonic() - _last_rx < SILENCE_TIMEOUT:
            continue
        iface = _interface
        send_hb = getattr(iface, "sendHeartbeat", None) if iface else None
        if send_hb is None:
            continue
        try:
            await asyncio.wait_for(asyncio.to_thread(send_hb), timeout=15)
            _last_rx = time.monotonic()  # link confirmed alive
        except Exception as e:
            print(f"[warn] mesh silent and heartbeat failed ({e}), forcing reconnect",
                  file=sys.stderr)
            _last_rx = time.monotonic()  # don't re-fire while reconnecting
            _reconnect_event.set()


# ── IPC socket server ──────────────────────────────────────────────────────────

async def _send_json(writer: asyncio.StreamWriter, obj: dict, lock: asyncio.Lock) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with contextlib.suppress(Exception):
        async with lock:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=CLIENT_SEND_TIMEOUT)


def _nodes_payload() -> list[dict]:
    if not _interface or not _interface.nodes:
        return []
    now_ts = datetime.datetime.now().timestamp()
    out = []
    # snapshot: the dict is mutated by the meshtastic thread as nodes appear
    for nid, node in list(_interface.nodes.items()):
        u = node.get("user", {})
        num = nid if isinstance(nid, int) else int(str(nid).lstrip("!"), 16)
        metrics = node.get("deviceMetrics", {})
        last_heard = node.get("lastHeard")
        # _fixupPosition() (mesh_interface.py) already converts latitudeI/longitudeI
        # into float-degree latitude/longitude on this dict — no unit conversion here
        position = node.get("position", {})
        out.append({
            "node_id": num,
            "long_name": u.get("longName"),
            "short_name": u.get("shortName"),
            "has_user": bool(u),
            "battery": metrics.get("batteryLevel"),
            "snr": node.get("snr"),
            "hops_away": node.get("hopsAway"),
            "seconds_ago": int(now_ts - last_heard) if last_heard else None,
            "lat": position.get("latitude"),
            "lon": position.get("longitude"),
            "alt": position.get("altitude"),
        })
    return out


def _send_emoji_packet(iface, text: str, channel_index: int, reply_id: int,
                        handler, destination_id):
    """Build and send a tapback-reaction packet by hand: the library's public
    sendText()/sendData() have no `emoji` kwarg (unlike the `Data` protobuf
    message, which does), so this mirrors sendData()'s internals with that one
    field added. Mirrors _do_traceroute()'s precedent for reaching past the
    public API when it doesn't expose a field we need."""
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    packet = mesh_pb2.MeshPacket()
    packet.channel = channel_index
    packet.decoded.payload = text.encode("utf-8")
    packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    packet.decoded.emoji = 1
    if reply_id:
        packet.decoded.reply_id = reply_id
    packet.id = iface._generatePacketId()
    iface._addResponseHandler(packet.id, handler)
    return iface._sendPacket(packet, destination_id, wantAck=True)


async def _send_message(cmd: str, text: str, channel_index: int, reply_id: int = 0,
                         node_id: int | None = None,
                         notify_writer: asyncio.StreamWriter | None = None,
                         notify_lock: asyncio.Lock | None = None,
                         attempts: int = 0, emoji: bool = False) -> dict:
    """Transmit via the device (send or dm) and log the result. Shared by the
    live IPC handler and the outbox flush after a reconnect."""
    if not _interface:
        return {"ok": False, "error": "not connected"}

    iface = _interface
    if reply_id and not emoji and "replyId" not in inspect.signature(iface.sendText).parameters:
        return {"ok": False, "error": "библиотека meshtastic не поддерживает replyId — "
                                       "обновите: pip install -U meshtastic"}

    pid_holder = {}
    fired = {"done": False}

    def emit_delivery(ok, error=None, hops=None):
        if fired["done"]:
            return
        fired["done"] = True
        if notify_writer is None or notify_lock is None:
            return
        event = {"event": "delivery", "packet_id": pid_holder.get("pid"),
                  "ok": ok, "text": text}
        if error:
            event["error"] = error
        if hops is not None:
            event["hops"] = hops
        _loop.create_task(_send_json(notify_writer, event, notify_lock))

    def handler(ack_packet):
        decoded = ack_packet.get("decoded", {}) if isinstance(ack_packet, dict) else {}
        routing = decoded.get("routing", {})
        error = routing.get("errorReason", "NONE")
        ok = (error == "NONE")
        # hops the ACK travelled back over — same hopStart-hopLimit arithmetic
        # as on_receive(), gives /ping a real "N хопов" figure
        hop_limit = ack_packet.get("hopLimit") if isinstance(ack_packet, dict) else None
        hop_start = ack_packet.get("hopStart") if isinstance(ack_packet, dict) else None
        hops = (hop_start - hop_limit) if hop_limit is not None and hop_start is not None else None
        if _loop:
            _loop.call_soon_threadsafe(emit_delivery, ok, None if ok else error, hops)

    async def ack_timeout_watcher():
        await asyncio.sleep(ACK_TIMEOUT_SECONDS)
        emit_delivery(None, "timeout")

    destination_id = node_id if cmd == "dm" else BROADCAST_ADDR
    kwargs = dict(wantAck=True, channelIndex=channel_index, onResponse=handler)
    if cmd == "dm":
        kwargs["destinationId"] = node_id
    if reply_id:
        kwargs["replyId"] = reply_id

    # sendText does a synchronous socket write — if the device link is
    # stalled it would freeze the whole event loop
    try:
        if emoji:
            sent = await asyncio.to_thread(_send_emoji_packet, iface, text, channel_index,
                                           reply_id, handler, destination_id)
        else:
            sent = await asyncio.to_thread(iface.sendText, text, **kwargs)
    except Exception as e:
        # линк умер, но connection.lost ещё не прилетел: сообщение — в outbox,
        # соединение — на переподключение (раньше сообщение просто терялось).
        # attempts не даёт «ядовитому» сообщению бесконечно ронять flush.
        _reconnect_event.set()
        if attempts < OUTBOX_RETRIES and len(_outbox) < OUTBOX_MAX:
            _outbox.append({"cmd": cmd, "text": text, "channel_index": channel_index,
                            "reply_id": reply_id, "node_id": node_id,
                            "writer": notify_writer, "lock": notify_lock,
                            "attempts": attempts + 1, "emoji": emoji})
            return {"ok": True, "queued": True}
        return {"ok": False, "error": f"отправка не удалась: {e}"}
    pid = _packet_id(sent)
    pid_holder["pid"] = pid
    _loop.create_task(ack_timeout_watcher())

    # именно iface, а не _interface: за время await мог случиться реконнект
    my_id = iface.myInfo.my_node_num
    ln, sn = _node_names(my_id)
    now = datetime.datetime.now().strftime("%H:%M:%S")
    if pid:
        _store_msg(pid, MsgRecord(ln, sn, text, now))

    # our own reply gets the same quote line in the log
    reply_rec = _store.get(reply_id) if reply_id else None
    if cmd == "dm":
        dest_ln, dest_sn = _node_names(node_id)
        _write_message(dest_ln, dest_sn, text, 0, dm_tag="DM → ",
                       reply_to=reply_rec, channel_index=channel_index, time_str=now,
                       meta={"packet_id": pid, "from_id": my_id, "to_id": node_id,
                             "is_dm": True, "channel_index": channel_index})
    else:
        _write_message(ln, sn, text, 0, reply_to=reply_rec,
                       channel_index=channel_index, time_str=now,
                       meta={"packet_id": pid, "from_id": my_id, "is_dm": False,
                             "channel_index": channel_index})

    return {"ok": True, "packet_id": pid}


async def _flush_outbox() -> None:
    """Send everything queued while the device was disconnected."""
    if not _outbox:
        return
    pending = list(_outbox)
    _outbox.clear()
    print(f"[info] flushing {len(pending)} queued message(s)", file=sys.stderr)
    for item in pending:
        if not _interface:
            _outbox.append(item)  # dropped again mid-flush, keep for the next attempt
            continue
        result = await _send_message(
            item["cmd"], item["text"], item["channel_index"], item["reply_id"],
            item["node_id"], notify_writer=item["writer"], notify_lock=item["lock"],
            attempts=item.get("attempts", 0), emoji=item.get("emoji", False))
        if not result.get("ok"):
            print(f"[warn] queued message failed to send: {result.get('error')}",
                  file=sys.stderr)
            # клиенту уже отвечали "queued" — о финальном провале сообщаем
            # delivery-событием, иначе сообщение пропадёт молча
            if item.get("writer") is not None:
                event = {"event": "delivery", "ok": False, "text": item["text"],
                         "error": result.get("error", "не отправлено")}
                _loop.create_task(_send_json(item["writer"], event, item["lock"]))


UNK_SNR = -128  # meshtastic's sentinel for "SNR unknown" in RouteDiscovery


async def _do_traceroute(dest: int, hop_limit: int, channel_index: int = 0) -> dict:
    from meshtastic.protobuf import mesh_pb2, portnums_pb2
    from google.protobuf.json_format import MessageToDict

    fut = _loop.create_future()

    def hop_info(node_num, snr_raw):
        ln, sn = _node_names(node_num)
        snr = None if snr_raw is None or snr_raw == UNK_SNR else snr_raw / 4
        return {"node_id": node_num, "long_name": ln, "short_name": sn, "snr": snr}

    def handle_response(p):
        if fut.done():
            return
        decoded = p.get("decoded", {}) if isinstance(p, dict) else {}
        portnum = decoded.get("portnum")
        if portnum == "ROUTING_APP":
            error = decoded.get("routing", {}).get("errorReason", "NONE")
            if error != "NONE":
                fut.set_result({"ok": False, "error": error})
            return  # otherwise: just an ack, keep waiting for the real response
        if portnum != "TRACEROUTE_APP":
            return

        route_discovery = mesh_pb2.RouteDiscovery()
        route_discovery.ParseFromString(decoded.get("payload", b""))
        as_dict = MessageToDict(route_discovery)

        # In the response packet, "to" is us (the requester) and "from" is the
        # traced node — mirrors meshtastic's own onResponseTraceRoute ordering.
        us_id = p.get("to", 0)
        dest_id = p.get("from", dest)

        route = as_dict.get("route", [])
        snr_towards = as_dict.get("snrTowards", [])
        towards = [hop_info(us_id, None)]
        towards += [hop_info(n, snr_towards[i] if i < len(snr_towards) else None)
                    for i, n in enumerate(route)]
        towards.append(hop_info(dest_id, snr_towards[-1] if snr_towards else None))

        route_back = as_dict.get("routeBack", [])
        snr_back = as_dict.get("snrBack", [])
        back = [hop_info(dest_id, None)]
        back += [hop_info(n, snr_back[i] if i < len(snr_back) else None)
                 for i, n in enumerate(route_back)]
        if snr_back:
            back.append(hop_info(us_id, snr_back[-1]))
        else:
            back = []  # no valid SNR data means routeBack wasn't actually populated

        fut.set_result({"ok": True, "towards": towards, "back": back})

    def threadsafe_handler(p):
        if _loop:
            _loop.call_soon_threadsafe(handle_response, p)

    r = mesh_pb2.RouteDiscovery()
    iface = _interface
    await asyncio.to_thread(
        iface.sendData, r, destinationId=dest,
        portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True,
        onResponse=threadsafe_handler, channelIndex=channel_index, hopLimit=hop_limit,
    )
    return await asyncio.wait_for(fut, timeout=TRACE_TIMEOUT)


# ── admin session key ─────────────────────────────────────────────────────────
#
# Admin messages (reboot, writeConfig, setOwner — anything _sendAdmin() sends)
# need a per-session passkey the firmware only hands out on request. The
# library's own node.ensureSessionKey() *fires* that request but doesn't wait
# for the reply — it arrives later, asynchronously, via _onAdminReceive() on
# meshtastic's pubsub thread. Calling reboot()/writeConfig() right after
# ensureSessionKey() (as the library itself does, e.g. inside setOwner()) races
# that reply: send the admin packet before the key lands and the firmware
# silently drops it — no exception, no NAK, `_send_admin` reports nothing
# wrong because there's nothing for it to see. This was the actual cause of
# "/reboot confirm said OK but the device never rebooted".

def _has_session_key(node) -> bool:
    nodeid = to_node_num(node.nodeNum)
    return node.iface._getOrCreateByNum(nodeid).get("adminSessionPassKey") is not None


def _ensure_session_key(node) -> None:
    """Blocks (call via asyncio.to_thread, same as the admin call it precedes)
    until the passkey is cached or SESSION_KEY_TIMEOUT elapses. If it times
    out we still proceed — some configurations don't require a passkey at all,
    and it's better to let the real admin call surface whatever's actually
    wrong than to invent a new error class for a case that may not apply."""
    if _has_session_key(node):
        return
    node.ensureSessionKey()
    deadline = time.monotonic() + SESSION_KEY_TIMEOUT
    while time.monotonic() < deadline and not _has_session_key(node):
        time.sleep(0.1)


# ── local node settings ───────────────────────────────────────────────────────
#
# Deliberately a small curated whitelist, not a generic protobuf-path get/set:
# - excludes `network` (WiFi credentials) — changing it over this same TCP/WiFi
#   link could sever our own connection to the device
# - excludes `bluetooth`/`security` — PINs and keys, not something to expose over
#   a plaintext local IPC socket
# - excludes `display`/`power`/`audio`/etc. — no headless-node use case here
# Every entry is (config_section, is_module_config, field_name, kind, extra).
# See SETTINGS.md for what each one does and why it's safe to expose.
_ROLE_ENUM = config_pb2.Config.DeviceConfig.Role
_GPS_MODE_ENUM = config_pb2.Config.PositionConfig.GpsMode

SETTINGS_REGISTRY: dict[str, tuple] = {
    "role": ("device", False, "role", "enum", _ROLE_ENUM),
    "node_info_broadcast_secs": ("device", False, "node_info_broadcast_secs", "int", (60, 86400)),
    "hop_limit": ("lora", False, "hop_limit", "int", (1, 7)),
    "tx_power": ("lora", False, "tx_power", "int", (0, 30)),
    "position_broadcast_secs": ("position", False, "position_broadcast_secs", "int", (30, 86400)),
    "position_smart_enabled": ("position", False, "position_broadcast_smart_enabled", "bool", None),
    "gps_mode": ("position", False, "gps_mode", "enum", _GPS_MODE_ENUM),
    "fixed_position": ("position", False, "fixed_position", "bool", None),
    "telemetry_device_secs": ("telemetry", True, "device_update_interval", "int", (60, 86400)),
    "telemetry_env_enabled": ("telemetry", True, "environment_measurement_enabled", "bool", None),
    # MQTT: bridges LoRa traffic to services like OneMesh over the internet,
    # not just the mesh itself. uplink_enabled/downlink_enabled are per-CHANNEL
    # flags (ChannelSettings, not moduleConfig) — see mqtt_uplink/mqtt_downlink
    # special-cased in _apply_setting()/_settings_snapshot() below, they don't
    # fit this table's (config_section, field) shape.
    "mqtt_enabled": ("mqtt", True, "enabled", "bool", None),
    "mqtt_address": ("mqtt", True, "address", "str", None),
    "mqtt_username": ("mqtt", True, "username", "str", None),
    "mqtt_password": ("mqtt", True, "password", "str", None),  # masked on read, see _settings_snapshot
    "mqtt_encryption_enabled": ("mqtt", True, "encryption_enabled", "bool", None),
    "mqtt_json_enabled": ("mqtt", True, "json_enabled", "bool", None),
    "mqtt_tls_enabled": ("mqtt", True, "tls_enabled", "bool", None),
    "mqtt_root": ("mqtt", True, "root", "str", None),
}

SETTINGS_DESCRIPTIONS = {
    "owner_long": "Полное имя узла",
    "owner_short": "Короткое имя узла (обычно до 4 символов)",
    "role": "Роль узла в сети (CLIENT, ROUTER, REPEATER, ...)",
    "node_info_broadcast_secs": "Как часто рассылать свой NodeInfo, секунды",
    "hop_limit": "Максимум хопов для пакетов, отправленных с этого узла",
    "tx_power": "Мощность передачи, дБм (0 = значение по умолчанию для региона)",
    "position_broadcast_secs": "Как часто рассылать позицию, секунды",
    "position_smart_enabled": "Рассылать позицию только при значимом перемещении",
    "gps_mode": "Режим GPS-приёмника (ENABLED, DISABLED, NOT_PRESENT)",
    "fixed_position": "Считать текущую позицию фиксированной (узел неподвижен)",
    "telemetry_device_secs": "Как часто слать телеметрию устройства, секунды",
    "telemetry_env_enabled": "Слать телеметрию окружающей среды (датчики), если есть",
    "mqtt_enabled": "Включить отправку сообщений в MQTT (в дополнение к LoRa)",
    "mqtt_address": "Адрес MQTT-брокера, host[:port] (например map.onemesh.ru)",
    "mqtt_username": "Логин для подключения к MQTT-брокеру",
    "mqtt_password": "Пароль для подключения к MQTT-брокеру (в списке — маской)",
    "mqtt_encryption_enabled": "Шифровать пакеты в MQTT тем же PSK, что и в канале",
    "mqtt_json_enabled": "Дополнительно публиковать сообщения в виде JSON",
    "mqtt_tls_enabled": "TLS-соединение с брокером",
    "mqtt_root": "Корневой топик MQTT (например msh/RU)",
    "mqtt_uplink": "Ретрансляция канала из LoRa в MQTT: <канал>:on|off, например Primary:on",
    "mqtt_downlink": "Ретрансляция канала из MQTT в LoRa: <канал>:on|off",
}

MQTT_PASSWORD_MASK = "••••••••"


def _get_config_value(node, entry: tuple):
    config_name, is_module, field, kind, extra = entry
    root = node.moduleConfig if is_module else node.localConfig
    section = getattr(root, config_name)
    value = getattr(section, field)
    return extra.Name(value) if kind == "enum" else value


def _channel_links_summary(node, field: str) -> str:
    """Reads uplink_enabled/downlink_enabled across every non-disabled channel
    — these are per-channel, so there's no single scalar to show like the rest
    of SETTINGS_REGISTRY."""
    try:
        channels = node.channels or []
    except Exception:
        channels = []
    parts = [f"{_channel_name(c.index)}:{'on' if getattr(c.settings, field) else 'off'}"
             for c in channels if c.role != 0]
    return ", ".join(parts) if parts else "—"


def _settings_snapshot() -> list[dict]:
    node = _interface.localNode
    my_id = _interface.myInfo.my_node_num
    ln, sn = _node_names(my_id)
    out = [
        {"key": "owner_long", "value": ln, "description": SETTINGS_DESCRIPTIONS["owner_long"]},
        {"key": "owner_short", "value": sn, "description": SETTINGS_DESCRIPTIONS["owner_short"]},
    ]
    for key, entry in SETTINGS_REGISTRY.items():
        try:
            value = _get_config_value(node, entry)
        except Exception:
            value = None
        if key == "mqtt_password" and value:
            value = MQTT_PASSWORD_MASK
        out.append({"key": key, "value": value, "description": SETTINGS_DESCRIPTIONS[key]})
    out.append({"key": "mqtt_uplink", "value": _channel_links_summary(node, "uplink_enabled"),
                "description": SETTINGS_DESCRIPTIONS["mqtt_uplink"]})
    out.append({"key": "mqtt_downlink", "value": _channel_links_summary(node, "downlink_enabled"),
                "description": SETTINGS_DESCRIPTIONS["mqtt_downlink"]})
    return out


def _parse_setting_value(raw: str, kind: str, extra) -> object:
    if kind == "str":
        return raw
    if kind == "bool":
        low = raw.strip().lower()
        if low in ("1", "true", "on", "yes", "да", "вкл"):
            return True
        if low in ("0", "false", "off", "no", "нет", "выкл"):
            return False
        raise ValueError("ожидалось булево значение (on/off, да/нет, 1/0)")
    if kind == "int":
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("ожидалось целое число") from None
        lo, hi = extra
        if not (lo <= value <= hi):
            raise ValueError(f"вне диапазона {lo}..{hi}")
        return value
    if kind == "enum":
        name = raw.strip().upper()
        if name not in extra.keys():
            raise ValueError(f"допустимые значения: {', '.join(extra.keys())}")
        return extra.Value(name)
    raise ValueError(f"неизвестный тип параметра: {kind}")


def _apply_channel_link(node, raw_value: str, field: str) -> str:
    """mqtt_uplink/mqtt_downlink: value is "<канал>:on|off" — writes a
    ChannelSettings flag via writeChannel(), not writeConfig(), since uplink/
    downlink live on the channel, not in moduleConfig.mqtt."""
    if ":" not in raw_value:
        raise ValueError("формат: <канал>:on|off, например Primary:on")
    channel_name, _, flag_str = raw_value.partition(":")
    channel_name = channel_name.strip()
    flag = _parse_setting_value(flag_str, "bool", None)
    try:
        channels = node.channels or []
    except Exception:
        channels = []
    match = next((c for c in channels if _channel_name(c.index).lower() == channel_name.lower()),
                 None)
    if match is None:
        raise ValueError(f"канал '{channel_name}' не найден")
    setattr(match.settings, field, flag)
    node.writeChannel(match.index)
    return f"{channel_name}:{'on' if flag else 'off'}"


def _apply_setting(node, key: str, raw_value: str) -> object:
    """Runs in a worker thread — writeConfig() does a synchronous admin-packet
    send. Returns the new value as it will read back via _settings_snapshot()."""
    _ensure_session_key(node)
    if key == "owner_long":
        node.setOwner(long_name=raw_value)
        return raw_value
    if key == "owner_short":
        node.setOwner(short_name=raw_value)
        return raw_value
    if key == "mqtt_uplink":
        return _apply_channel_link(node, raw_value, "uplink_enabled")
    if key == "mqtt_downlink":
        return _apply_channel_link(node, raw_value, "downlink_enabled")
    if key not in SETTINGS_REGISTRY:
        raise KeyError(key)
    config_name, is_module, field, kind, extra = SETTINGS_REGISTRY[key]
    value = _parse_setting_value(raw_value, kind, extra)
    root = node.moduleConfig if is_module else node.localConfig
    section = getattr(root, config_name)
    setattr(section, field, value)
    node.writeConfig(config_name)
    result = extra.Name(value) if kind == "enum" else value
    return MQTT_PASSWORD_MASK if key == "mqtt_password" else result


def _reboot_node(node) -> None:
    """node.reboot() also calls ensureSessionKey() itself, but without waiting
    — same race as _apply_setting(), so we wait here first for the same reason."""
    _ensure_session_key(node)
    node.reboot(REBOOT_DELAY_SECS)


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    write_lock = asyncio.Lock()
    _clients[writer] = write_lock
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                continue

            req_id = req.get("req_id")
            cmd = req.get("cmd")
            resp = {"req_id": req_id, "ok": False}

            try:
                if cmd == "whoami":
                    if _interface:
                        my_id = _interface.myInfo.my_node_num
                        ln, sn = _node_names(my_id)
                        resp = {"req_id": req_id, "ok": True,
                               "node_id": my_id, "long_name": ln, "short_name": sn}
                    else:
                        resp["error"] = "not connected"

                elif cmd == "nodes":
                    resp = {"req_id": req_id, "ok": True, "nodes": _nodes_payload()}

                elif cmd == "channels":
                    resp = {"req_id": req_id, "ok": True, "channels": _channels_payload()}

                elif cmd in ("send", "dm"):
                    text = req.get("text", "")
                    channel_index = req.get("channel", 0)
                    reply_id = req.get("reply_id", 0)
                    node_id = req.get("node_id") if cmd == "dm" else None
                    emoji = bool(req.get("emoji"))

                    if not _interface:
                        if len(_outbox) >= OUTBOX_MAX:
                            resp["error"] = "устройство офлайн, очередь исходящих переполнена"
                        else:
                            _outbox.append({"cmd": cmd, "text": text,
                                            "channel_index": channel_index,
                                            "reply_id": reply_id, "node_id": node_id,
                                            "writer": writer, "lock": write_lock,
                                            "emoji": emoji})
                            resp = {"req_id": req_id, "ok": True, "queued": True}
                    else:
                        result = await _send_message(cmd, text, channel_index, reply_id,
                                                     node_id, notify_writer=writer,
                                                     notify_lock=write_lock, emoji=emoji)
                        resp = {"req_id": req_id, **result}

                elif cmd == "trace":
                    if not _interface:
                        resp["error"] = "not connected"
                    else:
                        target = req.get("node_id")
                        hop_limit = max(1, min(7, req.get("hop_limit", 7)))
                        try:
                            result = await _do_traceroute(target, hop_limit,
                                                          req.get("channel", 0))
                            resp = {"req_id": req_id, **result}
                        except asyncio.TimeoutError:
                            resp["error"] = "узел не ответил (timeout)"
                        except Exception as e:
                            resp["error"] = f"traceroute failed: {e}"

                elif cmd == "settings":
                    if not _interface:
                        resp["error"] = "not connected"
                    else:
                        action = req.get("action", "list")
                        if action == "list":
                            resp = {"req_id": req_id, "ok": True,
                                    "settings": _settings_snapshot()}
                        elif action == "set":
                            key = req.get("key", "")
                            value = req.get("value", "")
                            try:
                                new_val = await asyncio.to_thread(
                                    _apply_setting, _interface.localNode, key, value)
                                resp = {"req_id": req_id, "ok": True,
                                        "key": key, "value": new_val}
                            except KeyError:
                                resp["error"] = f"неизвестный параметр: {key}"
                            except ValueError as e:
                                resp["error"] = str(e)
                            except Exception as e:
                                resp["error"] = f"не удалось применить: {e}"
                        else:
                            resp["error"] = f"unknown settings action: {action}"

                elif cmd == "reboot":
                    if not _interface:
                        resp["error"] = "not connected"
                    else:
                        try:
                            # reboot() does a synchronous admin-packet send (like writeConfig) —
                            # off the event loop so it can't stall other IPC clients. Ensuring
                            # the session key first (and actually waiting for it) in the same
                            # thread call is what makes this reboot land instead of getting
                            # silently dropped by the firmware's session-auth check.
                            await asyncio.to_thread(_reboot_node, _interface.localNode)
                            resp = {"req_id": req_id, "ok": True, "secs": REBOOT_DELAY_SECS}
                        except Exception as e:
                            resp["error"] = f"не удалось перезагрузить: {e}"

                elif cmd == "updatenames":
                    if not _interface:
                        resp["error"] = "not connected"
                    else:
                        result = await asyncio.to_thread(_run_onemesh_update)
                        resp = {"req_id": req_id, "ok": True, **result}

                elif cmd == "botping":
                    value = req.get("value")
                    if value not in ("0", "1"):
                        resp["error"] = "ожидается value: '0' или '1'"
                    else:
                        _set_botping(value == "1")
                        resp = {"req_id": req_id, "ok": True, "enabled": _botping_enabled,
                                "channel_found": _ping_channel_index() is not None}
                else:
                    resp["error"] = f"unknown cmd: {cmd}"
            except Exception as e:
                resp["error"] = str(e)

            await _send_json(writer, resp, write_lock)

    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        _clients.pop(writer, None)
        with contextlib.suppress(Exception):
            writer.close()


def _prepare_socket_path() -> None:
    if not SOCKET_PATH.exists():
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCKET_PATH))
        print(f"[error] logger already running on {SOCKET_PATH}", file=sys.stderr)
        sys.exit(1)
    except OSError:
        SOCKET_PATH.unlink(missing_ok=True)
    finally:
        with contextlib.suppress(Exception):
            s.close()


async def main() -> None:
    global _loop, _reconnect_event
    _loop = asyncio.get_running_loop()
    _reconnect_event = asyncio.Event()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _load_onemesh_cache()
    _prepare_socket_path()

    print(f"[info] connecting via {CONN_TYPE} to {CONN_TARGET}...")
    if not await asyncio.to_thread(_do_connect):
        # устройство недоступно — всё равно поднимаем IPC-сокет: клиенты видят
        # "not connected" и складывают отправки в outbox, а реконнект-цикл
        # продолжает попытки (раньше процесс тут умирал и до рестарта systemd
        # сокета не существовало вовсе)
        print("[warn] device unreachable at startup, serving IPC and retrying",
              file=sys.stderr)
        _reconnect_event.set()

    reconnect_task = asyncio.create_task(_reconnect_loop())
    watchdog_task = asyncio.create_task(_watchdog())
    onemesh_task = asyncio.create_task(_periodic_onemesh_update())
    autoping_task = asyncio.create_task(_periodic_autoping())
    server = await asyncio.start_unix_server(_handle_client, path=str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)  # only this user may send messages via IPC
    print(f"[info] listening on {SOCKET_PATH}")
    # ready as far as systemd is concerned even if the device itself isn't
    # connected yet — the IPC socket (what Type=notify cares about) is up
    _sd_notify("READY=1")

    try:
        async with server:
            await server.serve_forever()
    finally:
        reconnect_task.cancel()
        watchdog_task.cancel()
        onemesh_task.cancel()
        autoping_task.cancel()
        _unsubscribe()
        with contextlib.suppress(Exception):
            _interface.close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
