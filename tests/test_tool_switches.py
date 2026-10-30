from __future__ import annotations

import copy
import importlib
from pathlib import Path
import sys
import types
import unittest


PACKAGE = "tool_switch_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/catty_qq_ai")]
sys.modules[PACKAGE] = package
switches = importlib.import_module(f"{PACKAGE}.tool_switches")


def schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class ToolSwitchTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [schema(name) for name in ("catty_scan_messages", "catty_look_image", "catty_web_search", "catty_run_code")]

    def test_catalog_is_complete_and_unchanged_when_tools_are_off(self):
        original = copy.deepcopy(self.catalog)
        state = switches.ToolSwitches("research", permitted={"catty_scan_messages", "catty_look_image", "catty_web_search"}, enabled={"catty_web_search"})
        result = state.refresh(self.catalog)
        self.assertEqual(result[:-1], original)
        self.assertEqual(self.catalog, original)
        self.assertEqual(result[-1]["function"]["name"], switches.SWITCH_TOOL_NAME)
        self.assertEqual(state.denial("catty_look_image")["error"], "tool_switched_off")
        self.assertEqual(state.denial("catty_run_code")["error"], "tool_not_allowed")

    def test_role_can_enable_reading_then_vision_without_schema_changes(self):
        state = switches.ToolSwitches("research", permitted={"catty_scan_messages", "catty_look_image", "catty_web_search"}, enabled={"catty_web_search"})
        original = state.refresh(self.catalog)
        self.assertTrue(state.apply('{"enable":["catty_scan_messages"]}')["ok"])
        self.assertIsNone(state.denial("catty_scan_messages"))
        self.assertTrue(state.apply('{"enable":["catty_look_image"],"disable":["catty_web_search"]}')["ok"])
        self.assertIsNone(state.denial("catty_look_image"))
        self.assertEqual(state.denial("catty_web_search")["error"], "tool_switched_off")
        self.assertEqual(state.refresh(self.catalog), original)

    def test_locked_or_unknown_tool_cannot_be_enabled_and_changes_are_atomic(self):
        state = switches.ToolSwitches("main", permitted={"catty_scan_messages"})
        state.refresh(self.catalog)
        for arguments in ('{"enable":["catty_run_code"]}', '{"disable":["catty_scan_messages"],"enable":["unknown"]}', '{"role":"owner"}', '{"enable":["catty_tool_switch"]}', '{"enable":["catty_scan_messages"],"disable":["catty_scan_messages"]}'):
            with self.subTest(arguments=arguments):
                before = state.enabled
                self.assertFalse(state.apply(arguments)["ok"])
                self.assertEqual(state.enabled, before)

    def test_permission_revocation_overrides_an_earlier_enable(self):
        allowed = {"catty_scan_messages", "catty_look_image"}
        state = switches.ToolSwitches("main", permitted=lambda: allowed, enabled=set())
        state.refresh(self.catalog)
        state.apply('{"enable":["catty_look_image"]}')
        allowed.remove("catty_look_image")
        state.refresh(self.catalog)
        self.assertEqual(state.denial("catty_look_image")["error"], "tool_not_allowed")

    def test_marker_updates_only_its_own_tail_without_rewriting_prefix(self):
        state = switches.ToolSwitches("main")
        state.refresh(self.catalog)
        messages = [{"role": "system", "content": "long cached prefix" * 10000}, {"role": "user", "content": "read the group and image"}]
        prefix = copy.deepcopy(messages)
        state.update_messages(messages)
        state.apply('{"disable":["catty_web_search"]}')
        state.update_messages(messages)
        self.assertEqual(messages[:2], prefix)
        self.assertEqual(len(messages), 3)
        self.assertLess(len(messages[-1]["content"]), 600)
        self.assertIn('"off":["catty_web_search"]', messages[-1]["content"])

    def test_state_does_not_leak_between_requests_or_roles(self):
        first = switches.ToolSwitches("research")
        second = switches.ToolSwitches("main")
        first.refresh(self.catalog)
        second.refresh(self.catalog)
        first.apply('{"disable":["catty_look_image"]}')
        self.assertIsNone(second.denial("catty_look_image"))


if __name__ == "__main__":
    unittest.main()