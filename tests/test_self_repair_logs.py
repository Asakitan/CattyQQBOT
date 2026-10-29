from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catty_self_repair.config import RepairConfig
from catty_self_repair.logs import LogMonitor, LogStateError, redact


def line(message, level="ERROR", logger="catty_qq_ai.harness", number=12):
    return f"2026-10-29 10:00:00.123 | {level:<8} | {logger}:run:{number} - {message}\n"


class LogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "logs/bot_live.log"
        self.path.parent.mkdir()
        self.config = replace(RepairConfig(), log_paths=("logs/bot_live.log",), debounce_seconds=0, cooldown_seconds=100)

    def monitor(self, **changes):
        return LogMonitor(self.root, replace(self.config, **changes), ("fixture-secret-abcdefgh",))

    def append(self, text):
        with self.path.open("ab") as stream:
            stream.write(text.encode("utf-8") if isinstance(text, str) else text)

    def test_missing_source_is_idle(self):
        monitor = self.monitor()
        self.assertEqual(monitor.poll(now=1000), [])
        self.assertEqual(monitor.diagnostics[0]["code"], "source_missing")

    def test_actual_level_headers_only(self):
        self.append(line("contains WARNING and ERROR", "INFO") + "WARNING fake text\n")
        self.assertEqual(self.monitor().poll(now=1000), [])

    def test_warning_error_critical_all_trigger(self):
        self.append("".join(line(level, level) for level in ("WARNING", "ERROR", "CRITICAL")))
        items = self.monitor().poll(now=1000)
        self.assertEqual({item["level"] for item in items}, {"WARNING", "ERROR", "CRITICAL"})

    def test_traceback_is_grouped_but_diagnostic_values_are_removed(self):
        self.append(line("bad HTTP 503", "WARNING") + 'Traceback (most recent call last):\n  File "fixture.py", line 1\n    execute()\n    └ fixture-secret-abcdefgh\n      continuation-private-value\nValueError: bad input\n')
        result = self.monitor().poll(now=1000)[0]
        self.assertIn("Traceback", result["text"])
        self.assertIn("ValueError", result["text"])
        self.assertIn("503", result["text"])
        self.assertNotIn("fixture-secret", result["text"])
        self.assertNotIn("continuation-private", result["text"])

    def test_secret_scrubbing_is_idempotent(self):
        original = 'api_key="fixture-secret-abcdefgh"\nAuthorization: Bearer unknown-secret-value\nCookie: session=abc; credential=def\nhttps://name:password@example.test/a?signature=secret#private\nsk-12345678901234567890'
        cleaned = redact(original, ("fixture-secret-abcdefgh",))
        for secret in ("fixture-secret", "unknown-secret", "session=abc", "name:password", "signature=secret", "#private", "sk-123"):
            self.assertNotIn(secret, cleaned)
        self.assertEqual(redact(cleaned, ("fixture-secret-abcdefgh",)), cleaned)

    def test_empty_values_and_annotations_remain_usable_source(self):
        text = 'api_key: str = ""\ntoken = ""\n'
        self.assertEqual(redact(text), text)

    def test_debounce_emits_last_record_without_needing_next_header(self):
        self.append(line("one event"))
        monitor = self.monitor(debounce_seconds=10)
        self.assertEqual(monitor.poll(now=1000), [])
        self.assertEqual(monitor.poll(now=1009), [])
        self.assertEqual(len(monitor.poll(now=1010)), 1)
        self.assertEqual(monitor.poll(now=1020), [])

    def test_cooldown_and_cursor_survive_restart(self):
        self.append(line("same error"))
        self.assertEqual(len(self.monitor().poll(now=1000)), 1)
        self.append(line("same error", number=50))
        monitor = self.monitor()
        self.assertEqual(monitor.poll(now=1050), [])
        self.assertEqual(len(monitor.poll(now=1100)), 1)

    def test_clock_rollback_does_not_reset_cooldown(self):
        self.append(line("same"))
        self.assertEqual(len(self.monitor().poll(now=1000)), 1)
        self.append(line("same"))
        self.assertEqual(self.monitor().poll(now=900), [])

    def test_repeated_storm_is_one_counted_incident(self):
        self.append(line("repeated") * 100)
        result = self.monitor(max_incidents_per_run=1).poll(now=1000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["occurrences"], 100)

    def test_batch_limit_keeps_unselected_events(self):
        self.append("".join(line(f"event-{number}") for number in range(5)))
        monitor = self.monitor(max_incidents_per_run=2)
        emitted = monitor.poll(now=1000) + monitor.poll(now=1001) + monitor.poll(now=1002)
        self.assertEqual(len(emitted), 5)
        self.assertEqual(len({item["id"] for item in emitted}), 5)

    def test_rotation_and_truncation_are_detected(self):
        self.append(line("old"))
        monitor = self.monitor()
        monitor.poll(now=1000)
        self.path.rename(self.path.with_suffix(".old"))
        self.append(line("rotated"))
        self.assertIn("rotated", monitor.poll(now=1001)[0]["text"])
        self.path.write_text(line("x"), encoding="utf-8")
        self.assertIn(" - x", monitor.poll(now=1002)[0]["text"])

    def test_copy_truncate_and_regrow_past_offset_uses_anchor(self):
        self.append(line("old"))
        monitor = self.monitor()
        monitor.poll(now=1000)
        self.path.write_text(line("new-larger-message" * 10), encoding="utf-8")
        self.assertIn("new-larger-message", monitor.poll(now=1001)[0]["text"])

    def test_initial_zero_tail_ignores_existing_backlog(self):
        self.append(line("old"))
        monitor = self.monitor(initial_tail_bytes=0)
        self.assertEqual(monitor.poll(now=1000), [])
        self.append(line("new"))
        self.assertIn(" - new", monitor.poll(now=1001)[0]["text"])

    def test_utf8_partial_line_is_retried_without_persisting_raw_tail(self):
        raw = line("中文 fixture-secret-abcdefgh").encode("utf-8")
        split = raw.index("文".encode("utf-8")) + 1
        self.append(raw[:split])
        monitor = self.monitor()
        self.assertEqual(monitor.poll(now=1000), [])
        self.assertEqual(monitor.poll(now=1001), [])
        self.append(raw[split:])
        result = self.monitor().poll(now=1002)
        self.assertEqual(len(result), 1)
        self.assertIn("中文", result[0]["text"])
        self.assertNotIn("fixture-secret", monitor.path.read_text(encoding="utf-8"))

    def test_quiet_non_newline_record_and_split_secret_are_safe(self):
        self.append(line("issue fixture-secret-abc").rstrip("\n"))
        monitor = self.monitor()
        self.assertEqual(monitor.poll(now=1000), [])
        result = monitor.poll(now=1001)
        self.assertEqual(len(result), 1)
        self.assertNotIn("fixture-secret", result[0]["text"])
        self.append("defgh\n")
        self.assertEqual(monitor.poll(now=1002), [])
        self.assertNotIn("defgh", monitor.path.read_text(encoding="utf-8"))

    def test_oversize_line_cannot_pin_cursor_forever(self):
        self.append(line("x" * 5000) + line("later-readable-error"))
        monitor = self.monitor(max_log_bytes=1024)
        found = []
        offsets = []
        for index in range(12):
            found.extend(monitor.poll(now=1000 + index))
            self.assertLessEqual(monitor.bytes_read, 1024)
            offsets.append(json.loads(monitor.path.read_text(encoding="utf-8"))["files"]["logs/bot_live.log"]["offset"])
        self.assertGreater(offsets[-1], offsets[0])
        self.assertTrue(any("later-readable-error" in item["text"] for item in found), found)

    def test_evidence_length_is_bounded(self):
        self.append(line("a" * 1000) + "trace\n" * 1000)
        result = self.monitor(max_incident_chars=256).poll(now=1000)[0]
        self.assertLessEqual(len(result["text"]), 256)

    def test_own_repair_feedback_is_not_repaired(self):
        self.append(line("own failure", logger="catty_self_repair.agent") + line("real failure", logger="catty_hot_reload"))
        result = self.monitor().poll(now=1000)
        self.assertEqual(len(result), 1)
        self.assertIn("real failure", result[0]["text"])

    def test_error_codes_are_not_over_normalized(self):
        self.append(line("HTTP 400") + line("HTTP 500"))
        self.assertEqual(len(self.monitor().poll(now=1000)), 2)

    def test_corrupt_state_pauses_instead_of_replaying(self):
        monitor = self.monitor()
        monitor.poll(now=1000)
        monitor.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(LogStateError):
            self.monitor()
        self.assertEqual(monitor.path.read_text(encoding="utf-8"), "{broken")

    def test_failed_checkpoint_does_not_hand_off_incidents(self):
        self.append(line("failure"))
        monitor = self.monitor()
        with patch("catty_self_repair.logs.atomic_json", side_effect=OSError("fixture disk error")):
            with self.assertRaises(LogStateError):
                monitor.poll(now=1000)
        self.assertEqual(len(monitor.poll(now=1001)), 1)

    def test_linked_log_is_rejected(self):
        target = self.root / "outside.txt"
        target.write_text(line("must not be read"), encoding="utf-8")
        try:
            os.link(target, self.path)
        except OSError as exc:
            self.skipTest(str(exc))
        monitor = self.monitor()
        self.assertEqual(monitor.poll(now=1000), [])
        self.assertIn("source_unavailable", {item["code"] for item in monitor.diagnostics})


if __name__ == "__main__":
    unittest.main()