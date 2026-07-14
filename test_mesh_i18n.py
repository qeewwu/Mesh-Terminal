#!/usr/bin/env python3
"""Tests for mesh_i18n.py — the interface-language string tables. Pure
stdlib; doesn't touch the network or the meshtastic library.

Run: python3 -m unittest test_mesh_i18n -v
"""

import string
import unittest

import mesh_i18n as i18n

_FORMATTER = string.Formatter()

# The 22 keys in mesh_logger.py's SETTINGS_REGISTRY (see CLAUDE.md "Local node
# settings") — hardcoded here rather than importing mesh_logger, which would
# pull in the meshtastic library just to run a string-table test.
SETTING_KEYS = [
    "owner_long", "owner_short", "role", "node_info_broadcast_secs",
    "hop_limit", "tx_power", "position_broadcast_secs", "position_smart_enabled",
    "gps_mode", "fixed_position", "telemetry_device_secs", "telemetry_env_enabled",
    "mqtt_enabled", "mqtt_address", "mqtt_username", "mqtt_password",
    "mqtt_encryption_enabled", "mqtt_json_enabled", "mqtt_tls_enabled", "mqtt_root",
    "mqtt_uplink", "mqtt_downlink",
]


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in _FORMATTER.parse(template) if name}


class TestKeyParity(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        self.assertEqual(set(i18n._RU), set(i18n._EN))

    def test_setting_keys_all_present(self):
        table_keys = set(i18n._RU)
        for key in SETTING_KEYS:
            self.assertIn(f"setting_{key}", table_keys)


class TestPlaceholderParity(unittest.TestCase):
    def test_placeholders_match_between_languages(self):
        mismatches = []
        for key, ru_val in i18n._RU.items():
            en_val = i18n._EN[key]
            if isinstance(ru_val, str):
                ru_fields = _fields(ru_val)
                en_fields = _fields(en_val)
                if ru_fields != en_fields:
                    mismatches.append((key, ru_fields, en_fields))
        self.assertEqual(mismatches, [], f"placeholder mismatches: {mismatches}")

    def test_no_positional_placeholders(self):
        for key, val in i18n._RU.items():
            if not isinstance(val, str):
                continue
            for _, name, _, _ in _FORMATTER.parse(val):
                if name == "":
                    self.fail(f"key {key!r} uses a positional {{}} placeholder")

    def test_list_and_tuple_lengths_match(self):
        for key, ru_val in i18n._RU.items():
            en_val = i18n._EN[key]
            if key == "hops_forms":
                continue  # ru has 3 grammatical forms, en has 2 — by design
            if isinstance(ru_val, (list, tuple)):
                self.assertEqual(
                    len(ru_val), len(en_val),
                    f"key {key!r}: ru has {len(ru_val)} items, en has {len(en_val)}",
                )
                self.assertIs(type(ru_val), type(en_val), f"key {key!r} type mismatch")

    def test_hops_forms_shape(self):
        self.assertEqual(len(i18n._RU["hops_forms"]), 3)
        self.assertEqual(len(i18n._EN["hops_forms"]), 2)


class TestBalancedTags(unittest.TestCase):
    """Cheap smoke check for truncated translations: every <ansi*>/<b> opening
    tag in a template must have a matching close, in both languages."""

    def test_tags_balanced(self):
        for table_name, table in (("_RU", i18n._RU), ("_EN", i18n._EN)):
            for key, val in table.items():
                strings = val if isinstance(val, list) else [val] if isinstance(val, str) else []
                for s in strings:
                    self.assertEqual(
                        s.count("<b>"), s.count("</b>"),
                        f"{table_name}[{key!r}]: unbalanced <b> tags: {s!r}",
                    )
                    self.assertEqual(
                        s.count("<ansi"), s.count("</ansi"),
                        f"{table_name}[{key!r}]: unbalanced <ansi*> tags: {s!r}",
                    )


class TestPluralRussian(unittest.TestCase):
    def setUp(self):
        self._orig_table = i18n._TABLE
        i18n._TABLE = i18n._RU

    def tearDown(self):
        i18n._TABLE = self._orig_table

    def test_one(self):
        self.assertEqual(i18n.plural("hops_forms", 1), "1 хоп")
        self.assertEqual(i18n.plural("hops_forms", 21), "21 хоп")

    def test_few(self):
        self.assertEqual(i18n.plural("hops_forms", 2), "2 хопа")
        self.assertEqual(i18n.plural("hops_forms", 3), "3 хопа")
        self.assertEqual(i18n.plural("hops_forms", 4), "4 хопа")
        self.assertEqual(i18n.plural("hops_forms", 22), "22 хопа")

    def test_many(self):
        self.assertEqual(i18n.plural("hops_forms", 0), "0 хопов")
        self.assertEqual(i18n.plural("hops_forms", 5), "5 хопов")
        self.assertEqual(i18n.plural("hops_forms", 11), "11 хопов")
        self.assertEqual(i18n.plural("hops_forms", 12), "12 хопов")
        self.assertEqual(i18n.plural("hops_forms", 14), "14 хопов")
        self.assertEqual(i18n.plural("hops_forms", 111), "111 хопов")


class TestPluralEnglish(unittest.TestCase):
    def setUp(self):
        self._orig_table = i18n._TABLE
        i18n._TABLE = i18n._EN

    def tearDown(self):
        i18n._TABLE = self._orig_table

    def test_singular(self):
        self.assertEqual(i18n.plural("hops_forms", 1), "1 hop")

    def test_plural(self):
        self.assertEqual(i18n.plural("hops_forms", 0), "0 hops")
        self.assertEqual(i18n.plural("hops_forms", 2), "2 hops")
        self.assertEqual(i18n.plural("hops_forms", 5), "5 hops")
        self.assertEqual(i18n.plural("hops_forms", 21), "21 hops")


class TestT(unittest.TestCase):
    def setUp(self):
        self._orig_table = i18n._TABLE
        i18n._TABLE = i18n._RU

    def tearDown(self):
        i18n._TABLE = self._orig_table

    def test_unknown_key_returns_key_itself(self):
        self.assertEqual(i18n.t("this_key_does_not_exist"), "this_key_does_not_exist")

    def test_known_key_formats_kwargs(self):
        self.assertEqual(i18n.t("err_send_failed", error="boom"), "отправка не удалась: boom")

    def test_key_missing_from_active_language_falls_back_to_ru(self):
        i18n._RU["_test_temp_key"] = "проверка {x}"
        try:
            i18n._TABLE = i18n._EN
            self.assertEqual(i18n.t("_test_temp_key", x=1), "проверка 1")
        finally:
            del i18n._RU["_test_temp_key"]

    def test_t_list_returns_list_copy(self):
        result = i18n.t_list("help_lines")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_t_list_unknown_key(self):
        self.assertEqual(i18n.t_list("nonexistent_list_key"), ["nonexistent_list_key"])


if __name__ == "__main__":
    unittest.main()
