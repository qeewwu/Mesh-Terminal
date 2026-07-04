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
    snr: float | None = None


def truncate(text: str, limit: int = QUOTE_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


_NEWLINES = re.compile(r"[\r\n]+")


def sanitize_text(text: str) -> str:
    """The log format is one line per message — embedded newlines would both
    break parsing and let a remote node forge extra log lines."""
    return _NEWLINES.sub(" ⏎ ", text)


def format_quote_line(long_name: str, short_name: str, text: str,
                       time_str: str = "") -> str:
    time_prefix = f"[{time_str}] " if time_str else ""
    return (f"  ┆ {time_prefix}{long_name or '?'} ({short_name}): "
            f"{truncate(sanitize_text(text))}")


def _escape_name(long_name: str, dm_tag: str) -> str:
    """`<DM tag><name>` is ambiguous when the name itself could extend the tag
    ("DM Master" broadcast, or "→ X" after an incoming-DM tag) — the parser
    would misread it as a DM marker. Swap the ambiguous space for a
    non-breaking one; an empty name would not parse at all, so fall back."""
    if not dm_tag and long_name.startswith("DM "):
        long_name = "DM\u00a0" + long_name[3:]
    elif dm_tag == "DM " and long_name.startswith("→ "):
        long_name = "→\u00a0" + long_name[2:]
    return long_name or "?"


def format_message_line(time_str: str, long_name: str, short_name: str,
                         text: str, hops: int, dm_tag: str = "",
                         channel_name: str = "Primary",
                         snr: float | None = None) -> str:
    snr_suffix = f" SNR:{snr:.2f}" if snr is not None else ""
    return (f"{channel_name} [{time_str}] {dm_tag}{_escape_name(long_name, dm_tag)} "
            f"({short_name}): {sanitize_text(text)} | {hops}{snr_suffix}")


_MSG_RE = re.compile(
    r"^(?:(?P<channel>.+?) )?\[(?P<time>\d{2}:\d{2}:\d{2})\] "
    r"(?P<dm>DM (?:→ )?)?"
    r"(?P<name>.+?) \((?P<short>[^)]*)\): "
    r"(?P<text>.*) \| (?P<hops>-?\d+)(?: SNR:(?P<snr>-?\d+(?:\.\d+)?))?$"
)
_QUOTE_RE = re.compile(
    r"^  ┆ (?:\[(?P<time>\d{2}:\d{2}:\d{2})\] )?"
    r"(?P<name>.+?) \((?P<short>[^)]*)\): (?P<text>.*)$"
)


def parse_log_line(line: str):
    """Parse one log line into a ParsedLine, or None if unrecognised."""
    line = line.rstrip("\n")
    if not line:
        return None
    m = _MSG_RE.match(line)
    if m:
        dm_tag = m.group("dm") or ""
        snr_str = m.group("snr")
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
            snr=float(snr_str) if snr_str is not None else None,
        )
    m = _QUOTE_RE.match(line)
    if m:
        return ParsedLine(
            kind="quote",
            time_str=m.group("time") or "",
            long_name=m.group("name"),
            short_name=m.group("short"),
            text=m.group("text"),
        )
    return None
