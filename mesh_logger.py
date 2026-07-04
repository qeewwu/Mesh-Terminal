#!/usr/bin/env python3
"""Lightweight always-on Meshtastic logger and IPC broker.

Holds the single TCP connection to the Meshtastic device, appends every
text message to a daily log file under logs/, and exposes a Unix socket so other processes
(e.g. mesh_chat.py) can send messages and query node info without needing
their own connection to the device.
"""

import asyncio
import collections
import contextlib
import datetime
import inspect
import json
import os
import socket
import sys
import threading
import time
from typing import NamedTuple

import meshtastic.tcp_interface
from pubsub import pub

from mesh_common import (
    ACK_TIMEOUT_SECONDS,
    BROADCAST_ADDR,
    HOST,
    LOG_DIR,
    SOCKET_PATH,
    current_log_file,
    format_message_line,
    format_quote_line,
)

STORE_SIZE = 300
# no mesh traffic at all for this long → probe the TCP link with a heartbeat
# (a silently dead WiFi link doesn't always fire connection.lost)
SILENCE_TIMEOUT = 600
OUTBOX_MAX = 50  # queued sends while the device is disconnected
TRACE_TIMEOUT = 40  # a traceroute is a full round trip across the mesh, can be slow

_interface = None
_loop: asyncio.AbstractEventLoop | None = None
_reconnect_event: asyncio.Event | None = None
_last_rx: float = 0.0  # time.monotonic() of the last packet of any kind


class MsgRecord(NamedTuple):
    long_name: str
    short_name: str
    text: str
    time_str: str = ""


_store: dict[int, MsgRecord] = {}
_store_order: collections.deque[int] = collections.deque(maxlen=STORE_SIZE)

# Connected IPC clients, for pushing live message events with no polling delay
_clients: dict[asyncio.StreamWriter, asyncio.Lock] = {}

# Sends attempted while the device was disconnected; flushed once it reconnects
_outbox: collections.deque[dict] = collections.deque()


def _store_msg(packet_id: int, record: MsgRecord) -> None:
    if len(_store_order) == STORE_SIZE and _store_order[0] in _store:
        del _store[_store_order[0]]
    _store[packet_id] = record
    _store_order.append(packet_id)


def _node_names(node_id: int) -> tuple[str, str]:
    if _interface and _interface.nodes:
        hex_id = f"!{node_id:08x}"
        node = _interface.nodes.get(node_id) or _interface.nodes.get(hex_id)
        if node and "user" in node:
            u = node["user"]
            return u.get("longName", hex_id), u.get("shortName", "???")
    return f"!{node_id:08x}", "???"


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


# _write_message runs both on the meshtastic callback thread (on_receive) and
# the event loop thread (send/dm) — without a lock a quote+message pair could
# interleave with a concurrent write.
_log_lock = threading.Lock()


def _log(*lines: str) -> None:
    with _log_lock, open(current_log_file(), "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


async def _broadcast(lines: list[str], meta: dict | None = None) -> None:
    event = {"event": "message", "lines": lines}
    if meta:
        event.update(meta)
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    for writer, lock in list(_clients.items()):
        async with lock:
            with contextlib.suppress(Exception):
                writer.write(data)
                await writer.drain()


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
        _loop.call_soon_threadsafe(lambda: _loop.create_task(_broadcast(lines, meta)))


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
        print(f"[recv] {long_name} ({short_name}): {text}")
    except Exception as e:
        print(f"[error] on_receive: {e}", file=sys.stderr)


def on_connection_lost(interface):
    print("[warn] connection lost, will reconnect", file=sys.stderr)
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


def _do_connect() -> bool:
    global _interface, _last_rx
    try:
        _interface = meshtastic.tcp_interface.TCPInterface(hostname=HOST)
        _subscribe()
        _last_rx = time.monotonic()
        my_id = _interface.myInfo.my_node_num
        ln, sn = _node_names(my_id)
        print(f"[info] connected to {HOST} as {ln} ({sn})")
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
                    _interface.close()
            if _do_connect():
                attempt = 0
                break
            attempt += 1


async def _watchdog() -> None:
    """Probe the device link when the mesh has been silent for too long."""
    global _last_rx
    while True:
        await asyncio.sleep(60)
        if time.monotonic() - _last_rx < SILENCE_TIMEOUT:
            continue
        iface = _interface
        send_hb = getattr(iface, "sendHeartbeat", None) if iface else None
        if send_hb is None:
            continue
        try:
            await asyncio.wait_for(asyncio.to_thread(send_hb), timeout=15)
            _last_rx = time.monotonic()  # TCP link confirmed alive
        except Exception as e:
            print(f"[warn] mesh silent and heartbeat failed ({e}), forcing reconnect",
                  file=sys.stderr)
            _last_rx = time.monotonic()  # don't re-fire while reconnecting
            _reconnect_event.set()


# ── IPC socket server ──────────────────────────────────────────────────────────

async def _send_json(writer: asyncio.StreamWriter, obj: dict, lock: asyncio.Lock) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    async with lock:
        with contextlib.suppress(Exception):
            writer.write(data)
            await writer.drain()


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
        out.append({
            "node_id": num,
            "long_name": u.get("longName"),
            "short_name": u.get("shortName"),
            "has_user": bool(u),
            "battery": metrics.get("batteryLevel"),
            "snr": node.get("snr"),
            "hops_away": node.get("hopsAway"),
            "seconds_ago": int(now_ts - last_heard) if last_heard else None,
        })
    return out


async def _send_message(cmd: str, text: str, channel_index: int, reply_id: int = 0,
                         node_id: int | None = None,
                         notify_writer: asyncio.StreamWriter | None = None,
                         notify_lock: asyncio.Lock | None = None) -> dict:
    """Transmit via the device (send or dm) and log the result. Shared by the
    live IPC handler and the outbox flush after a reconnect."""
    if not _interface:
        return {"ok": False, "error": "not connected"}

    iface = _interface
    if reply_id and "replyId" not in inspect.signature(iface.sendText).parameters:
        return {"ok": False, "error": "библиотека meshtastic не поддерживает replyId — "
                                       "обновите: pip install -U meshtastic"}

    pid_holder = {}
    fired = {"done": False}

    def emit_delivery(ok, error=None):
        if fired["done"]:
            return
        fired["done"] = True
        if notify_writer is None or notify_lock is None:
            return
        event = {"event": "delivery", "packet_id": pid_holder.get("pid"),
                  "ok": ok, "text": text}
        if error:
            event["error"] = error
        _loop.create_task(_send_json(notify_writer, event, notify_lock))

    def handler(ack_packet):
        decoded = ack_packet.get("decoded", {}) if isinstance(ack_packet, dict) else {}
        routing = decoded.get("routing", {})
        error = routing.get("errorReason", "NONE")
        ok = (error == "NONE")
        if _loop:
            _loop.call_soon_threadsafe(emit_delivery, ok, None if ok else error)

    async def ack_timeout_watcher():
        await asyncio.sleep(ACK_TIMEOUT_SECONDS)
        emit_delivery(None, "timeout")

    kwargs = dict(wantAck=True, channelIndex=channel_index, onResponse=handler)
    if cmd == "dm":
        kwargs["destinationId"] = node_id
    if reply_id:
        kwargs["replyId"] = reply_id

    # sendText does a synchronous socket write — if the device link is
    # stalled it would freeze the whole event loop
    sent = await asyncio.to_thread(iface.sendText, text, **kwargs)
    pid = _packet_id(sent)
    pid_holder["pid"] = pid
    _loop.create_task(ack_timeout_watcher())

    my_id = _interface.myInfo.my_node_num
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
            item["node_id"], notify_writer=item["writer"], notify_lock=item["lock"])
        if not result.get("ok"):
            print(f"[warn] queued message failed to send: {result.get('error')}",
                  file=sys.stderr)


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

                    if not _interface:
                        if len(_outbox) >= OUTBOX_MAX:
                            resp["error"] = "устройство офлайн, очередь исходящих переполнена"
                        else:
                            _outbox.append({"cmd": cmd, "text": text,
                                            "channel_index": channel_index,
                                            "reply_id": reply_id, "node_id": node_id,
                                            "writer": writer, "lock": write_lock})
                            resp = {"req_id": req_id, "ok": True, "queued": True}
                    else:
                        result = await _send_message(cmd, text, channel_index, reply_id,
                                                     node_id, notify_writer=writer,
                                                     notify_lock=write_lock)
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
    _prepare_socket_path()

    print(f"[info] connecting to {HOST}...")
    if not _do_connect():
        sys.exit(1)

    reconnect_task = asyncio.create_task(_reconnect_loop())
    watchdog_task = asyncio.create_task(_watchdog())
    server = await asyncio.start_unix_server(_handle_client, path=str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)  # only this user may send messages via IPC
    print(f"[info] listening on {SOCKET_PATH}")

    try:
        async with server:
            await server.serve_forever()
    finally:
        reconnect_task.cancel()
        watchdog_task.cancel()
        _unsubscribe()
        with contextlib.suppress(Exception):
            _interface.close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
