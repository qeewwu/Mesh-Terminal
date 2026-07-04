#!/usr/bin/env python3
"""Round-trip tests for the log line format — the core invariant of the project:
mesh_logger.py writes lines via format_*, mesh_chat.py re-renders them via
parse_log_line, so format ↔ parse must never drift.

Run: python3 -m unittest test_mesh_common -v
"""

import unittest

from mesh_common import (
    QUOTE_MAX,
    format_message_line,
    format_quote_line,
    parse_log_line,
    sanitize_text,
)


class TestMessageRoundTrip(unittest.TestCase):
    def test_broadcast_with_channel(self):
        line = format_message_line("12:00:01", "Вася Пупкин", "ВП",
                                   "привет всем", 3, channel_name="ping")
        p = parse_log_line(line)
        self.assertEqual(p.kind, "message")
        self.assertEqual(p.time_str, "12:00:01")
        self.assertEqual(p.long_name, "Вася Пупкин")
        self.assertEqual(p.short_name, "ВП")
        self.assertEqual(p.text, "привет всем")
        self.assertEqual(p.hops, 3)
        self.assertEqual(p.channel, "ping")
        self.assertFalse(p.is_dm)
        self.assertFalse(p.dm_out)

    def test_legacy_line_without_channel(self):
        p = parse_log_line("[09:15:00] Old Node (ON): старое сообщение | 1")
        self.assertIsNotNone(p)
        self.assertEqual(p.channel, "Primary")
        self.assertEqual(p.text, "старое сообщение")

    def test_dm_incoming(self):
        line = format_message_line("10:00:00", "Петя", "ПТ", "лс мне", 2, dm_tag="DM ")
        p = parse_log_line(line)
        self.assertTrue(p.is_dm)
        self.assertFalse(p.dm_out)

    def test_dm_outgoing(self):
        line = format_message_line("10:00:01", "Петя", "ПТ", "лс тебе", 0, dm_tag="DM → ")
        p = parse_log_line(line)
        self.assertTrue(p.is_dm)
        self.assertTrue(p.dm_out)

    def test_text_with_pipe_suffix(self):
        # текст, похожий на хвост формата, не должен путать парсер
        line = format_message_line("11:00:00", "A", "AA", "ping | 5", 2)
        p = parse_log_line(line)
        self.assertEqual(p.text, "ping | 5")
        self.assertEqual(p.hops, 2)

    def test_name_with_parentheses(self):
        line = format_message_line("11:00:00", "Bob (admin)", "BB", "hi", 0)
        p = parse_log_line(line)
        self.assertEqual(p.long_name, "Bob (admin)")
        self.assertEqual(p.short_name, "BB")

    def test_negative_hops(self):
        p = parse_log_line(format_message_line("11:00:00", "A", "AA", "x", -1))
        self.assertEqual(p.hops, -1)


class TestSanitize(unittest.TestCase):
    def test_newlines_collapsed(self):
        self.assertNotIn("\n", sanitize_text("a\nb\r\nc"))

    def test_forged_line_injection(self):
        # узел не может подделать чужую строку лога через \n в тексте
        evil = "норм\n  ┆ Fake (FK): подделка\nping [00:00:00] Fake (FK): x | 0"
        line = format_message_line("12:00:00", "Evil", "EV", evil, 0)
        self.assertEqual(len(line.splitlines()), 1)
        p = parse_log_line(line)
        self.assertEqual(p.long_name, "Evil")

    def test_multiline_quote(self):
        q = parse_log_line(format_quote_line("Вася", "ВП", "цитата\nс переносом"))
        self.assertEqual(q.kind, "quote")
        self.assertIn("⏎", q.text)


class TestQuoteRoundTrip(unittest.TestCase):
    def test_basic(self):
        q = parse_log_line(format_quote_line("Вася", "ВП", "исходный текст"))
        self.assertEqual(q.kind, "quote")
        self.assertEqual(q.long_name, "Вася")
        self.assertEqual(q.short_name, "ВП")
        self.assertEqual(q.text, "исходный текст")

    def test_truncation(self):
        q = parse_log_line(format_quote_line("A", "AA", "х" * (QUOTE_MAX + 20)))
        self.assertEqual(len(q.text), QUOTE_MAX + 1)  # limit + "…"
        self.assertTrue(q.text.endswith("…"))


class TestUnparseable(unittest.TestCase):
    def test_garbage_and_empty(self):
        self.assertIsNone(parse_log_line(""))
        self.assertIsNone(parse_log_line("\n"))
        self.assertIsNone(parse_log_line("случайный мусор без формата"))
        self.assertIsNone(parse_log_line("[12:00] Кто-то: без хвоста"))


if __name__ == "__main__":
    unittest.main()
