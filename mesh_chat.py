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
    ENV_FILE,
    IPC_LINE_LIMIT,
    ONEMESH_CACHE_FILE,
    SOCKET_PATH,
    bearing_deg,
    compass_point,
    haversine_km,
    list_log_files,
    parse_log_line,
    update_env_file,
)
from mesh_i18n import plural, t, t_list

HISTORY_SIZE = 100
SEARCH_LIMIT = 20
LAST_LIMIT = 20
ONLINE_THRESHOLD_SECONDS = 15 * 60  # узел считается «онлайн», если был на связи недавнее этого
RECONNECT_DELAY = 3  # seconds between attempts to re-reach the logger socket
CACHE_RELOAD_INTERVAL = 5 * 60  # как часто перечитывать onemesh_cache.json с диска
PING_TIMEOUT = ACK_TIMEOUT_SECONDS + 5.0  # чуть больше, чем логгер сам ждёт ACK
REBOOT_CONFIRM_WINDOW = 30.0  # секунд на /reboot confirm после /reboot
# /settings <key> <value> ждёт сессионный ключ (до 5с в mesh_logger.py) + сам
# синхронный writeConfig() — дефолтные 10с IPCClient.request впритык на
# медленном линке; список настроек (action="list") не трогает устройство и
# укладывается в дефолт
SETTINGS_SET_TIMEOUT = 20.0

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
# monotonic deadline (_loop.time()) for /reboot confirm; None когда нет
# ожидающего подтверждения — см. _cmd_reboot
_reboot_confirm_deadline: float | None = None

# прочитано из onemesh_cache.json (пишет и резолвит только mesh_logger.py —
# см. "Node name resolution" в CLAUDE.md); клиент только перечитывает файл
_name_cache: dict[int, tuple[str, str]] = {}

# None => show/send on every channel (send defaults to Primary); otherwise the
# canonical channel name (and matching index) this session is restricted to.
_channel_filter: str | None = None
_channel_index: int = 0
# False while a --channel name is given but the device wasn't connected yet
# to confirm it exists (the "channels" IPC call degrades to a fake single
# "Primary" entry when _interface is None — see _channels_payload() in
# mesh_logger.py) — resolved for real once a "device: connected" event
# arrives, see _try_resolve_pending_channel()
_channel_resolved: bool = True

# Последние полученные live-сообщения с packet_id — цели для /reply
# (новые в конце). История из файлов id не содержит, поэтому реплай доступен
# только на сообщения, пришедшие после запуска клиента.
REPLYABLE_SIZE = 20
_replyables: collections.deque[dict] = collections.deque(maxlen=REPLYABLE_SIZE)
# Снимок _replyables (уже в порядке #1=новое..#N), сделанный в момент последнего
# показа списка через /reply или /react без аргументов. Явные #N обязаны бить
# точно в то, что человек реально видел на экране — если резолвить #N против
# живого _replyables в момент отправки, каждое новое входящее (а хуже того —
# каждое НАШЕ собственное отправленное сообщение, которое тоже прилетает
# обратно как live "message"-событие и добавляется в _replyables) сдвигает
# нумерацию, и то, что было #5 при печати списка, к моменту команды может
# оказаться совсем другим сообщением без единого предупреждения.
_last_reply_snapshot: list[dict] = []

# Кандидаты для tab-автодополнения (обновляются из ответов логгера)
_completion_nodes: list[str] = []
_completion_channels: list[str] = []
_completion_settings: list[str] = []


def _parse_args() -> tuple[str | None, int]:
    ap = argparse.ArgumentParser(
        description=t("argparse_description"),
        allow_abbrev=False,
    )
    ap.add_argument("-c", "--channel", metavar=t("argparse_channel_metavar"),
                    help=t("argparse_channel_help"))
    ap.add_argument("-n", "--history", type=int, default=HISTORY_SIZE, metavar="N",
                    help=t("argparse_history_help", default=HISTORY_SIZE))
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


# Live "message" events pushed by the logger before history has finished
# printing are buffered here, then flushed in order once history is done.
_history_ready = False
_pending_live: list[list[str]] = []


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return html.escape(_XML_INVALID.sub("", text or ""))


def _reload_onemesh_cache() -> None:
    """onemesh_cache.json is written by mesh_logger.py, possibly while this
    client is already running (its own background sweep, or another client's
    /updatenames) — reloading periodically (_periodic_cache_reload) picks up
    newly-resolved names without needing a restart."""
    if not ONEMESH_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(ONEMESH_CACHE_FILE.read_text(encoding="utf-8"))
        _name_cache.update({int(k): (v["long"], v["short"]) for k, v in raw.items()})
    except Exception:
        pass


async def _periodic_cache_reload() -> None:
    while True:
        await asyncio.sleep(CACHE_RELOAD_INTERVAL)
        with contextlib.suppress(Exception):
            _reload_onemesh_cache()


def _enrich_nodes(nodes: list[dict]) -> list[dict]:
    """Adds display_long/display_short to each node dict. The raw long_name/
    short_name from the "nodes" IPC response (mesh_logger.py's
    _nodes_payload()) are only ever what the mesh's own NodeInfo carries —
    None for a node we only know as `!hex (???)` with a since-resolved
    OneMesh cache entry. /nodes already computed this inline; factored out
    so /dm, /trace, /ping, /who, /ignore and tab-completion can match and
    display against the same name the user actually sees in /nodes, instead
    of only ever matching by raw name or !hex id for such a node."""
    out = []
    for n in nodes:
        if n["has_user"]:
            ln, sn = n["long_name"], n["short_name"]
        else:
            ln, sn = _name_cache.get(n["node_id"], (f"!{n['node_id']:08x}", "???"))
        out.append({**n, "display_long": ln, "display_short": sn})
    return out


def _find_node_in_list(nodes: list[dict], query: str) -> list[dict]:
    """Кандидаты по убыванию точности: точное имя/hex-id → префикс → подстрока.
    Раньше возвращалось первое подстрочное совпадение, и `/dm Alex` мог уйти
    узлу «Alexander», хотя в сети есть точный «Alex». Matches against the
    *display* name (falls back to the raw long/short_name if `nodes` wasn't
    run through _enrich_nodes) — a node visible only via `!hex (???)` on the
    wire but resolved via OneMesh (as /nodes shows it) would otherwise never
    match by the name the user actually typed. Tab-completion wraps a name
    containing a space in quotes (see _update_node_completions) so the
    completed text round-trips as one shell-like token — strip that same
    pair back off here, or a completed `/who "Meshtastic 6914"` would search
    for a node literally named with quote characters and never match."""
    q = query.strip()
    if len(q) >= 2 and q[0] == '"' and q[-1] == '"':
        q = q[1:-1]
    q = q.lower()
    exact, prefix, substr = [], [], []
    for n in nodes:
        ln = (n.get("display_long") or n.get("long_name") or "").lower()
        sn = (n.get("display_short") or n.get("short_name") or "").lower()
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
            f"<ansired>{t('err_node_not_found', query=_safe(query))}</ansired>"
        ))
        return None
    if len(matches) > 1:
        names = ", ".join(
            f"{n.get('display_long') or n.get('long_name') or '?'} "
            f"({n.get('display_short') or n.get('short_name') or '?'})"
            for n in matches[:5])
        more = " …" if len(matches) > 5 else ""
        print_formatted_text(HTML(
            f"<ansiyellow>{t('warn_node_ambiguous', query=_safe(query), names=_safe(names), more=more)}</ansiyellow>"
        ))
        return None
    return matches[0]


def _resolve_display_name(long_name: str, short_name: str) -> tuple[str, str]:
    """If a name looks like our own hex fallback, try the OneMesh cache
    (mesh_logger.py resolves and persists it; we just read it here)."""
    m = _HEX_ID_RE.match(long_name)
    if not m or short_name != "???":
        return long_name, short_name
    node_id = int(m.group(1), 16)
    return _name_cache.get(node_id, (long_name, short_name))


# ── tab completion ────────────────────────────────────────────────────────────

COMMANDS = ["/nodes", "/who", "/dm", "/reply", "/react", "/ch", "/send", "/search",
            "/trace", "/ping", "/stats", "/ignore", "/unignore",
            "/updatenames", "/settings", "/reboot",
            "/reconnect", "/clear", "/help"]


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
        if cmd in ("/dm", "/trace", "/ping", "/ignore", "/unignore", "/who"):
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
            for cand in t_list("stats_completions"):
                if cand.startswith(arg.lower()):
                    yield Completion(cand, start_position=-len(arg))
        elif cmd == "/settings":
            if " " not in arg:
                for cand in _completion_settings:
                    if cand.lower().startswith(arg.lower()):
                        yield Completion(cand, start_position=-len(arg))
        elif cmd == "/reboot":
            if "confirm".startswith(arg.lower()):
                yield Completion("confirm", start_position=-len(arg))


def _update_node_completions(nodes: list[dict]) -> None:
    """Expects nodes already run through _enrich_nodes — falls back to the
    raw long/short_name otherwise, same as _find_node_in_list."""
    global _completion_nodes
    out = set()
    for n in nodes:
        sn = n.get("display_short") or n.get("short_name")
        ln = n.get("display_long") or n.get("long_name")
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
        _update_node_completions(_enrich_nodes(resp.get("nodes", [])))
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
        self._reader, self._writer = await asyncio.open_unix_connection(
            str(SOCKET_PATH), limit=IPC_LINE_LIMIT)
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
                    fut.set_result({"ok": False, "error": t("err_no_logger_connection")})
            self._pending.clear()
            if not self._closing and self.on_disconnect:
                self.on_disconnect()

    async def request(self, cmd: str, timeout: float = 10.0, **fields) -> dict:
        if not self.connected:
            return {"ok": False, "error": t("err_no_logger_connection")}
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
            return {"ok": False, "error": t("err_no_logger_connection")}
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
            hop_str = f" ({plural('hops_forms', hops)})" if hops is not None else ""
            print_formatted_text(HTML(f"<ansigreen>{t('delivered', hop_str=hop_str, ref=ref)}</ansigreen>"))
        elif ok is False:
            print_formatted_text(HTML(
                f"<ansired>{t('not_delivered', error=_safe(str(obj.get('error', ''))), ref=ref)}</ansired>"
            ))
        elif obj.get("is_dm"):
            # a DM has one real recipient — a routing-layer ACK genuinely means
            # something, so a timeout here is worth flagging
            print_formatted_text(HTML(
                f"<ansiyellow>{t('delivery_unconfirmed', ref=ref)}</ansiyellow>"
            ))
        # else: broadcast/channel send with no ack — Meshtastic has no single
        # recipient to ACK a broadcast, only an unreliable "implicit ack" (heard
        # a neighbor rebroadcast it); a timeout here is the *normal*, expected
        # outcome on a busy shared channel, not a sign anything went wrong, so
        # printing it every time was pure noise — silently say nothing instead
    elif kind == "message":
        lines = obj.get("lines", [])
        if not lines or not _channel_matches(lines[-1]):
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
    elif kind == "device":
        # статус связи логгера с самим устройством (не с сокетом логгера —
        # тот отслеживается отдельно, через IPCClient.on_disconnect). Без
        # этого события /reboot confirm вешает пользователя в неведении:
        # команда "принята" мгновенно, а реальная перезагрузка и
        # переподключение происходят только между логгером и устройством.
        status = obj.get("status")
        if status == "disconnected":
            print_formatted_text(HTML(
                f"<ansired>{t('device_disconnected')}</ansired>"
            ))
        elif status == "connected":
            # own-identity cache: main()/_reconnect_logger() only ever populate
            # this when the client's own socket to the *logger* (re)connects —
            # a device-only reconnect (e.g. /reconnect while the client's
            # socket stayed up the whole time) would otherwise leave it stale
            # or, if the device wasn't reachable yet at client startup, never
            # populated at all. The raw (unescaped) fields, not the _safe()'d
            # ln/sn below — this gets compared against parsed log-line names.
            _client.whoami = (obj.get("long_name") or "?", obj.get("short_name") or "?")
            _client.whoami_id = obj.get("node_id")
            ln = _safe(obj.get("long_name") or "?")
            sn = _safe(obj.get("short_name") or "?")
            print_formatted_text(HTML(
                f"<ansigreen>{t('device_reconnected', ln=ln, sn=sn)}</ansigreen>"
            ))
            if not _channel_resolved:
                _loop.create_task(_try_resolve_pending_channel())
        elif status == "reconnect_failed":
            # only fired for a manually triggered /reconnect (see
            # mesh_logger.py's _reconnect_loop) — automatic retries stay
            # silent so a long outage doesn't spam the chat every few seconds
            print_formatted_text(HTML(
                f"<ansired>{t('reconnect_attempt_failed', error=_safe(obj.get('error', '?')))}</ansired>"
            ))


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
        f"<ansired>{t('logger_lost')}</ansired>"
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
            _render_if_new(quote, msg)


async def _reconnect_logger(lost_at: datetime.datetime) -> None:
    global _client, _reconnecting
    old = _client
    while True:
        await asyncio.sleep(RECONNECT_DELAY)
        if not SOCKET_PATH.exists():
            continue
        client = IPCClient(_loop)
        # set *before* connect(), not after: on_disconnect was previously
        # wired up only once connect() had already returned, so a drop in
        # that split second (read_loop's finally block runs with
        # on_disconnect still None) went completely unnoticed — the client
        # would silently sit disconnected forever, since _reconnecting was
        # never reset to trigger another attempt
        client.on_disconnect = _on_logger_lost
        try:
            await client.connect()
        except Exception:
            continue
        if client.connected:
            break
    with contextlib.suppress(Exception):
        await old.close()
    client.whoami, client.whoami_id = old.whoami, old.whoami_id
    who = await client.request("whoami")
    if who.get("ok"):
        client.whoami = (who["long_name"], who["short_name"])
        client.whoami_id = who["node_id"]
    _client = client
    _reconnecting = False
    if not client.connected:
        # dropped again during the whoami round-trip above; on_disconnect
        # was a no-op while _reconnecting was still True (guards against a
        # duplicate reconnect task), so nothing else will retry this —
        # kick it off ourselves now that the guard is clear
        _on_logger_lost()
        return
    print_formatted_text(HTML(
        f"<ansigreen>{t('logger_restored')}</ansigreen>"
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


def _format_last_heard(ago: int | None) -> tuple[str, str]:
    """(label, ansi-color) for a node's last-seen time — shared by /nodes and /who
    so the "online / Nм назад / Nч назад / ?" logic and thresholds don't drift
    between the two."""
    if ago is not None and ago < ONLINE_THRESHOLD_SECONDS:
        return t("online_now"), "ansigreen"
    if ago is not None:
        if ago < 3600:
            return t("ago_minutes", mins=ago // 60), "ansiyellow"
        return t("ago_hours", hrs=ago // 3600), "ansigray"
    return t("unknown_time"), "ansigray"


async def _cmd_nodes(args: str = "") -> None:
    mode = args.strip().lower() or "online"
    if mode not in NODE_SORT_MODES:
        print_formatted_text(HTML(
            f"<ansiyellow>{t('err_unknown_sort', mode=_safe(mode), modes=', '.join(NODE_SORT_MODES))}</ansiyellow>"
        ))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansiyellow>{_safe(resp.get('error') or t('err_no_node_data'))}</ansiyellow>"))
        return
    if not resp.get("nodes"):
        print_formatted_text(HTML(f"<ansiyellow>{t('err_no_node_data')}</ansiyellow>"))
        return

    enriched = _enrich_nodes(resp["nodes"])
    _update_node_completions(enriched)

    enriched.sort(key=lambda n: _node_sort_key(n, mode))

    print_formatted_text(HTML(f"<ansiwhite>{t('hdr_visible_nodes')}</ansiwhite>"))
    for n in enriched:
        ln, sn = _safe(n["display_long"]), _safe(n["display_short"])

        battery = n.get("battery")
        bat_str = f" 🔋{battery}%" if battery is not None else ""
        snr = n.get("snr")
        snr_str = f" SNR:{snr:.1f}" if snr is not None else ""
        hops = n.get("hops_away")
        hops_str = f" · {plural('hops_forms', hops)}" if hops is not None else ""

        heard, color = _format_last_heard(n["seconds_ago"])

        print_formatted_text(HTML(
            f"  <b><ansigreen>{ln}</ansigreen></b> <ansigreen>({sn})</ansigreen>"
            f"<ansiwhite>{bat_str}{snr_str}{hops_str}</ansiwhite>"
            f" <{color}>· {heard}</{color}>"
        ))


def _cmd_who_self() -> None:
    if not _client.whoami:
        print_formatted_text(HTML(f"<ansiyellow>{t('err_no_self_info')}</ansiyellow>"))
        return
    ln, sn = _client.whoami
    id_str = f"!{_client.whoami_id:08x}" if _client.whoami_id is not None else ""
    print_formatted_text(HTML(
        f"<ansiwhite>{t('who_me_label')}</ansiwhite>"
        f"<b><ansicyan>{_safe(ln)}</ansicyan></b> "
        f"<ansicyan>({_safe(sn)})</ansicyan>"
        f"<ansiwhite>{f' {_safe(id_str)}' if id_str else ''}</ansiwhite>"
    ))
    # держим .env в актуальном состоянии для собственной ноды без ручного
    # редактирования — молча, ошибка записи не должна ломать саму команду
    with contextlib.suppress(Exception):
        update_env_file(ENV_FILE, {
            "NODE_LONG_NAME": ln, "NODE_SHORT_NAME": sn, "NODE_ID": id_str,
        })


async def _cmd_who(args: str = "") -> None:
    """`/who` без аргумента — информация о себе (и, как раньше, самообновление
    .env). `/who <имя>` — полная информация о любом видимом узле (имена, hex id,
    батарея/SNR/хопы, когда был на связи, позиция и расстояние от нас); .env
    при этом не трогаем — тот self-update относится только к собственной ноде."""
    target = args.strip()
    if not target:
        _cmd_who_self()
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_nodes_fetch_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    nodes = _enrich_nodes(resp["nodes"])
    node = _pick_node(nodes, target)
    if not node:
        return

    ln = node.get("display_long") or node.get("long_name") or "?"
    sn = node.get("display_short") or node.get("short_name") or "?"
    id_str = f"!{node['node_id']:08x}"
    print_formatted_text(HTML(
        f"<ansiwhite>{t('who_node_line', ln=_safe(ln), sn=_safe(sn), id_str=id_str)}</ansiwhite>"
    ))

    battery = node.get("battery")
    bat_str = f" 🔋{battery}%" if battery is not None else ""
    snr = node.get("snr")
    snr_str = f" SNR:{snr:.1f}" if snr is not None else ""
    hops = node.get("hops_away")
    hops_str = f" · {plural('hops_forms', hops)}" if hops is not None else ""
    heard, color = _format_last_heard(node["seconds_ago"])
    print_formatted_text(HTML(
        f"  <ansiwhite>{bat_str}{snr_str}{hops_str}</ansiwhite> <{color}>· {heard}</{color}>"
    ))

    if node.get("lat") is not None and node.get("lon") is not None:
        alt = node.get("alt")
        alt_str = f", {alt} {t('unit_m')}" if alt is not None else ""
        lat_str = f"{node['lat']:.5f}"
        lon_str = f"{node['lon']:.5f}"
        print_formatted_text(HTML(
            f"<ansiwhite>{t('pos_line', ln=_safe(ln), sn=_safe(sn), lat=lat_str, lon=lon_str, alt_str=alt_str)}</ansiwhite>"
        ))
        me = next((n for n in nodes if n["node_id"] == _client.whoami_id), None) \
            if _client.whoami_id is not None else None
        if me and me.get("lat") is not None and me.get("lon") is not None and me["node_id"] != node["node_id"]:
            dist = haversine_km(me["lat"], me["lon"], node["lat"], node["lon"])
            brng = bearing_deg(me["lat"], me["lon"], node["lat"], node["lon"])
            dist_str = f"{dist:.1f} {t('unit_km')}" if dist >= 1 else f"{dist * 1000:.0f} {t('unit_m')}"
            print_formatted_text(HTML(
                f"<ansiwhite>{t('pos_distance', dist_str=dist_str, brng=f'{brng:.0f}', compass=compass_point(brng))}</ansiwhite>"
            ))
    else:
        print_formatted_text(HTML(f"<ansigray>{t('err_no_position', name=_safe(ln))}</ansigray>"))

    # matches against the resolved display name, not the raw typed query —
    # _pick_node may have resolved a prefix/substring to the full name, and
    # log lines are written under the same resolved name (see Node name
    # resolution in CLAUDE.md), so this is what actually appears in logs/
    matches = _scan_units(
        lambda p: p.long_name.lower() == ln.lower() or p.short_name.lower() == sn.lower(),
        LAST_LIMIT,
    )
    _print_unit_matches(t("hdr_last_from", query=_safe(ln)), matches, LAST_LIMIT,
                        t("last_empty", query=_safe(ln)))


def _handle_send_response(resp: dict, text: str) -> None:
    if resp.get("ok"):
        if resp.get("queued"):
            print_formatted_text(HTML(
                f"<ansiyellow>{t('queued', snippet=_safe(_snippet(text, 60)))}</ansiyellow>"
            ))
        return
    print_formatted_text(HTML(
        f"<ansired>{t('err_send_error', error=_safe(resp.get('error', '')))}</ansired>"
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
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_dm')}</ansiyellow>"))
        return
    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_nodes_fetch_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return

    node = _pick_node(_enrich_nodes(resp["nodes"]), target_name)
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
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_trace')}</ansiyellow>"))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_nodes_fetch_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    node = _pick_node(_enrich_nodes(resp["nodes"]), target_name)
    if not node:
        return

    print_formatted_text(HTML(
        f"<ansiwhite>{t('tracing', name=_safe(node.get('display_long') or node.get('long_name') or '?'), timeout=int(TRACE_TIMEOUT))}</ansiwhite>"
    ))
    trace_resp = await _client.request("trace", node_id=node["node_id"],
                                        channel=_channel_index, timeout=TRACE_TIMEOUT)
    if not trace_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_traceroute_failed', error=_safe(trace_resp.get('error', '')))}</ansired>"
        ))
        return

    towards = trace_resp.get("towards", [])
    back = trace_resp.get("back", [])
    if towards:
        print_formatted_text(HTML(
            f"<ansiwhite>{t('route_there')}</ansiwhite> "
            + " → ".join(_fmt_trace_hop(h) for h in towards)
        ))
    if back:
        print_formatted_text(HTML(
            f"<ansiwhite>{t('route_back')}</ansiwhite> "
            + " → ".join(_fmt_trace_hop(h) for h in back)
        ))
    else:
        print_formatted_text(HTML(
            f"<ansigray>{t('no_return_route')}</ansigray>"
        ))


async def _cmd_ping(args: str) -> None:
    """Меряет RTT до узла: silent wantAck-пакет (не текстовое сообщение — см.
    _send_ping_packet в mesh_logger.py, ничего не появляется в DM у адресата),
    дожидаемся своего delivery-события по packet_id (см. _pending_pings в
    _handle_event) и печатаем время + число хопов, которое ACK принёс с собой
    (см. handler() в mesh_logger.py)."""
    target_name = args.strip()
    if not target_name:
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_ping')}</ansiyellow>"))
        return

    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_nodes_fetch_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    node = _pick_node(_enrich_nodes(resp["nodes"]), target_name)
    if not node:
        return

    ln = node.get("display_long") or node.get("long_name") or "?"
    sn = node.get("display_short") or node.get("short_name") or "?"
    print_formatted_text(HTML(f"<ansiwhite>{t('pinging', ln=_safe(ln), sn=_safe(sn))}</ansiwhite>"))

    start = _loop.time()
    send_resp = await _client.request("ping", node_id=node["node_id"], channel=_channel_index)
    if not send_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_send_error', error=_safe(send_resp.get('error', '')))}</ansired>"
        ))
        return
    pid = send_resp.get("packet_id")
    if not pid:
        print_formatted_text(HTML(f"<ansired>{t('err_no_packet_id')}</ansired>"))
        return

    fut = _loop.create_future()
    _pending_pings[pid] = fut
    try:
        delivery = await asyncio.wait_for(fut, timeout=PING_TIMEOUT)
    except asyncio.TimeoutError:
        _pending_pings.pop(pid, None)
        print_formatted_text(HTML(
            f"<ansired>{t('ping_no_reply', ln=_safe(ln), timeout=int(PING_TIMEOUT))}</ansired>"
        ))
        return

    elapsed = _loop.time() - start
    ok = delivery.get("ok")
    if ok is True:
        hops = delivery.get("hops")
        hop_str = f", {plural('hops_forms', hops)}" if hops is not None else ""
        print_formatted_text(HTML(
            f"<ansigreen>{t('ping_delivered', ln=_safe(ln), elapsed=f'{elapsed:.1f}', hop_str=hop_str)}</ansigreen>"
        ))
    elif ok is False:
        print_formatted_text(HTML(
            f"<ansired>{t('ping_not_delivered', ln=_safe(ln), error=_safe(str(delivery.get('error', ''))))}</ansired>"
        ))
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>{t('ping_unconfirmed', ln=_safe(ln), elapsed=f'{elapsed:.1f}')}</ansiyellow>"
        ))


def _reply_label(r: dict, num: int) -> str:
    parsed = parse_log_line(r["line"])
    if not parsed:
        return f"#{num}"
    ln, _ = _resolve_display_name(parsed.long_name, parsed.short_name)
    dm = "DM " if parsed.is_dm else ""
    # same convention as live rendering (_print_msg): only show which channel
    # a message is from when the session isn't already filtered to one —
    # otherwise every listed item is obviously on that one channel already.
    # Unfiltered sessions mix channels together, and replying to the wrong
    # one because it wasn't obvious which channel #N was on is an easy miss.
    ch = f"<ansimagenta>{_safe(parsed.channel)}</ansimagenta> " if _channel_filter is None else ""
    return (f"  #{num} {ch}[{_safe(parsed.time_str)}] {dm}<b>{_safe(ln)}</b>: "
            f"{_safe(_snippet(parsed.text, 50))}")


def _list_replyables() -> None:
    global _last_reply_snapshot
    _last_reply_snapshot = list(reversed(_replyables))
    print_formatted_text(HTML(f"<ansiwhite>{t('hdr_replyables')}</ansiwhite>"))
    for num, r in enumerate(_last_reply_snapshot, start=1):
        print_formatted_text(HTML(f"<ansiwhite>{_reply_label(r, num)}</ansiwhite>"))
    print_formatted_text(HTML(f"<ansigray>{t('reply_hint')}</ansigray>"))


def _parse_target_and_text(args: str, usage_html: str) -> tuple[dict, str] | None:
    """Общий разбор `#N <текст>` / `<текст>` (на #1), используется /reply и /react.
    `#N` резолвится против _last_reply_snapshot — того самого списка, который
    человек только что видел на экране от /reply или /react без аргументов —
    а не против живого _replyables, чтобы номер не "уехал" из-за сообщений,
    прилетевших между показом списка и отправкой команды (см. комментарий у
    _last_reply_snapshot). Без номера (реплай на #1) — намеренно живой:
    "ответить на самое свежее прямо сейчас" не подразумевает, что список
    вообще показывался."""
    if args.startswith("#"):
        num_str, _, text = args[1:].partition(" ")
        text = text.strip()
        if not num_str.isdigit() or not text:
            print_formatted_text(HTML(usage_html))
            return None
        num = int(num_str)
        pool = _last_reply_snapshot or list(reversed(_replyables))
        if not 1 <= num <= len(pool):
            print_formatted_text(HTML(
                f"<ansired>{t('err_no_such_reply', num=num, max=len(pool))}</ansired>"
            ))
            return None
        return pool[num - 1], text
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
        print_formatted_text(HTML(f"<ansiyellow>{t('err_nothing_to_quote')}</ansiyellow>"))
        return

    if not args:
        _list_replyables()
        return

    target = _parse_target_and_text(args, f"<ansiyellow>{t('usage_reply')}</ansiyellow>")
    if target is None:
        return
    r, text = target

    parsed = parse_log_line(r["line"])
    if parsed:
        print_formatted_text(HTML(
            f"<ansigray>{t('replying_to', time=_safe(parsed.time_str), name=_safe(parsed.long_name), snippet=_safe(_snippet(parsed.text)))}</ansigray>"
        ))
    resp = await _send_to_reply_target(r, text)
    _handle_send_response(resp, text)


async def _cmd_react(args: str) -> None:
    args = args.strip()
    if not _replyables:
        print_formatted_text(HTML(f"<ansiyellow>{t('err_nothing_to_react')}</ansiyellow>"))
        return

    if not args:
        _list_replyables()
        print_formatted_text(HTML(f"<ansigray>{t('react_hint')}</ansigray>"))
        return

    target = _parse_target_and_text(args, f"<ansiyellow>{t('usage_react')}</ansiyellow>")
    if target is None:
        return
    r, emoji_text = target

    parsed = parse_log_line(r["line"])
    if parsed:
        print_formatted_text(HTML(
            f"<ansigray>{t('reacting_to', time=_safe(parsed.time_str), name=_safe(parsed.long_name), snippet=_safe(_snippet(parsed.text)))}</ansigray>"
        ))
    resp = await _send_to_reply_target(r, emoji_text, emoji=True)
    _handle_send_response(resp, emoji_text)


async def _try_resolve_pending_channel() -> None:
    """Retries resolving a --channel name given at startup while the device
    wasn't connected yet (see the _channel_resolved comment near its
    declaration) — called once the "device: connected" event actually
    confirms channels are known. If the name still doesn't match a real
    channel now that the device answered for real, it's a genuine typo:
    fall back to showing all channels instead of leaving the session stuck
    filtered to channel 0 with nothing displayed."""
    global _channel_filter, _channel_index, _channel_resolved
    if _channel_resolved or _channel_filter is None:
        return
    resp = await _client.request("channels")
    channels = resp.get("channels", []) if resp.get("ok") else []
    match = next((c for c in channels if c["name"].lower() == _channel_filter.lower()), None)
    if match:
        _channel_index = match["index"]
        _channel_filter = match["name"]
        _channel_resolved = True
        print_formatted_text(HTML(
            f"<b><ansicyan>{t('channel_confirmed', name=_safe(_channel_filter))}</ansicyan></b>"
        ))
    else:
        available = ", ".join(c["name"] for c in channels) or t("no_data")
        print_formatted_text(HTML(
            f"<ansired>{t('channel_not_found_device', name=_safe(_channel_filter), available=_safe(available))}</ansired>"
        ))
        _channel_filter = None
        _channel_index = 0
        _channel_resolved = True


async def _cmd_ch(args: str) -> None:
    global _channel_filter, _channel_index, _completion_channels, _channel_resolved
    name = args.strip()
    resp = await _client.request("channels")
    channels = resp.get("channels", []) if resp.get("ok") else []
    if channels:
        _completion_channels = [c["name"] for c in channels]

    if not name:
        current = _channel_filter or t("channel_all_word")
        channels_str = ", ".join(c["name"] for c in channels) or t("no_data")
        print_formatted_text(HTML(
            f"<ansiwhite>{t('channel_status', current=_safe(current), channels=_safe(channels_str))}</ansiwhite>"
        ))
        return

    if name.lower() in ("all", "*", "все"):
        _channel_filter = None
        _channel_index = 0
        _channel_resolved = True
        print_formatted_text(HTML(
            f"<b><ansicyan>{t('channel_all_banner')}</ansicyan></b>"
        ))
        return

    match = next((c for c in channels if c["name"].lower() == name.lower()), None)
    if not match:
        available = ", ".join(c["name"] for c in channels) or t("no_data")
        print_formatted_text(HTML(
            f"<ansired>{t('err_channel_not_found', name=_safe(name), available=_safe(available))}</ansired>"
        ))
        return

    _channel_filter = match["name"]
    _channel_index = match["index"]
    _channel_resolved = True
    # цели /reply из других каналов после переключения удивили бы
    kept = [r for r in _replyables if r["channel_index"] == match["index"]]
    _replyables.clear()
    _replyables.extend(kept)
    _last_reply_snapshot.clear()  # старые номера относились к прежнему списку каналов
    print_formatted_text(HTML(
        f"<b><ansicyan>{t('channel_banner', name=_safe(_channel_filter))}</ansicyan></b>"
    ))
    _print_initial_history()


async def _cmd_send(args: str) -> None:
    """One-off send to a specific channel without switching the session's
    current channel (unlike /ch, which changes what /reply and history filter to)."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_send')}</ansiyellow>"))
        return
    channel_name, text = parts

    resp = await _client.request("channels")
    channels = resp.get("channels", []) if resp.get("ok") else []
    match = next((c for c in channels if c["name"].lower() == channel_name.lower()), None)
    if not match:
        available = ", ".join(c["name"] for c in channels) or t("no_data")
        print_formatted_text(HTML(
            f"<ansired>{t('err_channel_not_found', name=_safe(channel_name), available=_safe(available))}</ansired>"
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
    suffix = t("shown_last_n", limit=limit) if len(matches) > limit else ""
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
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_search')}</ansiyellow>"))
        return

    matches = _scan_units(
        lambda p: (query in p.text.lower() or query in p.long_name.lower()
                   or query in p.short_name.lower()),
        SEARCH_LIMIT,
    )
    _print_unit_matches(t("hdr_search", query=_safe(query)), matches, SEARCH_LIMIT,
                        t("search_empty", query=_safe(query)))


def _collect_stats() -> dict:
    """Synchronous — called via asyncio.to_thread so a large logs/ directory
    doesn't stall the prompt. Device connection not needed: everything comes
    from logs/, same as /search. Defaults to the Primary channel rather than
    every channel mixed together (all-channels stats conflated unrelated
    groups' traffic into one meaningless total) — but still respects an
    explicit /ch switch to some other channel, since that's already a
    deliberate one-channel view."""
    channel_name = _channel_filter or "Primary"
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
            if parsed.channel.lower() != channel_name.lower():
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
        print_formatted_text(HTML(f"<ansiyellow>{t('err_no_stats')}</ansiyellow>"))
        return

    if mode in ("день", "дни", "day", "days"):
        print_formatted_text(HTML(f"<ansiwhite>{t('hdr_by_day')}</ansiwhite>"))
        days = sorted(stats["per_day"].items())
        max_count = max(c for _, c in days)
        for date_str, count in days:
            print_formatted_text(HTML(
                f"  {date_str}  <ansigreen>{_bar(count, max_count)}</ansigreen> {count}"
            ))
        return

    if mode in ("узел", "узлы", "node", "nodes"):
        print_formatted_text(HTML(f"<ansiwhite>{t('hdr_top_nodes')}</ansiwhite>"))
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
    print_formatted_text(HTML(f"<ansiwhite>{t('hdr_stats')}</ansiwhite>"))
    print_formatted_text(HTML(t("total_messages", total=stats['total'], days=days_count)))
    if busiest_hour:
        hour, cnt = busiest_hour[0]
        print_formatted_text(HTML(t("busiest_hour", hour=hour, count=cnt)))
    if top_nodes:
        print_formatted_text(HTML(t("top_nodes_label")))
        for (ln, sn), count in top_nodes:
            dln, dsn = _resolve_display_name(ln, sn)
            print_formatted_text(HTML(f"    <b>{_safe(dln)}</b> ({_safe(dsn)}) — {count}"))
    print_formatted_text(HTML(f"<ansigray>{t('stats_more_hint')}</ansigray>"))


def _cmd_clear() -> None:
    pt_clear()


async def _cmd_updatenames() -> None:
    """Resolution itself runs in mesh_logger.py now (continuously, at startup
    and every 30 min — see CLAUDE.md), so this just triggers an extra sweep on
    demand and reloads the shared cache to show the result immediately instead
    of waiting for the next _periodic_cache_reload tick."""
    resp = await _client.request("updatenames")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_update_names_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    _reload_onemesh_cache()
    resolved, targets = resp.get("resolved", 0), resp.get("targets", 0)
    if targets == 0:
        print_formatted_text(HTML(f"<ansiyellow>{t('nothing_to_update')}</ansiyellow>"))
    else:
        print_formatted_text(HTML(
            f"<ansigreen>{t('update_done', resolved=resolved, targets=targets)}</ansigreen>"
        ))


def _list_ignored(nodes: list[dict]) -> None:
    ignored = [n for n in nodes if n.get("is_ignored")]
    if not ignored:
        print_formatted_text(HTML(f"<ansiyellow>{t('empty_ignored_list')}</ansiyellow>"))
        return
    print_formatted_text(HTML(f"<ansiwhite>{t('hdr_ignored')}</ansiwhite>"))
    for n in ignored:
        ln = n.get("display_long") or n.get("long_name") or f"!{n['node_id']:08x}"
        sn = n.get("display_short") or n.get("short_name") or "???"
        print_formatted_text(HTML(f"  {_safe(ln)} ({_safe(sn)})"))
    print_formatted_text(HTML(f"<ansigray>{t('unignore_hint')}</ansigray>"))


async def _cmd_ignore(args: str) -> None:
    """Настоящий firmware-уровневый игнор (node.setIgnored(), см.
    _set_ignored_node в mesh_logger.py) — не app-level фильтр вроде старого
    /mute. Устройство отбрасывает пакеты игнорируемого узла ещё до того, как
    mesh_logger.py их увидит: сообщения перестают логироваться совсем, а не
    просто скрываются из живой ленты. Из-за этого, в отличие от /mute,
    игнорировать можно только реально видимый сейчас узел (нужен его
    node_id) — по имени вслепую, как раньше, больше не сработает."""
    query = args.strip()
    resp = await _client.request("nodes")
    nodes = _enrich_nodes(resp.get("nodes", [])) if resp.get("ok") else []
    if not query:
        _list_ignored(nodes)
        return

    node = _pick_node(nodes, query)
    if not node:
        return
    ln = node.get("display_long") or node.get("long_name") or "?"
    if node.get("is_ignored"):
        print_formatted_text(HTML(f"<ansiyellow>{t('already_ignored', name=_safe(ln))}</ansiyellow>"))
        return

    ignore_resp = await _client.request("ignore", action="add", node_id=node["node_id"])
    if not ignore_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_ignore_failed', error=_safe(ignore_resp.get('error', '')))}</ansired>"
        ))
        return
    print_formatted_text(HTML(f"<ansigray>{t('ignored_msg', name=_safe(ln))}</ansigray>"))


async def _cmd_unignore(args: str) -> None:
    query = args.strip()
    resp = await _client.request("nodes")
    nodes = _enrich_nodes(resp.get("nodes", [])) if resp.get("ok") else []
    if not query:
        _list_ignored(nodes)
        return

    node = _pick_node(nodes, query)
    if not node:
        return
    ln = node.get("display_long") or node.get("long_name") or "?"
    if not node.get("is_ignored"):
        print_formatted_text(HTML(f"<ansiyellow>{t('not_ignored', name=_safe(ln))}</ansiyellow>"))
        return

    ignore_resp = await _client.request("ignore", action="remove", node_id=node["node_id"])
    if not ignore_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_ignore_failed', error=_safe(ignore_resp.get('error', '')))}</ansired>"
        ))
        return
    print_formatted_text(HTML(f"<ansigreen>{t('unignored_msg', name=_safe(ln))}</ansigreen>"))




async def _cmd_settings(args: str) -> None:
    """Список/изменение параметров ЛОКАЛЬНОГО узла (см. SETTINGS.md для полного
    описания каждого параметра и почему часть конфига устройства сюда
    сознательно не вынесена — network/bluetooth/security)."""
    parts = args.strip().split(None, 1)
    if not parts:
        resp = await _client.request("settings", action="list")
        if not resp.get("ok"):
            print_formatted_text(HTML(
                f"<ansired>{t('err_settings_fetch_failed', error=_safe(resp.get('error', '')))}</ansired>"
            ))
            return
        print_formatted_text(HTML(f"<ansiwhite>{t('hdr_settings')}</ansiwhite>"))
        for item in resp.get("settings", []):
            print_formatted_text(HTML(
                f"  <b>{_safe(item['key'])}</b> = {_safe(str(item['value']))}  "
                f"<ansigray>{_safe(item['description'])}</ansigray>"
            ))
        print_formatted_text(HTML(
            f"<ansigray>{t('settings_change_hint', doc=t('settings_doc'))}</ansigray>"
        ))
        return

    if len(parts) < 2:
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_settings')}</ansiyellow>"))
        return
    key, value = parts
    resp = await _client.request("settings", action="set", key=key, value=value,
                                  timeout=SETTINGS_SET_TIMEOUT)
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_settings_apply_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    print_formatted_text(HTML(
        t("settings_applied", param=_safe(key), value=_safe(str(resp['value'])))
    ))


async def _cmd_reboot(args: str) -> None:
    """Двухшаговое подтверждение вместо одного деструктивного действия: /reboot
    показывает предупреждение и открывает окно REBOOT_CONFIRM_WINDOW секунд, в
    течение которого только /reboot confirm реально шлёт reboot(). Отдельная
    команда — а не да/нет-промпт поверх PromptSession — чтобы не тащить сессию
    prompt_toolkit в обработчик команд."""
    global _reboot_confirm_deadline
    arg = args.strip().lower()
    if arg not in ("", "confirm"):
        print_formatted_text(HTML(f"<ansiyellow>{t('usage_reboot')}</ansiyellow>"))
        return

    now = _loop.time()
    if arg == "confirm":
        if _reboot_confirm_deadline is None or now > _reboot_confirm_deadline:
            print_formatted_text(HTML(f"<ansiyellow>{t('err_no_pending_confirm')}</ansiyellow>"))
            return
        _reboot_confirm_deadline = None
        resp = await _client.request("reboot")
        if not resp.get("ok"):
            print_formatted_text(HTML(
                f"<ansired>{t('err_reboot_failed_client', error=_safe(resp.get('error', '')))}</ansired>"
            ))
            return
        print_formatted_text(HTML(f"<ansigreen>{t('reboot_rebooting')}</ansigreen>"))
        return

    _reboot_confirm_deadline = now + REBOOT_CONFIRM_WINDOW
    print_formatted_text(HTML(
        f"<ansiyellow>{t('reboot_warn', window=int(REBOOT_CONFIRM_WINDOW))}</ansiyellow>"
    ))


async def _cmd_reconnect(args: str) -> None:
    """Forces the logger to redial the device right now, regardless of
    whether it currently thinks it's connected or is already mid-backoff
    after an earlier drop — see _trigger_hard_reconnect() in mesh_logger.py.
    Fire-and-forget like /reboot: the actual outcome shows up via the
    existing "device" event (device_connected/device_disconnected), not a
    blocking IPC response, since a real reconnect can take several seconds."""
    host = args.strip() or None
    resp = await _client.request("reconnect", host=host)
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>{t('err_reconnect_failed', error=_safe(resp.get('error', '')))}</ansired>"
        ))
        return
    if host:
        print_formatted_text(HTML(f"<ansiyellow>{t('reconnect_triggered_host', host=_safe(host))}</ansiyellow>"))
    else:
        print_formatted_text(HTML(f"<ansiyellow>{t('reconnect_triggered')}</ansiyellow>"))


def _cmd_help() -> None:
    for line in t_list("help_lines"):
        print_formatted_text(HTML(f"<ansiwhite>{line}</ansiwhite>"))


async def _handle_command(text: str) -> None:
    parts = text[1:].split(None, 1)
    cmd  = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if   cmd == "nodes":       await _cmd_nodes(args)
    elif cmd == "who":         await _cmd_who(args)
    elif cmd == "dm":          await _cmd_dm(args)
    elif cmd == "reply":       await _cmd_reply(args)
    elif cmd == "react":       await _cmd_react(args)
    elif cmd == "ch":          await _cmd_ch(args)
    elif cmd == "send":        await _cmd_send(args)
    elif cmd == "search":      _cmd_search(args)
    elif cmd == "stats":       await _cmd_stats(args)
    elif cmd == "trace":       await _cmd_trace(args)
    elif cmd == "ping":        await _cmd_ping(args)
    elif cmd == "ignore":      await _cmd_ignore(args)
    elif cmd == "unignore":    await _cmd_unignore(args)
    elif cmd == "updatenames": await _cmd_updatenames()
    elif cmd == "settings":    await _cmd_settings(args)
    elif cmd == "reboot":     await _cmd_reboot(args)
    elif cmd == "reconnect":   await _cmd_reconnect(args)
    elif cmd == "clear":       _cmd_clear()
    elif cmd == "help":        _cmd_help()
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>{t('unknown_command', cmd=_safe(cmd))}</ansiyellow>"
        ))


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _loop, _client, _history_ready, _channel_filter, _channel_index, _channel_resolved
    _loop = asyncio.get_running_loop()
    _reload_onemesh_cache()
    _channel_filter, history_size = _parse_args()

    if not SOCKET_PATH.exists():
        print_formatted_text(HTML(
            f"<ansired>{t('err_logger_not_running', socket=SOCKET_PATH)}</ansired>"
        ))
        sys.exit(1)

    _client = IPCClient(_loop)
    _client.on_disconnect = _on_logger_lost
    try:
        await _client.connect()
    except Exception as e:
        print_formatted_text(HTML(
            f"<ansired>{t('err_logger_connect_failed', error=_safe(str(e)))}</ansired>"
        ))
        sys.exit(1)

    who = await _client.request("whoami")
    device_connected = who.get("ok", False)
    if device_connected:
        _client.whoami = (who["long_name"], who["short_name"])
        _client.whoami_id = who["node_id"]
        print_formatted_text(HTML(
            f"<b><ansigreen>{t('connected_banner', ln=_safe(who['long_name']), sn=_safe(who['short_name']))}</ansigreen></b>"
        ))
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>{t('device_not_connected_yet')}</ansiyellow>"
        ))

    if _channel_filter is not None:
        chresp = await _client.request("channels")
        channels = chresp.get("channels", []) if chresp.get("ok") else []
        match = next((c for c in channels if c["name"].lower() == _channel_filter.lower()), None)
        if not match and not device_connected:
            # can't tell a real typo from "the device just hasn't answered
            # yet" — the "channels" IPC call degrades to a single fake
            # "Primary" entry while _interface is None (mesh_logger.py's
            # _channels_payload()). Don't exit: keep the given name, send on
            # Primary until _try_resolve_pending_channel confirms it for
            # real off the "device: connected" event.
            _channel_resolved = False
            print_formatted_text(HTML(
                f"<ansiyellow>{t('channel_pending_msg', name=_safe(_channel_filter))}</ansiyellow>"
            ))
        elif not match:
            available = ", ".join(c["name"] for c in channels) or t("no_data")
            print_formatted_text(HTML(
                f"<ansired>{t('err_channel_not_found_startup', name=_safe(_channel_filter), available=_safe(available))}</ansired>"
            ))
            sys.exit(1)
        else:
            _channel_index = match["index"]
            _channel_filter = match["name"]
            print_formatted_text(HTML(
                f"<b><ansicyan>{t('channel_banner', name=_safe(_channel_filter))}</ansicyan></b>"
            ))
    else:
        print_formatted_text(HTML(
            f"<ansiwhite>{t('all_channels_banner')}</ansiwhite>"
        ))

    _print_initial_history(history_size)
    _history_ready = True
    for lines in _pending_live:
        quote = lines[0] if len(lines) == 2 else None
        _render_if_new(quote, lines[-1])
    _pending_live.clear()

    await _refresh_completions()
    asyncio.create_task(_periodic_cache_reload())
    session = PromptSession(completer=MeshCompleter(), complete_while_typing=False)
    print_formatted_text(HTML(
        f"\n<ansiwhite>{t('startup_hint')}</ansiwhite>\n"
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
                            f"<ansired>{t('err_command_failed', error=_safe(str(e)))}</ansired>"
                        ))
                    continue

                resp = await _client.request("send", text=text, channel=_channel_index)
                _handle_send_response(resp, text)

            except (KeyboardInterrupt, EOFError):
                break

    print_formatted_text(HTML(f"\n<ansiwhite>{t('disconnecting')}</ansiwhite>"))
    await _client.close()


if __name__ == "__main__":
    asyncio.run(main())
