"""Offline persona commands/generation; no NoneBot startup or live model calls."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sys
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PKG = "catty_persona_admin_test"
if _PKG not in sys.modules:
    _package = types.ModuleType(_PKG)
    _package.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
    sys.modules[_PKG] = _package

admin = importlib.import_module(f"{_PKG}.persona_admin")
store = importlib.import_module(f"{_PKG}.persona_store")
Config = importlib.import_module(f"{_PKG}.config").Config


def definition(**updates):
    result = {
        "schema_version": 1,
        "name": "sky_guide",
        "char_name": "小岚",
        "core_persona": "你是小岚，温柔的天文爱好者；闲聊简短，解释观星问题时详细。",
        "aliases": ["小岚"],
    }
    result.update(updates)
    return result


def response(**updates):
    return json.dumps(definition(**updates), ensure_ascii=False)


def config(**updates):
    settings = {
        "catty_openai_base_url": "https://model.invalid/v1",
        "catty_openai_model": "main-model-fixture",
        "catty_max_tokens": 1200,
        "catty_persona_generation_timeout_seconds": 60.0,
        "catty_persona_generation_max_tokens": 4096,
    }
    settings.update(updates)
    return Config().model_copy(update=settings)


class PersonaCommandTests(unittest.TestCase):
    def test_command_dataclass_is_immutable(self):
        command = admin.PersonaCommand("switch", "sky_guide")
        with self.assertRaises(FrozenInstanceError):
            command.name = "other"
        self.assertEqual(command.body, "")
        self.assertEqual(command.command, "")
        self.assertFalse(command.overwrite)

    def test_overview_list_and_reload(self):
        for text, action in (
            ("/人格", "overview"), (" \n/人格 \t", "overview"),
            ("/人格 overview", "overview"), ("/人格 列表", "list"),
            ("/人格 LIST", "list"), ("/人格 重载", "reload"),
            ("/人格 reload", "reload"),
        ):
            with self.subTest(text=text):
                self.assertEqual(admin.parse_persona_command(text), admin.PersonaCommand(action))

    def test_legacy_names_remain_unresolved(self):
        for name in ("笨猫", "猫猫", "机机", "小机", "发电机", "不稳定发电机", "catty", "FADIANJI"):
            with self.subTest(name=name):
                self.assertEqual(
                    admin.parse_persona_command(f"/人格 {name}"),
                    admin.PersonaCommand("switch", name=name),
                )

    def test_switch_and_show(self):
        for verb in ("切换", "use", "SWITCH"):
            self.assertEqual(
                admin.parse_persona_command(f"/人格 {verb} sky_guide"),
                admin.PersonaCommand("switch", name="sky_guide"),
            )
        for verb in ("查看", "show"):
            self.assertEqual(
                admin.parse_persona_command(f"/人格 {verb} 小岚"),
                admin.PersonaCommand("show", name="小岚"),
            )

    def test_all_legacy_reset_forms(self):
        for name in ("默认", "清除", "default", "reset", "DEFAULT"):
            self.assertEqual(admin.parse_persona_command(f"/人格 {name}"), admin.PersonaCommand("reset"))

    def test_multiline_import_preserves_json_and_pipes(self):
        body = json.dumps(definition(core_persona="说‘晴 | 阴’\n保留第二行"), ensure_ascii=False, indent=2)
        for option, overwrite in (("", False), (" 覆盖", True), (" overwrite", True)):
            with self.subTest(option=option):
                parsed = admin.parse_persona_command(f"/人格 导入{option} |\n{body}\n")
                self.assertEqual(parsed, admin.PersonaCommand("import", body=body, overwrite=overwrite))

    def test_generation_keeps_contextual_description(self):
        for verb in ("生成", "generate"):
            parsed = admin.parse_persona_command(f"/人格 {verb} sky_guide 覆盖 | 照刚才那个，第二点改成简短。\n保留 A | B。")
            self.assertEqual(parsed.action, "generate")
            self.assertEqual(parsed.name, "sky_guide")
            self.assertEqual(parsed.body, "照刚才那个，第二点改成简短。\n保留 A | B。")
            self.assertTrue(parsed.overwrite)

    def test_bind_unbind_keep_whole_command(self):
        command = "开启 小岚 | 观星 模式"
        self.assertEqual(
            admin.parse_persona_command(f"/人格 指令 sky_guide | {command}"),
            admin.PersonaCommand("bind", name="sky_guide", command=command),
        )
        self.assertEqual(
            admin.parse_persona_command(f"/人格 解绑 | {command}"),
            admin.PersonaCommand("unbind", command=command),
        )

    def test_custom_mapping_matches_full_message_only(self):
        commands = {"进入 观星模式": "sky_guide", "/小岚": "sky_guide", "sky mode": "sky_guide"}
        before = dict(commands)
        for text in ("进入  观星模式", " 进入  观星模式\t", "/小岚", "SKY  Mode"):
            self.assertEqual(
                admin.parse_persona_command(text, commands),
                admin.PersonaCommand("switch", name="sky_guide", command=store.normalize_switch_command(text)),
            )
        for text in ("请进入  观星模式", "进入  观星模式！", "/小岚 好呀", "sky\nmode"):
            self.assertIsNone(admin.parse_persona_command(text, commands))
        self.assertEqual(commands, before)

    def test_builtin_syntax_cannot_be_shadowed(self):
        self.assertEqual(
            admin.parse_persona_command("/人格 默认", {"/人格 默认": "sky_guide"}),
            admin.PersonaCommand("reset"),
        )

    def test_unrelated_or_contextual_chat_is_not_a_command(self):
        for text in ("", "人格", "/人格化", "请 /人格 机机", "就换刚才那个", "生成 sky_guide | 观星", None):
            with self.subTest(text=text):
                self.assertIsNone(admin.parse_persona_command(text))

    def test_unknown_subcommand_is_a_useful_error(self):
        for text in ("/人格 删除 sky_guide", "/人格 delet sky_guide", "/人格 nonsense | body"):
            with self.subTest(text=text), self.assertRaisesRegex(admin.PersonaCommandParseError, "未知人格子命令.*用法"):
                admin.parse_persona_command(text)

    def test_unknown_bare_name_is_left_to_registry_validation(self):
        self.assertEqual(
            admin.parse_persona_command("/人格 not_installed"),
            admin.PersonaCommand("switch", name="not_installed"),
        )

    def test_invalid_commands_do_not_fall_through(self):
        invalid = (
            "/人格 列表 extra", "/人格 重载 | extra", "/人格 默认 extra",
            "/人格 查看", "/人格 查看 a b", "/人格 use", "/人格 use a | b",
            "/人格 导入", "/人格 导入 |", "/人格 导入 a | {}",
            "/人格 导入 覆盖 extra | {}", "/人格 生成 | desc",
            "/人格 生成 name", "/人格 生成 name 覆盖 |",
            "/人格 生成 name extra | desc", "/人格 指令 name",
            "/人格 指令 | /name", "/人格 指令 a b | /name",
            "/人格 解绑 /name", "/人格 解绑 |", "/人格 | {}",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaisesRegex(admin.PersonaCommandParseError, "用法"):
                admin.parse_persona_command(text)


class PersonaGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_validation_and_config_copy(self):
        original = config(catty_openai_extra_body={"fixture": {"unchanged": True}})
        before = original.model_dump()
        seen = []

        async def complete(request_config, messages):
            seen.append((request_config, copy.deepcopy(messages)))
            request_config.catty_openai_extra_body["fixture"]["unchanged"] = False
            return response()

        result = await admin.generate_persona_definition(
            original, name="sky_guide", description="温柔天文爱好者", completion_fn=complete,
        )
        self.assertEqual(result, store.validate_persona_definition(definition()))
        self.assertEqual(original.model_dump(), before)
        self.assertIsNot(seen[0][0], original)
        self.assertEqual(seen[0][0].catty_max_tokens, 4096)
        self.assertEqual(seen[0][0].catty_openai_model, original.catty_openai_model)
        self.assertEqual(seen[0][0].catty_openai_base_url, original.catty_openai_base_url)
        self.assertEqual(len(seen), 1)

    async def test_optional_fence_and_lenient_json(self):
        complete = AsyncMock(return_value="```json\n" + response()[:-1] + ",}\n```")
        result = await admin.generate_persona_definition(
            config(), name="sky_guide", description="小岚", completion_fn=complete,
        )
        self.assertEqual(result["char_name"], "小岚")
        complete.assert_awaited_once()

    async def test_context_existing_and_schema_are_supplied_without_rewriting(self):
        old = definition()
        before = copy.deepcopy(old)
        context = "主人：名字小岚，喜欢观星。\n补充：不说固定口头禅。"
        text = "照刚才那个，把普通闲聊改得更短。"
        messages_seen = []

        async def complete(_config, messages):
            messages_seen.extend(copy.deepcopy(messages))
            return response()

        await admin.generate_persona_definition(
            config(), name="sky_guide", description=text, existing=old,
            context_text=context, completion_fn=complete,
        )
        payload = json.loads(messages_seen[1]["content"])
        self.assertEqual(payload["description"], text)
        self.assertEqual(payload["quoted_context"], context)
        self.assertEqual(payload["requested_id"], "sky_guide")
        self.assertEqual(payload["existing_definition"], store.validate_persona_definition(old))
        self.assertEqual(old, before)
        schema = json.loads(messages_seen[0]["content"].split("PERSONA_SCHEMA:\n", 1)[1])
        self.assertEqual(schema, store.PERSONA_SCHEMA)
        self.assertIn("最后一句", messages_seen[0]["content"])
        self.assertIn("保留", messages_seen[0]["content"])

    async def test_repairs_malformed_json_once(self):
        captured = []

        async def complete(_config, messages):
            captured.append(copy.deepcopy(messages))
            return "{broken JSON" if len(captured) == 1 else response()

        result = await admin.generate_persona_definition(
            config(), name="sky_guide", description="观星角色", completion_fn=complete,
        )
        self.assertEqual(result["name"], "sky_guide")
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[1][:2], captured[0])
        self.assertEqual(captured[1][-2], {"role": "assistant", "content": "{broken JSON"})
        self.assertIn("校验", captured[1][-1]["content"])

    async def test_schema_failure_can_be_repaired(self):
        complete = AsyncMock(side_effect=[response(char_name=123), response()])
        result = await admin.generate_persona_definition(
            config(), name="sky_guide", description="观星角色", completion_fn=complete,
        )
        self.assertEqual(result["char_name"], "小岚")
        self.assertEqual(complete.await_count, 2)

    async def test_malformed_results_raise_after_only_one_repair(self):
        for raw in ("not JSON", "[]", "", response(char_name=123)):
            with self.subTest(raw=raw):
                complete = AsyncMock(return_value=raw)
                with self.assertRaises(admin.PersonaGenerationError):
                    await admin.generate_persona_definition(
                        config(), name="sky_guide", description="观星角色", completion_fn=complete,
                    )
                self.assertEqual(complete.await_count, 2)

    async def test_id_mismatch_never_silently_renames(self):
        complete = AsyncMock(return_value=response(name="another_persona"))
        with self.assertRaisesRegex(admin.PersonaGenerationError, "name.*sky_guide"):
            await admin.generate_persona_definition(
                config(), name="sky_guide", description="观星角色", completion_fn=complete,
            )
        self.assertEqual(complete.await_count, 2)

    async def test_model_can_repair_id_mismatch(self):
        complete = AsyncMock(side_effect=[response(name="another_persona"), response()])
        result = await admin.generate_persona_definition(
            config(), name="sky_guide", description="观星角色", completion_fn=complete,
        )
        self.assertEqual(result["name"], "sky_guide")
        self.assertEqual(complete.await_count, 2)

    async def test_invalid_input_is_rejected_before_model(self):
        for values in (
            {"name": "../escape", "description": "小岚"},
            {"name": " sky_guide ", "description": "小岚"},
            {"name": "SKY_GUIDE", "description": "小岚"},
            {"name": "sky_guide", "description": " "},
            {"name": "sky_guide", "description": "小岚", "existing": definition(name="other")},
        ):
            with self.subTest(values=values):
                complete = AsyncMock(return_value=response())
                with self.assertRaises(admin.PersonaGenerationError):
                    await admin.generate_persona_definition(config(), completion_fn=complete, **values)
                complete.assert_not_awaited()

    async def test_invalid_limits_are_rejected_before_model(self):
        for values in (
            {"catty_persona_generation_timeout_seconds": 0},
            {"catty_persona_generation_timeout_seconds": float("inf")},
            {"catty_persona_generation_timeout_seconds": float("nan")},
            {"catty_persona_generation_timeout_seconds": True},
            {"catty_persona_generation_max_tokens": 0},
            {"catty_persona_generation_max_tokens": 1.5},
        ):
            with self.subTest(values=values):
                complete = AsyncMock(return_value=response())
                with self.assertRaises(admin.PersonaGenerationError):
                    await admin.generate_persona_definition(
                        config(**values), name="sky_guide", description="小岚", completion_fn=complete,
                    )
                complete.assert_not_awaited()

    async def test_timeout_cancels_request_without_repair(self):
        cancelled = asyncio.Event()
        calls = []

        async def complete(_config, _messages):
            calls.append(True)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(admin.PersonaGenerationTimeoutError):
            await admin.generate_persona_definition(
                config(catty_persona_generation_timeout_seconds=0.05),
                name="sky_guide", description="小岚", completion_fn=complete,
            )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(calls), 1)

    async def test_repair_shares_one_total_deadline(self):
        calls = []
        cancelled = asyncio.Event()
        actual_wait_for = asyncio.wait_for

        async def complete(_config, _messages):
            calls.append(True)
            if len(calls) == 1:
                return "malformed"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(admin.asyncio, "wait_for", wraps=actual_wait_for) as waiter:
            with self.assertRaises(admin.PersonaGenerationTimeoutError):
                await admin.generate_persona_definition(
                    config(catty_persona_generation_timeout_seconds=0.05),
                    name="sky_guide", description="小岚", completion_fn=complete,
                )
        self.assertEqual(len(calls), 2)
        waiter.assert_awaited_once()
        self.assertGreater(waiter.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(waiter.call_args.kwargs["timeout"], 0.05)
        self.assertTrue(cancelled.is_set())

    async def test_deadline_rounding_never_extends_configured_timeout(self):
        loop = asyncio.get_running_loop()
        timeouts = []

        async def capture_wait(coro, *, timeout):
            timeouts.append(timeout)
            coro.close()
            return definition()

        with patch.object(loop, "time", return_value=1_000_000.0):
            with patch.object(admin.asyncio, "wait_for", side_effect=capture_wait):
                await admin.generate_persona_definition(
                    config(catty_persona_generation_timeout_seconds=0.05),
                    name="sky_guide", description="小岚", completion_fn=AsyncMock(),
                )
        self.assertEqual(timeouts, [0.05])

    async def test_external_cancellation_propagates_in_first_request_and_repair(self):
        for cancel_on in (1, 2):
            with self.subTest(cancel_on=cancel_on):
                started = asyncio.Event()
                cancelled = asyncio.Event()
                calls = []

                async def complete(_config, _messages):
                    calls.append(True)
                    if len(calls) < cancel_on:
                        return "malformed"
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                task = asyncio.create_task(admin.generate_persona_definition(
                    config(), name="sky_guide", description="小岚", completion_fn=complete,
                ))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(cancelled.is_set())
                self.assertEqual(len(calls), cancel_on)

    async def test_transport_error_is_not_repaired_or_exposed(self):
        complete = AsyncMock(side_effect=RuntimeError("TOKEN secret provider response"))
        with self.assertRaises(admin.PersonaGenerationError) as caught:
            await admin.generate_persona_definition(
                config(), name="sky_guide", description="小岚", completion_fn=complete,
            )
        self.assertIn("RuntimeError", str(caught.exception))
        self.assertNotIn("TOKEN", str(caught.exception))
        complete.assert_awaited_once()

    async def test_default_route_uses_aux_plain_chat_without_tools(self):
        client = types.ModuleType(f"{_PKG}.openai_client")
        client.chat_completion_aux = AsyncMock(return_value=response())
        client.set_distill_suppressed = Mock(return_value=object())
        client.reset_distill_suppressed = Mock()
        with patch.dict(sys.modules, {client.__name__: client}):
            result = await admin.generate_persona_definition(
                config(), name="sky_guide", description="小岚",
            )
        self.assertEqual(result["name"], "sky_guide")
        client.chat_completion_aux.assert_awaited_once()
        self.assertEqual(len(client.chat_completion_aux.call_args.args), 2)
        self.assertEqual(
            client.chat_completion_aux.call_args.kwargs,
            {"max_tokens": 4096, "request_route": "persona_definition"},
        )
        self.assertEqual(client.chat_completion_aux.call_args.args[0].catty_max_tokens, 4096)
        client.set_distill_suppressed.assert_called_once_with(True)
        client.reset_distill_suppressed.assert_called_once_with(client.set_distill_suppressed.return_value)

    async def test_default_route_restores_distillation_on_cancellation(self):
        client = types.ModuleType(f"{_PKG}.openai_client")
        client.chat_completion_aux = AsyncMock(side_effect=asyncio.CancelledError)
        client.set_distill_suppressed = Mock(return_value=object())
        client.reset_distill_suppressed = Mock()
        with patch.dict(sys.modules, {client.__name__: client}):
            with self.assertRaises(asyncio.CancelledError):
                await admin.generate_persona_definition(config(), name="sky_guide", description="小岚")
        client.chat_completion_aux.assert_awaited_once()
        client.reset_distill_suppressed.assert_called_once_with(client.set_distill_suppressed.return_value)

    async def test_generation_does_not_persist_any_file(self):
        complete = AsyncMock(return_value=response())
        with (
            patch("pathlib.Path.write_text", side_effect=AssertionError("unexpected write")),
            patch("pathlib.Path.write_bytes", side_effect=AssertionError("unexpected write")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch("os.replace", side_effect=AssertionError("unexpected publish")),
        ):
            result = await admin.generate_persona_definition(
                config(), name="sky_guide", description="小岚", completion_fn=complete,
            )
        self.assertEqual(result["name"], "sky_guide")


if __name__ == "__main__":
    unittest.main()