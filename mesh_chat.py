#!/usr/bin/env python3

import asyncio
import datetime
import html
import sys
from pathlib import Path

import meshtastic.tcp_interface
from pubsub import pub
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout

HOST = "meshtastic.local"
LOG_FILE = Path("chat.log")

_interface = None
_loop: asyncio.AbstractEventLoop | None = None


def _node_names(node_id: int) -> tuple[str, str]:
    if _interface and _interface.nodes:
        hex_id = f"!{node_id:08x}"
        node = _interface.nodes.get(node_id) or _interface.nodes.get(hex_id)
        if node and "user" in node:
            u = node["user"]
            return u.get("longName", hex_id), u.get("shortName", "???")
    return f"!{node_id:08x}", "???"


def _print(time_str: str, long_name: str, short_name: str,
           text: str, hops: int, own: bool = False) -> None:
    t  = html.escape(time_str)
    ln = html.escape(long_name)
    sn = html.escape(short_name)
    tx = html.escape(text)
    name_style  = "bold ansiblue"  if own else "bold ansigreen"
    short_style = "ansiblue"       if own else "ansigreen"
    print_formatted_text(HTML(
        f"<ansiwhite>[{t}]</ansiwhite> "
        f"<{name_style}>{ln}</{name_style}> "
        f"<{short_style}>({sn})</{short_style}>"
        f": {tx} "
        f"<ansiwhite>| {hops}</ansiwhite>"
    ))


def _log(time_str: str, long_name: str, short_name: str,
         text: str, hops: int) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time_str}] {long_name} ({short_name}): {text} | {hops}\n")


def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        text      = decoded.get("text", "")
        from_id   = packet.get("from", 0)
        hop_limit = packet.get("hopLimit", 0)
        hop_start = packet.get("hopStart", hop_limit)
        hops      = hop_start - hop_limit

        long_name, short_name = _node_names(from_id)
        now = datetime.datetime.now().strftime("%H:%M:%S")

        _log(now, long_name, short_name, text, hops)
        if _loop:
            _loop.call_soon_threadsafe(_print, now, long_name, short_name, text, hops)

    except Exception as e:
        if _loop:
            _loop.call_soon_threadsafe(
                print_formatted_text,
                HTML(f"<ansired>[Ошибка при получении: {html.escape(str(e))}]</ansired>")
            )


async def main() -> None:
    global _interface, _loop
    _loop = asyncio.get_running_loop()

    print_formatted_text(HTML(f"<ansiwhite>Подключение к <ansicyan>{HOST}</ansicyan>...</ansiwhite>"))
    try:
        _interface = meshtastic.tcp_interface.TCPInterface(hostname=HOST)
        pub.subscribe(on_receive, "meshtastic.receive.text")
        my_id = _interface.myInfo.my_node_num
        long_name, short_name = _node_names(my_id)
        print_formatted_text(HTML(
            f"<bold><ansigreen>Подключено.</ansigreen></bold> "
            f"Узел: <ansicyan>{html.escape(long_name)} ({html.escape(short_name)})</ansicyan> "
            f"<ansiwhite>!{my_id:08x}</ansiwhite>"
        ))
    except Exception as e:
        print_formatted_text(HTML(f"<ansired>Не удалось подключиться: {html.escape(str(e))}</ansired>"))
        sys.exit(1)

    session = PromptSession()
    print_formatted_text(HTML("<ansiwhite>Введите сообщение и нажмите Enter. Ctrl+C / Ctrl+D — выход.</ansiwhite>\n"))

    with patch_stdout():
        while True:
            try:
                text = await session.prompt_async("> ")
                text = text.strip()
                if not text:
                    continue

                _interface.sendText(text, channelIndex=0)

                my_id = _interface.myInfo.my_node_num
                long_name, short_name = _node_names(my_id)
                now = datetime.datetime.now().strftime("%H:%M:%S")
                _log(now, long_name, short_name, text, 0)
                _print(now, long_name, short_name, text, 0, own=True)

            except (KeyboardInterrupt, EOFError):
                break

    print_formatted_text(HTML("\n<ansiwhite>Отключение...</ansiwhite>"))
    _interface.close()


if __name__ == "__main__":
    asyncio.run(main())
