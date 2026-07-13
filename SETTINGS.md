# Node settings (`/settings`)

*(Русская версия: [SETTINGS.ru.md](SETTINGS.ru.md))*

`/settings` with no arguments shows the current values. `/settings <key> <value>` changes one
setting and writes it to the device immediately (`node.writeConfig(...)` in `mesh_logger.py`).
This is the configuration of the **local node** — the one `mesh_logger.py` is connected to — not
of any other node on the network.

The list of settings is deliberately small and curated (see `SETTINGS_REGISTRY` in
`mesh_logger.py`), not generic access to an arbitrary protobuf field. Reasons:

- **`network` (WiFi) is not exposed.** The logger's connection to the device runs over that same
  WiFi/TCP link. A typo in the SSID/password, or disabling the WiFi interface, would sever that
  same connection — the device would become unreachable remotely.
- **`bluetooth`/`security` are not exposed.** These hold PINs and cryptographic keys; letting
  them be changed over a local Unix socket with no extra protection is an unnecessary risk.
- **`display`/`power`/`audio`/other modules are not exposed** — they're meaningless for a headless
  node connected only over WiFi.

Some settings (mainly `lora.*`) only take effect after the device reboots — writing the setting
by itself doesn't trigger that. That's what **`/reboot`** is for: it shows a warning and requires
an explicit `/reboot confirm` within 30 seconds (a two-step confirmation, so a single typo can't
reboot the node by accident). The TCP connection drops for a moment during the reboot and
`mesh_logger.py` reconnects on its own (see the reconnect logic in `CLAUDE.md`) — any messages
sent during that window queue in the outbox and go out automatically once it's back.

## Parameters

| Key | Config section | Type | What it does |
|---|---|---|---|
| `owner_long` | — (`setOwner`) | string | Node's full name, as seen by everyone else on the network |
| `owner_short` | — (`setOwner`) | string | Node's short name (usually up to 4 characters), shown compactly in lists/logs |
| `role` | `device` | enum | The node's role on the network — see the role table below |
| `node_info_broadcast_secs` | `device` | int, 60–86400 | How often the node broadcasts its own NodeInfo (name, hardware details) to others |
| `hop_limit` | `lora` | int, 1–7 | Maximum retransmissions (hops) for packets sent from this node |
| `tx_power` | `lora` | int, 0–30 | Transmit power in dBm. `0` isn't a wattage — it's a flag meaning "use the region's default": the device substitutes the real power itself, and `/settings` will show *that* value (not `0`) after a reboot — expected behavior, not a bug |
| `position_broadcast_secs` | `position` | int, 30–86400 | How often the node broadcasts its GPS position |
| `position_smart_enabled` | `position` | bool | If enabled, position is broadcast on meaningful movement rather than on a fixed timer |
| `gps_mode` | `position` | enum: `ENABLED`, `DISABLED`, `NOT_PRESENT` | Mode of the built-in GPS receiver |
| `fixed_position` | `position` | bool | Treat the current position as fixed (a stationary node, e.g. a base station) |
| `telemetry_device_secs` | `telemetry` (module) | int, 60–86400 | How often to send device telemetry (battery, voltage, uptime) |
| `telemetry_env_enabled` | `telemetry` (module) | bool | Whether to send environmental telemetry (temperature/pressure/humidity), if a sensor is present |
| `mqtt_enabled` | `mqtt` (module) | bool | Enable the LoRa ⇄ MQTT bridge — messages also go out to the internet, not just the mesh (e.g. to [OneMesh](https://map.onemesh.ru)) |
| `mqtt_address` | `mqtt` (module) | string | Broker address, `host[:port]` (e.g. `map.onemesh.ru:1883`) |
| `mqtt_username` | `mqtt` (module) | string | Login for the broker |
| `mqtt_password` | `mqtt` (module) | string | Password — written as given, but `/settings` always shows it masked (`••••••••`), never in plaintext |
| `mqtt_encryption_enabled` | `mqtt` (module) | bool | Encrypt MQTT packets with the same PSK as the channel (instead of sending them in the clear) |
| `mqtt_json_enabled` | `mqtt` (module) | bool | Also publish messages as JSON (handy for third-party integrations) |
| `mqtt_tls_enabled` | `mqtt` (module) | bool | Use TLS to connect to the broker — enable if the broker requires it |
| `mqtt_root` | `mqtt` (module) | string | MQTT root topic (e.g. `msh/RU`) |
| `mqtt_uplink` | channel (`ChannelSettings`) | `<channel>:on\|off` | Relay a channel from LoRa into MQTT — e.g. `/settings mqtt_uplink Primary:on` |
| `mqtt_downlink` | channel (`ChannelSettings`) | `<channel>:on\|off` | Relay a channel from MQTT back into LoRa — e.g. `/settings mqtt_downlink Primary:off` |

`mqtt_uplink`/`mqtt_downlink` are the only two settings that don't write to `moduleConfig` but to a
specific channel's settings instead (`node.writeChannel()`, not `node.writeConfig()`), so they
have their own value format — a channel name and a flag joined by a colon, rather than a plain
number/bool/enum. In the `/settings` list they show a summary across every channel at once
(`Primary:on, LongFast:off`), not a single value, because each channel has its own state.

**To actually get MQTT working**, you typically need at least: `mqtt_enabled on`, `mqtt_address`,
possibly `mqtt_username`/`mqtt_password` (public brokers like mqtt.meshtastic.org work without a
login; check with services like OneMesh for their own requirements), and
`mqtt_uplink <your_channel>:on` — without that last step, messages keep flowing over LoRa only,
even with `mqtt_enabled` on.

### Node roles (`role`)

| Value | Meaning |
|---|---|
| `CLIENT` | A regular participant node (the default) |
| `CLIENT_MUTE` | Participates in the network but doesn't relay other nodes' packets |
| `CLIENT_HIDDEN` | Doesn't participate in NodeInfo exchange — the smallest possible network footprint |
| `CLIENT_BASE` | A client with a fixed position (a stationary base station that doesn't relay) |
| `ROUTER` | An infrastructure node — relays packets, isn't shown as a regular chat participant |
| `ROUTER_CLIENT` | A relay that also participates in chat like a regular node |
| `ROUTER_LATE` | A relay with delayed forwarding (reduces collisions in dense networks) |
| `REPEATER` | A pure physical-layer repeater, with no NodeInfo of its own |
| `TRACKER` / `TAK_TRACKER` | A tracker node (frequent position updates), including TAK integration |
| `SENSOR` | A node with sensors, geared toward telemetry rather than chat |
| `TAK` | Integration with ATAK/WinTAK |
| `LOST_AND_FOUND` | "Lost and found" mode — aggressive position broadcasting |

Changing `role` changes the node's behavior on the mesh (relaying, visibility), but doesn't touch
WiFi/TCP — the logger's connection is unaffected.

## Value format

- **Integers** — just a number: `/settings hop_limit 4`
- **Booleans** — `on`/`off`, `true`/`false`, `1`/`0` (Russian `да`/`нет` also accepted, since the
  client's own UI is in Russian): `/settings position_smart_enabled on`
- **Enums** — the value's name, case-insensitive: `/settings role router`, `/settings gps_mode disabled`
- **Strings** (`owner_long`/`owner_short`, `mqtt_address`/`mqtt_username`/`mqtt_password`/`mqtt_root`) —
  as-is, no quoting: `/settings owner_long My Node`
- **Channel:flag** (`mqtt_uplink`/`mqtt_downlink` only) — `<channel name>:on|off`:
  `/settings mqtt_uplink Primary:on`

Ranges and valid enum values are checked on the logger's side before anything is sent to the
device — on a mistake, `/settings` returns a clear error instead of sending garbage to the device.
