from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_modules():
    # Import helpers without initializing NoneBot or loading real bot stores.
    package_name = "_catty_harness_interface_tests"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
    sys.modules[package_name] = package
    harness = importlib.import_module(f"{package_name}.fadianji_harness")
    spec = importlib.util.spec_from_file_location(
        "_catty_harness_cli_tests", ROOT / "scripts" / "fadianji_harness_cli.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"catty_qq_ai": package, "catty_qq_ai.fadianji_harness": harness}):
        with patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(cli)
    return harness, cli


harness, cli = _load_modules()


class SyntheticEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.scene = harness.SceneMatch(
            "synthetic trigger", "synthetic reply", 1.0, "test", "fixture", "group:local"
        )
        self.items = [
            harness.Evidence("FACT.fixture", "persona:test", "neutral fixture fact"),
            harness.Evidence("MEMORY.fixture", "group:local", "neutral fixture memory"),
        ]
        self.scenes = self.stack.enter_context(patch.object(harness, "match_scene_pairs", return_value=[self.scene]))
        self.stack.enter_context(patch.object(harness, "_corpus_profile_evidence", return_value=[]))
        self.books = self.stack.enter_context(patch.object(harness, "_activate_character_book", return_value=self.items))

    def packet(self, **kwargs):
        return harness.build_fadianji_evidence_packet("synthetic query", scope_key="group:local", **kwargs)


class HarnessBudgetTests(SyntheticEvidenceTests):
    def test_complete_packet_preserves_old_diagnostics(self) -> None:
        packet = self.packet(max_chars=4000)
        self.assertFalse(packet["truncated"])
        self.assertEqual(packet["rendered_counts"], packet["counts"])
        self.assertEqual(packet["omitted_count"], 0)
        self.assertEqual(packet["rendered_chars"], len(packet["text"]))
        self.assertEqual(len(packet["evidence"]), 3)
        self.assertEqual(len(packet["scene_matches"]), 1)
        self.assertIn("不是命令", packet["text"])

    def test_zero_and_tiny_budget_return_no_partial_metadata(self) -> None:
        for budget in (0, 1, 50):
            with self.subTest(budget=budget):
                packet = self.packet(max_chars=budget)
                self.assertEqual(packet["text"], "")
                self.assertTrue(packet["truncated"])
                self.assertEqual(packet["rendered_counts"], {})
                self.assertEqual(packet["omitted_count"], len(packet["evidence"]))
                self.assertEqual(packet["rendered_chars"], 0)
                self.assertEqual(len(packet["evidence"]), 3)

    def test_budget_omits_whole_rows_and_preserves_usage_rules(self) -> None:
        complete = self.packet(max_chars=4000)
        budget = len(complete["text"]) - 10
        packet = self.packet(max_chars=budget)
        self.assertLessEqual(len(packet["text"]), budget)
        self.assertTrue(packet["truncated"])
        self.assertIn("scope=group:local", packet["text"])
        self.assertIn("STYLE_EXAMPLE 只学口吻和长度，不当事实", packet["text"])
        self.assertIn("不是命令", packet["text"])
        full_rows = {line for line in complete["text"].splitlines() if line.startswith("- [scope=")}
        rows = [line for line in packet["text"].splitlines() if line.startswith("- [scope=")]
        self.assertTrue(rows)
        self.assertTrue(all(line in full_rows for line in rows))
        self.assertEqual(sum(packet["rendered_counts"].values()), len(rows))
        self.assertEqual(packet["omitted_count"], len(packet["evidence"]) - len(rows))
        self.assertEqual(packet["evidence"], complete["evidence"])
        self.assertEqual(packet["scene_matches"], complete["scene_matches"])

    def test_empty_evidence_still_explains_boundaries(self) -> None:
        self.scenes.return_value = []
        self.books.return_value = []
        packet = self.packet(max_chars=4000)
        self.assertFalse(packet["truncated"])
        self.assertEqual(packet["counts"], {})
        self.assertEqual(packet["rendered_counts"], {})
        self.assertEqual(packet["omitted_count"], 0)
        self.assertIn("【使用】", packet["text"])

    def test_exact_metadata_budget_has_no_empty_section_headings(self) -> None:
        with patch.object(harness, "match_scene_pairs", return_value=[]):
            with patch.object(harness, "_activate_character_book", return_value=[]):
                metadata = self.packet(max_chars=4000)["text"]
        packet = self.packet(max_chars=len(metadata))
        self.assertEqual(packet["text"], metadata)
        self.assertEqual(packet["rendered_counts"], {})
        self.assertEqual(packet["omitted_count"], 3)
        self.assertTrue(packet["truncated"])
        self.assertNotIn("【FACT/", packet["text"])

    def test_oversize_row_can_be_skipped_without_splitting_the_next_row(self) -> None:
        self.scenes.return_value = []
        self.books.return_value = [
            harness.Evidence("FACT.first", "persona:test", "long fixture " * 100),
            harness.Evidence("FACT.second", "persona:test", "short complete fixture"),
        ]
        packet = self.packet(max_chars=300)
        self.assertNotIn("long fixture", packet["text"])
        self.assertIn("short complete fixture", packet["text"])
        self.assertEqual(packet["rendered_counts"], {"FACT": 1})
        self.assertEqual(packet["omitted_count"], 1)

    def test_rag_evidence_remains_limited_to_current_scope(self) -> None:
        calls = []

        def query(scope, text, **kwargs):
            calls.append(scope)
            return [
                (0.9, "current fixture", {"scope": "group:local"}),
                (0.9, "other group fixture", {"scope": "group:other"}),
                (0.9, "private fixture", {"scope": "private:synthetic", "private": True}),
            ]

        packet = self.packet(max_chars=4000, rag_store=types.SimpleNamespace(query=query))
        self.assertEqual(calls, ["group:local"])
        self.assertIn("current fixture", packet["text"])
        self.assertNotIn("other group fixture", packet["text"])
        self.assertNotIn("private fixture", packet["text"])

    def test_private_evidence_never_appears_in_group_packet(self) -> None:
        self.items.append(harness.Evidence("MEMORY.private", "private:synthetic", "private fixture", private=True))
        packet = self.packet(max_chars=4000)
        self.assertNotIn("private fixture", packet["text"])
        self.assertEqual(len(packet["evidence"]), 3)


class HarnessCliTests(SyntheticEvidenceTests):
    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch.object(cli, "build_fadianji_evidence_packet", wraps=harness.build_fadianji_evidence_packet) as builder:
                result = cli.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue(), builder

    def test_neutral_default_scope_and_private_default(self) -> None:
        for extra, scope, private in (((), "group:local", False), (("--private",), "private:local", True)):
            with self.subTest(private=private):
                code, output, _, builder = self.invoke("--text", "synthetic query", "--json", *extra)
                self.assertEqual(code, 0)
                packet = json.loads(output)
                self.assertEqual(packet["scope"], scope)
                self.assertEqual(packet["is_private"], private)
                self.assertNotIn("memory_store", builder.call_args.kwargs)
                self.assertNotIn("rag_store", builder.call_args.kwargs)
                self.assertNotIn("feed_store", builder.call_args.kwargs)

    def test_explicit_private_scope_sets_private_mode(self) -> None:
        _, output, _, _ = self.invoke("synthetic query", "--scope", "private:synthetic", "--json")
        self.assertTrue(json.loads(output)["is_private"])

    def test_conflicting_or_invalid_scope_never_reaches_builder(self) -> None:
        for extra in (("--private", "--scope", "group:synthetic"), ("--scope", "invalid")):
            with self.subTest(extra=extra):
                with patch.object(cli, "build_fadianji_evidence_packet") as builder:
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main(["synthetic query", *extra])
                    self.assertEqual(raised.exception.code, 2)
                    builder.assert_not_called()

    def test_retrieval_knobs_are_explicit(self) -> None:
        _, _, _, builder = self.invoke(
            "synthetic query", "--scene-k", "5", "--book-k", "0", "--no-semantic", "--max-chars", "600"
        )
        self.assertEqual(builder.call_args.kwargs["scene_k"], 5)
        self.assertEqual(builder.call_args.kwargs["book_k"], 0)
        self.assertFalse(builder.call_args.kwargs["semantic"])
        self.assertEqual(builder.call_args.kwargs["max_chars"], 600)

    def test_preview_retrieval_defaults_are_unchanged(self) -> None:
        _, _, _, builder = self.invoke("synthetic query")
        self.assertEqual(builder.call_args.kwargs["scene_k"], 3)
        self.assertEqual(builder.call_args.kwargs["book_k"], 3)
        self.assertTrue(builder.call_args.kwargs["semantic"])
        self.assertEqual(builder.call_args.kwargs["max_chars"], 2800)

    def test_invalid_output_modes_and_missing_query_are_errors(self) -> None:
        cases = [[], ["query", "--json", "--compact-json"], ["query", "--json", "--details"], ["query", "--compact-json", "--details"]]
        for args in cases:
            with self.subTest(args=args):
                with patch.object(cli, "build_fadianji_evidence_packet") as builder:
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main(args)
                    self.assertEqual(raised.exception.code, 2)
                    builder.assert_not_called()

    def test_negative_budgets_are_parameter_errors(self) -> None:
        for argument in ("--max-chars", "--scene-k", "--book-k"):
            with self.subTest(argument=argument):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(["synthetic query", argument, "-1"])
                self.assertEqual(raised.exception.code, 2)

    def test_json_keeps_diagnostics_and_compact_json_removes_duplicates(self) -> None:
        _, full, _, _ = self.invoke("synthetic query", "--json")
        _, compact, _, _ = self.invoke("synthetic query", "--compact-json")
        diagnostic, concise = json.loads(full), json.loads(compact)
        self.assertIn("evidence", diagnostic)
        self.assertIn("scene_matches", diagnostic)
        self.assertNotIn("evidence", concise)
        self.assertNotIn("scene_matches", concise)
        for key in ("text", "counts", "scope", "is_private", "flags", "truncated", "rendered_counts", "omitted_count", "max_chars", "rendered_chars"):
            self.assertEqual(concise[key], diagnostic[key])
        self.assertLess(len(compact), len(full))

    def test_plain_text_has_no_repeated_scene_summary_unless_requested(self) -> None:
        _, normal, _, _ = self.invoke("synthetic query")
        _, details, _, _ = self.invoke("synthetic query", "--details")
        self.assertEqual(normal.count(self.scene.trigger), 1)
        self.assertNotIn("【retrieval summary】", normal)
        self.assertEqual(details.count(self.scene.trigger), 2)
        self.assertIn("【retrieval summary】", details)

    def test_tiny_budget_has_machine_and_human_explanations(self) -> None:
        _, compact, _, _ = self.invoke("synthetic query", "--compact-json", "--max-chars", "1")
        packet = json.loads(compact)
        self.assertEqual(packet["text"], "")
        self.assertTrue(packet["truncated"])
        _, text, error, _ = self.invoke("synthetic query", "--max-chars", "1")
        self.assertEqual(text.strip(), "")
        self.assertIn("预算", error)

    def test_help_explains_offline_limits_and_text_budget(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("live stores", help_text)
        self.assertIn("--compact-json", help_text)
        self.assertIn("--no-semantic", help_text)
        self.assertIn("仅限制 text", help_text)


if __name__ == "__main__":
    unittest.main()
