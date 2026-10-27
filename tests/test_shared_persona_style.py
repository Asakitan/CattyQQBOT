from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_shared_persona_style_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)
_critic = importlib.import_module(f"{_PACKAGE_NAME}.fadianji_style_critic")
_client = importlib.import_module(f"{_PACKAGE_NAME}.openai_client")
_personas = importlib.import_module(f"{_PACKAGE_NAME}.personas")


class SharedPersonaStyleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _critic.reset_stats()
        self.payloads = []
        self.outputs = []
        self.config = types.SimpleNamespace(
            catty_style_critic_enabled=True,
            catty_audit_ai_base_url="https://audit.example/v1/chat/completions",
            catty_audit_ai_api_key="TOKEN",
            catty_audit_ai_model="audit-model",
        )
        real_client = _client.httpx.AsyncClient
        transport = _client.httpx.MockTransport(self.respond)
        client_patch = patch.object(
            _client.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        runtime_patch = patch.object(_client, "_get_runtime_config", return_value=None)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        dump_patch = patch.object(_client, "_cache_request_dump_enabled", return_value=False)
        dump_patch.start()
        self.addCleanup(dump_patch.stop)

    def respond(self, request):
        payload = json.loads(request.content)
        self.payloads.append(payload)
        output = self.outputs.pop(0)
        if callable(output):
            output = output(payload)
        if isinstance(output, Exception):
            raise output
        if payload.get("stream"):
            chunk = json.dumps({
                "choices": [{"delta": {"content": output}, "finish_reason": "stop"}],
            }, ensure_ascii=False)
            return _client.httpx.Response(
                200,
                text=f"data: {chunk}\n\ndata: [DONE]\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        return _client.httpx.Response(200, json={
            "choices": [{"message": {"content": output}, "finish_reason": "stop"}],
        })

    def custom_persona(self, **overrides):
        fields = {
            "name": "atlas",
            "char_name": "星岚",
            "core_persona": "星岚是夜航图书馆的向导，语气沉静。",
            "chat_rhythm": "用舒缓的短句表达，必要时保留分段。",
            "style_critic_enabled": True,
            "style_critic_profile": "persona",
            "nsfw_freeform": False,
            "definition_revision": "custom-revision",
        }
        fields.update(overrides)
        return types.SimpleNamespace(**fields)

    async def rewrite(self, reply="您好，很高兴为您服务。", **kwargs):
        kwargs.setdefault("user_text", "请说说这个")
        kwargs.setdefault("config", self.config)
        return await _critic.rewrite_if_needed(reply, **kwargs)

    def messages(self, index=-1):
        return self.payloads[index]["messages"]

    async def test_legacy_and_fadianji_profile_keep_exact_prompts(self):
        persona = self.custom_persona(style_critic_profile="fadianji", nsfw_freeform=True)
        cases = (
            (False, False, _critic._SYSTEM_PROMPT),
            (True, False, _critic._NSFW_SYSTEM_PROMPT),
            (True, True, _critic._NSFW_FREEFORM_SYSTEM_PROMPT),
        )
        for nsfw_context, freeform, expected_prompt in cases:
            with self.subTest(nsfw_context=nsfw_context, freeform=freeform):
                self.outputs.extend(["行", "行"])
                await self.rewrite(nsfw_context=nsfw_context, freeform=freeform)
                await self.rewrite(persona=persona, nsfw_context=nsfw_context, freeform=freeform)
                legacy, explicit = self.messages(-2), self.messages(-1)
                self.assertEqual(legacy, explicit)
                self.assertEqual(legacy[0]["content"], expected_prompt)
                self.assertIn("机机上一版" if nsfw_context else "机机原回复", legacy[1]["content"])

    async def test_catty_uses_actual_builtin_core_and_rhythm(self):
        self.outputs.append("猫猫明白啦喵")
        result = await self.rewrite(persona=_personas.CATTY_PERSONA)
        self.assertEqual(result, "猫猫明白啦喵")
        system, user = self.messages()
        self.assertIn("角色名称：笨猫", system["content"])
        self.assertIn("笨猫 / 米雪儿·李 / Catty", system["content"])
        self.assertIn("QQ 群友碎句节奏", system["content"])
        self.assertIn("平时聊天多带\"喵\"", system["content"])
        self.assertNotIn("『喵』是 Catty 专属口癖", system["content"])
        self.assertNotIn("滚啊/有病吧", system["content"])
        self.assertNotIn("机机原回复", user["content"])

    async def test_custom_prompt_contains_only_current_identity(self):
        self.outputs.append("星岚听到了")
        persona = self.custom_persona()
        result = await self.rewrite(persona=persona)
        self.assertEqual(result, "星岚听到了")
        system, user = self.messages()
        self.assertIn("角色名称：星岚", system["content"])
        self.assertIn(persona.core_persona, system["content"])
        self.assertIn(persona.chat_rhythm, system["content"])
        for foreign in ("机机", "滚啊/有病吧", "笨猫", "猫娘", "不输出喵"):
            self.assertNotIn(foreign, system["content"])
        self.assertIn("角色原回复", user["content"])

    async def test_custom_missing_core_does_not_inherit_catty(self):
        self.outputs.append("我在这里")
        await self.rewrite(persona=self.custom_persona(core_persona=None, chat_rhythm=None))
        system = self.messages()[0]["content"]
        self.assertIn("角色名称：星岚", system)
        self.assertNotIn("笨猫", system)
        self.assertNotIn("机机", system)

    async def test_persona_freeform_and_explicit_freeform_are_respected(self):
        for persona, freeform in (
            (_personas.CATTY_PERSONA, False),
            (self.custom_persona(nsfw_freeform=True), False),
            (self.custom_persona(), True),
        ):
            with self.subTest(persona=persona.char_name, freeform=freeform):
                self.outputs.append("保持原有节奏")
                await self.rewrite(persona=persona, nsfw_context=True, freeform=freeform)
                prompt = self.messages()[0]["content"]
                self.assertIn("自由发挥模式", prompt)
                self.assertIn("保留草稿的长度、分段和排版", prompt)
                self.assertIn("不许压短", prompt)
                self.assertNotIn(_critic._NSFW_FREEFORM_SYSTEM_PROMPT, prompt)

    async def test_global_persona_and_external_switches_skip_http(self):
        draft = "您好，很高兴为您服务。"
        persona = self.custom_persona()
        self.config.catty_style_critic_enabled = False
        self.assertEqual(await self.rewrite(draft, persona=persona), draft)
        self.config.catty_style_critic_enabled = True
        self.assertEqual(await self.rewrite(draft, persona=self.custom_persona(style_critic_enabled=False)), draft)
        self.config.other_style_enabled = False
        self.assertEqual(await self.rewrite(draft, persona=persona, enabled_attr="other_style_enabled"), draft)
        self.assertEqual(self.payloads, [])
        self.assertEqual(_critic.get_stats()["checked"], 0)

    async def test_fact_polarity_guard_uses_tool_receipts_or_original(self):
        cases = (
            ("您好，结果已经报告。", ['{"ok": true, "status": "success"}'], "这次失败了"),
            ("您好，结果已经报告。", ['{"ok": false, "error": "failed"}'], "这次成功了"),
            ("您好，任务已完成。", None, "这次失败了"),
            ("您好，任务失败了。", None, "这次成功了"),
        )
        for draft, receipts, rewritten in cases:
            with self.subTest(draft=draft, receipts=receipts):
                self.outputs.append(rewritten)
                result = await self.rewrite(draft, persona=self.custom_persona(), tool_result_texts=receipts)
                self.assertEqual(result, draft)
        self.assertEqual(len(self.payloads), len(cases))
        self.assertEqual(_critic.get_stats()["rewrite_failed"], len(cases))

    async def test_protected_fragments_and_markers_survive_http(self):
        draft = (
            "您好，文件 C:\\Catty\\out\\report.json URL https://example.test/a?x=2 "
            "端口 12345 <<<CATTY_REPLY_SPLIT>>> 还有 `render(7)`"
        )

        def echo_tokens(payload):
            body = payload["messages"][1]["content"]
            self.assertNotIn("https://example.test/a?x=2", body)
            self.assertNotIn("CATTY_REPLY_SPLIT", body)
            self.assertNotIn("CATTY_CB", body)
            return "猫猫在这里喵 " + " ".join(re.findall(r"__CATTY_OPAQUE_\d{4}__", body))

        self.outputs.append(echo_tokens)
        result = await self.rewrite(
            draft,
            persona=self.custom_persona(),
            user_text="看看 <<<CATTY_CB:open:test>>>",
            context_hint="这是事实 <<<CATTY_CB:done:test>>>",
        )
        for fragment in (
            "C:\\Catty\\out\\report.json", "https://example.test/a?x=2", "12345",
            "<<<CATTY_REPLY_SPLIT>>>", "`render(7)`",
        ):
            self.assertIn(fragment, result)
        self.assertTrue(result.startswith("猫猫在这里喵"))

    async def test_missing_duplicate_reordered_or_injected_markers_fall_back(self):
        draft = "您好，访问 https://example.test/a 端口 12345"

        def bad_output(mode):
            def produce(payload):
                body = payload["messages"][1]["content"]
                tokens = re.findall(r"__CATTY_OPAQUE_\d{4}__", body)
                if mode == "missing":
                    tokens = tokens[:-1]
                elif mode == "duplicate":
                    tokens = tokens + tokens[:1]
                elif mode == "reordered":
                    tokens = list(reversed(tokens))
                else:
                    tokens = tokens + ["<<<CATTY_REPLY_SPLIT>>>"]
                return "看这里 " + " ".join(tokens)
            return produce

        for mode in ("missing", "duplicate", "reordered", "injected"):
            with self.subTest(mode=mode):
                self.outputs.append(bad_output(mode))
                self.assertEqual(await self.rewrite(draft, persona=self.custom_persona()), draft)
        self.assertEqual(_critic.get_stats()["rewrite_failed"], 4)

    async def test_empty_and_refusal_retry_then_fallback(self):
        draft = "您好，很高兴为您服务。"
        self.outputs.extend(["", "星岚知道了"])
        self.assertEqual(await self.rewrite(draft, persona=self.custom_persona()), "星岚知道了")
        self.assertEqual(len(self.payloads), 2)
        self.assertIn(_critic._RETRY_ADDENDUM, self.messages()[0]["content"])
        self.outputs.extend(["我不能改写", ""])
        self.assertEqual(await self.rewrite(draft, persona=self.custom_persona()), draft)
        self.assertEqual(len(self.payloads), 4)
        self.assertEqual(_critic.get_stats()["rewrite_failed"], 1)

    async def test_http_failure_and_exempt_replies_keep_original(self):
        draft = "您好，很高兴为您服务。"
        self.outputs.append(_client.httpx.ConnectError("mock connection failure"))
        self.assertEqual(await self.rewrite(draft, persona=self.custom_persona()), draft)
        self.assertTrue(self.payloads)
        count = len(self.payloads)
        for reply in ("<<<CATTY_NO_REPLY>>>", "[图片]", "您好" + "正文" * 3001):
            self.assertEqual(await self.rewrite(reply, persona=self.custom_persona()), reply)
        self.assertEqual(len(self.payloads), count)


if __name__ == "__main__":
    unittest.main()
