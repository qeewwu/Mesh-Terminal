#!/usr/bin/env python3
"""Lightweight always-on Meshtastic logger and IPC broker.

Holds the single TCP connection to the Meshtastic device, appends every
text message to chat.log, and exposes a Unix socket so other processes
(e.g. mesh_chat.py) can send messages and query node info without needing
their own connection to the device.
"""

import asyncio
import collections
import contextlib
import datetime
import json
import socket
import sys
from typing import NamedTuple

import meshtastic.tcp_interface
from pubsub import pub

from mesh_common import (
    BROADCAST_ADDR,
    HOST,
    LOG_FILE,
    SOCKET_PATH,
    format_message_line,
    format_quote_line,
)

STORE_SIZE = 300

_interface = None
_loop: asyncio.AbstractEventLoop | None = None
_reconnect_event: asyncio.Event | None = None


class MsgRecord(NamedTuple):
    long_name: str
    short_name: str
    text: str


_store: dict[int, MsgRecord] = {}
_store_order: collections.deque[int] = collections.deque(maxlen=STORE_SIZE)

# Connected IPC clients, for pushing live message events with no polling delay
_clients: dict[asyncio.StreamWriter, asyncio.Lock] = {}


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


def _log(*lines: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


async def _broadcast(lines: list[str]) -> None:
    event = {"event": "message", "lines": lines}
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    for writer, lock in list(_clients.items()):
        async with lock:
            with contextlib.suppress(Exception):
                writer.write(data)
                await writer.drain()


def _write_message(long_name: str, short_name: str, text: str, hops: int,
                    dm_tag: str = "", reply_to: MsgRecord | None = None) -> None:
    now = datetime.datetime.now().strftime("%H:%M:%S")
    lines = []
    if reply_to:
        lines.append(format_quote_line(reply_to.long_name, reply_to.short_name, reply_to.text))
    lines.append(format_message_line(now, long_name, short_name, text, hops, dm_tag))
    _log(*lines)
    if _loop:
        # on_receive runs on meshtastic's background thread; schedule safely
        _loop.call_soon_threadsafe(lambda: _loop.create_task(_broadcast(lines)))


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
        hop_limit = packet.get("hopLimit", 0)
        hop_start = packet.get("hopStart", hop_limit)
        hops      = hop_start - hop_limit
        is_dm     = (to_id != BROADCAST_ADDR)

        long_name, short_name = _node_names(from_id)
        reply_to = _store.get(reply_id) if reply_id else None
        if packet_id:
            _store_msg(packet_id, MsgRecord(long_name, short_name, text))

        _write_message(long_name, short_name, text, hops,
                       dm_tag=("DM " if is_dm else ""), reply_to=reply_to)
        print(f"[recv] {long_name} ({short_name}): {text}")
    except Exception as e:
        print(f"[error] on_receive: {e}", file=sys.stderr)


def on_connection_lost(interface):
    print("[warn] connection lost, will reconnect", file=sys.stderr)
    if _loop and _reconnect_event:
        _loop.call_soon_threadsafe(_reconnect_event.set)


def _subscribe():
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")


def _unsubscribe():
    for fn, topic in ((on_receive, "meshtastic.receive.text"),
                      (on_connection_lost, "meshtastic.connection.lost")):
        with contextlib.suppress(Exception):
            pub.unsubscribe(fn, topic)


def _do_connect() -> bool:
    global _interface
    try:
        _interface = meshtastic.tcp_interface.TCPInterface(hostname=HOST)
        _subscribe()
        my_id = _interface.myInfo.my_node_num
        ln, sn = _node_names(my_id)
        print(f"[info] connected to {HOST} as {ln} ({sn})")
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
    for nid, node in _interface.nodes.items():
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
            "seconds_ago": int(now_ts - last_heard) if last_heard else None,
        })
    return out


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

                elif cmd in ("send", "dm"):
                    if not _interface:
                        resp["error"] = "not connected"
                    else:
                        text = req.get("text", "")
                        pid_holder = {}

                        def handler(ack_packet, _holder=pid_holder, _writer=writer, _lock=write_lock):
                            decoded = ack_packet.get("decoded", {}) if isinstance(ack_packet, dict) else {}
                            routing = decoded.get("routing", {})
                            error = routing.get("errorReason", "NONE")
                            ok = (error == "NONE")
                            event = {"event": "delivery", "packet_id": _holder.get("pid"), "ok": ok}
                            if not ok:
                                event["error"] = error
                            if _loop:
                                _loop.call_soon_threadsafe(
                                    lambda: _loop.create_task(_send_json(_writer, event, _lock))
                                )

                        kwargs = dict(wantAck=True, channelIndex=0, onResponse=handler)
                        if cmd == "dm":
                            kwargs["destinationId"] = req.get("node_id")
                        sent = _interface.sendText(text, **kwargs)
                        pid = _packet_id(sent)
                        pid_holder["pid"] = pid

                        my_id = _interface.myInfo.my_node_num
                        ln, sn = _node_names(my_id)
                        if pid:
                            _store_msg(pid, MsgRecord(ln, sn, text))

                        if cmd == "dm":
                            dest_ln, dest_sn = _node_names(req.get("node_id", 0))
                            _write_message(dest_ln, dest_sn, text, 0, dm_tag="DM → ")
                        else:
                            _write_message(ln, sn, text, 0)

                        resp = {"req_id": req_id, "ok": True, "packet_id": pid}
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

    _prepare_socket_path()

    print(f"[info] connecting to {HOST}...")
    if not _do_connect():
        sys.exit(1)

    reconnect_task = asyncio.create_task(_reconnect_loop())
    server = await asyncio.start_unix_server(_handle_client, path=str(SOCKET_PATH))
    print(f"[info] listening on {SOCKET_PATH}")

    try:
        async with server:
            await server.serve_forever()
    finally:
        reconnect_task.cancel()
        _unsubscribe()
        with contextlib.suppress(Exception):
            _interface.close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
