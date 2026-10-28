"""catty_scan_messages / catty_look_image 与全量工具暴露 (2026-10-26)。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_tool_scan_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "tools.py"
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.tools", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_tools = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _tools
_SPEC.loader.exec_module(_tools)


def _config(**overrides) -> types.SimpleNamespace:
    base = dict(
        catty_tools_enabled=True,
        catty_tools_full_exposure=True,
        catty_tools_lazy_schema_enabled=False,
        catty_tools_disabled_in_private=[],
        catty_image_search_enabled=True,
        catty_image_search_cooldown_seconds=0,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _message(seq, text, *, sender="阿明", user_id="10001", age=30.0, images=(), is_bot=False):
    return {
        "seq": seq,
        "user_id": user_id,
        "display_name": sender,
        "text": text,
        "is_bot": is_bot,
        "age_seconds": age,
        "images": [
            {"url": url, "key": f"key-{seq}-{index}"}
            for index, url in enumerate(images)
        ],
    }


def _ctx(**overrides) -> _tools.ToolContext:
    base = dict(
        config=_config(),
        memory_store=None,
        event=None,
        scope_key="group:42",
        recent_image_urls=[
            "https://cdn.example.invalid/newest.png",
            "https://cdn.example.invalid/older.png",
        ],
        recent_messages=[
            _message(1, "今天画了一只猫", age=600.0),
            _message(
                2,
                "看这个效果图",
                age=120.0,
                images=["https://cdn.example.invalid/effect.png"],
            ),
            _message(3, "这个梗好笑", sender="小红", user_id="10002", age=10.0),
            _message(4, "刚生成的图", age=5.0, images=[
                "https://cdn.example.invalid/a.png",
                "https://cdn.example.invalid/b.png",
            ]),
        ],
    )
    base.update(overrides)
    return _tools.ToolContext(**base)


class ScanMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_and_has_image_filters(self) -> None:
        result = await _tools._exec_scan_messages({"query": "图"}, _ctx())
        text = str(result)
        self.assertIn("m2", text)
        self.assertIn("m4", text)
        self.assertNotIn("m3", text)

        images_only = await _tools._exec_scan_messages({"has_image": True}, _ctx())
        refs = [item["ref"] for item in images_only["messages"]]
        self.assertEqual(refs, ["m4", "m2"])
        self.assertEqual(images_only["image_count"], 3)

    async def test_sender_limit_and_minutes(self) -> None:
        by_sender = await _tools._exec_scan_messages({"sender": "小红"}, _ctx())
        self.assertEqual([item["ref"] for item in by_sender["messages"]], ["m3"])

        limited = await _tools._exec_scan_messages({"limit": 2}, _ctx())
        self.assertEqual(len(limited["messages"]), 2)

        recent = await _tools._exec_scan_messages({"minutes": 3}, _ctx())
        refs = [item["ref"] for item in recent["messages"]]
        self.assertEqual(refs, ["m4", "m3", "m2"])

    async def test_empty_buffer_returns_empty_result(self) -> None:
        result = await _tools._exec_scan_messages({}, _ctx(recent_messages=[]))
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["messages"], [])

    async def test_images_keep_same_name_senders_and_bot_identity_separate(self) -> None:
        shared = "https://cdn.example.invalid/shared.png"
        ctx = _ctx(recent_messages=[
            _message(1, "第一张", sender="同名", user_id="101", images=[shared]),
            _message(2, "第二张", sender="同名", user_id="202", images=[shared], is_bot=True),
        ])
        result = await _tools._exec_scan_messages({"has_image": True}, ctx)
        self.assertEqual(result["scope"], "group:42")
        self.assertEqual([item["ref"] for item in result["messages"]], ["m2", "m1"])
        self.assertEqual([item["sender"] for item in result["messages"]], ["同名", "同名"])
        self.assertEqual([item["user_id"] for item in result["messages"]], ["202", "101"])
        for message in result["messages"]:
            image = message["images"][0]
            self.assertEqual(image["url"], shared)
            self.assertEqual(image["source"], {
                "message_ref": message["ref"],
                "image_ref": image["image_ref"],
                "user_id": message["user_id"],
                "display_name": message["sender"],
                "is_bot": message["is_bot"],
            })


class LookImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_only_input_is_visible_to_both_tools(self) -> None:
        # 调用方将引用图并入 input_image_urls；当前消息自身可以没有附图。
        quoted = "https://cdn.example.invalid/quoted.png"
        ctx = _ctx(input_image_urls=[quoted], recent_image_urls=[],
                   describe_images_fn=AsyncMock(return_value="引用图"))
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))) as search:
            looked = await _tools._exec_look_image({"image_ref": "0"}, ctx)
            searched = await _tools._exec_image_search({"kind": "artwork"}, ctx)
        self.assertEqual(looked["image_url"], quoted)
        self.assertEqual(searched["image_url"], quoted)
        search.assert_awaited_once_with(ctx.config, quoted, kind="artwork", engines=None)

    async def test_mixed_candidates_have_identical_stable_deduplicated_order(self) -> None:
        current = "https://cdn.example.invalid/current.png"
        quoted = "https://cdn.example.invalid/quoted.png"
        recent = "https://cdn.example.invalid/recent.png"
        older = "https://cdn.example.invalid/older.png"
        ctx = _ctx(input_image_urls=[current, quoted, current],
                   recent_image_urls=[quoted, recent, older, recent],
                   describe_images_fn=AsyncMock(return_value="已看图"))
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            for index, expected in enumerate([current, quoted, recent, older]):
                with self.subTest(index=index):
                    looked = await _tools._exec_look_image({"image_ref": str(index)}, ctx)
                    searched = await _tools._exec_image_search({"kind": "artwork", "image_index": index}, ctx)
                    self.assertEqual(looked["image_url"], expected)
                    self.assertEqual(searched["image_url"], expected)
                    self.assertEqual(searched["candidate_count"], 4)

    async def test_message_reference_can_be_passed_to_both_tools(self) -> None:
        ctx = _ctx(describe_images_fn=AsyncMock(return_value="已看图"))
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            looked = await _tools._exec_look_image({"image_ref": "m4#1"}, ctx)
            searched = await _tools._exec_image_search({"kind": "artwork", "image_url": "m4#1"}, ctx)
        self.assertEqual(looked["image_url"], "https://cdn.example.invalid/b.png")
        self.assertEqual(searched["image_url"], looked["image_url"])

    async def test_invalid_explicit_message_reference_does_not_search_an_unrelated_image(self) -> None:
        with patch.object(_tools, "reverse_image_search", new_callable=AsyncMock) as search:
            for ref in ("m99#0", "m4#99"):
                result = await _tools._exec_image_search({"kind": "artwork", "image_url": ref}, _ctx())
                self.assertIn("error", result)
        search.assert_not_awaited()

    async def test_explicit_url_takes_priority_over_numeric_index(self) -> None:
        direct = "https://cdn.example.invalid/direct.png"
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            result = await _tools._exec_image_search(
                {"kind": "artwork", "image_url": direct, "image_index": 1}, _ctx()
            )
        self.assertEqual(result["image_url"], direct)

    async def test_message_ref_resolves_and_calls_describe(self) -> None:
        calls: list[tuple[list[str], str]] = []

        async def describe(urls, question):
            calls.append((list(urls), question))
            return "画面里是一只白猫趴在窗台上"

        ctx = _ctx(describe_images_fn=describe)
        result = await _tools._exec_look_image(
            {"image_ref": "m4#1", "question": "图里有什么"}, ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["image_url"], "https://cdn.example.invalid/b.png")
        self.assertEqual(calls, [(["https://cdn.example.invalid/b.png"], "图里有什么")])

    async def test_url_and_recent_index_refs(self) -> None:
        seen: list[str] = []

        async def describe(urls, _question):
            seen.append(urls[0])
            return "ok"

        ctx = _ctx(describe_images_fn=describe)
        await _tools._exec_look_image(
            {"image_ref": "https://cdn.example.invalid/direct.png"}, ctx
        )
        await _tools._exec_look_image({"image_ref": "1"}, ctx)
        self.assertEqual(
            seen,
            [
                "https://cdn.example.invalid/direct.png",
                "https://cdn.example.invalid/older.png",
            ],
        )

    async def test_invalid_refs_and_missing_callback_are_errors(self) -> None:
        ctx = _ctx(describe_images_fn=None)
        for ref in ("m99#0", "m4#5", "nonsense"):
            with self.subTest(ref=ref):
                result = await _tools._exec_look_image({"image_ref": ref}, ctx)
                self.assertTrue(result.get("error"))

        missing = await _tools._exec_look_image(
            {"image_ref": "m4#0"}, _ctx(describe_images_fn=None)
        )
        self.assertIn("看图能力未接入", missing["error"])


class ImageSourceReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_catty_search_guidance_keeps_source_priority_without_owner_address(self) -> None:
        hit = types.SimpleNamespace(
            source="yandex", title="图片来源", url="https://x.com/example/status/1",
            similarity=83.0, author="example", kind="photo", extra={"is_x_twitter": True},
        )
        ctx = _ctx(persona=types.SimpleNamespace(name="catty"))
        errors = {"yandex": "YandexRegionBlockedError"}
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([hit], errors))):
            result = await _tools._exec_image_search({"kind": "photo"}, ctx)
        self.assertTrue(result["yandex_blocked"])
        self.assertEqual(result["x_twitter_urls"], [hit.url])
        self.assertIn("saucenao 或 source=yandex", result["guidance"])
        for field in ("guidance", "yandex_blocked_hint", "context_text"):
            self.assertNotIn("主人", result[field])
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], errors))):
            empty = await _tools._exec_image_search({"kind": "photo"}, ctx)
        self.assertNotIn("主人", empty["guidance"])
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([hit], errors))):
            fdj_result = await _tools._exec_image_search(
                {"kind": "photo"}, _ctx(persona=types.SimpleNamespace(name="fadianji")),
            )
        self.assertNotIn("笨猫人格", fdj_result["context_text"])

    def _shared_context(self) -> tuple[_tools.ToolContext, str, str, str]:
        current = "https://cdn.example.invalid/current.png"
        shared = "https://cdn.example.invalid/shared.png?x=%2F&y=1"
        other = "https://cdn.example.invalid/other.png"
        ctx = _ctx(
            input_image_urls=[current, shared, current],
            recent_image_urls=[shared, other, shared],
            recent_messages=[
                _message(4, "第一张", sender="同名", user_id="101", images=[shared]),
                _message(5, "第二张", sender="同名", user_id="202", images=[shared], is_bot=True),
                _message(6, "第三张", sender="第三人", user_id="303", images=[other, shared]),
            ],
            describe_images_fn=AsyncMock(return_value="画面描述"),
        )
        return ctx, current, shared, other

    async def test_exact_reference_selects_one_event_even_when_url_is_shared(self) -> None:
        ctx, _current, shared, _other = self._shared_context()
        expected = {
            "message_ref": "m6",
            "image_ref": "m6#1",
            "user_id": "303",
            "display_name": "第三人",
            "is_bot": False,
        }
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))) as search:
            looked = await _tools._exec_look_image({"image_ref": "m6#1", "question": "看看"}, ctx)
            searched = await _tools._exec_image_search(
                {"kind": "artwork", "image_url": "m6#1", "image_index": 2}, ctx
            )
        for result in (looked, searched):
            self.assertEqual(result["image_url"], shared)
            self.assertEqual(result["source_scope"], "group:42")
            self.assertEqual(result["image_source_status"], "message_ref")
            self.assertEqual(result["image_sources"], [expected])
        self.assertEqual(searched["image_index"], 0)
        ctx.describe_images_fn.assert_awaited_once_with([shared], "看看")
        search.assert_awaited_once_with(ctx.config, shared, kind="artwork", engines=None)
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            bot_looked = await _tools._exec_look_image({"image_ref": "m5#0"}, ctx)
            bot_searched = await _tools._exec_image_search({"kind": "artwork", "image_url": "m5#0"}, ctx)
        for result in (bot_looked, bot_searched):
            self.assertEqual(result["image_sources"], [{
                "message_ref": "m5", "image_ref": "m5#0", "user_id": "202",
                "display_name": "同名", "is_bot": True,
            }])

    async def test_url_and_index_return_all_visible_matches_without_unique_claim(self) -> None:
        ctx, _current, shared, _other = self._shared_context()
        expected = [
            {"message_ref": "m4", "image_ref": "m4#0", "user_id": "101", "display_name": "同名", "is_bot": False},
            {"message_ref": "m5", "image_ref": "m5#0", "user_id": "202", "display_name": "同名", "is_bot": True},
            {"message_ref": "m6", "image_ref": "m6#1", "user_id": "303", "display_name": "第三人", "is_bot": False},
        ]
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            results = [
                await _tools._exec_look_image({"image_ref": shared}, ctx),
                await _tools._exec_look_image({"image_ref": "1"}, ctx),
                await _tools._exec_image_search({"kind": "artwork", "image_url": shared}, ctx),
                await _tools._exec_image_search({"kind": "artwork", "image_index": 1}, ctx),
            ]
        for result in results:
            self.assertEqual(result["image_url"], shared)
            self.assertEqual(result["image_source_status"], "url_matches")
            self.assertEqual(result["image_sources"], expected)
            self.assertNotIn("source_user_id", result)

    async def test_candidates_keep_current_quote_recent_order_and_exact_urls(self) -> None:
        current = "https://cdn.example.invalid/current.png?encoded=%2F"
        quoted = "https://cdn.example.invalid/quoted.png?encoded=%2B"
        recent = "https://cdn.example.invalid/recent.png?encoded=%3D"
        ctx = _ctx(
            input_image_urls=[current, quoted, current],
            recent_image_urls=[quoted, recent, recent],
            recent_messages=[
                _message(1, "当前", user_id="101", images=[current]),
                _message(2, "引用", user_id="202", images=[quoted]),
                _message(3, "较早", user_id="303", images=[recent]),
            ],
            describe_images_fn=AsyncMock(return_value="画面描述"),
        )
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))) as search:
            for index, (url, uid) in enumerate(((current, "101"), (quoted, "202"), (recent, "303"))):
                looked = await _tools._exec_look_image({"image_ref": str(index)}, ctx)
                searched = await _tools._exec_image_search({"kind": "artwork", "image_index": index}, ctx)
                self.assertEqual(looked["image_url"], url)
                self.assertEqual(searched["image_url"], url)
                self.assertEqual(searched["image_index"], index)
                self.assertEqual(searched["candidate_count"], 3)
                self.assertEqual(looked["image_sources"][0]["user_id"], uid)
                self.assertEqual(searched["image_sources"], looked["image_sources"])
                self.assertEqual(search.await_args.args[1], url)
                self.assertEqual(ctx.describe_images_fn.await_args.args[0], [url])

    async def test_unmatched_url_does_not_guess_current_sender(self) -> None:
        url = "https://cdn.example.invalid/unseen.png"
        event = types.SimpleNamespace(user_id="requester")
        ctx = _ctx(
            config=_config(catty_user_titles={}),
            event=event,
            input_image_urls=[url],
            recent_image_urls=[],
            recent_messages=[],
            describe_images_fn=AsyncMock(return_value="画面描述"),
        )
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            results = [
                await _tools._exec_look_image({"image_ref": url}, ctx),
                await _tools._exec_look_image({"image_ref": "0"}, ctx),
                await _tools._exec_image_search({"kind": "artwork", "image_url": url}, ctx),
                await _tools._exec_image_search({"kind": "artwork", "image_index": 0}, ctx),
            ]
        for result in results:
            self.assertEqual(result["image_source_status"], "unknown")
            self.assertEqual(result["image_sources"], [])
            self.assertNotIn("requester", str(result["image_sources"]))

    async def test_missing_sender_fields_remain_unknown(self) -> None:
        url = "https://cdn.example.invalid/partial.png"
        ctx = _ctx(
            recent_messages=[{"seq": 7, "images": [{"url": url}]}],
            describe_images_fn=AsyncMock(return_value="画面描述"),
        )
        result = await _tools._exec_look_image({"image_ref": "m7#0"}, ctx)
        self.assertEqual(result["image_source_status"], "message_ref")
        self.assertEqual(result["image_sources"], [{
            "message_ref": "m7", "image_ref": "m7#0", "user_id": "",
            "display_name": "", "is_bot": None,
        }])
        scanned = await _tools._exec_scan_messages({"has_image": True}, ctx)
        self.assertIsNone(scanned["messages"][0]["is_bot"])
        self.assertEqual(scanned["messages"][0]["images"][0]["source"], result["image_sources"][0])

    async def test_scope_isolation_including_private_conversation(self) -> None:
        shared = "https://cdn.example.invalid/shared.png"
        message = Message("hello")
        event = _tools.PrivateMessageEvent(
            time=0, self_id=999, post_type="message", sub_type="friend",
            user_id=303, message_type="private", message_id=1,
            message=message, original_message=message, raw_message="hello", font=0,
            sender=Sender(user_id=303, nickname="私聊者", card=""),
        )
        contexts = [
            _ctx(scope_key="group:1", recent_messages=[
                _message(7, "群一", user_id="101", images=[shared]),
            ]),
            _ctx(scope_key="group:2", recent_messages=[
                _message(7, "群二", user_id="202", images=[shared]),
                _message(8, "群二另一张", user_id="202", images=[shared]),
            ]),
            _ctx(scope_key="private:303", event=event, config=_config(catty_user_titles={}),
                 recent_messages=[_message(7, "私聊", user_id="303", images=[shared])]),
        ]
        self.assertTrue(contexts[2].is_private)
        with patch.object(_tools, "reverse_image_search", new=AsyncMock(return_value=([], {}))):
            for ctx, expected_ids in zip(contexts, (["101"], ["202", "202"], ["303"])):
                ctx.input_image_urls = [shared]
                ctx.recent_image_urls = []
                ctx.describe_images_fn = AsyncMock(return_value="画面描述")
                looked = await _tools._exec_look_image({"image_ref": shared}, ctx)
                searched = await _tools._exec_image_search({"kind": "artwork", "image_index": 0}, ctx)
                for result in (looked, searched):
                    self.assertEqual(result["source_scope"], ctx.scope_key)
                    self.assertEqual([source["user_id"] for source in result["image_sources"]], expected_ids)
                exact_looked = await _tools._exec_look_image({"image_ref": "m7#0"}, ctx)
                exact_searched = await _tools._exec_image_search(
                    {"kind": "artwork", "image_url": "m7#0"}, ctx
                )
                for result in (exact_looked, exact_searched):
                    self.assertEqual(result["source_scope"], ctx.scope_key)
                    self.assertEqual(result["image_source_status"], "message_ref")
                    self.assertEqual([source["user_id"] for source in result["image_sources"]], expected_ids[:1])
            missing = await _tools._exec_look_image({"image_ref": "m8#0"}, contexts[0])
            self.assertIn("error", missing)
            missing_search = await _tools._exec_image_search(
                {"kind": "artwork", "image_url": "m8#0"}, contexts[0]
            )
            self.assertIn("error", missing_search)

    async def test_invalid_reference_and_cooldown_do_not_search_another_image(self) -> None:
        ctx = _ctx(
            config=_config(catty_image_search_cooldown_seconds=60),
            recent_messages=[_message(4, "非 URL", images=["file://not-an-http-url"])],
        )
        with patch.object(_tools, "reverse_image_search", new_callable=AsyncMock) as search:
            invalid = await _tools._exec_image_search({"kind": "artwork", "image_url": "m4#0"}, ctx)
            self.assertIn("error", invalid)
            with patch.dict(_tools._image_search_cooldowns, {"anonymous": 100.0}, clear=True):
                with patch.object(_tools.time, "monotonic", return_value=110.0):
                    cooldown = await _tools._exec_image_search({"kind": "artwork"}, ctx)
            self.assertIn("冷却", cooldown["error"])
        search.assert_not_awaited()


class FullExposureTests(unittest.TestCase):
    def test_all_tools_are_exposed_without_direct_request(self) -> None:
        names = {
            (schema.get("function") or {}).get("name")
            for schema in _tools.available_tool_schemas(
                _config(),
                is_private=False,
                user_text="普通闲聊",
                is_directly_requested=False,
            )
        }
        for name in (
            "catty_imagegen",
            "catty_write_file",
            "catty_nai_director",
            "catty_scan_messages",
            "catty_look_image",
        ):
            with self.subTest(tool=name):
                self.assertIn(name, names)
        self.assertEqual(
            set(_tools.ALL_TOOL_SCHEMAS) - names,
            # 结构性/安全闸门保留: 群里不给 NSFW 搜索; 沙箱代码工具只给拥有者直呼;
            # catty_run_code 由 legacy 开关单独控制 (默认关);
            # 语音工具只在配置了音色的人格下暴露 (默认仅机机)
            {
                "catty_nsfw_search",
                "catty_run_code",
                "catty_python_run",
                "catty_python_install",
                "catty_voice_say",
            },
        )

    def test_image_search_without_image_points_to_scan(self) -> None:
        result = asyncio.run(
            _tools._exec_image_search({"kind": "artwork"}, _ctx(recent_image_urls=[]))
        )
        self.assertIn("catty_scan_messages", result["error"])
        self.assertIn("catty_scan_messages", result["guidance"])


if __name__ == "__main__":
    unittest.main()
