#!/usr/bin/env python3

import asyncio
import datetime
import sys
from pathlib import Path

import meshtastic.tcp_interface
from pubsub import pub
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

HOST = "meshtastic.local"
LOG_FILE = Path("chat.log")

# ANSI colour codes — work safely with prompt_toolkit's patch_stdout
DIM    = "\033[2m"
RESET  = "\033[0m"
BGREEN = "\033[1;32m"
GREEN  = "\033[32m"
BBLUE  = "\033[1;34m"
BLUE   = "\033[34m"
RED    = "\033[31m"
CYAN   = "\033[36m"

_interface = None


def _node_names(node_id: int) -> tuple[str, str]:
    """Return (long_name, short_name) for a node numeric ID."""
    if _interface and _interface.nodes:
        hex_id = f"!{node_id:08x}"
        node = _interface.nodes.get(node_id) or _interface.nodes.get(hex_id)
        if node and "user" in node:
            u = node["user"]
            return u.get("longName", hex_id), u.get("shortName", "???")
    return f"!{node_id:08x}", "???"


def _render(time_str: str, long_name: str, short_name: str,
            text: str, hops: int, own: bool = False) -> str:
    name_c = BBLUE if own else BGREEN
    short_c = BLUE if own else GREEN
    return (
        f"{DIM}[{time_str}]{RESET} "
        f"{name_c}{long_name}{RESET} "
        f"{short_c}({short_name}){RESET}"
        f": {text} "
        f"{DIM}| {hops}{RESET}"
    )


def _log_and_print(line: str, rendered: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(rendered)


def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        text = decoded.get("text", "")
        from_id = packet.get("from", 0)
        hop_limit = packet.get("hopLimit", 0)
        hop_start = packet.get("hopStart", hop_limit)
        hops = hop_start - hop_limit

        long_name, short_name = _node_names(from_id)
        now = datetime.datetime.now().strftime("%H:%M:%S")

        log_line = f"[{now}] {long_name} ({short_name}): {text} | {hops}"
        rendered = _render(now, long_name, short_name, text, hops)
        _log_and_print(log_line, rendered)

    except Exception as e:
        print(f"{RED}[Ошибка при получении: {e}]{RESET}")


async def main() -> None:
    global _interface

    print(f"{DIM}Подключение к {CYAN}{HOST}{RESET}{DIM}...{RESET}")
    try:
        _interface = meshtastic.tcp_interface.TCPInterface(hostname=HOST)
        pub.subscribe(on_receive, "meshtastic.receive.text")
        my_id = _interface.myInfo.my_node_num
        long_name, short_name = _node_names(my_id)
        print(
            f"{BGREEN}Подключено.{RESET} "
            f"Узел: {CYAN}{long_name} ({short_name}){RESET} "
            f"{DIM}!{my_id:08x}{RESET}"
        )
    except Exception as e:
        print(f"{RED}Не удалось подключиться: {e}{RESET}")
        sys.exit(1)

    session = PromptSession()
    print(f"{DIM}Введите сообщение и нажмите Enter. Ctrl+C / Ctrl+D — выход.{RESET}\n")

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
                log_line = f"[{now}] {long_name} ({short_name}): {text} | 0"
                rendered = _render(now, long_name, short_name, text, 0, own=True)
                _log_and_print(log_line, rendered)

            except (KeyboardInterrupt, EOFError):
                break

    print(f"\n{DIM}Отключение...{RESET}")
    _interface.close()


if __name__ == "__main__":
    asyncio.run(main())
