#!/usr/bin/env python3

import asyncio
import collections
import datetime
import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import meshtastic.tcp_interface
from pubsub import pub
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as pt_clear

HOST = "meshtastic.local"
LOG_FILE = Path("chat.log")
NAME_CACHE_FILE = Path("node_names_cache.json")
STORE_SIZE = 300
QUOTE_MAX = 60
BROADCAST_ADDR = 0xFFFFFFFF
ONEMESH_API = "https://map.onemesh.ru/api/v1/nodes/{}"
ONEMESH_DELAY = 0.25  # secs between requests, to be polite to the public API

# Characters invalid in XML 1.0 — would crash prompt_toolkit's HTML parser
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_interface = None
_loop: asyncio.AbstractEventLoop | None = None
_reconnect_event: asyncio.Event | None = None


class MsgRecord(NamedTuple):
    time_str: str
    long_name: str
    short_name: str
    text: str


_store: dict[int, MsgRecord] = {}
_store_order: collections.deque[int] = collections.deque(maxlen=STORE_SIZE)

# Names resolved from OneMesh, keyed by numeric node id — persisted to disk
_name_cache: dict[int, tuple[str, str]] = {}
# Node ids we've seen but couldn't resolve to a name locally
_unresolved_ids: set[int] = set()


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    """Strip XML-invalid control characters, then HTML-escape."""
    return html.escape(_XML_INVALID.sub("", text))


def _store_msg(packet_id: int, record: MsgRecord) -> None:
    if len(_store_order) == STORE_SIZE and _store_order[0] in _store:
        del _store[_store_order[0]]
    _store[packet_id] = record
    _store_order.append(packet_id)


def _packet_id(sent) -> int:
    """Extract numeric ID from the object returned by sendText."""
    if sent is None:
        return 0
    if isinstance(sent, dict):
        return sent.get("id", 0)
    return getattr(sent, "id", 0)


def _node_names(node_id: int) -> tuple[str, str]:
    if _interface and _interface.nodes:
        hex_id = f"!{node_id:08x}"
        node = _interface.nodes.get(node_id) or _interface.nodes.get(hex_id)
        if node and "user" in node:
            u = node["user"]
            return u.get("longName", hex_id), u.get("shortName", "???")
    if node_id in _name_cache:
        return _name_cache[node_id]
    _unresolved_ids.add(node_id)
    return f"!{node_id:08x}", "???"


def _load_name_cache() -> None:
    global _name_cache
    if not NAME_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
        _name_cache = {int(k): (v["long"], v["short"]) for k, v in raw.items()}
    except Exception:
        _name_cache = {}


def _save_name_cache() -> None:
    try:
        raw = {str(k): {"long": v[0], "short": v[1]} for k, v in _name_cache.items()}
        NAME_CACHE_FILE.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _fetch_onemesh_name(node_id: int) -> tuple[str, str] | None:
    """Blocking HTTP call to the OneMesh public API. Run via asyncio.to_thread."""
    req = urllib.request.Request(
        ONEMESH_API.format(node_id),
        headers={"User-Agent": "mesh-chat/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None

    node = data.get("node")
    if not node:
        return None
    long_name = node.get("long_name") or f"!{node_id:08x}"
    short_name = node.get("short_name") or "???"
    return long_name, short_name


def _find_node(query: str) -> tuple[int, str, str] | None:
    """Find node by short/long name (case-insensitive). Returns (num_id, long, short)."""
    if not _interface or not _interface.nodes:
        return None
    q = query.lower()
    for nid, node in _interface.nodes.items():
        if "user" not in node:
            continue
        u = node["user"]
        if q in u.get("longName", "").lower() or q in u.get("shortName", "").lower():
            if isinstance(nid, str) and nid.startswith("!"):
                num = int(nid[1:], 16)
            else:
                num = int(nid)
            return num, u.get("longName", str(nid)), u.get("shortName", "???")
    return None


# ── display ───────────────────────────────────────────────────────────────────

def _print_msg(time_str: str, long_name: str, short_name: str,
               text: str, hops: int, own: bool = False,
               is_dm: bool = False,
               reply_to: MsgRecord | None = None) -> None:
    if reply_to:
        qt = reply_to.text if len(reply_to.text) <= QUOTE_MAX else reply_to.text[:QUOTE_MAX] + "…"
        print_formatted_text(HTML(
            f"<ansigray>  ┆ {_safe(reply_to.long_name)} ({_safe(reply_to.short_name)})"
            f": {_safe(qt)}</ansigray>"
        ))

    t  = _safe(time_str)
    ln = _safe(long_name)
    sn = _safe(short_name)
    tx = _safe(text)

    if is_dm:
        print_formatted_text(HTML(
            f"<ansired>[{t}] DM <b>{ln}</b> ({sn}): {tx} "
            f"| {hops}</ansired>"
        ))
    else:
        color = "ansiblue" if own else "ansigreen"
        print_formatted_text(HTML(
            f"<ansiwhite>[{t}]</ansiwhite> "
            f"<b><{color}>{ln}</{color}></b> "
            f"<{color}>({sn})</{color}>"
            f": {tx} "
            f"<ansiwhite>| {hops}</ansiwhite>"
        ))


def _log(time_str: str, long_name: str, short_name: str,
         text: str, hops: int, is_dm: bool = False,
         reply_to: MsgRecord | None = None) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if reply_to:
            qt = reply_to.text if len(reply_to.text) <= QUOTE_MAX else reply_to.text[:QUOTE_MAX] + "…"
            f.write(f"  ┆ {reply_to.long_name} ({reply_to.short_name}): {qt}\n")
        prefix = "DM " if is_dm else ""
        f.write(f"[{time_str}] {prefix}{long_name} ({short_name}): {text} | {hops}\n")


def _ack_callback(ok: bool, error: str = "") -> None:
    if ok:
        print_formatted_text(HTML("<ansigreen>  ✓ Доставлено</ansigreen>"))
    else:
        print_formatted_text(HTML(
            f"<ansired>  ✗ Не доставлено ({_safe(error)})</ansired>"
        ))


def _make_ack_handler():
    """Returns an onResponse callback suitable for passing to sendText."""
    def handler(packet):
        decoded = packet.get("decoded", {}) if isinstance(packet, dict) else {}
        routing = decoded.get("routing", {})
        error = routing.get("errorReason", "NONE")
        ok = (error == "NONE")
        if _loop:
            _loop.call_soon_threadsafe(_ack_callback, ok, error if not ok else "")
    return handler


# ── pubsub callbacks ──────────────────────────────────────────────────────────

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
        now = datetime.datetime.now().strftime("%H:%M:%S")
        reply_to = _store.get(reply_id) if reply_id else None
        record = MsgRecord(now, long_name, short_name, text)
        if packet_id:
            _store_msg(packet_id, record)

        _log(now, long_name, short_name, text, hops, is_dm, reply_to)
        if _loop:
            _loop.call_soon_threadsafe(
                lambda: _print_msg(now, long_name, short_name,
                                   text, hops, False, is_dm, reply_to)
            )
    except Exception as e:
        if _loop:
            _loop.call_soon_threadsafe(
                print_formatted_text,
                HTML(f"<ansired>[Ошибка: {_safe(str(e))}]</ansired>")
            )


def on_connection_lost(interface):
    if _loop and _reconnect_event:
        _loop.call_soon_threadsafe(
            print_formatted_text,
            HTML("<ansired>⚠ Соединение потеряно. Переподключаюсь...</ansired>")
        )
        _loop.call_soon_threadsafe(_reconnect_event.set)


# ── connection management ─────────────────────────────────────────────────────

_TOPICS = ("meshtastic.receive.text", "meshtastic.connection.lost")


def _subscribe():
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")


def _unsubscribe():
    for fn, topic in ((on_receive, "meshtastic.receive.text"),
                      (on_connection_lost, "meshtastic.connection.lost")):
        try:
            pub.unsubscribe(fn, topic)
        except Exception:
            pass


def _do_connect() -> bool:
    global _interface
    try:
        _interface = meshtastic.tcp_interface.TCPInterface(hostname=HOST)
        _subscribe()
        return True
    except Exception as e:
        if _loop:
            _loop.call_soon_threadsafe(
                print_formatted_text,
                HTML(f"<ansired>Ошибка подключения: {_safe(str(e))}</ansired>")
            )
        return False


async def _reconnect_loop() -> None:
    delays = [3, 5, 10, 30, 60]
    attempt = 0
    while True:
        await _reconnect_event.wait()
        _reconnect_event.clear()

        while True:
            delay = delays[min(attempt, len(delays) - 1)]
            print_formatted_text(HTML(
                f"<ansiyellow>Переподключение через {delay}с "
                f"(попытка {attempt + 1})...</ansiyellow>"
            ))
            await asyncio.sleep(delay)

            _unsubscribe()
            try:
                if _interface:
                    _interface.close()
            except Exception:
                pass

            if _do_connect():
                attempt = 0
                print_formatted_text(HTML("<ansigreen>✓ Переподключено</ansigreen>"))
                break
            attempt += 1


# ── commands ──────────────────────────────────────────────────────────────────

def _cmd_nodes() -> None:
    if not _interface or not _interface.nodes:
        print_formatted_text(HTML("<ansiyellow>Нет данных об узлах</ansiyellow>"))
        return

    now_ts = datetime.datetime.now().timestamp()
    print_formatted_text(HTML("<ansiwhite>─── Видимые узлы ───</ansiwhite>"))

    for nid, node in _interface.nodes.items():
        u = node.get("user", {})
        if u:
            ln = _safe(u.get("longName", str(nid)))
            sn = _safe(u.get("shortName", "???"))
        else:
            num = nid if isinstance(nid, int) else int(str(nid).lstrip("!"), 16)
            cached = _name_cache.get(num)
            ln, sn = (_safe(cached[0]), _safe(cached[1])) if cached else (_safe(str(nid)), "???")

        metrics  = node.get("deviceMetrics", {})
        battery  = metrics.get("batteryLevel")
        bat_str  = f" 🔋{battery}%" if battery is not None else ""

        snr      = node.get("snr")
        snr_str  = f" SNR:{snr:.1f}" if snr is not None else ""

        last     = node.get("lastHeard", 0)
        if last:
            ago = int(now_ts - last)
            if ago < 60:
                heard = f"{ago}с"
                color = "ansigreen"
            elif ago < 3600:
                heard = f"{ago // 60}м"
                color = "ansiyellow"
            else:
                heard = f"{ago // 3600}ч"
                color = "ansigray"
        else:
            heard, color = "?", "ansigray"

        print_formatted_text(HTML(
            f"  <b><ansigreen>{ln}</ansigreen></b> <ansigreen>({sn})</ansigreen>"
            f"<ansiwhite>{bat_str}{snr_str}</ansiwhite>"
            f" <{color}>· {heard} назад</{color}>"
        ))


def _cmd_who() -> None:
    if not _interface:
        return
    my_id = _interface.myInfo.my_node_num
    ln, sn = _node_names(my_id)
    print_formatted_text(HTML(
        f"<ansiwhite>Я: </ansiwhite>"
        f"<b><ansicyan>{_safe(ln)}</ansicyan></b> "
        f"<ansicyan>({_safe(sn)})</ansicyan> "
        f"<ansiwhite>!{my_id:08x}</ansiwhite>"
    ))


def _cmd_dm(args: str) -> None:
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /dm &lt;имя&gt; &lt;текст&gt;</ansiyellow>"
        ))
        return

    target_name, text = parts
    result = _find_node(target_name)
    if not result:
        print_formatted_text(HTML(
            f"<ansired>Узел '{_safe(target_name)}' не найден. "
            f"Проверьте /nodes</ansired>"
        ))
        return

    dest_id, dest_ln, dest_sn = result
    try:
        sent = _interface.sendText(
            text,
            destinationId=dest_id,
            wantAck=True,
            onResponse=_make_ack_handler(),
            channelIndex=0,
        )
        my_id = _interface.myInfo.my_node_num
        ln, sn = _node_names(my_id)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        pid = _packet_id(sent)
        if pid:
            _store_msg(pid, MsgRecord(now, ln, sn, text))
        print_formatted_text(HTML(
            f"<ansired>[{_safe(now)}] → DM "
            f"<b>{_safe(dest_ln)}</b> ({_safe(dest_sn)}): "
            f"{_safe(text)}</ansired>"
        ))
        _log(now, ln, sn, f"→ DM {dest_ln}: {text}", 0)
    except Exception as e:
        print_formatted_text(HTML(f"<ansired>Ошибка отправки: {_safe(str(e))}</ansired>"))


def _cmd_clear() -> None:
    pt_clear()


async def _cmd_updatenames() -> None:
    targets = set(_unresolved_ids)
    if _interface and _interface.nodes:
        for nid, node in _interface.nodes.items():
            if not node.get("user"):
                num = nid if isinstance(nid, int) else int(str(nid).lstrip("!"), 16)
                if num not in _name_cache:
                    targets.add(num)

    if not targets:
        print_formatted_text(HTML(
            "<ansiyellow>Нечего обновлять — все видимые узлы уже с именами</ansiyellow>"
        ))
        return

    targets = sorted(targets)
    print_formatted_text(HTML(
        f"<ansiwhite>Запрашиваю имена {len(targets)} узлов через OneMesh...</ansiwhite>"
    ))

    resolved = 0
    for nid in targets:
        result = await asyncio.to_thread(_fetch_onemesh_name, nid)
        if result:
            _name_cache[nid] = result
            _unresolved_ids.discard(nid)
            resolved += 1
            print_formatted_text(HTML(
                f"  <ansigreen>✓</ansigreen> !{nid:08x} → "
                f"<b>{_safe(result[0])}</b> ({_safe(result[1])})"
            ))
        await asyncio.sleep(ONEMESH_DELAY)

    _save_name_cache()
    print_formatted_text(HTML(
        f"<ansigreen>Готово: обновлено {resolved} из {len(targets)} имён</ansigreen>"
    ))


def _cmd_help() -> None:
    lines = [
        "─── Команды ───────────────────────────────",
        "  /nodes               список видимых узлов",
        "  /who                 информация о себе",
        "  /dm &lt;имя&gt; &lt;текст&gt;   личное сообщение",
        "  /updatenames         подтянуть имена узлов с OneMesh",
        "  /clear               очистить экран",
        "  /help                эта справка",
        "───────────────────────────────────────────",
    ]
    for line in lines:
        print_formatted_text(HTML(f"<ansiwhite>{line}</ansiwhite>"))


async def _handle_command(text: str) -> None:
    parts = text[1:].split(None, 1)
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1] if len(parts) > 1 else ""

    if   cmd == "nodes":       _cmd_nodes()
    elif cmd == "who":         _cmd_who()
    elif cmd == "dm":          _cmd_dm(args)
    elif cmd == "updatenames": await _cmd_updatenames()
    elif cmd == "clear":       _cmd_clear()
    elif cmd == "help":        _cmd_help()
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>Неизвестная команда /{_safe(cmd)}. "
            f"Введите /help</ansiyellow>"
        ))


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _interface, _loop, _reconnect_event
    _loop = asyncio.get_running_loop()
    _reconnect_event = asyncio.Event()
    _load_name_cache()

    print_formatted_text(HTML(
        f"<ansiwhite>Подключение к <ansicyan>{HOST}</ansicyan>...</ansiwhite>"
    ))

    if not _do_connect():
        sys.exit(1)

    my_id = _interface.myInfo.my_node_num
    ln, sn = _node_names(my_id)
    print_formatted_text(HTML(
        f"<b><ansigreen>Подключено.</ansigreen></b> "
        f"Узел: <ansicyan>{_safe(ln)} ({_safe(sn)})</ansicyan> "
        f"<ansiwhite>!{my_id:08x}</ansiwhite>"
    ))

    session = PromptSession()
    print_formatted_text(HTML(
        "<ansiwhite>Сообщение или /help. Ctrl+C / Ctrl+D — выход.</ansiwhite>\n"
    ))

    reconnect_task = asyncio.create_task(_reconnect_loop())

    with patch_stdout():
        while True:
            try:
                text = await session.prompt_async("> ")
                text = text.strip()
                if not text:
                    continue

                if text.startswith("/"):
                    await _handle_command(text)
                    continue

                sent = _interface.sendText(
                    text,
                    wantAck=True,
                    onResponse=_make_ack_handler(),
                    channelIndex=0,
                )
                my_id = _interface.myInfo.my_node_num
                ln, sn = _node_names(my_id)
                now = datetime.datetime.now().strftime("%H:%M:%S")
                pid = _packet_id(sent)
                if pid:
                    _store_msg(pid, MsgRecord(now, ln, sn, text))
                _log(now, ln, sn, text, 0)
                _print_msg(now, ln, sn, text, 0, own=True)

            except (KeyboardInterrupt, EOFError):
                break

    reconnect_task.cancel()
    print_formatted_text(HTML("\n<ansiwhite>Отключение...</ansiwhite>"))
    _unsubscribe()
    try:
        _interface.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
