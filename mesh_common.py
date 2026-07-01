#!/usr/bin/env python3
"""Shared constants and chat.log line format used by mesh_logger.py and mesh_chat.py."""

import re
from pathlib import Path
from typing import NamedTuple

HOST = "meshtastic.local"
LOG_FILE = Path("chat.log")
SOCKET_PATH = Path("/tmp/mesh_chat.sock")
BROADCAST_ADDR = 0xFFFFFFFF
QUOTE_MAX = 60


class ParsedLine(NamedTuple):
    kind: str  # "quote" | "message"
    time_str: str = ""
    is_dm: bool = False
    long_name: str = ""
    short_name: str = ""
    text: str = ""
    hops: int = 0


def truncate(text: str, limit: int = QUOTE_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def format_quote_line(long_name: str, short_name: str, text: str) -> str:
    return f"  ┆ {long_name} ({short_name}): {truncate(text)}"


def format_message_line(time_str: str, long_name: str, short_name: str,
                         text: str, hops: int, dm_tag: str = "") -> str:
    return f"[{time_str}] {dm_tag}{long_name} ({short_name}): {text} | {hops}"


_MSG_RE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\] "
    r"(?P<dm>DM (?:→ )?)?"
    r"(?P<name>.+?) \((?P<short>[^)]*)\): "
    r"(?P<text>.*) \| (?P<hops>-?\d+)$"
)
_QUOTE_RE = re.compile(r"^  ┆ (?P<name>.+?) \((?P<short>[^)]*)\): (?P<text>.*)$")


def parse_log_line(line: str):
    """Parse one chat.log line into a ParsedLine, or None if unrecognised."""
    line = line.rstrip("\n")
    if not line:
        return None
    m = _MSG_RE.match(line)
    if m:
        return ParsedLine(
            kind="message",
            time_str=m.group("time"),
            is_dm=bool(m.group("dm")),
            long_name=m.group("name"),
            short_name=m.group("short"),
            text=m.group("text"),
            hops=int(m.group("hops")),
        )
    m = _QUOTE_RE.match(line)
    if m:
        return ParsedLine(
            kind="quote",
            long_name=m.group("name"),
            short_name=m.group("short"),
            text=m.group("text"),
        )
    return None
