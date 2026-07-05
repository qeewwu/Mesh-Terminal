#!/usr/bin/env python3

import argparse
import asyncio
import collections
import contextlib
import datetime
import html
import json
import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as pt_clear

from mesh_common import (
    ACK_TIMEOUT_SECONDS,
    BASE_DIR,
    SOCKET_PATH,
    bearing_deg,
    compass_point,
    haversine_km,
    list_log_files,
    parse_log_line,
)

NAME_CACHE_FILE = BASE_DIR / "node_names_cache.json"
HISTORY_SIZE = 100
CH_SWITCH_HISTORY = 10   # сколько истории показать после /ch
SEARCH_LIMIT = 20
LAST_LIMIT = 20
ONLINE_THRESHOLD_SECONDS = 15 * 60  # узел считается «онлайн», если был на связи недавнее этого
RECONNECT_DELAY = 3  # seconds between attempts to re-reach the logger socket
ONEMESH_API = "https://map.onemesh.ru/api/v1/nodes/{}"
ONEMESH_DELAY = 0.25
UPDATENAMES_INTERVAL = 30 * 60  # фоновое /updatenames: при запуске и раз в 30 минут
PING_TIMEOUT = ACK_TIMEOUT_SECONDS + 5.0  # чуть больше, чем логгер сам ждёт ACK

_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HEX_ID_RE = re.compile(r"^!([0-9a-f]{8})$")
# Heuristic for tapback reactions read back from a log file (no "emoji" bit is
# stored in the log format itself): a short quoted reply whose text is nothing
# but emoji is almost certainly a reaction, not a real message.
_EMOJI_ONLY_RE = re.compile(
    "^[\U0001F300-\U0001FAFF☀-➿⬀-⯿️‍\\s]{1,8}$"
)


def _is_reaction_text(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_EMOJI_ONLY_RE.match(t))

_loop: asyncio.AbstractEventLoop | None = None
_client: "IPCClient | None" = None

_name_cache: dict[int, tuple[str, str]] = {}
_unresolved_ids: set[int] = set()
# отображаемое имя (long_name), в нижнем регистре — устройство не хранит
# node_id в текстовом логе, поэтому мьют матчится по имени, а не по id
# (см. CLAUDE.md); переживает переименование ноды только для новых сообщений
_muted_names: set[str] = set()

# None => show/send on every channel (send defaults to Primary); otherwise the
# canonical channel name (and matching index) this session is restricted to.
_channel_filter: str | None = None
_channel_index: int = 0

# Последние полученные live-сообщения с packet_id — цели для /reply
# (новые в конце). История из файлов id не содержит, поэтому реплай доступен
# только на сообщения, пришедшие после запуска клиента.
REPLYABLE_SIZE = 20
_replyables: collections.deque[dict] = collections.deque(maxlen=REPLYABLE_SIZE)

# Кандидаты для tab-автодополнения (обновляются из ответов логгера)
_completion_nodes: list[str] = []
_completion_channels: list[str] = []
_completion_settings: list[str] = []


def _parse_args() -> tuple[str | None, int]:
    ap = argparse.ArgumentParser(
        description="Терминальный чат-клиент Meshtastic (работает через mesh_logger.py)",
        allow_abbrev=False,
    )
    ap.add_argument("-c", "--channel", metavar="ИМЯ",
                    help="показывать и отправлять только в этот канал")
    ap.add_argument("-n", "--history", type=int, default=HISTORY_SIZE, metavar="N",
                    help=f"сколько сообщений истории показать (по умолчанию {HISTORY_SIZE})")
    args, rest = ap.parse_known_args()
    channel = args.channel
    if channel is None:
        # обратная совместимость: `mesh_chat.py --ping` == `--channel ping`
        for arg in rest:
            if arg.startswith("--") and len(arg) > 2:
                channel = arg[2:]
                break
    return channel, max(1, args.history)


def _channel_matches(msg_line: str) -> bool:
    if _channel_filter is None:
        return True
    parsed = parse_log_line(msg_line)
    if not parsed:
        return False
    return parsed.channel.lower() == _channel_filter.lower()


def _is_muted(msg_line: str) -> bool:
    """Only gates the live/history feed (startup, live tail, /ch, reconnect
    replay) — /search and /last still surface muted senders on an explicit
    query, same as /reply staying reachable for them."""
    if not _muted_names:
        return False
    parsed = parse_log_line(msg_line)
    if not parsed or parsed.kind != "message":
        return False
    return (parsed.long_name.strip().lower() in _muted_names
            or parsed.short_name.strip().lower() in _muted_names)

# Live "message" events pushed by the logger before history has finished
# printing are buffered here, then flushed in order once history is done.
_history_ready = False
_pending_live: list[list[str]] = []


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return html.escape(_XML_INVALID.sub("", text or ""))


def _load_name_cache() -> None:
    """The cache file predates /mute: an old file is a flat {id: {long,short}}
    map, so a "names"/"muted" wrapper key is what marks the new format —
    old files load fine with an empty mute list."""
    global _name_cache, _muted_names
    if not NAME_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
        if "names" in raw or "muted" in raw:
            names, muted = raw.get("names", {}), raw.get("muted", [])
        else:
            names, muted = raw, []
        _name_cache = {int(k): (v["long"], v["short"]) for k, v in names.items()}
        _muted_names = set(muted)
    except Exception:
        _name_cache = {}
        _muted_names = set()


def _save_name_cache() -> None:
    try:
        names = {str(k): {"long": v[0], "short": v[1]} for k, v in _name_cache.items()}
        raw = {"names": names, "muted": sorted(_muted_names)}
        NAME_CACHE_FILE.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _fetch_onemesh_name(node_id: int) -> tuple[str, str] | None:
    import urllib.request

    req = urllib.request.Request(
        ONEMESH_API.format(node_id),
        headers={"User-Agent": "mesh-chat/1.0"},
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


def _find_node_in_list(nodes: list[dict], query: str) -> list[dict]:
    """Кандидаты по убыванию точности: точное имя/hex-id → префикс → подстрока.
    Раньше возвращалось первое подстрочное совпадение, и `/dm Alex` мог уйти
    узлу «Alexander», хотя в сети есть точный «Alex»."""
    q = query.lower()
    exact, prefix, substr = [], [], []
    for n in nodes:
        ln = (n.get("long_name") or "").lower()
        sn = (n.get("short_name") or "").lower()
        if q in (ln, sn, f"!{n['node_id']:08x}"):
            exact.append(n)
        elif ln.startswith(q) or sn.startswith(q):
            prefix.append(n)
        elif q in ln or q in sn:
            substr.append(n)
    return exact or prefix or substr


def _pick_node(nodes: list[dict], query: str) -> dict | None:
    """Единственный подходящий узел, иначе None с объяснением пользователю."""
    matches = _find_node_in_list(nodes, query)
    if not matches:
        print_formatted_text(HTML(
            f"<ansired>Узел '{_safe(query)}' не найден. Проверьте /nodes</ansired>"
        ))
        return None
    if len(matches) > 1:
        names = ", ".join(f"{n.get('long_name') or '?'} ({n.get('short_name') or '?'})"
                          for n in matches[:5])
        more = " …" if len(matches) > 5 else ""
        print_formatted_text(HTML(
            f"<ansiyellow>Имя '{_safe(query)}' неоднозначно, совпадают: "
            f"{_safe(names)}{more} — уточните запрос</ansiyellow>"
        ))
        return None
    return matches[0]


def _resolve_display_name(long_name: str, short_name: str) -> tuple[str, str]:
    """If a name looks like our own hex fallback, try the OneMesh cache and
    track it as unresolved otherwise."""
    m = _HEX_ID_RE.match(long_name)
    if not m or short_name != "???":
        return long_name, short_name
    node_id = int(m.group(1), 16)
    if node_id in _name_cache:
        return _name_cache[node_id]
    _unresolved_ids.add(node_id)
    return long_name, short_name


# ── tab completion ────────────────────────────────────────────────────────────

COMMANDS = ["/nodes", "/who", "/dm", "/reply", "/react", "/ch", "/send", "/search",
            "/last", "/trace", "/ping", "/pos", "/stats", "/mute", "/unmute",
            "/updatenames", "/settings", "/clear", "/help"]


class MeshCompleter(Completer):
    """Completes command names, node names after /dm, channel names after /ch."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for c in COMMANDS:
                if c.startswith(text.lower()):
                    yield Completion(c, start_position=-len(text))
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/dm", "/trace", "/ping", "/pos", "/mute", "/unmute", "/last"):
            probe = arg.lstrip('"').lower()
            for cand in _completion_nodes:
                if cand.strip('"').lower().startswith(probe):
                    yield Completion(cand, start_position=-len(arg))
        elif cmd == "/nodes":
            for cand in NODE_SORT_MODES:
                if cand.startswith(arg.lower()):
                    yield Completion(cand, start_position=-len(arg))
        elif cmd == "/ch":
            for cand in _completion_channels + ["all"]:
                if cand.lower().startswith(arg.lower()):
                    yield Completion(cand, start_position=-len(arg))
        elif cmd == "/send":
            # completes only the first token (channel name); leaves the text alone
            if " " not in arg:
                for cand in _completion_channels:
                    if cand.lower().startswith(arg.lower()):
                        yield Completion(cand, start_position=-len(arg))
        elif cmd == "/stats":
            for cand in ("день", "узел"):
                if cand.startswith(arg.lower()):
                    yield Completion(cand, start_position=-len(arg))
        elif cmd == "/settings":
            if " " not in arg:
                for cand in _completion_settings:
                    if cand.lower().startswith(arg.lower()):
                        yield Completion(cand, start_position=-len(arg))


def _update_node_completions(nodes: list[dict]) -> None:
    global _completion_nodes
    out = set()
    for n in nodes:
        sn, ln = n.get("short_name"), n.get("long_name")
        if sn and sn != "???":
            out.add(sn)
        if ln:
            # имя с пробелом дополняем в кавычках — /dm понимает такой синтаксис
            out.add(f'"{ln}"' if " " in ln else ln)
    _completion_nodes = sorted(out)


async def _refresh_completions() -> None:
    global _completion_channels, _completion_settings
    resp = await _client.request("nodes")
    if resp.get("ok"):
        _update_node_completions(resp.get("nodes", []))
    resp = await _client.request("channels")
    if resp.get("ok"):
        _completion_channels = [c["name"] for c in resp.get("channels", [])]
    resp = await _client.request("settings", action="list")
    if resp.get("ok"):
        _completion_settings = [s["key"] for s in resp.get("settings", [])]


# ── IPC client ───────────────────────────────────────────────────────────────

class IPCClient:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._req_counter = 0
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self.connected = False
        self.on_disconnect = None  # called once when the link to the logger dies
        self.whoami: tuple[str, str] | None = None
        self.whoami_id: int | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        self.connected = True
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if "event" in obj:
                    _handle_event(obj)
                else:
                    fut = self._pending.pop(obj.get("req_id"), None)
                    if fut and not fut.done():
                        fut.set_result(obj)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self.connected = False
            # не заставляем запросы в полёте ждать свои 10-секундные таймауты
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result({"ok": False, "error": "нет соединения с логгером"})
            self._pending.clear()
            if not self._closing and self.on_disconnect:
                self.on_disconnect()

    async def request(self, cmd: str, timeout: float = 10.0, **fields) -> dict:
        if not self.connected:
            return {"ok": False, "error": "нет соединения с логгером"}
        self._req_counter += 1
        req_id = self._req_counter
        fut = self._loop.create_future()
        self._pending[req_id] = fut
        payload = {"req_id": req_id, "cmd": cmd, **fields}
        try:
            self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception:
            self.connected = False
            self._pending.pop(req_id, None)
            return {"ok": False, "error": "нет соединения с логгером"}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"ok": False, "error": "timeout"}

    async def close(self) -> None:
        self._closing = True
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()


def _snippet(text: str, limit: int = 30) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


# /ping ждёт delivery-событие своего собственного packet_id напрямую (см. _cmd_ping);
# когда оно найдено здесь, обычная печать "✓ Доставлено" подавляется — команда
# сама покажет результат вместе с измеренным временем.
_pending_pings: dict[int, asyncio.Future] = {}


def _handle_event(obj: dict) -> None:
    kind = obj.get("event")
    if kind == "delivery":
        pid = obj.get("packet_id")
        fut = _pending_pings.pop(pid, None) if pid else None
        if fut is not None:
            if not fut.done():
                fut.set_result(obj)
            return
        ref = f' "{_safe(_snippet(obj.get("text", "")))}"'
        ok = obj.get("ok")
        if ok is True:
            hops = obj.get("hops")
            hop_str = f" ({hops} хоп{'' if hops == 1 else 'а' if 2 <= hops <= 4 else 'ов'})" \
                if hops is not None else ""
            print_formatted_text(HTML(f"<ansigreen>  ✓ Доставлено{hop_str}{ref}</ansigreen>"))
        elif ok is False:
            print_formatted_text(HTML(
                f"<ansired>  ✗ Не доставлено ({_safe(str(obj.get('error', '')))}){ref}</ansired>"
            ))
        else:
            print_formatted_text(HTML(
                f"<ansiyellow>  ⏳ Нет подтверждения доставки{ref} "
                f"(мог дойти, но ACK не получен)</ansiyellow>"
            ))
    elif kind == "message":
        lines = obj.get("lines", [])
        if not lines or not _channel_matches(lines[-1]):
            return
        if _is_muted(lines[-1]):
            return
        if obj.get("packet_id"):
            _replyables.append({
                "packet_id": obj["packet_id"],
                "from_id": obj.get("from_id"),
                "to_id": obj.get("to_id"),
                "is_dm": obj.get("is_dm", False),
                "channel_index": obj.get("channel_index", 0),
                "line": lines[-1],
            })
        if _history_ready:
            quote = lines[0] if len(lines) == 2 else None
            _render_if_new(quote, lines[-1])
        else:
            _pending_live.append(lines)


# ── display ───────────────────────────────────────────────────────────────────

def _print_quote(long_name: str, short_name: str, text: str, time_str: str = "") -> None:
    ln, sn = _resolve_display_name(long_name, short_name)
    t = f"[{_safe(time_str)}] " if time_str else ""
    print_formatted_text(HTML(
        f"<ansigray>  ┆ {t}{_safe(ln)} ({_safe(sn)}): {_safe(text)}</ansigray>"
    ))


def _print_msg(time_str: str, long_name: str, short_name: str,
               text: str, hops: int, own: bool = False, is_dm: bool = False,
               dm_out: bool = False, channel_name: str = "",
               snr: float | None = None) -> None:
    ln, sn = _resolve_display_name(long_name, short_name)
    t  = _safe(time_str)
    ln = _safe(ln)
    sn = _safe(sn)
    tx = _safe(text)
    ch = f"<ansimagenta>{_safe(channel_name)}</ansimagenta> " if _channel_filter is None else ""
    snr_str = f" SNR:{snr:.1f}" if snr is not None else ""

    if is_dm:
        # outgoing DM: the logged name is the recipient, so mark direction and
        # use the own-message colour; incoming DM stays red
        dm_color = "ansiblue" if dm_out else "ansired"
        arrow = "→ " if dm_out else ""
        print_formatted_text(HTML(
            f"{ch}<{dm_color}>[{t}] DM {arrow}<b>{ln}</b> ({sn}): {tx} | {hops}{snr_str}</{dm_color}>"
        ))
    else:
        color = "ansiblue" if own else "ansigreen"
        print_formatted_text(HTML(
            f"{ch}<ansiwhite>[{t}]</ansiwhite> "
            f"<b><{color}>{ln}</{color}></b> "
            f"<{color}>({sn})</{color}>"
            f": {tx} "
            f"<ansiwhite>| {hops}{snr_str}</ansiwhite>"
        ))


def _print_reaction(parsed, quote_parsed) -> None:
    """Compact one-line rendering of a tapback reaction on top of its quote."""
    ln, sn = _resolve_display_name(parsed.long_name, parsed.short_name)
    qln, _ = _resolve_display_name(quote_parsed.long_name, quote_parsed.short_name)
    qt = f"[{_safe(quote_parsed.time_str)}] " if quote_parsed.time_str else ""
    print_formatted_text(HTML(
        f"<ansigray>  {_safe(parsed.text)} <b>{_safe(ln)}</b> → "
        f"{qt}{_safe(qln)}: «{_safe(_snippet(quote_parsed.text, 40))}»</ansigray>"
    ))


def _render_line(line: str) -> None:
    parsed = parse_log_line(line)
    if not parsed:
        return
    if parsed.kind == "quote":
        _print_quote(parsed.long_name, parsed.short_name, parsed.text, parsed.time_str)
    else:
        own = (_client.whoami is not None
              and (parsed.long_name, parsed.short_name) == _client.whoami)
        _print_msg(parsed.time_str, parsed.long_name, parsed.short_name,
                   parsed.text, parsed.hops, own=own, is_dm=parsed.is_dm,
                   dm_out=parsed.dm_out, channel_name=parsed.channel, snr=parsed.snr)


def _render_unit(quote: str | None, msg: str) -> None:
    """Render a (quote, message) pair, compacting tapback reactions to one line."""
    parsed = parse_log_line(msg)
    quote_parsed = parse_log_line(quote) if quote else None
    if parsed and parsed.kind == "message" and quote_parsed and _is_reaction_text(parsed.text):
        _print_reaction(parsed, quote_parsed)
        return
    if quote:
        _render_line(quote)
    _render_line(msg)


# Последние отрисованные строки сообщений. Закрывает гонки двойного показа:
# сообщение, записанное в лог между чтением истории и обработкой live-событий
# (при старте), и между live-событиями нового сокета и перечитыванием лога
# (_replay_missed при реконнекте к логгеру), пришло бы на экран дважды.
_recent_lines: collections.deque[str] = collections.deque(maxlen=200)


def _render_if_new(quote: str | None, msg: str) -> None:
    if msg in _recent_lines:
        return
    _recent_lines.append(msg)
    _render_unit(quote, msg)


# ── history + live tail ────────────────────────────────────────────────────────

def _split_into_units(lines: list[str]) -> list[tuple[str | None, str]]:
    """Pair each quote line with the message line that follows it."""
    units: list[tuple[str | None, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("  ┆ ") and i + 1 < len(lines):
            units.append((lines[i], lines[i + 1]))
            i += 2
        else:
            units.append((None, lines[i]))
            i += 1
    return units


def _print_initial_history(n: int = HISTORY_SIZE) -> None:
    units: list[tuple[str | None, str]] = []
    for path in list_log_files():  # newest date first
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        file_units = _split_into_units(lines)
        if _channel_filter is not None:
            file_units = [u for u in file_units if _channel_matches(u[1])]
        if _muted_names:
            file_units = [u for u in file_units if not _is_muted(u[1])]
        units = file_units + units
        if len(units) >= n:
            break

    for quote, msg in units[-n:]:
        # note but don't skip: /ch re-shows recent lines on purpose
        _recent_lines.append(msg)
        _render_unit(quote, msg)


# ── reconnect to logger ───────────────────────────────────────────────────────

_reconnecting = False


def _on_logger_lost() -> None:
    global _reconnecting
    if _reconnecting:
        return
    _reconnecting = True
    print_formatted_text(HTML(
        "<ansired>⚠ Соединение с логгером потеряно — переподключаюсь...</ansired>"
    ))
    _loop.create_task(_reconnect_logger(datetime.datetime.now()))


def _replay_missed(lost_at: datetime.datetime) -> None:
    """Render messages that were logged while we were disconnected."""
    cutoff_time = lost_at.strftime("%H:%M:%S")
    for path in sorted(list_log_files()):  # oldest date first
        try:
            file_date = datetime.date.fromisoformat(path.stem.removeprefix("chat-"))
        except ValueError:
            continue
        if file_date < lost_at.date():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for quote, msg in _split_into_units(lines):
            parsed = parse_log_line(msg)
            if not parsed:
                continue
            if file_date == lost_at.date() and parsed.time_str < cutoff_time:
                continue
            if not _channel_matches(msg):
                continue
            if _is_muted(msg):
                continue
            _render_if_new(quote, msg)


async def _reconnect_logger(lost_at: datetime.datetime) -> None:
    global _client, _reconnecting
    old = _client
    while True:
        await asyncio.sleep(RECONNECT_DELAY)
        if not SOCKET_PATH.exists():
            continue
        client = IPCClient(_loop)
        try:
            await client.connect()
            break
        except Exception:
            continue
    with contextlib.suppress(Exception):
        await old.close()
    client.on_disconnect = _on_logger_lost
    client.whoami, client.whoami_id = old.whoami, old.whoami_id
    who = await client.request("whoami")
    if who.get("ok"):
        client.whoami = (who["long_name"], who["short_name"])
        client.whoami_id = who["node_id"]
    _client = client
    _reconnecting = False
    print_formatted_text(HTML(
        "<ansigreen>✓ Соединение с логгером восстановлено</ansigreen>"
    ))
    _replay_missed(lost_at)


# ── commands ──────────────────────────────────────────────────────────────────

NODE_SORT_MODES = ("online", "names", "hops")


def _node_sort_key(n: dict, mode: str = "online"):
    is_online = n["seconds_ago"] is not None and n["seconds_ago"] < ONLINE_THRESHOLD_SECONDS
    name_key = n["display_long"].lower()
    hops = n.get("hops_away")
    hops_key = hops if hops is not None else 999
    if mode == "names":
        return (name_key, not is_online, hops_key)
    if mode == "hops":
        return (hops_key, not is_online, name_key)
    return (not is_online, name_key, hops_key)  # mode == "online" (default)


async def _cmd_nodes(args: str = "") -> None:
    mode = args.strip().lower() or "online"
    if mode not in NODE_SORT_MODES:
        print_formatted_text(HTML(
            f"<ansiyellow>Неизвестная сортировка '{_safe(mode)}'. "
            f"Доступные: {', '.join(NODE_SORT_MODES)}</ansiyellow>"
        ))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok") or not resp.get("nodes"):
        print_formatted_text(HTML("<ansiyellow>Нет данных об узлах</ansiyellow>"))
        return

    _update_node_completions(resp["nodes"])

    enriched = []
    for n in resp["nodes"]:
        if n["has_user"]:
            ln, sn = n["long_name"], n["short_name"]
        else:
            cached = _name_cache.get(n["node_id"])
            if cached:
                ln, sn = cached
            else:
                ln, sn = f"!{n['node_id']:08x}", "???"
                _unresolved_ids.add(n["node_id"])
        enriched.append({**n, "display_long": ln, "display_short": sn})

    enriched.sort(key=lambda n: _node_sort_key(n, mode))

    print_formatted_text(HTML("<ansiwhite>─── Видимые узлы ───</ansiwhite>"))
    for n in enriched:
        ln, sn = _safe(n["display_long"]), _safe(n["display_short"])

        battery = n.get("battery")
        bat_str = f" 🔋{battery}%" if battery is not None else ""
        snr = n.get("snr")
        snr_str = f" SNR:{snr:.1f}" if snr is not None else ""
        hops = n.get("hops_away")
        hops_str = f" · {hops} хоп{'' if hops == 1 else 'а' if 2 <= hops <= 4 else 'ов'}" \
            if hops is not None else ""

        ago = n["seconds_ago"]
        if ago is not None and ago < ONLINE_THRESHOLD_SECONDS:
            heard, color = "Онлайн", "ansigreen"
        elif ago is not None:
            if ago < 3600:
                heard, color = f"{ago // 60}м назад", "ansiyellow"
            else:
                heard, color = f"{ago // 3600}ч назад", "ansigray"
        else:
            heard, color = "?", "ansigray"

        print_formatted_text(HTML(
            f"  <b><ansigreen>{ln}</ansigreen></b> <ansigreen>({sn})</ansigreen>"
            f"<ansiwhite>{bat_str}{snr_str}{hops_str}</ansiwhite>"
            f" <{color}>· {heard}</{color}>"
        ))


def _cmd_who() -> None:
    if not _client.whoami:
        print_formatted_text(HTML("<ansiyellow>Информация о себе недоступна</ansiyellow>"))
        return
    ln, sn = _client.whoami
    id_str = f" !{_client.whoami_id:08x}" if _client.whoami_id is not None else ""
    print_formatted_text(HTML(
        f"<ansiwhite>Я: </ansiwhite>"
        f"<b><ansicyan>{_safe(ln)}</ansicyan></b> "
        f"<ansicyan>({_safe(sn)})</ansicyan>"
        f"<ansiwhite>{id_str}</ansiwhite>"
    ))


def _handle_send_response(resp: dict, text: str) -> None:
    if resp.get("ok"):
        if resp.get("queued"):
            print_formatted_text(HTML(
                f"<ansiyellow>  ⏳ В очереди: «{_safe(_snippet(text, 60))}» — устройство "
                f"офлайн, отправлю сразу при переподключении</ansiyellow>"
            ))
        return
    print_formatted_text(HTML(
        f"<ansired>Ошибка отправки: {_safe(resp.get('error', ''))}</ansired>"
    ))


def _split_dm_args(args: str) -> tuple[str | None, str | None]:
    """`/dm Имя текст` или `/dm "Имя С Пробелами" текст`."""
    args = args.strip()
    if args.startswith('"'):
        end = args.find('"', 1)
        if end > 1:
            return args[1:end], args[end + 1:].strip() or None
        return None, None
    parts = args.split(None, 1)
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


async def _cmd_dm(args: str) -> None:
    target_name, text = _split_dm_args(args)
    if not target_name or not text:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /dm &lt;имя&gt; &lt;текст&gt; "
            "(имя с пробелами — в кавычках)</ansiyellow>"
        ))
        return
    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось получить список узлов: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return

    node = _pick_node(resp["nodes"], target_name)
    if not node:
        return

    send_resp = await _client.request("dm", node_id=node["node_id"], text=text,
                                       channel=_channel_index)
    _handle_send_response(send_resp, text)


TRACE_TIMEOUT = 45.0  # a bit above the logger's own TRACE_TIMEOUT (40s)


def _fmt_trace_hop(h: dict) -> str:
    ln, sn = _resolve_display_name(h.get("long_name") or "", h.get("short_name") or "???")
    snr = h.get("snr")
    snr_str = f" ({snr:+.1f}dB)" if snr is not None else " (?dB)"
    return f"{_safe(ln)} ({_safe(sn)}){snr_str}"


async def _cmd_trace(args: str) -> None:
    target_name = args.strip()
    if not target_name:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /trace &lt;имя узла&gt;</ansiyellow>"
        ))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось получить список узлов: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return
    node = _pick_node(resp["nodes"], target_name)
    if not node:
        return

    print_formatted_text(HTML(
        f"<ansiwhite>Трассирую до {_safe(node.get('long_name') or '?')} — "
        f"может занять до {int(TRACE_TIMEOUT)}с...</ansiwhite>"
    ))
    trace_resp = await _client.request("trace", node_id=node["node_id"],
                                        timeout=TRACE_TIMEOUT)
    if not trace_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Traceroute не удался: {_safe(trace_resp.get('error', ''))}</ansired>"
        ))
        return

    towards = trace_resp.get("towards", [])
    back = trace_resp.get("back", [])
    if towards:
        print_formatted_text(HTML(
            f"<ansiwhite>Маршрут туда:</ansiwhite> "
            + " → ".join(_fmt_trace_hop(h) for h in towards)
        ))
    if back:
        print_formatted_text(HTML(
            f"<ansiwhite>Маршрут обратно:</ansiwhite> "
            + " → ".join(_fmt_trace_hop(h) for h in back)
        ))
    else:
        print_formatted_text(HTML(
            "<ansigray>(обратный маршрут не получен)</ansigray>"
        ))


async def _cmd_ping(args: str) -> None:
    """Меряет RTT до узла: DM с wantAck, дожидаемся своего delivery-события
    по packet_id (см. _pending_pings в _handle_event) и печатаем время + число
    хопов, которое ACK принёс с собой (см. handler() в mesh_logger.py)."""
    target_name = args.strip()
    if not target_name:
        print_formatted_text(HTML("<ansiyellow>Использование: /ping &lt;имя узла&gt;</ansiyellow>"))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось получить список узлов: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return
    node = _pick_node(resp["nodes"], target_name)
    if not node:
        return

    ln, sn = node.get("long_name") or "?", node.get("short_name") or "?"
    print_formatted_text(HTML(f"<ansiwhite>🏓 Пингую {_safe(ln)} ({_safe(sn)})...</ansiwhite>"))

    start = _loop.time()
    send_resp = await _client.request("dm", node_id=node["node_id"], text="🏓 ping",
                                       channel=_channel_index)
    if not send_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Ошибка отправки: {_safe(send_resp.get('error', ''))}</ansired>"
        ))
        return
    if send_resp.get("queued"):
        print_formatted_text(HTML(
            "<ansiyellow>⏳ Устройство офлайн — пинг встал в очередь, RTT не измерить</ansiyellow>"
        ))
        return
    pid = send_resp.get("packet_id")
    if not pid:
        print_formatted_text(HTML("<ansired>Не удалось получить packet_id для пинга</ansired>"))
        return

    fut = _loop.create_future()
    _pending_pings[pid] = fut
    try:
        delivery = await asyncio.wait_for(fut, timeout=PING_TIMEOUT)
    except asyncio.TimeoutError:
        _pending_pings.pop(pid, None)
        print_formatted_text(HTML(
            f"<ansired>🏓 {_safe(ln)}: нет ответа за {int(PING_TIMEOUT)}с</ansired>"
        ))
        return

    elapsed = _loop.time() - start
    ok = delivery.get("ok")
    if ok is True:
        hops = delivery.get("hops")
        hop_str = f", {hops} хоп{'' if hops == 1 else 'а' if 2 <= hops <= 4 else 'ов'}" \
            if hops is not None else ""
        print_formatted_text(HTML(
            f"<ansigreen>🏓 {_safe(ln)}: доставлено за {elapsed:.1f}с{hop_str}</ansigreen>"
        ))
    elif ok is False:
        print_formatted_text(HTML(
            f"<ansired>🏓 {_safe(ln)}: не доставлено "
            f"({_safe(str(delivery.get('error', '')))})</ansired>"
        ))
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>🏓 {_safe(ln)}: нет подтверждения доставки за {elapsed:.1f}с "
            "(мог дойти, но ACK не получен)</ansiyellow>"
        ))


async def _cmd_pos(args: str) -> None:
    target_name = args.strip()
    if not target_name:
        print_formatted_text(HTML("<ansiyellow>Использование: /pos &lt;имя узла&gt;</ansiyellow>"))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось получить список узлов: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return
    nodes = resp["nodes"]
    node = _pick_node(nodes, target_name)
    if not node:
        return
    if node.get("lat") is None or node.get("lon") is None:
        print_formatted_text(HTML(
            f"<ansiyellow>У узла {_safe(node.get('long_name') or '?')} нет данных о позиции "
            "(не шлёт GPS-координаты)</ansiyellow>"
        ))
        return

    ln, sn = node.get("long_name") or "?", node.get("short_name") or "?"
    alt = node.get("alt")
    alt_str = f", {alt} м" if alt is not None else ""
    print_formatted_text(HTML(
        f"<ansiwhite>📍 {_safe(ln)} ({_safe(sn)}): "
        f"{node['lat']:.5f}, {node['lon']:.5f}{alt_str}</ansiwhite>"
    ))

    me = next((n for n in nodes if n["node_id"] == _client.whoami_id), None) \
        if _client.whoami_id is not None else None
    if me and me.get("lat") is not None and me.get("lon") is not None:
        dist = haversine_km(me["lat"], me["lon"], node["lat"], node["lon"])
        brng = bearing_deg(me["lat"], me["lon"], node["lat"], node["lon"])
        dist_str = f"{dist:.1f} км" if dist >= 1 else f"{dist * 1000:.0f} м"
        print_formatted_text(HTML(
            f"<ansiwhite>   от меня: {dist_str}, азимут {brng:.0f}° "
            f"({compass_point(brng)})</ansiwhite>"
        ))
    else:
        print_formatted_text(HTML(
            "<ansigray>   (у моего узла нет своей позиции — расстояние не посчитать)</ansigray>"
        ))


def _reply_label(r: dict, num: int) -> str:
    parsed = parse_log_line(r["line"])
    if not parsed:
        return f"#{num}"
    ln, _ = _resolve_display_name(parsed.long_name, parsed.short_name)
    dm = "DM " if parsed.is_dm else ""
    return (f"  #{num} [{_safe(parsed.time_str)}] {dm}<b>{_safe(ln)}</b>: "
            f"{_safe(_snippet(parsed.text, 50))}")


def _list_replyables() -> None:
    print_formatted_text(HTML(
        "<ansiwhite>─── На что можно ответить (#1 — самое свежее) ───</ansiwhite>"
    ))
    for num, r in enumerate(reversed(_replyables), start=1):
        print_formatted_text(HTML(f"<ansiwhite>{_reply_label(r, num)}</ansiwhite>"))
    print_formatted_text(HTML(
        "<ansigray>Ответить: /reply #&lt;номер&gt; &lt;текст&gt; — "
        "или /reply &lt;текст&gt; на #1</ansigray>"
    ))


def _parse_target_and_text(args: str, usage_html: str) -> tuple[dict, str] | None:
    """Общий разбор `#N <текст>` / `<текст>` (на #1), используется /reply и /react."""
    if args.startswith("#"):
        num_str, _, text = args[1:].partition(" ")
        text = text.strip()
        if not num_str.isdigit() or not text:
            print_formatted_text(HTML(usage_html))
            return None
        num = int(num_str)
        if not 1 <= num <= len(_replyables):
            print_formatted_text(HTML(
                f"<ansired>Нет сообщения #{num} — доступны #1…#{len(_replyables)}</ansired>"
            ))
            return None
        return _replyables[-num], text
    return _replyables[-1], args


async def _send_to_reply_target(r: dict, text: str, emoji: bool = False) -> dict:
    fields = dict(text=text, reply_id=r["packet_id"], channel=r["channel_index"], emoji=emoji)
    if r["is_dm"]:
        # For both incoming and outgoing DM: use to_id if it exists (outgoing),
        # otherwise from_id (incoming)
        target = r.get("to_id") or r.get("from_id")
        if target:
            return await _client.request("dm", node_id=target, **fields)
    return await _client.request("send", **fields)


async def _cmd_reply(args: str) -> None:
    args = args.strip()
    if not _replyables:
        print_formatted_text(HTML(
            "<ansiyellow>Пока нечего цитировать: /reply работает для сообщений, "
            "полученных после запуска клиента</ansiyellow>"
        ))
        return

    if not args:
        _list_replyables()
        return

    target = _parse_target_and_text(args, (
        "<ansiyellow>Использование: /reply #&lt;номер&gt; &lt;текст&gt; "
        "(номера — в /reply без аргументов)</ansiyellow>"
    ))
    if target is None:
        return
    r, text = target

    parsed = parse_log_line(r["line"])
    if parsed:
        print_formatted_text(HTML(
            f"<ansigray>Отвечаю на: [{_safe(parsed.time_str)}] {_safe(parsed.long_name)} — "
            f"{_safe(_snippet(parsed.text))}</ansigray>"
        ))
    resp = await _send_to_reply_target(r, text)
    _handle_send_response(resp, text)


async def _cmd_react(args: str) -> None:
    args = args.strip()
    if not _replyables:
        print_formatted_text(HTML(
            "<ansiyellow>Пока нечего реактить: /react работает для сообщений, "
            "полученных после запуска клиента</ansiyellow>"
        ))
        return

    if not args:
        _list_replyables()
        print_formatted_text(HTML(
            "<ansigray>Реакция: /react #&lt;номер&gt; &lt;эмодзи&gt; — "
            "или /react &lt;эмодзи&gt; на #1</ansigray>"
        ))
        return

    target = _parse_target_and_text(args, (
        "<ansiyellow>Использование: /react #&lt;номер&gt; &lt;эмодзи&gt; "
        "(номера — в /react без аргументов)</ansiyellow>"
    ))
    if target is None:
        return
    r, emoji_text = target

    parsed = parse_log_line(r["line"])
    if parsed:
        print_formatted_text(HTML(
            f"<ansigray>Реагирую на: [{_safe(parsed.time_str)}] {_safe(parsed.long_name)} — "
            f"{_safe(_snippet(parsed.text))}</ansigray>"
        ))
    resp = await _send_to_reply_target(r, emoji_text, emoji=True)
    _handle_send_response(resp, emoji_text)


async def _cmd_ch(args: str) -> None:
    global _channel_filter, _channel_index, _completion_channels
    name = args.strip()
    resp = await _client.request("channels")
    channels = resp.get("channels", []) if resp.get("ok") else []
    if channels:
        _completion_channels = [c["name"] for c in channels]

    if not name:
        current = _channel_filter or "все"
        print_formatted_text(HTML(
            f"<ansiwhite>Сейчас: <b>{_safe(current)}</b>. Каналы: "
            f"{_safe(', '.join(c['name'] for c in channels) or 'нет данных')}. "
            f"Переключение: /ch &lt;имя&gt; или /ch all</ansiwhite>"
        ))
        return

    if name.lower() in ("all", "*", "все"):
        _channel_filter = None
        _channel_index = 0
        print_formatted_text(HTML(
            "<b><ansicyan>Все каналы (отправка — в Primary)</ansicyan></b>"
        ))
        return

    match = next((c for c in channels if c["name"].lower() == name.lower()), None)
    if not match:
        available = ", ".join(c["name"] for c in channels) or "нет данных"
        print_formatted_text(HTML(
            f"<ansired>Канал '{_safe(name)}' не найден. "
            f"Доступные: {_safe(available)}</ansired>"
        ))
        return

    _channel_filter = match["name"]
    _channel_index = match["index"]
    # цели /reply из других каналов после переключения удивили бы
    kept = [r for r in _replyables if r["channel_index"] == match["index"]]
    _replyables.clear()
    _replyables.extend(kept)
    print_formatted_text(HTML(
        f"<b><ansicyan>Канал: {_safe(_channel_filter)}</ansicyan></b>"
    ))
    _print_initial_history(CH_SWITCH_HISTORY)


async def _cmd_send(args: str) -> None:
    """One-off send to a specific channel without switching the session's
    current channel (unlike /ch, which changes what /reply and history filter to)."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /send &lt;канал&gt; &lt;текст&gt;</ansiyellow>"
        ))
        return
    channel_name, text = parts

    resp = await _client.request("channels")
    channels = resp.get("channels", []) if resp.get("ok") else []
    match = next((c for c in channels if c["name"].lower() == channel_name.lower()), None)
    if not match:
        available = ", ".join(c["name"] for c in channels) or "нет данных"
        print_formatted_text(HTML(
            f"<ansired>Канал '{_safe(channel_name)}' не найден. "
            f"Доступные: {_safe(available)}</ansired>"
        ))
        return

    send_resp = await _client.request("send", text=text, channel=match["index"])
    _handle_send_response(send_resp, text)


def _scan_units(predicate, limit: int) -> list[tuple[str, str | None, str]]:
    """Walk logs/ newest-file-first, keep (date, quote, msg) units whose
    message line matches `predicate(parsed)`, stop once `limit` collected.
    Shared by /search and /last — same file-walk/limit/ordering logic, just a
    different match rule; per-file order stays oldest-to-newest, and prepending
    each (older) file's matches keeps the overall list chronological."""
    matches: list[tuple[str, str | None, str]] = []
    for path in list_log_files():  # newest date first
        date_str = path.stem.removeprefix("chat-")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        found = []
        for quote, msg in _split_into_units(lines):
            parsed = parse_log_line(msg)
            if not parsed or parsed.kind != "message" or not _channel_matches(msg):
                continue
            if predicate(parsed):
                found.append((date_str, quote, msg))
        matches = found + matches
        if len(matches) >= limit:
            break
    return matches


def _print_unit_matches(header: str, matches: list[tuple[str, str | None, str]],
                         limit: int, empty_message: str) -> None:
    if not matches:
        print_formatted_text(HTML(f"<ansiyellow>{empty_message}</ansiyellow>"))
        return
    shown = matches[-limit:]
    suffix = f" (показаны последние {limit})" if len(matches) > limit else ""
    print_formatted_text(HTML(f"<ansiwhite>{header}{_safe(suffix)} ───</ansiwhite>"))
    last_date = None
    for date_str, quote, msg in shown:
        if date_str != last_date:
            print_formatted_text(HTML(f"<ansigray>── {date_str} ──</ansigray>"))
            last_date = date_str
        _render_unit(quote, msg)


def _cmd_search(args: str) -> None:
    query = args.strip().lower()
    if not query:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /search &lt;текст&gt;</ansiyellow>"
        ))
        return

    matches = _scan_units(
        lambda p: (query in p.text.lower() or query in p.long_name.lower()
                   or query in p.short_name.lower()),
        SEARCH_LIMIT,
    )
    _print_unit_matches(f"─── Поиск: «{_safe(query)}»", matches, SEARCH_LIMIT,
                        f"По запросу «{_safe(query)}» ничего не найдено")


def _cmd_last(args: str) -> None:
    query = args.strip()
    if not query:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /last &lt;имя узла&gt; (короткое или полное, "
            "точное совпадение)</ansiyellow>"
        ))
        return

    q = query.lower()
    matches = _scan_units(
        lambda p: p.long_name.lower() == q or p.short_name.lower() == q,
        LAST_LIMIT,
    )
    _print_unit_matches(f"─── Последние сообщения от «{_safe(query)}»", matches, LAST_LIMIT,
                        f"Сообщений от «{_safe(query)}» не найдено")


def _collect_stats() -> dict:
    """Synchronous — called via asyncio.to_thread so a large logs/ directory
    doesn't stall the prompt. Device connection not needed: everything comes
    from logs/, same as /search."""
    per_day: collections.Counter = collections.Counter()
    per_node: collections.Counter = collections.Counter()
    per_hour: collections.Counter = collections.Counter()
    total = 0
    for path in list_log_files():
        date_str = path.stem.removeprefix("chat-")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            parsed = parse_log_line(line)
            if not parsed or parsed.kind != "message":
                continue
            if _channel_filter is not None and parsed.channel.lower() != _channel_filter.lower():
                continue
            total += 1
            per_day[date_str] += 1
            per_node[(parsed.long_name, parsed.short_name)] += 1
            if parsed.time_str[:2].isdigit():
                per_hour[int(parsed.time_str[:2])] += 1
    return {"total": total, "per_day": per_day, "per_node": per_node, "per_hour": per_hour}


def _bar(count: int, max_count: int, width: int = 20) -> str:
    if max_count <= 0 or count <= 0:
        return ""
    return "█" * max(1, round(count / max_count * width))


async def _cmd_stats(args: str = "") -> None:
    mode = args.strip().lower()
    stats = await asyncio.to_thread(_collect_stats)
    if stats["total"] == 0:
        print_formatted_text(HTML("<ansiyellow>Нет данных для статистики</ansiyellow>"))
        return

    if mode in ("день", "дни", "day", "days"):
        print_formatted_text(HTML("<ansiwhite>─── Сообщений по дням ───</ansiwhite>"))
        days = sorted(stats["per_day"].items())
        max_count = max(c for _, c in days)
        for date_str, count in days:
            print_formatted_text(HTML(
                f"  {date_str}  <ansigreen>{_bar(count, max_count)}</ansigreen> {count}"
            ))
        return

    if mode in ("узел", "узлы", "node", "nodes"):
        print_formatted_text(HTML("<ansiwhite>─── Топ активных узлов ───</ansiwhite>"))
        top = stats["per_node"].most_common(15)
        max_count = top[0][1] if top else 0
        for (ln, sn), count in top:
            dln, dsn = _resolve_display_name(ln, sn)
            print_formatted_text(HTML(
                f"  <b>{_safe(dln)}</b> ({_safe(dsn)})  "
                f"<ansigreen>{_bar(count, max_count)}</ansigreen> {count}"
            ))
        return

    days_count = len(stats["per_day"])
    top_nodes = stats["per_node"].most_common(5)
    busiest_hour = stats["per_hour"].most_common(1)
    print_formatted_text(HTML("<ansiwhite>─── Статистика ───</ansiwhite>"))
    print_formatted_text(HTML(f"  Всего сообщений: <b>{stats['total']}</b> за {days_count} дн."))
    if busiest_hour:
        hour, cnt = busiest_hour[0]
        print_formatted_text(HTML(f"  Самый активный час: {hour:02d}:00 ({cnt} сообщений)"))
    if top_nodes:
        print_formatted_text(HTML("  Топ узлов:"))
        for (ln, sn), count in top_nodes:
            dln, dsn = _resolve_display_name(ln, sn)
            print_formatted_text(HTML(f"    <b>{_safe(dln)}</b> ({_safe(dsn)}) — {count}"))
    print_formatted_text(HTML("<ansigray>Подробнее: /stats день · /stats узел</ansigray>"))


def _cmd_clear() -> None:
    pt_clear()


async def _cmd_updatenames(quiet: bool = False) -> None:
    """quiet=True — фоновый вызов (при старте и раз в UPDATENAMES_INTERVAL):
    без промежуточных строк, только короткая сводка если что-то нашлось, и
    полная тишина, если обновлять нечего — команда не должна отвлекать
    от набора текста в разговоре."""
    targets = set(_unresolved_ids)
    resp = await _client.request("nodes")
    if resp.get("ok"):
        for n in resp["nodes"]:
            if not n["has_user"] and n["node_id"] not in _name_cache:
                targets.add(n["node_id"])

    if not targets:
        if not quiet:
            print_formatted_text(HTML(
                "<ansiyellow>Нечего обновлять — все видимые узлы уже с именами</ansiyellow>"
            ))
        return

    targets = sorted(targets)
    if not quiet:
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
            if not quiet:
                print_formatted_text(HTML(
                    f"  <ansigreen>✓</ansigreen> !{nid:08x} → "
                    f"<b>{_safe(result[0])}</b> ({_safe(result[1])})"
                ))
        await asyncio.sleep(ONEMESH_DELAY)

    if resolved:
        _save_name_cache()
    if quiet:
        if resolved:
            print_formatted_text(HTML(
                f"<ansigray>🔎 Автообновление имён: +{resolved} новых</ansigray>"
            ))
    else:
        print_formatted_text(HTML(
            f"<ansigreen>Готово: обновлено {resolved} из {len(targets)} имён</ansigreen>"
        ))


async def _periodic_updatenames() -> None:
    while True:
        with contextlib.suppress(Exception):
            await _cmd_updatenames(quiet=True)
        await asyncio.sleep(UPDATENAMES_INTERVAL)


def _list_muted() -> None:
    if not _muted_names:
        print_formatted_text(HTML("<ansiyellow>Список замьюченных пуст</ansiyellow>"))
        return
    print_formatted_text(HTML("<ansiwhite>─── Замьюченные ───</ansiwhite>"))
    for name in sorted(_muted_names):
        print_formatted_text(HTML(f"  {_safe(name)}"))
    print_formatted_text(HTML("<ansigray>Снять: /unmute &lt;имя&gt;</ansigray>"))


async def _cmd_mute(args: str) -> None:
    """Мьютит по имени, а не по node_id: лог хранит только текстовые имена
    (см. CLAUDE.md), поэтому это единственный ключ, который работает и для
    живых, и для исторических сообщений. Живой список узлов используется
    только чтобы взять каноническое long_name при точном совпадении; если
    узел сейчас не виден (офлайн/неизвестен), мьютим по введённому тексту
    напрямую — тоже сработает, раз /is_muted проверяет и short_name."""
    query = args.strip()
    if not query:
        _list_muted()
        return

    resp = await _client.request("nodes")
    nodes = resp.get("nodes", []) if resp.get("ok") else []
    matches = _find_node_in_list(nodes, query)
    target = matches[0].get("long_name") if len(matches) == 1 and matches[0].get("long_name") else query

    key = target.strip().lower()
    if key in _muted_names:
        print_formatted_text(HTML(f"<ansiyellow>«{_safe(target)}» уже замьючен</ansiyellow>"))
        return
    _muted_names.add(key)
    _save_name_cache()
    print_formatted_text(HTML(
        f"<ansigray>🔇 «{_safe(target)}» замьючен — больше не появится в чате "
        "(/search и /last по-прежнему находят)</ansigray>"
    ))


async def _cmd_unmute(args: str) -> None:
    query = args.strip()
    if not query:
        _list_muted()
        return
    key = query.lower()
    if key not in _muted_names:
        print_formatted_text(HTML(
            f"<ansiyellow>«{_safe(query)}» не в списке замьюченных — /unmute без аргументов "
            "покажет список</ansiyellow>"
        ))
        return
    _muted_names.discard(key)
    _save_name_cache()
    print_formatted_text(HTML(f"<ansigreen>🔊 «{_safe(query)}» размьючен</ansigreen>"))


async def _cmd_settings(args: str) -> None:
    """Список/изменение параметров ЛОКАЛЬНОГО узла (см. SETTINGS.md для полного
    описания каждого параметра и почему часть конфига устройства сюда
    сознательно не вынесена — network/bluetooth/security)."""
    parts = args.strip().split(None, 1)
    if not parts:
        resp = await _client.request("settings", action="list")
        if not resp.get("ok"):
            print_formatted_text(HTML(
                f"<ansired>Не удалось получить настройки: {_safe(resp.get('error', ''))}</ansired>"
            ))
            return
        print_formatted_text(HTML("<ansiwhite>─── Настройки узла ───</ansiwhite>"))
        for item in resp.get("settings", []):
            print_formatted_text(HTML(
                f"  <b>{_safe(item['key'])}</b> = {_safe(str(item['value']))}  "
                f"<ansigray>{_safe(item['description'])}</ansigray>"
            ))
        print_formatted_text(HTML(
            "<ansigray>Изменить: /settings &lt;параметр&gt; &lt;значение&gt;. "
            "Подробности — SETTINGS.md</ansigray>"
        ))
        return

    if len(parts) < 2:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /settings &lt;параметр&gt; &lt;значение&gt; "
            "(список параметров — /settings без аргументов)</ansiyellow>"
        ))
        return
    key, value = parts
    resp = await _client.request("settings", action="set", key=key, value=value)
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось применить: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return
    print_formatted_text(HTML(
        f"<ansigreen>✓ {_safe(key)} = {_safe(str(resp['value']))}</ansigreen> "
        "<ansigray>(некоторые параметры применяются только после перезагрузки устройства)</ansigray>"
    ))


def _cmd_help() -> None:
    lines = [
        "─── Команды ───────────────────────────────────────────────",
        "  /nodes [online|names|hops]  список видимых узлов (сортировка, по умолчанию online)",
        "  /who                 информация о себе",
        "  /dm &lt;имя&gt; &lt;текст&gt;   личное сообщение (имя с пробелами — в кавычках)",
        "  /reply               список недавних сообщений, на которые можно ответить",
        "  /reply &lt;текст&gt;       ответ (с цитатой) на последнее полученное сообщение",
        "  /reply #N &lt;текст&gt;    ответ на сообщение №N из списка /reply",
        "  /react &lt;эмодзи&gt;      реакция (tapback) на последнее сообщение",
        "  /react #N &lt;эмодзи&gt;   реакция на сообщение №N из списка /reply",
        "  /ch [имя|all]        показать/сменить канал",
        "  /send &lt;канал&gt; &lt;текст&gt; разовая отправка в канал без переключения сессии",
        "  /search &lt;текст&gt;     поиск по истории (учитывает текущий канал)",
        "  /last &lt;имя&gt;         последние сообщения узла (точное имя, короткое или полное)",
        "  /stats [день|узел]   статистика по истории переписки",
        "  /trace &lt;имя&gt;        маршрут пакетов до узла (traceroute)",
        "  /ping &lt;имя&gt;         время доставки (RTT) и число хопов до узла",
        "  /pos &lt;имя&gt;          позиция узла, расстояние и азимут от меня",
        "  /mute &lt;имя&gt;         скрыть сообщения узла из чата (история — всё ещё в /search)",
        "  /unmute [имя]        снять мьют (без аргумента — список замьюченных)",
        "  /updatenames         подтянуть имена узлов с OneMesh (и так — раз в 30 мин фоном)",
        "  /settings [параметр значение]  настройки локального узла (см. SETTINGS.md)",
        "  /clear               очистить экран",
        "  /help                эта справка",
        "  Tab                  автодополнение команд, имён узлов и каналов",
        "────────────────────────────────────────────────────────────",
    ]
    for line in lines:
        print_formatted_text(HTML(f"<ansiwhite>{line}</ansiwhite>"))


async def _handle_command(text: str) -> None:
    parts = text[1:].split(None, 1)
    cmd  = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if   cmd == "nodes":       await _cmd_nodes(args)
    elif cmd == "who":         _cmd_who()
    elif cmd == "dm":          await _cmd_dm(args)
    elif cmd == "reply":       await _cmd_reply(args)
    elif cmd == "react":       await _cmd_react(args)
    elif cmd == "ch":          await _cmd_ch(args)
    elif cmd == "send":        await _cmd_send(args)
    elif cmd == "search":      _cmd_search(args)
    elif cmd == "last":        _cmd_last(args)
    elif cmd == "stats":       await _cmd_stats(args)
    elif cmd == "trace":       await _cmd_trace(args)
    elif cmd == "ping":        await _cmd_ping(args)
    elif cmd == "pos":         await _cmd_pos(args)
    elif cmd == "mute":        await _cmd_mute(args)
    elif cmd == "unmute":      await _cmd_unmute(args)
    elif cmd == "updatenames": await _cmd_updatenames()
    elif cmd == "settings":    await _cmd_settings(args)
    elif cmd == "clear":       _cmd_clear()
    elif cmd == "help":        _cmd_help()
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>Неизвестная команда /{_safe(cmd)}. Введите /help</ansiyellow>"
        ))


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _loop, _client, _history_ready, _channel_filter, _channel_index
    _loop = asyncio.get_running_loop()
    _load_name_cache()
    _channel_filter, history_size = _parse_args()

    if not SOCKET_PATH.exists():
        print_formatted_text(HTML(
            f"<ansired>Логгер не запущен ({SOCKET_PATH} не найден). "
            f"Запустите mesh_logger.py (или systemctl start mesh-logger).</ansired>"
        ))
        sys.exit(1)

    _client = IPCClient(_loop)
    _client.on_disconnect = _on_logger_lost
    try:
        await _client.connect()
    except Exception as e:
        print_formatted_text(HTML(
            f"<ansired>Не удалось подключиться к логгеру: {_safe(str(e))}</ansired>"
        ))
        sys.exit(1)

    who = await _client.request("whoami")
    if who.get("ok"):
        _client.whoami = (who["long_name"], who["short_name"])
        _client.whoami_id = who["node_id"]
        print_formatted_text(HTML(
            f"<b><ansigreen>Подключено к логгеру.</ansigreen></b> "
            f"Узел: <ansicyan>{_safe(who['long_name'])} ({_safe(who['short_name'])})</ansicyan>"
        ))
    else:
        print_formatted_text(HTML(
            "<ansiyellow>Логгер работает, но устройство пока не подключено</ansiyellow>"
        ))

    if _channel_filter is not None:
        chresp = await _client.request("channels")
        channels = chresp.get("channels", []) if chresp.get("ok") else []
        match = next((c for c in channels if c["name"].lower() == _channel_filter.lower()), None)
        if not match:
            available = ", ".join(c["name"] for c in channels) or "нет данных"
            print_formatted_text(HTML(
                f"<ansired>Канал '{_safe(_channel_filter)}' не найден. "
                f"Доступные каналы: {_safe(available)}</ansired>"
            ))
            sys.exit(1)
        _channel_index = match["index"]
        _channel_filter = match["name"]
        print_formatted_text(HTML(
            f"<b><ansicyan>Канал: {_safe(_channel_filter)}</ansicyan></b>"
        ))
    else:
        print_formatted_text(HTML(
            "<ansiwhite>Показываю сообщения со всех каналов (отправка — в Primary)</ansiwhite>"
        ))

    _print_initial_history(history_size)
    _history_ready = True
    for lines in _pending_live:
        quote = lines[0] if len(lines) == 2 else None
        _render_if_new(quote, lines[-1])
    _pending_live.clear()

    await _refresh_completions()
    asyncio.create_task(_periodic_updatenames())
    session = PromptSession(completer=MeshCompleter(), complete_while_typing=False)
    print_formatted_text(HTML(
        "\n<ansiwhite>Сообщение или /help. Ctrl+C / Ctrl+D — выход.</ansiwhite>\n"
    ))

    with patch_stdout():
        while True:
            try:
                text = await session.prompt_async("> ")
                text = text.strip()
                if not text:
                    continue

                if text.startswith("/"):
                    try:
                        await _handle_command(text)
                    except Exception as e:
                        print_formatted_text(HTML(
                            f"<ansired>Ошибка команды: {_safe(str(e))}</ansired>"
                        ))
                    continue

                resp = await _client.request("send", text=text, channel=_channel_index)
                _handle_send_response(resp, text)

            except (KeyboardInterrupt, EOFError):
                break

    print_formatted_text(HTML("\n<ansiwhite>Отключение...</ansiwhite>"))
    await _client.close()


if __name__ == "__main__":
    asyncio.run(main())
