from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"))
_PKG = "catty_shared_persona_integration_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(_SOURCE.parent)]
sys.modules.setdefault(_PKG, _package)
sys.modules.setdefault(f"{_PKG}.test_runtime", types.ModuleType(f"{_PKG}.test_runtime"))
message_utils = importlib.import_module(f"{_PKG}.message_utils")
markers = importlib.import_module(f"{_PKG}.reply_markers")
parsers = importlib.import_module(f"{_PKG}.parsers")
Config = importlib.import_module(f"{_PKG}.config").Config


def _load(names, namespace):
    nodes = []
    for node in _TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            node = copy.deepcopy(node)
            if not isinstance(node, ast.ClassDef):
                node.decorator_list = []
            nodes.append(node)
    if len(nodes) != len(names):
        raise AssertionError(names - {node.name for node in nodes})
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[],
    )
    namespace.setdefault("__name__", f"{_PKG}.test_runtime")
    namespace.setdefault("__package__", _PKG)
    exec(compile(ast.fix_missing_locations(module), str(_SOURCE), "exec"), namespace)
    return namespace


def _event(text="闲聊", *, at=False, at_all=False, user=20001):
    message = Message(text)
    if at:
        message = Message(MessageSegment.at(999)) + message
    if at_all:
        message = Message(MessageSegment.at("all")) + message
    return GroupMessageEvent(
        time=0, self_id=999, post_type="message", sub_type="normal", user_id=user,
        message_type="group", message_id=17, message=message, original_message=message,
        raw_message=str(message), font=0, sender=Sender(user_id=user, nickname="群友"), group_id=10001,
    )


def _persona(name, wake, **overrides):
    values = dict(
        name=name, char_name=wake, trigger_prefixes=(wake,), directed_keywords=(wake,),
        mention_only_trigger=False, followup_reply_budget=2, group_admission_enabled=True,
        definition_revision=None, feature_disabled=lambda feature: False,
        semantic_harness_enabled=True, output_filter_profile="persona",
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


class SharedGroupAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.persona = _persona("catty", "猫猫")
        self.continuation = False
        self.references = []
        self.config = Config(
            catty_filter_enabled=True, catty_allowed_group_ids=set(), catty_allowed_user_ids=set(),
            catty_enable_group=True, catty_ignore_bot_self_intro_enabled=True,
        )
        self.gate = AsyncMock(return_value=True)
        self.namespace = _load(
            {"_fdj_explicit_wake", "_mention_only_real_at_allowed", "_fresh_mention_for_event",
             "_group_persona_trigger_allowed", "_extract_trigger_incoming", "_extract_incoming_with_voice", "_rule"},
            {
                "GroupMessageEvent": GroupMessageEvent, "PrivateMessageEvent": PrivateMessageEvent,
                "config": self.config, "logger": Mock(), "_persona_for_event": lambda event: self.persona,
                "event_plain_text": message_utils.event_plain_text, "has_real_at_self": message_utils.has_real_at_self,
                "extract_incoming_message": message_utils.extract_incoming_message,
                "_resolve_reply_sources": AsyncMock(side_effect=lambda *args: self.references),
                "_recent_bot_prompted_user": lambda event: self.continuation,
                "_remember_recent_conversation_event": Mock(), "_model_should_reply_to_group": self.gate,
                "_close_bot_reply_continuation": Mock(), "_is_session_closing": lambda text: False,
                "_reset_followup_idle": Mock(), "_bot_reply_continuation_remaining": lambda event: 2,
                "_has_consumed_reply_source": lambda event: False,
                "_force_direct_reply_enabled": lambda event, incoming: False,
                "_take_due_group_filter_batch": AsyncMock(return_value=None),
                "memory_store": types.SimpleNamespace(is_special_care_user=lambda event: False),
            },
        )
        message_utils.set_persona_resolver(lambda event: self.persona)
        self.addCleanup(message_utils.set_persona_resolver, None)
        self.bot = types.SimpleNamespace(self_id=999)

    async def test_name_at_reply_and_continuation_for_multiple_personas(self):
        for name, wake in (("catty", "猫猫"), ("fadianji", "机机"), ("custom", "小青")):
            self.persona = _persona(name, wake)
            for text, at, quoted, continuation in (
                (wake, False, False, False), ("在吗", True, False, False),
                ("接着说", False, True, False), ("接着说", False, False, True),
            ):
                with self.subTest(persona=name, text=text, at=at, quoted=quoted, continuation=continuation):
                    self.references = [types.SimpleNamespace(is_current_bot=True)] if quoted else []
                    self.continuation = continuation
                    state = {}
                    event = _event(text, at=at)
                    self.assertTrue(await self.namespace["_rule"](self.bot, event, state))
                    self.assertEqual(self.namespace["_fresh_mention_for_event"](event, state["catty_incoming"]), not continuation)

    async def test_mention_only_requires_real_at_but_keeps_followup(self):
        self.persona = _persona("custom", "小青", mention_only_trigger=True)
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("小青 在吗"), {}))
        self.references = [types.SimpleNamespace(is_current_bot=True)]
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("接着说"), {}))
        self.references = []
        self.assertTrue(await self.namespace["_rule"](self.bot, _event("在吗", at=True), {}))
        self.continuation = True
        self.assertTrue(await self.namespace["_rule"](self.bot, _event("接着说"), {}))

    async def test_broadcast_acl_and_model_denial_are_preserved(self):
        self.persona = _persona("custom", "小青")
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("小青", at_all=True), {}))
        self.config.catty_allowed_group_ids = {123}
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("小青", at=True), {}))
        self.config.catty_allowed_group_ids = set()
        self.continuation = True
        self.gate.return_value = False
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("接着说"), {}))
        self.namespace["_close_bot_reply_continuation"].assert_called_once()

    async def test_existing_continuation_when_filter_is_disabled(self):
        self.persona = _persona("custom", "小青")
        self.config.catty_filter_enabled = False
        self.config.catty_group_require_mention_or_prefix = True
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("接着说"), {}))
        self.continuation = True
        self.assertTrue(await self.namespace["_rule"](self.bot, _event("接着说"), {}))
        self.assertFalse(await self.namespace["_rule"](self.bot, _event("接着说", at_all=True), {}))

    async def test_unaddressed_file_never_uses_model_gate_even_during_continuation(self):
        for continuation in (False, True):
            self.continuation = continuation
            event = _event("分享文件")
            event.message.append(MessageSegment(type="file", data={"id": "document-id", "name": "notes.txt"}))
            with self.subTest(continuation=continuation):
                self.assertFalse(await self.namespace["_rule"](self.bot, event, {}))
        self.gate.assert_not_awaited()
        self.namespace["_remember_recent_conversation_event"].assert_not_called()

    async def test_addressed_file_preserves_normal_reply_admission(self):
        event = _event("读一下这个文件", at=True)
        event.message.append(MessageSegment(type="file", data={"id": "document-id", "name": "notes.txt"}))
        state = {}
        self.assertTrue(await self.namespace["_rule"](self.bot, event, state))
        self.assertTrue(state["catty_incoming"].directly_requested)

    async def test_pending_file_request_restores_native_event_and_original_instruction(self):
        host_module = importlib.import_module(f"{_PKG}.task_host")
        host = host_module.TaskHost({
            "config": self.config,
            "affection_store": types.SimpleNamespace(is_owner=lambda user: False),
            "_persona_for_scope": lambda scope: self.persona,
            "_task_event_allowed": lambda event, config: True,
            "MessageSegment": MessageSegment,
        })
        host.service = types.SimpleNamespace(policy=types.SimpleNamespace(enabled=True))
        request = _event("分析接下来发的文件", at=True)
        host.observe_files(request)
        upload = _event("")
        upload.message_id = 18
        upload.message.append(MessageSegment(type="file", data={"id": "document-id", "name": "notes.txt"}))
        host.observe_files(upload)
        self.assertTrue(host.prepare_file_message(upload))
        state = {}
        self.assertTrue(await self.namespace["_rule"](self.bot, upload, state))
        self.assertTrue(state["catty_incoming"].directly_requested)
        self.assertIn("分析接下来发的文件", state["catty_incoming"].text)
        self.gate.assert_not_awaited()

    async def test_native_face_keeps_existing_group_wake_and_continuation(self):
        for name, wake in (("catty", "猫猫"), ("fadianji", "机机"), ("custom", "小青")):
            self.persona = _persona(name, wake)
            event = _event("")
            event.message.append(MessageSegment(type="face", data={"id": "182"}))
            event.raw_message = str(event.message)
            with self.subTest(persona=name, route="no-wake"):
                self.continuation = False
                self.assertFalse(await self.namespace["_rule"](self.bot, event, {}))
            with self.subTest(persona=name, route="continuation"):
                self.continuation = True
                state = {}
                self.assertTrue(await self.namespace["_rule"](self.bot, event, state))
                self.assertIn("QQ表情：笑哭", state["catty_incoming"].history_content)


class SharedContinuationTests(unittest.TestCase):
    def setUp(self):
        self.persona = _persona("catty", "猫猫")
        self.namespace = _load(
            {"BotReplyContinuationState", "_persona_identity", "_bot_reply_continuation_key",
             "_prune_bot_reply_continuations", "_mark_bot_reply_continuation",
             "_has_bot_reply_continuation", "_bot_reply_continuation_remaining"},
            {
                "dataclass": dataclass, "time": time, "logger": Mock(),
                "config": types.SimpleNamespace(catty_followup_window_seconds=25, catty_followup_idle_limit=3),
                "_bot_reply_continuations": {}, "_persona_for_scope": lambda scope: self.persona,
                "_persona_for_event": lambda event: self.persona,
                "_conversation_queue_key": lambda event: "group:10001",
            },
        )

    def test_budget_and_persona_revision_isolation(self):
        ns, event = self.namespace, _event()
        ns["_mark_bot_reply_continuation"]("group:10001", "20001", fresh_mention=True)
        self.assertEqual(ns["_bot_reply_continuation_remaining"](event), 2)
        ns["_mark_bot_reply_continuation"]("group:10001", "20001")
        self.assertEqual(ns["_bot_reply_continuation_remaining"](event), 1)
        self.persona = _persona("custom", "小青", definition_revision="v1")
        self.assertFalse(ns["_has_bot_reply_continuation"](event))
        ns["_mark_bot_reply_continuation"]("group:10001", "20001", fresh_mention=True)
        self.assertTrue(ns["_has_bot_reply_continuation"](event))
        self.persona.definition_revision = "v2"
        self.assertFalse(ns["_has_bot_reply_continuation"](event))
        self.persona.followup_reply_budget = 0
        ns["_mark_bot_reply_continuation"]("group:10001", "20001", fresh_mention=True)
        self.assertFalse(ns["_has_bot_reply_continuation"](event))

    def test_recent_messages_keep_humans_but_not_other_persona_bot(self):
        persona = _persona("custom", "小青", definition_revision="v1")
        rows = [types.SimpleNamespace(text="human", persona_identity=""),
                types.SimpleNamespace(text="old", persona_identity="catty"),
                types.SimpleNamespace(text="own", persona_identity="custom@v1")]
        ns = _load({"_persona_identity", "_recent_conversation_for_scope"}, {
            "_persona_for_scope": lambda scope: persona,
            "_recent_conversation_messages": {"group:1": rows},
        })
        self.assertEqual([row.text for row in ns["_recent_conversation_for_scope"]("group:1")], ["human", "own"])
        persona.definition_revision = "v2"
        self.assertEqual([row.text for row in ns["_recent_conversation_for_scope"]("group:1")], ["human"])


class SharedEvidenceAndOutputTests(unittest.TestCase):
    def test_wake_skeleton_follows_persona_capabilities(self):
        template = next(node.value for node in _TREE.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "_WAKE_CONTEXT_SKELETON_TPL"
                                for target in node.targets))
        ns = _load({"build_wake_context_skeleton"}, {"_WAKE_CONTEXT_SKELETON_TPL": ast.literal_eval(template)})
        for name, char in (("catty", "猫猫"), ("fadianji", "机机"), ("custom", "小青")):
            prompt = ns["build_wake_context_skeleton"](_persona(name, char))
            self.assertIn(f"{char}由当前人格的唤醒词", prompt)
            self.assertNotIn("机机由现有唤醒词", prompt)
        strict = _persona("custom", "小青", mention_only_trigger=True)
        self.assertIn("对只认真 @ 的人格", ns["build_wake_context_skeleton"](strict))

    def test_empty_search_fallback_keeps_persona_voice(self):
        handle = next(node for node in _TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_chat")
        block = next(node for node in ast.walk(handle) if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "not sanitized.strip()"
                     and "reply_context" in ast.unparse(node))
        module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(block)], type_ignores=[]))
        for name in ("catty", "fadianji", "custom"):
            for has_image in (False, True):
                persona = _persona(name, name)
                namespace = {
                    "event": object(), "nsfw_image_segments": [object()] if has_image else [], "sanitized": "",
                    "_persona_reply_context_for_event": lambda event: types.SimpleNamespace(persona=persona),
                    "_addr_user": lambda event: "主人",
                }
                exec(compile(module, str(_SOURCE), "exec"), namespace)
                result = namespace["sanitized"]
                if name == "catty":
                    self.assertIn("喵", result)
                else:
                    self.assertNotIn("喵", result)
                    self.assertNotIn("猫猫", result)
                    self.assertNotIn("主人", result)
                self.assertTrue(result)

    def test_cognitive_bundle_uses_bound_persona_and_allows_opt_out(self):
        packet = Mock(return_value={"text": "EVIDENCE", "rendered_counts": {}})
        harness = types.ModuleType(f"{_PKG}.fadianji_harness")
        harness.build_fadianji_evidence_packet = packet
        ns = _load({"_build_cognitive_turn_context_sync"}, {
            "config": types.SimpleNamespace(), "logger": Mock(), "memory_store": object(),
            "catty_rag_store": object(), "qzone_feed_store": None,
            "timeline_store": types.SimpleNamespace(list=lambda *args, **kwargs: []),
            "adaptive_prompt_store": types.SimpleNamespace(list_prompts=lambda *args, **kwargs: []),
        })
        with patch.dict(sys.modules, {harness.__name__: harness}):
            for name in ("catty", "fadianji", "custom"):
                persona = _persona(name, name)
                system, _ = ns["_build_cognitive_turn_context_sync"](
                    text="", quoted_text="引用旧话题", persona=persona, scope_key="group:1",
                    is_private=False, user_id="7", group_id="1",
                )
                self.assertEqual(system, "EVIDENCE")
                self.assertIs(packet.call_args.kwargs["persona"], persona)
                self.assertEqual(packet.call_args.kwargs["quoted_text"], "引用旧话题")
            packet.reset_mock()
            persona.semantic_harness_enabled = False
            system, _ = ns["_build_cognitive_turn_context_sync"](
                text="问题", persona=persona, scope_key="group:1", is_private=False, user_id="7", group_id="1",
            )
            self.assertEqual(system, "")
            packet.assert_not_called()

    def test_proactive_cleaner_respects_profile_and_legacy_entry(self):
        ns = _load({"_clean_fdj_proactive_reply"}, {
            "re": __import__("re"), "NO_REPLY_MARKER": markers.NO_REPLY_MARKER,
            "REPLY_SPLIT_MARKER": markers.REPLY_SPLIT_MARKER, "_strip_catty_markers": parsers.strip_catty_markers,
            "_strip_tone_parenthetical": markers.strip_tone_parenthetical,
            "_strip_fadianji_catty_tics": markers.strip_fadianji_catty_tics,
        })
        clean = ns["_clean_fdj_proactive_reply"]
        self.assertIn("喵", clean("今天真好喵", _persona("catty", "猫猫")))
        self.assertNotIn("喵", clean("今天真好喵", _persona("custom", "小青", output_filter_profile="fadianji")))
        self.assertNotIn("喵", clean("今天真好喵"))

    def test_main_critic_receives_bound_persona_and_only_shared_scheduler_starts(self):
        handle = next(node for node in _TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_chat")
        critic_calls = [node for node in ast.walk(handle) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr == "rewrite_if_needed"]
        self.assertEqual(len(critic_calls), 1)
        persona_arg = next(item.value for item in critic_calls[0].keywords if item.arg == "persona")
        self.assertEqual(ast.unparse(persona_arg), "_persona")
        source = ast.unparse(handle)
        self.assertIn("style_critic_enabled", source)
        self.assertIn("style_critic_profile", source)
        self.assertIn("output_filter_profile", source)
        startup = next(node for node in _TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_memory_summary_loop")
        self.assertIn("_fdj_proactive_bubble_loop()", ast.unparse(startup))
        self.assertNotIn("_proactive_bubble_loop()", ast.unparse(startup).replace("_fdj_proactive_bubble_loop()", ""))


class SharedNsfwIsolationTests(unittest.TestCase):
    def test_exit_resets_only_bound_phase_and_preserves_catty(self):
        phase = types.ModuleType(f"{_PKG}.nsfw_phase")
        phase.get_phase_state = Mock(return_value=types.SimpleNamespace(current_phase=2, arc_count=1))
        phase.reset_phase = Mock()
        phase.revoke_all_nsfw_grantees = Mock(return_value=0)
        image = types.ModuleType(f"{_PKG}.catty_nsfw_imagegen")
        image.reset_counter = Mock()
        catty_key = "private:42:42"
        other_scope = "private:42#nsfw=custom"
        other_key = other_scope + ":42"
        sticky = {catty_key: time.time() + 120, other_key: time.time() + 120}
        ns = _load({"_phase_scope_for", "_apply_nsfw_state_flag"}, {
            "_NSFW_STICKY_BY_SCOPE": sticky, "_NSFW_STICKY_IDLE_COUNT": {}, "_NSFW_EXIT_FLAG_COUNT": {},
            "_NSFW_EXIT_FLAG_LIMIT": 3, "_clear_nsfw_group_occupant": Mock(), "logger": Mock(),
            "mood_overlay_store": types.SimpleNamespace(write=Mock()),
        })
        self.assertEqual(ns["_phase_scope_for"]("private:42", _persona("catty", "猫猫")), "private:42")
        self.assertEqual(ns["_phase_scope_for"]("private:42", _persona("custom", "小青")), other_scope)
        with patch.dict(sys.modules, {phase.__name__: phase, image.__name__: image}):
            for _ in range(3):
                ns["_apply_nsfw_state_flag"](other_key, "42", "exit", is_owner=False, phase_scope=other_scope)
        self.assertIn(catty_key, sticky)
        self.assertNotIn(other_key, sticky)
        phase.get_phase_state.assert_called_once_with(other_scope, "42")
        phase.reset_phase.assert_called_once_with(other_scope, "42")
        phase.revoke_all_nsfw_grantees.assert_called_once_with(other_scope)
        image.reset_counter.assert_called_once_with(other_scope, "42")

    def test_group_occupant_scans_only_matching_persona_prefix(self):
        now = time.time()
        ns = _load({"_mark_nsfw_group_occupant", "_active_nsfw_group_occupant"}, {
            "time": time, "_NSFW_GROUP_OCCUPANT_BY_SCOPE": {},
            "_NSFW_STICKY_BY_SCOPE": {"group:1:10": now + 30, "group:1#nsfw=custom:20": now + 30},
        })
        self.assertEqual(ns["_active_nsfw_group_occupant"]("group:1")[0], "10")
        self.assertEqual(ns["_active_nsfw_group_occupant"]("group:1#nsfw=custom")[0], "20")
        self.assertIsNone(ns["_active_nsfw_group_occupant"]("group:1#nsfw=third"))

    def test_runtime_paths_use_phase_scope_for_sticky_grants_and_image_counter(self):
        build = next(node for node in _TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_messages")
        handle = next(node for node in _TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_chat")
        source = ast.unparse(build)
        self.assertIn("_sticky_key = f'{_phase_scope}:{event.user_id}'", source)
        self.assertIn("_is_granted(_phase_scope, str(event.user_id))", source)
        self.assertIn("_grant_nsfw(_phase_scope, _t_uid)", source)
        self.assertIn("_reset_nsfw_img_counter(_phase_scope, str(event.user_id))", source)
        self.assertIn("phase_scope=_phase_scope_chat", ast.unparse(handle))
        self.assertIn("_nsfw_img_scope = _phase_scope_chat", ast.unparse(handle))


class SharedPregnancyStateTests(unittest.TestCase):
    def _func(self, name):
        return next(node for node in _TREE.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == name)

    def test_build_messages_exports_and_resets_pregnancy_predict_global(self):
        source = ast.unparse(self._func("_build_messages"))
        handle = ast.unparse(self._func("handle_chat"))
        self.assertIn("global _PREGNANCY_PREDICT", source)
        self.assertIn("_PREGNANCY_PREDICT = {}", source)
        self.assertIn("_predict_meta = _PREGNANCY_PREDICT or {}", handle)
        self.assertIn("_predict_meta_main = _PREGNANCY_PREDICT or {}", handle)

    def test_main_path_scene_hint_reads_phase_scope_not_raw_arc_scope(self):
        source = ast.unparse(self._func("_build_messages"))
        self.assertIn("_gps_main2(_phase_scope, str(event.user_id))", source)
        self.assertIn("_build_phase_params_main2(_phase_scope, str(event.user_id)", source)
        self.assertNotIn("_gps_main2(_arc_scope", source)

    def test_cpu_engine_phase_read_uses_phase_scope_for(self):
        source = ast.unparse(self._func("_cpu_engine_rule"))
        self.assertIn("_ce_get_phase_state(_phase_scope_for(scope, _persona_for_scope(scope)), user_id)", source)

    def test_pregnancy_flush_hooks_cover_per_persona_stores(self):
        loop = next(node for node in _TREE.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_managed_store_flush_loop")
        shutdown = self._func("_flush_pregnancy_store_on_shutdown")
        self.assertIn("_PREGNANCY_STORES.items()", ast.unparse(loop))
        self.assertIn("_PREGNANCY_STORES.items()", ast.unparse(shutdown))


if __name__ == "__main__":
    unittest.main()
