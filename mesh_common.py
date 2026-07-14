#!/usr/bin/env python3
"""Shared constants and log line format used by mesh_logger.py and mesh_chat.py."""

import datetime
import math
import re
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal stdlib .env reader — KEY=VALUE per line, '#' comments, blank
    lines ignored, values may be wrapped in matching quotes. Not a full .env
    spec (no multiline values, no escape sequences) — deliberately simple
    since this project has no other dependencies to justify pulling in
    python-dotenv for."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def _format_env_value(value: str) -> str:
    if not value or any(c in value for c in " \t#\"'"):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite `path`, updating/appending `updates` in place and leaving every
    other line (including comments and unrelated keys) untouched. Used by
    mesh_chat.py's /who to keep NODE_LONG_NAME/NODE_SHORT_NAME/NODE_ID current
    without clobbering the rest of a hand-edited .env."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in updates:
            out.append(f"{key}={_format_env_value(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={_format_env_value(value)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


_ENV = parse_env_file(ENV_FILE)

HOST = _ENV.get("MESH_HOST", "meshtastic.local")
# how mesh_logger.py talks to the device: "wifi" (TCP), "usb" (serial), or "ble"
# (Bluetooth Low Energy). USB_PORT/BLE_ADDRESS are optional — left empty (None),
# the meshtastic lib auto-detects the sole attached USB device / does a BLE scan
# and connects if exactly one Meshtastic device is found.
CONN_TYPE = _ENV.get("MESH_CONN_TYPE", "wifi").strip().lower()
USB_PORT = _ENV.get("MESH_USB_PORT", "").strip() or None
BLE_ADDRESS = _ENV.get("MESH_BLE_ADDRESS", "").strip() or None
PING_CHANNEL_NAME = _ENV.get("PING_CHANNEL", "Ping")
# interface language for mesh_i18n.py (ru|en, default ru — unchanged behavior
# for existing users). Read here, not in mesh_i18n.py itself, to avoid a
# circular import (mesh_i18n imports LANG from this module); mesh_common must
# never import mesh_i18n back, since the log format it owns is never localized.
LANG = _ENV.get("MESH_LANG", "ru").strip().lower()
if LANG not in ("ru", "en"):
    LANG = "ru"
LOG_DIR = BASE_DIR / "logs"
SOCKET_PATH = Path("/tmp/mesh_chat.sock")
BROADCAST_ADDR = 0xFFFFFFFF
QUOTE_MAX = 60
ACK_TIMEOUT_SECONDS = 60  # how long to wait for a mesh ACK/NAK before reporting "unknown"
# OneMesh-resolved !hex -> (long_name, short_name) cache. Owned and written by
# mesh_logger.py (it feeds real log lines, not just client display) — see
# "Node name resolution" in CLAUDE.md. mesh_chat.py only reads it.
ONEMESH_CACHE_FILE = BASE_DIR / "onemesh_cache.json"


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


def _sanitize_name(name: str) -> str:
    """long_name/short_name come from another node's NodeInfo — just as
    untrusted as message text (sanitize_text above), and just as capable of
    forging extra log lines via an embedded newline if left alone."""
    return _NEWLINES.sub(" ⏎ ", name)


def _sanitize_short_name(short_name: str) -> str:
    """short_name is wrapped bare as `(<short_name>)` with no delimiter of its
    own, and the parser's short-name group is `[^)]*` (stops at the first
    ')') — an untrusted short_name containing ')' would misalign the
    name/short split or break parsing outright, unlike long_name's ')'
    (which the parser's non-greedy name match already tolerates)."""
    return _sanitize_name(short_name).replace(")", "⟩")


def format_quote_line(long_name: str, short_name: str, text: str,
                       time_str: str = "") -> str:
    time_prefix = f"[{time_str}] " if time_str else ""
    long_name = _sanitize_name(long_name)
    short_name = _sanitize_short_name(short_name)
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
    long_name = _sanitize_name(long_name)
    short_name = _sanitize_short_name(short_name)
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


# ── geo helpers (used by mesh_chat.py's /who <name>) ───────────────────────────

_EARTH_RADIUS_KM = 6371.0088
_COMPASS_POINTS_RU = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ",
                      "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]
_COMPASS_POINTS_EN = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                      "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing (0-360, 0=North) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_point(deg: float, lang: str | None = None) -> str:
    """`lang` defaults to the module-level LANG (set from MESH_LANG); an
    explicit override exists only so tests can pin both languages without
    touching the environment."""
    points = _COMPASS_POINTS_EN if (lang or LANG) == "en" else _COMPASS_POINTS_RU
    return points[round(deg / 22.5) % 16]
