from __future__ import annotations

import ast
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_reply_delivery_state import _module as delivery

_ENTRY = Path(__file__).resolve().parents[1] / "src/catty_qq_ai/__init__.py"


def load_delivery_functions(sender):
    tree = ast.parse(_ENTRY.read_text(encoding="utf-8"))
    names = {"_send_reply_message", "_commit_sent_reply_history"}
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(functions) == 2
    namespace = {
        "Message": object,
        "_delivery_state": delivery.ReplyDeliveryState(),
        "matcher": types.SimpleNamespace(send=sender),
        "state": {},
        "event": object(),
        "fresh_mention": True,
        "logger": Mock(),
        "_remember_bot_reply_for_event": Mock(),
        "_mark_consumed_reply_source_if_sent": Mock(),
        "REPLY_SPLIT_MARKER": "<<<CATTY_REPLY_SPLIT>>>",
        "history_key": "group:fixture",
        "_enriched_user_content_for_history": "fixture user",
        "_append_history": Mock(),
        "special_care_context": "fixture",
        "memory_store": Mock(),
        "_save_assistant_training_sample": Mock(),
        "incoming": object(),
        "messages": [],
        "emoji_query": "",
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(_ENTRY), "exec"), namespace)
    return namespace


class ReplyDeliveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_delivery_commits_only_confirmed_text(self):
        async def sender(message):
            if message == "second":
                raise RuntimeError("unconfirmed")

        ns = load_delivery_functions(sender)
        await ns["_send_reply_message"]("first", text="first")
        with self.assertRaises(RuntimeError):
            await ns["_send_reply_message"]("second", text="second")
        await ns["_send_reply_message"]("third", text="third")
        ns["_append_history"].assert_not_called()
        ns["_commit_sent_reply_history"]()
        ns["_append_history"].assert_called_once_with(
            "group:fixture", "fixture user", "first<<<CATTY_REPLY_SPLIT>>>third",
        )
        self.assertEqual(ns["_remember_bot_reply_for_event"].call_count, 2)
        self.assertEqual(ns["_mark_consumed_reply_source_if_sent"].call_count, 2)
        ns["memory_store"].record_special_care_reply_sent.assert_called_once_with(ns["event"], "first\nthird")

    async def test_first_send_failure_has_no_history_or_affection_update(self):
        async def sender(message):
            raise RuntimeError("unconfirmed")

        ns = load_delivery_functions(sender)
        with self.assertRaises(RuntimeError):
            await ns["_send_reply_message"]("first", text="first")
        ns["_commit_sent_reply_history"]()
        ns["_append_history"].assert_not_called()
        ns["_remember_bot_reply_for_event"].assert_not_called()
        ns["_save_assistant_training_sample"].assert_not_called()

    async def test_side_effect_failure_does_not_resend_acknowledged_message(self):
        sent = []

        async def sender(message):
            sent.append(message)

        ns = load_delivery_functions(sender)
        ns["_remember_bot_reply_for_event"].side_effect = RuntimeError("store failure")
        await ns["_send_reply_message"]("first", text="first")
        ns["_commit_sent_reply_history"]()
        self.assertEqual(sent, ["first"])
        ns["_append_history"].assert_called_once_with("group:fixture", "fixture user", "first")


if __name__ == "__main__":
    unittest.main()