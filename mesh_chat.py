#!/usr/bin/env python3

import asyncio
import html
import json
import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as pt_clear

from mesh_common import SOCKET_PATH, list_log_files, parse_log_line

NAME_CACHE_FILE = Path("node_names_cache.json")
HISTORY_SIZE = 50
ONEMESH_API = "https://map.onemesh.ru/api/v1/nodes/{}"
ONEMESH_DELAY = 0.25

_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HEX_ID_RE = re.compile(r"^!([0-9a-f]{8})$")

_loop: asyncio.AbstractEventLoop | None = None
_client: "IPCClient | None" = None

_name_cache: dict[int, tuple[str, str]] = {}
_unresolved_ids: set[int] = set()

# Live "message" events pushed by the logger before history has finished
# printing are buffered here, then flushed in order once history is done.
_history_ready = False
_pending_live: list[list[str]] = []


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return html.escape(_XML_INVALID.sub("", text or ""))


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
    import urllib.error
    import urllib.request

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


def _find_node_in_list(nodes: list[dict], query: str) -> dict | None:
    q = query.lower()
    for n in nodes:
        ln = (n.get("long_name") or "").lower()
        sn = (n.get("short_name") or "").lower()
        if q in ln or q in sn:
            return n
    return None


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
        self.whoami: tuple[str, str] | None = None
        self.whoami_id: int | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
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
            if not self._closing:
                print_formatted_text(HTML("<ansired>⚠ Соединение с логгером потеряно</ansired>"))

    async def request(self, cmd: str, timeout: float = 10.0, **fields) -> dict:
        self._req_counter += 1
        req_id = self._req_counter
        fut = self._loop.create_future()
        self._pending[req_id] = fut
        payload = {"req_id": req_id, "cmd": cmd, **fields}
        self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._writer.drain()
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


def _handle_event(obj: dict) -> None:
    kind = obj.get("event")
    if kind == "delivery":
        if obj.get("ok"):
            print_formatted_text(HTML("<ansigreen>  ✓ Доставлено</ansigreen>"))
        else:
            print_formatted_text(HTML(
                f"<ansired>  ✗ Не доставлено ({_safe(str(obj.get('error', '')))})</ansired>"
            ))
    elif kind == "message":
        lines = obj.get("lines", [])
        if _history_ready:
            for line in lines:
                _render_line(line)
        else:
            _pending_live.append(lines)


# ── display ───────────────────────────────────────────────────────────────────

def _print_quote(long_name: str, short_name: str, text: str) -> None:
    ln, sn = _resolve_display_name(long_name, short_name)
    print_formatted_text(HTML(
        f"<ansigray>  ┆ {_safe(ln)} ({_safe(sn)}): {_safe(text)}</ansigray>"
    ))


def _print_msg(time_str: str, long_name: str, short_name: str,
               text: str, hops: int, own: bool = False, is_dm: bool = False) -> None:
    ln, sn = _resolve_display_name(long_name, short_name)
    t  = _safe(time_str)
    ln = _safe(ln)
    sn = _safe(sn)
    tx = _safe(text)

    if is_dm:
        print_formatted_text(HTML(
            f"<ansired>[{t}] DM <b>{ln}</b> ({sn}): {tx} | {hops}</ansired>"
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


def _render_line(line: str) -> None:
    parsed = parse_log_line(line)
    if not parsed:
        return
    if parsed.kind == "quote":
        _print_quote(parsed.long_name, parsed.short_name, parsed.text)
    else:
        own = (_client.whoami is not None
              and (parsed.long_name, parsed.short_name) == _client.whoami)
        _print_msg(parsed.time_str, parsed.long_name, parsed.short_name,
                   parsed.text, parsed.hops, own=own, is_dm=parsed.is_dm)


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
        units = _split_into_units(lines) + units
        if len(units) >= n:
            break

    for quote, msg in units[-n:]:
        if quote:
            _render_line(quote)
        _render_line(msg)


# ── commands ──────────────────────────────────────────────────────────────────

async def _cmd_nodes() -> None:
    resp = await _client.request("nodes")
    if not resp.get("ok") or not resp.get("nodes"):
        print_formatted_text(HTML("<ansiyellow>Нет данных об узлах</ansiyellow>"))
        return

    print_formatted_text(HTML("<ansiwhite>─── Видимые узлы ───</ansiwhite>"))
    for n in resp["nodes"]:
        if n["has_user"]:
            ln, sn = _safe(n["long_name"]), _safe(n["short_name"])
        else:
            cached = _name_cache.get(n["node_id"])
            if cached:
                ln, sn = _safe(cached[0]), _safe(cached[1])
            else:
                ln, sn = f"!{n['node_id']:08x}", "???"
                _unresolved_ids.add(n["node_id"])

        battery = n.get("battery")
        bat_str = f" 🔋{battery}%" if battery is not None else ""
        snr = n.get("snr")
        snr_str = f" SNR:{snr:.1f}" if snr is not None else ""

        ago = n.get("seconds_ago")
        if ago is not None:
            if ago < 60:
                heard, color = f"{ago}с", "ansigreen"
            elif ago < 3600:
                heard, color = f"{ago // 60}м", "ansiyellow"
            else:
                heard, color = f"{ago // 3600}ч", "ansigray"
        else:
            heard, color = "?", "ansigray"

        print_formatted_text(HTML(
            f"  <b><ansigreen>{ln}</ansigreen></b> <ansigreen>({sn})</ansigreen>"
            f"<ansiwhite>{bat_str}{snr_str}</ansiwhite>"
            f" <{color}>· {heard} назад</{color}>"
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


async def _cmd_dm(args: str) -> None:
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_formatted_text(HTML(
            "<ansiyellow>Использование: /dm &lt;имя&gt; &lt;текст&gt;</ansiyellow>"
        ))
        return

    target_name, text = parts
    resp = await _client.request("nodes")
    if not resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Не удалось получить список узлов: {_safe(resp.get('error', ''))}</ansired>"
        ))
        return

    node = _find_node_in_list(resp["nodes"], target_name)
    if not node:
        print_formatted_text(HTML(
            f"<ansired>Узел '{_safe(target_name)}' не найден. Проверьте /nodes</ansired>"
        ))
        return

    send_resp = await _client.request("dm", node_id=node["node_id"], text=text)
    if not send_resp.get("ok"):
        print_formatted_text(HTML(
            f"<ansired>Ошибка отправки: {_safe(send_resp.get('error', ''))}</ansired>"
        ))


def _cmd_clear() -> None:
    pt_clear()


async def _cmd_updatenames() -> None:
    targets = set(_unresolved_ids)
    resp = await _client.request("nodes")
    if resp.get("ok"):
        for n in resp["nodes"]:
            if not n["has_user"] and n["node_id"] not in _name_cache:
                targets.add(n["node_id"])

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
    cmd  = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if   cmd == "nodes":       await _cmd_nodes()
    elif cmd == "who":         _cmd_who()
    elif cmd == "dm":          await _cmd_dm(args)
    elif cmd == "updatenames": await _cmd_updatenames()
    elif cmd == "clear":       _cmd_clear()
    elif cmd == "help":        _cmd_help()
    else:
        print_formatted_text(HTML(
            f"<ansiyellow>Неизвестная команда /{_safe(cmd)}. Введите /help</ansiyellow>"
        ))


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _loop, _client, _history_ready
    _loop = asyncio.get_running_loop()
    _load_name_cache()

    if not SOCKET_PATH.exists():
        print_formatted_text(HTML(
            f"<ansired>Логгер не запущен ({SOCKET_PATH} не найден). "
            f"Запустите mesh_logger.py (или systemctl start mesh-logger).</ansired>"
        ))
        sys.exit(1)

    _client = IPCClient(_loop)
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

    _print_initial_history(HISTORY_SIZE)
    _history_ready = True
    for lines in _pending_live:
        for line in lines:
            _render_line(line)
    _pending_live.clear()

    session = PromptSession()
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
                    await _handle_command(text)
                    continue

                resp = await _client.request("send", text=text)
                if not resp.get("ok"):
                    print_formatted_text(HTML(
                        f"<ansired>Ошибка отправки: {_safe(resp.get('error', ''))}</ansired>"
                    ))

            except (KeyboardInterrupt, EOFError):
                break

    print_formatted_text(HTML("\n<ansiwhite>Отключение...</ansiwhite>"))
    await _client.close()


if __name__ == "__main__":
    asyncio.run(main())
