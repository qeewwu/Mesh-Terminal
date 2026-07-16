"""UI string tables for mesh_chat.py and mesh_logger.py, selected by MESH_LANG
(mesh_common.LANG). The log file format (mesh_common.py: format_message_line,
format_quote_line, parse_log_line, channel names, "DM "/"DM → " tags) is
**never** localized — it's a persistent on-disk format both processes must
agree on regardless of interface language. This module only holds
presentation strings: command output and error messages.

Convention: templates hold the same prompt_toolkit HTML markup the old
f-strings did (`<ansired>`, literal `<b>`, pre-escaped `&lt;name&gt;`) and are
trusted; every interpolated *value* is `_safe()`-escaped by the caller before
being passed as a kwarg to `t()`, exactly like the f-string call sites used
to do. Templates use `.format()` placeholders, never positional `{}`.

No gettext/.po files — this is a two-language personal project, not a
framework; a flat dict pair is simpler to read, diff, and keep in sync.
"""
from mesh_common import LANG

# ── Russian (default) ─────────────────────────────────────────────────────────

_RU: dict[str, object] = {
    # -- mesh_logger.py: send path --------------------------------------------
    "err_no_replyid_support": (
        "библиотека meshtastic не поддерживает replyId — "
        "обновите: pip install -U meshtastic"
    ),
    "err_message_too_long": "сообщение слишком длинное ({bytes} байт, максимум {max})",
    "err_send_failed": "отправка не удалась: {error}",
    "err_not_sent": "не отправлено",

    # -- mesh_logger.py: SETTINGS_DESCRIPTIONS --------------------------------
    "setting_owner_long": "Полное имя узла",
    "setting_owner_short": "Короткое имя узла (обычно до 4 символов)",
    "setting_role": "Роль узла в сети (CLIENT, ROUTER, REPEATER, ...)",
    "setting_node_info_broadcast_secs": "Как часто рассылать свой NodeInfo, секунды",
    "setting_hop_limit": "Максимум хопов для пакетов, отправленных с этого узла",
    "setting_tx_power": "Мощность передачи, дБм (0 = значение по умолчанию для региона)",
    "setting_position_broadcast_secs": "Как часто рассылать позицию, секунды",
    "setting_position_smart_enabled": "Рассылать позицию только при значимом перемещении",
    "setting_gps_mode": "Режим GPS-приёмника (ENABLED, DISABLED, NOT_PRESENT)",
    "setting_fixed_position": "Считать текущую позицию фиксированной (узел неподвижен)",
    "setting_telemetry_device_secs": "Как часто слать телеметрию устройства, секунды",
    "setting_telemetry_env_enabled": "Слать телеметрию окружающей среды (датчики), если есть",
    "setting_mqtt_enabled": "Включить отправку сообщений в MQTT (в дополнение к LoRa)",
    "setting_mqtt_address": "Адрес MQTT-брокера, host[:port] (например map.onemesh.ru)",
    "setting_mqtt_username": "Логин для подключения к MQTT-брокеру",
    "setting_mqtt_password": "Пароль для подключения к MQTT-брокеру (в списке — маской)",
    "setting_mqtt_encryption_enabled": "Шифровать пакеты в MQTT тем же PSK, что и в канале",
    "setting_mqtt_json_enabled": "Дополнительно публиковать сообщения в виде JSON",
    "setting_mqtt_tls_enabled": "TLS-соединение с брокером",
    "setting_mqtt_root": "Корневой топик MQTT (например msh/RU)",
    "setting_mqtt_uplink": (
        "Ретрансляция канала из LoRa в MQTT: <канал>:on|off, например Primary:on"
    ),
    "setting_mqtt_downlink": "Ретрансляция канала из MQTT в LoRa: <канал>:on|off",

    # -- mesh_logger.py: _parse_setting_value / _apply_channel_link ----------
    "err_bool_expected": "ожидалось булево значение (on/off, да/нет, 1/0)",
    "err_int_expected": "ожидалось целое число",
    "err_out_of_range": "вне диапазона {lo}..{hi}",
    "err_enum_allowed": "допустимые значения: {values}",
    "err_unknown_setting_type": "неизвестный тип параметра: {kind}",
    "err_channel_link_format": "формат: <канал>:on|off, например Primary:on",
    "err_mqtt_channel_not_found": "канал '{name}' не найден",

    # -- mesh_logger.py: IPC handler errors -----------------------------------
    "err_outbox_full": "устройство офлайн, очередь исходящих переполнена",
    "err_traceroute_timeout": "узел не ответил (timeout)",
    "err_unknown_param": "неизвестный параметр: {param}",
    "err_apply_failed": "не удалось применить: {error}",
    "err_reboot_failed": "не удалось перезагрузить: {error}",
    "err_reconnect_wifi_only": (
        "смена адреса поддерживается только при MESH_CONN_TYPE=wifi (сейчас: {conn_type})"
    ),

    # -- shared plural forms ---------------------------------------------------
    "hops_forms": ("хоп", "хопа", "хопов"),

    # -- mesh_chat.py: argparse ------------------------------------------------
    "argparse_description": "Терминальный чат-клиент Meshtastic (работает через mesh_logger.py)",
    "argparse_channel_metavar": "ИМЯ",
    "argparse_channel_help": "показывать и отправлять только в этот канал",
    "argparse_history_help": "сколько сообщений истории показать (по умолчанию {default})",

    # -- mesh_chat.py: _pick_node ----------------------------------------------
    "err_node_not_found": "Узел '{query}' не найден. Проверьте /nodes",
    "warn_node_ambiguous": (
        "Имя '{query}' неоднозначно, совпадают: {names}{more} — уточните запрос"
    ),

    # -- mesh_chat.py: tab completion -------------------------------------------
    "stats_completions": ("день", "узел"),

    # -- mesh_chat.py: IPCClient -------------------------------------------------
    "err_no_logger_connection": "нет соединения с логгером",

    # -- mesh_chat.py: _handle_event delivery ------------------------------------
    "delivered": "  ✓ Доставлено{hop_str}{ref}",
    "not_delivered": "  ✗ Не доставлено ({error}){ref}",
    "delivery_unconfirmed": "  ⏳ Нет подтверждения доставки{ref} (мог дойти, но ACK не получен)",

    # -- mesh_chat.py: _handle_event device --------------------------------------
    "device_disconnected": "⚠ Устройство отключилось — жду переподключения логгера",
    "device_reconnected": "✓ Устройство снова на связи: {ln} ({sn})",

    # -- mesh_chat.py: logger reconnect -------------------------------------------
    "logger_lost": "⚠ Соединение с логгером потеряно — переподключаюсь...",
    "logger_restored": "✓ Соединение с логгером восстановлено",

    # -- mesh_chat.py: /nodes -----------------------------------------------------
    "err_unknown_sort": "Неизвестная сортировка '{mode}'. Доступные: {modes}",
    "err_no_node_data": "Нет данных об узлах",
    "hdr_visible_nodes": "─── Видимые узлы ───",
    "online_now": "Онлайн",
    "ago_minutes": "{mins}м назад",
    "ago_hours": "{hrs}ч назад",
    "unknown_time": "?",

    # -- mesh_chat.py: /who --------------------------------------------------------
    "err_no_self_info": "Информация о себе недоступна",
    "who_me_label": "Я: ",
    "who_node_line": "👤 {ln} ({sn}) {id_str}",

    # -- mesh_chat.py: _handle_send_response ----------------------------------------
    "queued": (
        "  ⏳ В очереди: «{snippet}» — устройство "
        "офлайн, отправлю сразу при переподключении"
    ),
    "err_send_error": "Ошибка отправки: {error}",

    # -- mesh_chat.py: /dm ------------------------------------------------------------
    "usage_dm": "Использование: /dm &lt;имя&gt; &lt;текст&gt; (имя с пробелами — в кавычках)",
    "err_nodes_fetch_failed": "Не удалось получить список узлов: {error}",

    # -- mesh_chat.py: /trace ----------------------------------------------------------
    "usage_trace": "Использование: /trace &lt;имя узла&gt;",
    "tracing": "Трассирую до {name} — может занять до {timeout}с...",
    "err_traceroute_failed": "Traceroute не удался: {error}",
    "route_there": "Маршрут туда:",
    "route_back": "Маршрут обратно:",
    "no_return_route": "(обратный маршрут не получен)",

    # -- mesh_chat.py: /ping -------------------------------------------------------------
    "usage_ping": "Использование: /ping &lt;имя узла&gt;",
    "pinging": "🏓 Пингую {ln} ({sn})...",
    "err_no_packet_id": "Не удалось получить packet_id для пинга",
    "ping_no_reply": "🏓 {ln}: нет ответа за {timeout}с",
    "ping_delivered": "🏓 {ln}: доставлено за {elapsed}с{hop_str}",
    "ping_not_delivered": "🏓 {ln}: не доставлено ({error})",
    "ping_unconfirmed": (
        "🏓 {ln}: нет подтверждения доставки за {elapsed}с "
        "(мог дойти, но ACK не получен)"
    ),

    # -- mesh_chat.py: /who (position lines, shown when the node has GPS data) ------------
    "unit_m": "м",
    "unit_km": "км",
    "err_no_position": "У узла {name} нет данных о позиции (не шлёт GPS-координаты)",
    "pos_line": "📍 {ln} ({sn}): {lat}, {lon}{alt_str}",
    "pos_distance": "   от меня: {dist_str}, азимут {brng}° ({compass})",

    # -- mesh_chat.py: /reply, /react (shared _replyables plumbing) -----------------------
    "hdr_replyables": "─── На что можно ответить (#1 — самое свежее) ───",
    "reply_hint": "Ответить: /reply #&lt;номер&gt; &lt;текст&gt; — или /reply &lt;текст&gt; на #1",
    "react_hint": "Реакция: /react #&lt;номер&gt; &lt;эмодзи&gt; — или /react &lt;эмодзи&gt; на #1",
    "err_no_such_reply": "Нет сообщения #{num} — доступны #1…#{max}",

    "err_nothing_to_quote": (
        "Пока нечего цитировать: /reply работает для сообщений, "
        "полученных после запуска клиента"
    ),
    "usage_reply": (
        "Использование: /reply #&lt;номер&gt; &lt;текст&gt; "
        "(номера — в /reply без аргументов)"
    ),
    "replying_to": "Отвечаю на: [{time}] {name} — {snippet}",

    "err_nothing_to_react": (
        "Пока нечего реактить: /react работает для сообщений, "
        "полученных после запуска клиента"
    ),
    "usage_react": (
        "Использование: /react #&lt;номер&gt; &lt;эмодзи&gt; "
        "(номера — в /react без аргументов)"
    ),
    "reacting_to": "Реагирую на: [{time}] {name} — {snippet}",

    # -- mesh_chat.py: channel resolution/switching ----------------------------------------
    "channel_confirmed": "Канал подтверждён: {name}",
    "channel_not_found_device": (
        "Канал '{name}' не найден на устройстве. "
        "Доступные: {available} — показываю все каналы"
    ),
    "channel_status": "Сейчас: <b>{current}</b>. Каналы: {channels}. Переключение: /ch &lt;имя&gt; или /ch all",
    "channel_all_word": "все",
    "channel_all_banner": "Все каналы (отправка — в Primary)",
    "err_channel_not_found": "Канал '{name}' не найден. Доступные: {available}",
    "channel_banner": "Канал: {name}",
    "no_data": "нет данных",

    # -- mesh_chat.py: /send --------------------------------------------------------------
    "usage_send": "Использование: /send &lt;канал&gt; &lt;текст&gt;",

    # -- mesh_chat.py: /search --------------------------------------------------------------
    "usage_search": "Использование: /search &lt;текст&gt;",
    "hdr_search": "─── Поиск: «{query}»",
    "search_empty": "По запросу «{query}» ничего не найдено",
    "shown_last_n": " (показаны последние {limit})",

    # -- mesh_chat.py: /who (recent messages from that sender) --------------------------------
    "hdr_last_from": "─── Последние сообщения от «{query}»",
    "last_empty": "Сообщений от «{query}» не найдено",

    # -- mesh_chat.py: /stats -----------------------------------------------------------------
    "err_no_stats": "Нет данных для статистики",
    "hdr_by_day": "─── Сообщений по дням ───",
    "hdr_top_nodes": "─── Топ активных узлов ───",
    "hdr_stats": "─── Статистика ───",
    "total_messages": "  Всего сообщений: <b>{total}</b> за {days} дн.",
    "busiest_hour": "  Самый активный час: {hour:02d}:00 ({count} сообщений)",
    "top_nodes_label": "  Топ узлов:",
    "stats_more_hint": "Подробнее: /stats день · /stats узел",

    # -- mesh_chat.py: /updatenames -------------------------------------------------------------
    "err_update_names_failed": "Не удалось обновить имена: {error}",
    "nothing_to_update": "Нечего обновлять — все видимые узлы уже с именами",
    "update_done": "Готово: обновлено {resolved} из {targets} имён",

    # -- mesh_chat.py: /ignore, /unignore ----------------------------------------------------------
    "empty_ignored_list": "Список игнорируемых пуст",
    "hdr_ignored": "─── Игнорируемые ───",
    "unignore_hint": "Снять: /unignore &lt;имя&gt;",
    "already_ignored": "«{name}» уже в игноре",
    "ignored_msg": "🔇 «{name}» добавлен в игнор устройства — его сообщения больше не будут доходить и логироваться",
    "not_ignored": "«{name}» не в списке игнорируемых — /unignore без аргументов покажет список",
    "unignored_msg": "🔊 «{name}» убран из игнора",
    "err_ignore_failed": "не удалось изменить игнор: {error}",

    # -- mesh_chat.py: /settings ---------------------------------------------------------------------
    "err_settings_fetch_failed": "Не удалось получить настройки: {error}",
    "hdr_settings": "─── Настройки узла ───",
    "settings_change_hint": "Изменить: /settings &lt;параметр&gt; &lt;значение&gt;. Подробности — {doc}",
    "usage_settings": (
        "Использование: /settings &lt;параметр&gt; &lt;значение&gt; "
        "(список параметров — /settings без аргументов)"
    ),
    "err_settings_apply_failed": "Не удалось применить: {error}",
    "settings_applied": (
        "<ansigreen>✓ {param} = {value}</ansigreen> "
        "<ansigray>(некоторые параметры требуют /reboot, чтобы вступить в силу)</ansigray>"
    ),
    "settings_doc": "SETTINGS.ru.md",

    # -- mesh_chat.py: /reboot ------------------------------------------------------------------------
    "usage_reboot": "Использование: /reboot — предупреждение, /reboot confirm — подтвердить перезагрузку",
    "err_no_pending_confirm": "Нет ожидающего подтверждения — сначала наберите /reboot",
    "err_reboot_failed_client": "Не удалось перезагрузить: {error}",
    "reboot_rebooting": (
        "🔄 Узел перезагружается — соединение на секунду оборвётся, "
        "логгер переподключится сам"
    ),
    "reboot_warn": (
        "⚠ Это перезагрузит устройство (пригодится и после /settings — "
        "часть параметров вступает в силу только после перезапуска). Подтвердите: "
        "<b>/reboot confirm</b> — окно {window}с"
    ),

    "err_reconnect_failed": "Не удалось запустить переподключение: {error}",
    "reconnect_triggered": "🔌 Принудительное переподключение к устройству запущено",
    "reconnect_triggered_host": "🔌 Переподключаюсь к {host}...",
    "reconnect_attempt_failed": "✗ Переподключиться не удалось: {error} — логгер продолжит пробовать сам",

    # -- mesh_chat.py: /help ------------------------------------------------------------------------------
    "help_lines": [
        "─── Команды ───────────────────────────────────────────────",
        "  /nodes [online|names|hops]  список видимых узлов (сортировка, по умолчанию online)",
        "  /who [имя]           информация о себе, или об узле — имена, hex id, батарея/SNR/хопы, позиция, последние сообщения",
        "  /dm &lt;имя&gt; &lt;текст&gt;   личное сообщение (имя с пробелами — в кавычках)",
        "  /reply               список недавних сообщений, на которые можно ответить",
        "  /reply &lt;текст&gt;       ответ (с цитатой) на последнее полученное сообщение",
        "  /reply #N &lt;текст&gt;    ответ на сообщение №N из списка /reply",
        "  /react &lt;эмодзи&gt;      реакция (tapback) на последнее сообщение",
        "  /react #N &lt;эмодзи&gt;   реакция на сообщение №N из списка /reply",
        "  /ch [имя|all]        показать/сменить канал",
        "  /send &lt;канал&gt; &lt;текст&gt; разовая отправка в канал без переключения сессии",
        "  /search &lt;текст&gt;     поиск по истории (учитывает текущий канал)",
        "  /stats [день|узел]   статистика по истории переписки (по умолчанию — канал Primary)",
        "  /trace &lt;имя&gt;        маршрут пакетов до узла (traceroute)",
        "  /ping &lt;имя&gt;         время доставки (RTT) и число хопов до узла",
        "  /ignore &lt;имя&gt;       игнорировать узел на уровне устройства (без аргумента — список)",
        "  /unignore [имя]      снять игнор (без аргумента — список игнорируемых)",
        "  /updatenames         подтянуть имена узлов с OneMesh (и так — раз в 30 мин фоном)",
        "  /settings [параметр значение]  настройки локального узла (см. SETTINGS.ru.md)",
        "  /reboot             перезагрузить узел (требует /reboot confirm)",
        "  /reconnect [host]    принудительно переподключиться (можно сменить IP/host для wifi)",
        "  /clear               очистить экран",
        "  /help                эта справка",
        "  Tab                  автодополнение команд, имён узлов и каналов",
        "────────────────────────────────────────────────────────────",
    ],

    # -- mesh_chat.py: _handle_command --------------------------------------------------------------------------
    "unknown_command": "Неизвестная команда /{cmd}. Введите /help",

    # -- mesh_chat.py: main() ---------------------------------------------------------------------------------------
    "err_logger_not_running": (
        "Логгер не запущен ({socket} не найден). "
        "Запустите mesh_logger.py (или systemctl start mesh-logger)."
    ),
    "err_logger_connect_failed": "Не удалось подключиться к логгеру: {error}",
    "connected_banner": "Подключено к логгеру. Узел: {ln} ({sn})",
    "device_not_connected_yet": "Логгер работает, но устройство пока не подключено",
    "channel_pending_msg": (
        "Канал '{name}' пока не проверить — устройство "
        "не подключено. Приму как есть и уточню, когда оно выйдет на связь "
        "(отправка пока — в Primary)"
    ),
    "err_channel_not_found_startup": "Канал '{name}' не найден. Доступные каналы: {available}",
    "all_channels_banner": "Показываю сообщения со всех каналов (отправка — в Primary)",
    "startup_hint": "Сообщение или /help. Ctrl+C / Ctrl+D — выход.",
    "err_command_failed": "Ошибка команды: {error}",
    "disconnecting": "Отключение...",
}

# ── English ────────────────────────────────────────────────────────────────

_EN: dict[str, object] = {
    "err_no_replyid_support": (
        "the meshtastic library doesn't support replyId — "
        "upgrade: pip install -U meshtastic"
    ),
    "err_message_too_long": "message too long ({bytes} bytes, max {max})",
    "err_send_failed": "send failed: {error}",
    "err_not_sent": "not sent",

    "setting_owner_long": "Node's full name",
    "setting_owner_short": "Node's short name (usually up to 4 characters)",
    "setting_role": "Node's role on the network (CLIENT, ROUTER, REPEATER, ...)",
    "setting_node_info_broadcast_secs": "How often to broadcast its own NodeInfo, seconds",
    "setting_hop_limit": "Max hops for packets sent from this node",
    "setting_tx_power": "Transmit power, dBm (0 = region default)",
    "setting_position_broadcast_secs": "How often to broadcast position, seconds",
    "setting_position_smart_enabled": "Broadcast position only on meaningful movement",
    "setting_gps_mode": "Built-in GPS receiver mode (ENABLED, DISABLED, NOT_PRESENT)",
    "setting_fixed_position": "Treat the current position as fixed (a stationary node)",
    "setting_telemetry_device_secs": "How often to send device telemetry, seconds",
    "setting_telemetry_env_enabled": "Send environmental telemetry (sensors), if present",
    "setting_mqtt_enabled": "Enable sending messages to MQTT (in addition to LoRa)",
    "setting_mqtt_address": "MQTT broker address, host[:port] (e.g. map.onemesh.ru)",
    "setting_mqtt_username": "Login for the MQTT broker",
    "setting_mqtt_password": "Password for the MQTT broker (shown masked in the list)",
    "setting_mqtt_encryption_enabled": "Encrypt MQTT packets with the same PSK as the channel",
    "setting_mqtt_json_enabled": "Also publish messages as JSON",
    "setting_mqtt_tls_enabled": "Use TLS to connect to the broker",
    "setting_mqtt_root": "MQTT root topic (e.g. msh/RU)",
    "setting_mqtt_uplink": (
        "Relay a channel from LoRa to MQTT: <channel>:on|off, e.g. Primary:on"
    ),
    "setting_mqtt_downlink": "Relay a channel from MQTT to LoRa: <channel>:on|off",

    "err_bool_expected": "expected a boolean value (on/off, yes/no, 1/0)",
    "err_int_expected": "expected an integer",
    "err_out_of_range": "out of range {lo}..{hi}",
    "err_enum_allowed": "allowed values: {values}",
    "err_unknown_setting_type": "unknown setting type: {kind}",
    "err_channel_link_format": "format: <channel>:on|off, e.g. Primary:on",
    "err_mqtt_channel_not_found": "channel '{name}' not found",

    "err_outbox_full": "device offline, outbox queue is full",
    "err_traceroute_timeout": "node didn't respond (timeout)",
    "err_unknown_param": "unknown parameter: {param}",
    "err_apply_failed": "failed to apply: {error}",
    "err_reboot_failed": "failed to reboot: {error}",
    "err_reconnect_wifi_only": (
        "changing the address is only supported for MESH_CONN_TYPE=wifi (current: {conn_type})"
    ),

    "hops_forms": ("hop", "hops"),

    "argparse_description": "Terminal chat client for Meshtastic (talks to mesh_logger.py)",
    "argparse_channel_metavar": "NAME",
    "argparse_channel_help": "show and send only in this channel",
    "argparse_history_help": "how many history messages to show (default {default})",

    "err_node_not_found": "Node '{query}' not found. Check /nodes",
    "warn_node_ambiguous": (
        "Name '{query}' is ambiguous, matches: {names}{more} — please narrow it down"
    ),

    "stats_completions": ("day", "node"),

    "err_no_logger_connection": "no connection to the logger",

    "delivered": "  ✓ Delivered{hop_str}{ref}",
    "not_delivered": "  ✗ Not delivered ({error}){ref}",
    "delivery_unconfirmed": "  ⏳ Delivery not confirmed{ref} (may have arrived, but no ACK received)",

    "device_disconnected": "⚠ Device disconnected — waiting for the logger to reconnect",
    "device_reconnected": "✓ Device back online: {ln} ({sn})",

    "logger_lost": "⚠ Connection to the logger lost — reconnecting...",
    "logger_restored": "✓ Connection to the logger restored",

    "err_unknown_sort": "Unknown sort mode '{mode}'. Available: {modes}",
    "err_no_node_data": "No node data available",
    "hdr_visible_nodes": "─── Visible nodes ───",
    "online_now": "Online",
    "ago_minutes": "{mins}m ago",
    "ago_hours": "{hrs}h ago",
    "unknown_time": "?",

    "err_no_self_info": "Info about your own node is unavailable",
    "who_me_label": "Me: ",
    "who_node_line": "👤 {ln} ({sn}) {id_str}",

    "queued": (
        "  ⏳ Queued: «{snippet}» — device "
        "offline, will send as soon as it reconnects"
    ),
    "err_send_error": "Send error: {error}",

    "usage_dm": "Usage: /dm &lt;name&gt; &lt;text&gt; (quote names containing spaces)",
    "err_nodes_fetch_failed": "Failed to fetch node list: {error}",

    "usage_trace": "Usage: /trace &lt;node name&gt;",
    "tracing": "Tracing to {name} — may take up to {timeout}s...",
    "err_traceroute_failed": "Traceroute failed: {error}",
    "route_there": "Route there:",
    "route_back": "Route back:",
    "no_return_route": "(no return route received)",

    "usage_ping": "Usage: /ping &lt;node name&gt;",
    "pinging": "🏓 Pinging {ln} ({sn})...",
    "err_no_packet_id": "Couldn't get a packet_id for the ping",
    "ping_no_reply": "🏓 {ln}: no response in {timeout}s",
    "ping_delivered": "🏓 {ln}: delivered in {elapsed}s{hop_str}",
    "ping_not_delivered": "🏓 {ln}: not delivered ({error})",
    "ping_unconfirmed": (
        "🏓 {ln}: delivery not confirmed after {elapsed}s "
        "(may have arrived, but no ACK received)"
    ),

    "unit_m": "m",
    "unit_km": "km",
    "err_no_position": "Node {name} has no position data (doesn't send GPS coordinates)",
    "pos_line": "📍 {ln} ({sn}): {lat}, {lon}{alt_str}",
    "pos_distance": "   from me: {dist_str}, bearing {brng}° ({compass})",

    "hdr_replyables": "─── Replyable messages (#1 = newest) ───",
    "reply_hint": "Reply: /reply #&lt;number&gt; &lt;text&gt; — or /reply &lt;text&gt; for #1",
    "react_hint": "Reaction: /react #&lt;number&gt; &lt;emoji&gt; — or /react &lt;emoji&gt; for #1",
    "err_no_such_reply": "No message #{num} — available: #1…#{max}",

    "err_nothing_to_quote": (
        "Nothing to quote yet: /reply only works for messages "
        "received since the client started"
    ),
    "usage_reply": (
        "Usage: /reply #&lt;number&gt; &lt;text&gt; "
        "(see numbers via /reply with no arguments)"
    ),
    "replying_to": "Replying to: [{time}] {name} — {snippet}",

    "err_nothing_to_react": (
        "Nothing to react to yet: /react only works for messages "
        "received since the client started"
    ),
    "usage_react": (
        "Usage: /react #&lt;number&gt; &lt;emoji&gt; "
        "(see numbers via /react with no arguments)"
    ),
    "reacting_to": "Reacting to: [{time}] {name} — {snippet}",

    "channel_confirmed": "Channel confirmed: {name}",
    "channel_not_found_device": (
        "Channel '{name}' not found on the device. "
        "Available: {available} — showing all channels"
    ),
    "channel_status": "Current: <b>{current}</b>. Channels: {channels}. Switch: /ch &lt;name&gt; or /ch all",
    "channel_all_word": "all",
    "channel_all_banner": "All channels (sending goes to Primary)",
    "err_channel_not_found": "Channel '{name}' not found. Available: {available}",
    "channel_banner": "Channel: {name}",
    "no_data": "no data",

    "usage_send": "Usage: /send &lt;channel&gt; &lt;text&gt;",

    "usage_search": "Usage: /search &lt;text&gt;",
    "hdr_search": "─── Search: «{query}»",
    "search_empty": "No results for «{query}»",
    "shown_last_n": " (showing last {limit})",

    "hdr_last_from": "─── Recent messages from «{query}»",
    "last_empty": "No messages found from «{query}»",

    "err_no_stats": "No data for statistics",
    "hdr_by_day": "─── Messages by day ───",
    "hdr_top_nodes": "─── Top active nodes ───",
    "hdr_stats": "─── Statistics ───",
    "total_messages": "  Total messages: <b>{total}</b> over {days} days",
    "busiest_hour": "  Busiest hour: {hour:02d}:00 ({count} messages)",
    "top_nodes_label": "  Top nodes:",
    "stats_more_hint": "More detail: /stats day · /stats node",

    "err_update_names_failed": "Failed to update names: {error}",
    "nothing_to_update": "Nothing to update — all visible nodes already have names",
    "update_done": "Done: resolved {resolved} of {targets} names",

    "empty_ignored_list": "Ignore list is empty",
    "hdr_ignored": "─── Ignored ───",
    "unignore_hint": "Un-ignore: /unignore &lt;name&gt;",
    "already_ignored": "«{name}» is already ignored",
    "ignored_msg": "🔇 «{name}» added to the device's ignore list — their messages will no longer arrive or get logged",
    "not_ignored": "«{name}» isn't in the ignore list — /unignore with no arguments shows the list",
    "unignored_msg": "🔊 «{name}» un-ignored",
    "err_ignore_failed": "failed to change ignore state: {error}",

    "err_settings_fetch_failed": "Failed to fetch settings: {error}",
    "hdr_settings": "─── Node settings ───",
    "settings_change_hint": "Change: /settings &lt;key&gt; &lt;value&gt;. Details — {doc}",
    "usage_settings": (
        "Usage: /settings &lt;key&gt; &lt;value&gt; "
        "(list parameters — /settings with no arguments)"
    ),
    "err_settings_apply_failed": "Failed to apply: {error}",
    "settings_applied": (
        "<ansigreen>✓ {param} = {value}</ansigreen> "
        "<ansigray>(some settings require /reboot to take effect)</ansigray>"
    ),
    "settings_doc": "SETTINGS.md",

    "usage_reboot": "Usage: /reboot — shows a warning, /reboot confirm — confirms the reboot",
    "err_no_pending_confirm": "No pending confirmation — run /reboot first",
    "err_reboot_failed_client": "Failed to reboot: {error}",
    "reboot_rebooting": (
        "🔄 Node is rebooting — the connection will drop for a moment, "
        "the logger will reconnect on its own"
    ),
    "reboot_warn": (
        "⚠ This will reboot the device (also useful after /settings — "
        "some settings only take effect after a restart). Confirm: "
        "<b>/reboot confirm</b> — window: {window}s"
    ),

    "err_reconnect_failed": "Failed to trigger reconnect: {error}",
    "reconnect_triggered": "🔌 Forcing a reconnect to the device",
    "reconnect_triggered_host": "🔌 Reconnecting to {host}...",
    "reconnect_attempt_failed": "✗ Reconnect attempt failed: {error} — the logger will keep retrying on its own",

    "help_lines": [
        "─── Commands ──────────────────────────────────────────────",
        "  /nodes [online|names|hops]  list visible nodes (sort mode, default online)",
        "  /who [name]          info about your own node, or another — names, hex id, battery/SNR/hops, position, recent messages",
        "  /dm &lt;name&gt; &lt;text&gt;   direct message (quote names containing spaces)",
        "  /reply               list recent messages you can reply to",
        "  /reply &lt;text&gt;       reply (with quote) to the latest received message",
        "  /reply #N &lt;text&gt;    reply to message #N from the /reply list",
        "  /react &lt;emoji&gt;      tapback reaction to the latest message",
        "  /react #N &lt;emoji&gt;   reaction to message #N from the /reply list",
        "  /ch [name|all]       show/switch channel",
        "  /send &lt;channel&gt; &lt;text&gt; one-off send to a channel without switching session",
        "  /search &lt;text&gt;      search history (respects the active channel)",
        "  /stats [day|node]    message history statistics (defaults to the Primary channel)",
        "  /trace &lt;name&gt;       packet route to a node (traceroute)",
        "  /ping &lt;name&gt;        delivery time (RTT) and hop count to a node",
        "  /ignore &lt;name&gt;      device-level ignore for a node (no argument — list)",
        "  /unignore [name]     remove ignore (no argument — list ignored nodes)",
        "  /updatenames         pull node names from OneMesh (also runs every 30 min)",
        "  /settings [key value]  local node settings (see SETTINGS.md)",
        "  /reboot              reboot the node (requires /reboot confirm)",
        "  /reconnect [host]    force a reconnect (optionally change IP/host for wifi)",
        "  /clear               clear the screen",
        "  /help                this help",
        "  Tab                  autocomplete commands, node names, and channels",
        "───────────────────────────────────────────────────────────",
    ],

    "unknown_command": "Unknown command /{cmd}. Type /help",

    "err_logger_not_running": (
        "Logger isn't running ({socket} not found). "
        "Start mesh_logger.py (or systemctl start mesh-logger)."
    ),
    "err_logger_connect_failed": "Failed to connect to the logger: {error}",
    "connected_banner": "Connected to the logger. Node: {ln} ({sn})",
    "device_not_connected_yet": "The logger is running, but the device isn't connected yet",
    "channel_pending_msg": (
        "Can't verify channel '{name}' yet — the device isn't connected. "
        "I'll accept it as given and confirm once it's back online "
        "(sending to Primary meanwhile)"
    ),
    "err_channel_not_found_startup": "Channel '{name}' not found. Available channels: {available}",
    "all_channels_banner": "Showing messages from all channels (sending goes to Primary)",
    "startup_hint": "Type a message or /help. Ctrl+C / Ctrl+D to quit.",
    "err_command_failed": "Command error: {error}",
    "disconnecting": "Disconnecting...",
}

_TABLE = _EN if LANG == "en" else _RU


def t(key: str, **kwargs) -> str:
    """Looks up `key` in the active-language table, falling back to Russian
    and then to the key itself — the TUI must never raise over a missing
    translation. `kwargs` are applied via str.format()."""
    template = _TABLE.get(key)
    if template is None:
        template = _RU.get(key, key)
    if kwargs:
        return template.format(**kwargs)
    return template


def t_list(key: str) -> list:
    """For list-valued keys (currently just help_lines) — returned as-is,
    no .format() applied."""
    value = _TABLE.get(key)
    if value is None:
        value = _RU.get(key, [key])
    return list(value)


def plural(key: str, n: int) -> str:
    """Renders "{n} {word}" using the plural-forms tuple stored at `key`.
    A 2-form tuple is treated as English (singular, plural); a 3-form tuple
    uses the full Russian rule (n%100 in 11-14 -> form 3; n%10==1 -> form 1;
    n%10 in 2-4 -> form 2; else -> form 3)."""
    forms = _TABLE.get(key) or _RU[key]
    if len(forms) == 2:
        word = forms[0] if n == 1 else forms[1]
    else:
        if n % 100 in (11, 12, 13, 14):
            word = forms[2]
        elif n % 10 == 1:
            word = forms[0]
        elif n % 10 in (2, 3, 4):
            word = forms[1]
        else:
            word = forms[2]
    return f"{n} {word}"
