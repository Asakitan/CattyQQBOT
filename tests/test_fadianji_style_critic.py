import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_fdj_style_critic_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_PATH = _ROOT / "src" / "catty_qq_ai" / "fadianji_style_critic.py"
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.fadianji_style_critic", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SC = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SC
_SPEC.loader.exec_module(_SC)


class FadianjiStyleCriticTests(unittest.TestCase):
    def setUp(self) -> None:
        _SC.reset_stats()

    def assertSuspect(self, reply: str, user_text: str = "") -> None:
        suspect, reasons = _SC.check_ai_smell(reply, user_text=user_text)
        self.assertTrue(suspect, reply)
        self.assertTrue(reasons, reply)

    def test_customer_service_words(self) -> None:
        self.assertSuspect("您好，很高兴为您服务，请问还有什么需要？")

    def test_unexpected_fragment_cannot_use_short_reply_exemption(self) -> None:
        suspect, reasons = _SC.check_ai_smell("咋了 ප්‍ර了", user_text="和我相亲", min_reply_chars=100)
        self.assertTrue(suspect)
        self.assertIn("无来源的异常字符片段", reasons)

    def test_expected_foreign_text_code_and_quotes_are_preserved(self) -> None:
        for reply, user_text, context in (
            ("这里的 ප්‍ර 是原文", "解释 ප්‍ර", ""),
            ("这里的 ප්‍ර 是原文", "解释上面的话", "群友写了 ප්‍ර"),
            ("翻译结果 ප්‍ර", "翻译成 Sinhala", ""),
            ("代码 `value = 'ප්‍ර'`", "检查代码", ""),
            ("hello 你好 こんにちは", "打个招呼", ""),
        ):
            with self.subTest(reply=reply):
                self.assertFalse(_SC.has_unexpected_script_fragment(reply, user_text=user_text, context_hint=context))

    def test_unexpected_fragment_uses_existing_rewrite_and_failure_fallback(self) -> None:
        for result, expected in (("你认真的？", "你认真的？"), ("咋了 ප්‍ර了", "网线抽了，再来")):
            with self.subTest(result=result), patch.object(_SC, "_post_chat_completion", new=AsyncMock(return_value=result)) as post:
                rewritten = self._run(_SC.rewrite_if_needed(
                    "咋了 ප්‍ර了", user_text="和我相亲", config=self._config(enabled=False),
                    fallback_reply="网线抽了，再来",
                ))
                self.assertEqual(rewritten, expected)
                post.assert_awaited_once()

    def test_unexpected_fragment_without_audit_does_not_leak_when_fallback_provided(self) -> None:
        self.assertEqual(self._run(_SC.rewrite_if_needed(
            "咋了 ප්‍ර了", user_text="和我相亲", config=self._config(configured=False),
            fallback_reply="网线抽了，再来",
        )), "网线抽了，再来")

    def test_structure_words(self) -> None:
        self.assertSuspect("首先看原因，其次看现象，最后再处理。")
        self.assertSuspect("总的来说，这件事就是这样。")

    def test_technical_structure_and_code_are_allowed(self) -> None:
        reply = "首先看日志，其次改配置，最后重启。\n```python\nprint('ok')\n```"
        suspect, reasons = _SC.check_ai_smell(reply, user_text="这个 Python 报错怎么处理")
        self.assertFalse(suspect)
        self.assertEqual(reasons, [])

    def test_markdown_heading_and_bullets(self) -> None:
        self.assertSuspect("## 处理结果\n这是一段说明文字")
        self.assertSuspect("- 第一项内容\n- 第二项内容\n- 第三项内容")

    def test_numbered_list(self) -> None:
        self.assertSuspect("1. 先看日志\n2. 再改配置\n3. 最后重启")

    def test_punctuation_smell(self) -> None:
        self.assertSuspect("这个回复真的很长。这里还有一句。最后又来一句。")
        self.assertSuspect("这也太夸张了！！")

    def test_non_technical_length_smell(self) -> None:
        self.assertSuspect("这是一段没有技术内容的普通闲聊回复" * 5)

    def test_technical_long_reply_is_exempt_from_length(self) -> None:
        reply = "这是一段关于代码排错的说明" * 8
        suspect, reasons = _SC.check_ai_smell(reply, user_text="这个代码报错怎么处理")
        self.assertFalse(suspect)
        self.assertNotIn("长度味：非技术问题回复超过 60 字", reasons)

    def test_nontechnical_substrings_do_not_trigger_technical_mode(self) -> None:
        for user_text in ("今天看网络节目怎么样", "你的接口人到了吗", "这个技术分享挺好看"):
            suspect, reasons = _SC.check_ai_smell("这是一段普通闲聊回复" * 8, user_text=user_text)
            self.assertTrue(suspect, user_text)
            self.assertIn("长度味：非技术问题回复超过 60 字", reasons)

    def test_emoji_mixing(self) -> None:
        self.assertSuspect("这个真的太开心了😭😂👍")

    def test_explanation_words(self) -> None:
        self.assertSuspect("让我来给你解释一下这个事情的原因")

    def test_real_short_fadianji_replies_have_no_false_positive(self) -> None:
        replies = [
            "谢谢宝宝", "滚啊", "好好好", "我去", "我靠", "好耶", "唉", "不知道",
            "没事", "哈哈", "有病吧", "神经病",
        ]
        for reply in replies:
            suspect, reasons = _SC.check_ai_smell(reply)
            self.assertFalse(suspect, reply)
            self.assertEqual(reasons, [], reply)

    def test_exempt_markers_and_pure_emoji_or_image(self) -> None:
        self.assertEqual(_SC.check_ai_smell("很长很长的文本" * 20 + "<<<CATTY_NO_REPLY>>>")[0], False)
        self.assertEqual(_SC.check_ai_smell("😭😭😂😂")[0], False)
        self.assertEqual(_SC.check_ai_smell("[图片]")[0], False)

    def test_adult_creative_request_bypasses_length_smell(self) -> None:
        # 主人 2026-08-15: 机机写小黄文是长散文, user_text 命中创作请求词 → 全预筛豁免
        long_fiction = "下播后的直播间灯还没关, 我趴在数位板上赶稿, 你的手顺着领口摸进来了" * 8
        for user_text in ("机机给我写一段小黄文", "来点涩的", "写长一点认真写", "开车 上车", "整一篇肉文"):
            suspect, reasons = _SC.check_ai_smell(long_fiction, user_text=user_text)
            self.assertFalse(suspect, user_text)
            self.assertEqual(reasons, [], user_text)

    def test_adult_content_reply_bypasses_length_smell(self) -> None:
        # 回复本体成人内容词 ≥2 → 豁免 (防接龙/续写轮 user 没带请求词)
        fiction = (
            "他的手指抵上乳头的时候我整个人抖了一下, 呻吟卡在喉咙里, "
            "大腿内侧全是汗, 高潮来得毫无预兆"
        )
        suspect, reasons = _SC.check_ai_smell(fiction, user_text="继续")
        self.assertFalse(suspect)
        self.assertEqual(reasons, [])

    def test_single_adult_word_not_enough_for_bypass(self) -> None:
        # 只有 1 个成人词 + 无请求词 → 仍走正常预筛 (防误豁免)
        suspect, reasons = _SC.check_ai_smell(
            "这是一段没有任何结构内容的普通闲聊长回复" * 8 + "高潮", user_text="讲个故事",
        )
        self.assertTrue(suspect)
        self.assertIn("长度味：非技术问题回复超过 60 字", reasons)

    def test_short_ai_reveal_and_customer_service_are_still_suspect(self) -> None:
        self.assertSuspect("我是AI")
        self.assertSuspect("您好呀")

    def _config(self, *, enabled: bool = True, configured: bool = True) -> SimpleNamespace:
        values = {"catty_style_critic_enabled": enabled}
        if configured:
            values.update({
                "catty_audit_ai_base_url": "https://audit.example/v1",
                "catty_audit_ai_api_key": "TOKEN",
                "catty_audit_ai_model": "audit-model",
            })
        return SimpleNamespace(**values)

    def _rewrite(self, reply: str, config: SimpleNamespace | None = None):
        return _SC.rewrite_if_needed(
            reply,
            user_text="你觉得这个怎么样",
            config=config or self._config(),
        )

    def test_rewrite_switch_off_returns_original(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock()) as mocked:
            result = self._run(self._rewrite(original, self._config(enabled=False)))
        self.assertEqual(result, original)
        mocked.assert_not_awaited()
        self.assertEqual(_SC.get_stats(), {"checked": 0, "suspect": 0, "rewritten": 0, "rewrite_failed": 0})

    def test_missing_audit_config_returns_original(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock()) as mocked:
            result = self._run(self._rewrite(original, self._config(configured=False)))
        self.assertEqual(result, original)
        mocked.assert_not_awaited()
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_suspect_calls_audit_and_cleans_prefix_and_quotes(self) -> None:
        mocked = AsyncMock(return_value='“改写：滚啊”')
        original = "您好，很高兴为您服务，请问还有什么需要？"
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, "滚啊")
        mocked.assert_awaited_once()
        kwargs = mocked.await_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://audit.example/v1")
        self.assertEqual(kwargs["api_key"], "TOKEN")
        self.assertEqual(kwargs["model"], "audit-model")
        self.assertEqual(kwargs["temperature"], 0.3)
        self.assertEqual(kwargs["max_tokens"], 1000)
        self.assertEqual(kwargs["timeout"], 20.0)
        self.assertEqual(kwargs["request_route"], "style_critic_rewrite")
        self.assertEqual(_SC.get_stats()["rewritten"], 1)

    def test_configured_audit_parameters_are_forwarded(self) -> None:
        mocked = AsyncMock(return_value="滚啊")
        config = self._config()
        config.catty_style_critic_min_reply_chars = 12
        config.catty_audit_ai_temperature = 0.7
        config.catty_audit_ai_max_tokens = 77
        config.catty_audit_ai_request_timeout = 9.0
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite("您好，很高兴为您服务，请问还有什么需要？", config))
        self.assertEqual(result, "滚啊")
        kwargs = mocked.await_args.kwargs
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["max_tokens"], 77)
        self.assertEqual(kwargs["timeout"], 9.0)

    def test_timeout_keeps_20_seconds_when_config_is_missing(self) -> None:
        mocked = AsyncMock(return_value="滚啊")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            self._run(self._rewrite("您好，很高兴为您服务，请问还有什么需要？"))
        self.assertEqual(mocked.await_args.kwargs["timeout"], 20.0)

    def test_full_reply_and_tool_context_are_sent_without_markers(self) -> None:
        tail = "尾部事实必须完整保留: result-tail-987654"
        original = "您好，很高兴为您服务。" + ("普通内容。" * 90) + tail
        mocked = AsyncMock(return_value="滚啊")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            self._run(
                _SC.rewrite_if_needed(
                    original,
                    user_text="请查一下 <<<CATTY_CB:open:plan:test>>>",
                    config=self._config(),
                    tool_result_texts=["工具成功: port=25565 <<<CATTY_FD_MOOD:success>>>", "tool failed"],
                    context_hint="工具事实以结果为准 <<<CATTY_CB:done:cb1>>>",
                )
            )
        body = mocked.await_args.kwargs["messages"][1]["content"]
        protected_original, protected_values = _SC._protect_opaque_spans(original)
        self.assertIn(protected_original, body)
        self.assertIn("result-tail-987654", protected_values)
        self.assertIn("尾部事实必须完整保留:", body)
        self.assertIn("工具成功: port=25565", body)
        self.assertIn("工具事实以结果为准", body)
        self.assertNotIn("CATTY_CB", body)
        self.assertNotIn("CATTY_FD_MOOD", body)

    def test_long_reply_skips_audit_instead_of_truncating(self) -> None:
        original = "您好，很高兴为您服务。" + ("完整正文。" * 1300)
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock()) as mocked:
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        mocked.assert_not_awaited()

    def test_opaque_fields_are_restored_byte_for_byte(self) -> None:
        original = (
            "您好，请看 https://example.com/api/v1?a=2，`foo(25565)`，\n"
            "```python\nprint('v2')\n```\n"
            "公式 \\(x=\\frac{1}{2}\\)，路径 C:\\Catty\\src\\main.py 和 /opt/catty/run.py，"
            "函数 handle_reply()，版本 v1.2.3，端口 25565，"
            "<<<CATTY_INLINE_IMAGE:https://img.example/a.png>>> <<<CATTY_REPLY_SPLIT>>>"
        )

        async def _rewrite_with_tokens(**kwargs):
            body = kwargs["messages"][1]["content"]
            tokens = re.findall(r"__CATTY_OPAQUE_\d{4}__", body)
            self.assertNotIn("https://example.com/api/v1?a=2", body)
            self.assertNotIn("CATTY_INLINE_IMAGE", body)
            return "改写后的 prose\n" + " ".join(tokens)

        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(side_effect=_rewrite_with_tokens)):
            result = self._run(self._rewrite(original))
        for fragment in (
            "https://example.com/api/v1?a=2", "`foo(25565)`", "```python\nprint('v2')\n```",
            "\\(x=\\frac{1}{2}\\)", "C:\\Catty\\src\\main.py", "/opt/catty/run.py",
            "handle_reply()", "v1.2.3", "25565",
            "<<<CATTY_INLINE_IMAGE:https://img.example/a.png>>>", "<<<CATTY_REPLY_SPLIT>>>",
        ):
            self.assertIn(fragment, result)

    def test_missing_opaque_placeholder_falls_back_to_original(self) -> None:
        original = "您好，请看 https://example.com/a 和端口 25565"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(return_value="滚啊")):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_tool_fact_polarity_reversal_falls_back(self) -> None:
        original = "您好，工具已经成功生成 output.json"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(return_value="我去，失败了")):
            result = self._run(
                _SC.rewrite_if_needed(
                    original,
                    user_text="结果呢",
                    config=self._config(),
                    tool_result_texts=['{"ok": true, "file": "output.json", "status": "success"}'],
                )
            )
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_success_json_with_null_error_reversal_falls_back(self) -> None:
        original = "您好，工具结果已收到"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(return_value="我去，失败了")):
            result = self._run(
                _SC.rewrite_if_needed(
                    original,
                    user_text="结果呢",
                    config=self._config(),
                    tool_result_texts=['{"ok": true, "error": null, "status": "success"}'],
                )
            )
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_failure_json_with_error_reversal_falls_back(self) -> None:
        original = "您好，工具结果已收到"
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(return_value="好耶，成功了")):
            result = self._run(
                _SC.rewrite_if_needed(
                    original,
                    user_text="结果呢",
                    config=self._config(),
                    tool_result_texts=['{"ok": false, "error": "failed"}'],
                )
            )
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_ordinary_image_opaque_fields_are_restored_byte_for_byte(self) -> None:
        images = (
            "[图片:plot.png]",
            "[图片]",
            "<image:diagram.png>",
            "![alt](https://example.com/a.png)",
        )
        for image in images:
            original = f"您好，请看 {image}"

            async def _rewrite_with_tokens(**kwargs):
                body = kwargs["messages"][1]["content"]
                tokens = re.findall(r"__CATTY_OPAQUE_\d{4}__", body)
                return "改写后的 prose\n" + " ".join(tokens)

            with self.subTest(image=image), patch.object(
                _SC, "_post_chat_completion", new=AsyncMock(side_effect=_rewrite_with_tokens)
            ):
                result = self._run(self._rewrite(original))
            self.assertIn(image, result)

    def test_duplicate_or_out_of_order_opaque_placeholder_falls_back(self) -> None:
        original = "您好，请看 https://example.com/a 和端口 25565"

        async def _bad_rewrite(**kwargs):
            tokens = re.findall(r"__CATTY_OPAQUE_\d{4}__", kwargs["messages"][1]["content"])
            return "改写 " + " ".join(reversed(tokens)) + " " + tokens[0]

        with patch.object(_SC, "_post_chat_completion", new=AsyncMock(side_effect=_bad_rewrite)):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_audit_exception_returns_original(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        mocked = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_bad_rewrite_with_customer_service_words_is_discarded(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        mocked = AsyncMock(return_value="您好，感谢您的咨询")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        self.assertEqual(_SC.get_stats()["rewritten"], 0)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    def test_pass_reply_does_not_call_api(self) -> None:
        with patch.object(_SC, "_post_chat_completion", new=AsyncMock()) as mocked:
            result = self._run(self._rewrite("谢谢宝宝"))
        self.assertEqual(result, "谢谢宝宝")
        mocked.assert_not_awaited()
        self.assertEqual(_SC.get_stats(), {"checked": 1, "suspect": 0, "rewritten": 0, "rewrite_failed": 0})

    def test_nsfw_user_text_gets_adult_editor_prompt(self) -> None:
        # 2026-10-25: 露骨输入照常走质检, 但 system 换成成人 RP 内部润色指令, 不免检。
        draft = "（翅膀呼地一下张开挡在身前）首先按住衣角，其次后退半步，最后咬着下唇看你。"
        mocked = AsyncMock(return_value="滚啊")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(
                _SC.rewrite_if_needed(
                    draft, user_text="把大几把顶住你的蜜穴", config=self._config(),
                )
            )
        self.assertEqual(result, "滚啊")
        mocked.assert_awaited_once()
        system = mocked.await_args.kwargs["messages"][0]["content"]
        self.assertIn("角色扮演", system)
        self.assertIn("机机口吻", system)
        self.assertIn("保持草稿的长度", system)
        body = mocked.await_args.kwargs["messages"][1]["content"]
        self.assertIn("对方刚说", body)

    def test_nsfw_context_flag_forces_adult_editor_prompt(self) -> None:
        draft = "首先按住衣角，其次后退半步，最后咬着下唇看你。"
        mocked = AsyncMock(return_value="滚啊")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(
                _SC.rewrite_if_needed(
                    draft, user_text="陪我", config=self._config(), nsfw_context=True,
                )
            )
        self.assertEqual(result, "滚啊")
        system = mocked.await_args.kwargs["messages"][0]["content"]
        self.assertIn("角色扮演", system)

    def test_nsfw_reply_gets_rewritten_not_refused(self) -> None:
        draft = "（耳尖发烫）首先往后缩，其次把领口按住，最后小声说再往前一步我真电你了，插进来就插进来吧。"
        mocked = AsyncMock(return_value="（耳尖发烫）滚啊 再往前一步试试")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(
                _SC.rewrite_if_needed(draft, user_text="继续", config=self._config())
            )
        self.assertEqual(result, "（耳尖发烫）滚啊 再往前一步试试")
        self.assertEqual(_SC.get_stats()["rewritten"], 1)

    def test_freeform_nsfw_uses_length_preserving_prompt(self) -> None:
        # freeform 人格 (机机) 只顺口吻, 不压短、不套模板
        draft = "（耳尖发烫）首先按住衣角，其次往后缩，最后小声说再往前一步我真电你了，插进来就插进来吧。"
        mocked = AsyncMock(return_value="（耳尖发烫）好长一段自由发挥的机机回复…")
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(
                _SC.rewrite_if_needed(
                    draft, user_text="把大几把顶住你的蜜穴", config=self._config(),
                    freeform=True,
                )
            )
        self.assertEqual(result, "（耳尖发烫）好长一段自由发挥的机机回复…")
        system = mocked.await_args.kwargs["messages"][0]["content"]
        self.assertIn("不许压短", system)
        self.assertIn("保留草稿的长度", system)

    def test_audit_refusal_retries_once_then_uses_rewrite(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        refusal = (
            "这个我没法改——原回复是露骨性内容，我不能参与改写或润色\n"
            "如果这是角色扮演场景，建议换成非性化的互动。"
        )
        mocked = AsyncMock(side_effect=[refusal, "滚啊"])
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(
                _SC.rewrite_if_needed(original, user_text="把大几把顶住你的蜜穴", config=self._config())
            )
        self.assertEqual(result, "滚啊")
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(_SC.get_stats()["rewritten"], 1)

    def test_empty_rewrite_retries_once(self) -> None:
        # 思考模型 max_tokens 偏小时会只出 reasoning、正文为空 → 重试一次
        original = "您好，很高兴为您服务，请问还有什么需要？"
        mocked = AsyncMock(side_effect=["", "滚啊"])
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, "滚啊")
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(_SC.get_stats()["rewritten"], 1)

    def test_audit_refusal_twice_keeps_original_reply(self) -> None:
        original = "您好，很高兴为您服务，请问还有什么需要？"
        refusal = "我没法改写这种露骨性内容，建议换个方向。"
        mocked = AsyncMock(return_value=refusal)
        with patch.object(_SC, "_post_chat_completion", new=mocked):
            result = self._run(self._rewrite(original))
        self.assertEqual(result, original)
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(_SC.get_stats()["rewritten"], 0)
        self.assertEqual(_SC.get_stats()["rewrite_failed"], 1)

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()