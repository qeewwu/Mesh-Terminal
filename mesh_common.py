#!/usr/bin/env python3
"""Shared constants and log line format used by mesh_logger.py and mesh_chat.py."""

import datetime
import re
from pathlib import Path
from typing import NamedTuple

HOST = "meshtastic.local"
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SOCKET_PATH = Path("/tmp/mesh_chat.sock")
BROADCAST_ADDR = 0xFFFFFFFF
QUOTE_MAX = 60
ACK_TIMEOUT_SECONDS = 60  # how long to wait for a mesh ACK/NAK before reporting "unknown"


def log_file_for(date: datetime.date) -> Path:
    return LOG_DIR / f"chat-{date.isoformat()}.log"


def current_log_file() -> Path:
    return log_file_for(datetime.date.today())


def list_log_files() -> list[Path]:
    """All log files, newest date first."""
    if not LOG_DIR.exists():
        return []
    return sorted(LOG_DIR.glob("chat-*.log"), reverse=True)


class ParsedLine(NamedTuple):
    kind: str  # "quote" | "message"
    time_str: str = ""
    is_dm: bool = False
    dm_out: bool = False
    long_name: str = ""
    short_name: str = ""
    text: str = ""
    hops: int = 0
    channel: str = "Primary"


def truncate(text: str, limit: int = QUOTE_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


_NEWLINES = re.compile(r"[\r\n]+")


def sanitize_text(text: str) -> str:
    """The log format is one line per message — embedded newlines would both
    break parsing and let a remote node forge extra log lines."""
    return _NEWLINES.sub(" ⏎ ", text)


def format_quote_line(long_name: str, short_name: str, text: str) -> str:
    return f"  ┆ {long_name} ({short_name}): {truncate(sanitize_text(text))}"


def format_message_line(time_str: str, long_name: str, short_name: str,
                         text: str, hops: int, dm_tag: str = "",
                         channel_name: str = "Primary") -> str:
    return (f"{channel_name} [{time_str}] {dm_tag}{long_name} ({short_name}): "
            f"{sanitize_text(text)} | {hops}")


_MSG_RE = re.compile(
    r"^(?:(?P<channel>.+?) )?\[(?P<time>\d{2}:\d{2}:\d{2})\] "
    r"(?P<dm>DM (?:→ )?)?"
    r"(?P<name>.+?) \((?P<short>[^)]*)\): "
    r"(?P<text>.*) \| (?P<hops>-?\d+)$"
)
_QUOTE_RE = re.compile(r"^  ┆ (?P<name>.+?) \((?P<short>[^)]*)\): (?P<text>.*)$")


def parse_log_line(line: str):
    """Parse one log line into a ParsedLine, or None if unrecognised."""
    line = line.rstrip("\n")
    if not line:
        return None
    m = _MSG_RE.match(line)
    if m:
        dm_tag = m.group("dm") or ""
        return ParsedLine(
            kind="message",
            time_str=m.group("time"),
            is_dm=bool(dm_tag),
            dm_out="→" in dm_tag,
            long_name=m.group("name"),
            short_name=m.group("short"),
            text=m.group("text"),
            hops=int(m.group("hops")),
            channel=m.group("channel") or "Primary",
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
