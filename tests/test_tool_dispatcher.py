from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_tool_dispatcher_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)


def _load_module(name: str):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_tools = _load_module("tools")
_client = _load_module("openai_client")
_cascade = _load_module("tool_cascade")


def _config() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        catty_tools_enabled=True,
        catty_tools_lazy_schema_enabled=True,
        # 主人 2026-08-13: 本测试文件多数用例写的是旧 intent-gate 行为,
        # 默认固定回切模式; 全量暴露默认行为由专门用例锁定。
        catty_tools_full_exposure=False,
        catty_tools_disabled_in_private=[],
        catty_anthropic_native_enabled=False,
        catty_openai_base_url="https://cloud.example.com/v1",
        catty_openai_api_key="cloud-key",
        catty_openai_model="cloud-model",
        catty_openai_extra_headers={},
        catty_openai_extra_body={},
        catty_temperature=0.7,
        catty_max_tokens=100,
        catty_request_timeout=60.0,
        catty_http_proxy="",
        catty_prompt_cache_enabled=False,
        catty_prompt_cache_depth=2,
        catty_profile_memory_enabled=True,
        catty_scope_meme_enabled=True,
        catty_timeline_enabled=True,
        catty_adaptive_prompt_enabled=True,
    )


class _Persona:
    def __init__(self, *disabled: str) -> None:
        self._disabled = set(disabled)

    def feature_disabled(self, name: str) -> bool:
        return name in self._disabled


def _schema_names(schemas: list[dict]) -> set[str]:
    return {
        str((schema.get("function") or {}).get("name") or schema.get("name") or "")
        for schema in schemas
    }


class ToolCapabilityTests(unittest.TestCase):
    def test_harness_guide_uses_current_persona_name(self) -> None:
        for name, char_name in (("catty", "笨猫"), ("fadianji", "机机"), ("custom", "阿青")):
            persona = types.SimpleNamespace(
                name=name, char_name=char_name, semantic_harness_enabled=True,
            )
            hint = _tools.tools_system_hint(persona)
            self.assertIn(f"【{char_name}·证据使用指南】", hint)
            if name != "fadianji":
                self.assertNotIn("【机机·证据使用指南】", hint)

    def test_scope_image_direct_and_persona_gates(self) -> None:
        cfg = _config()

        group_nsfw = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="pixiv 色图",
                is_directly_requested=True,
            )
        )
        self.assertNotIn("catty_nsfw_search", group_nsfw)

        private_group_tool = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=True,
                user_text="这群玩什么",
                is_directly_requested=True,
            )
        )
        self.assertNotIn("catty_group_game_tag", private_group_tool)

        disabled_story = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="开 arc 记一个故事",
                is_directly_requested=True,
                persona=_Persona("story_arc"),
            )
        )
        self.assertNotIn("catty_story_arc_set", disabled_story)

        image_not_direct = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="帮忙查作者和修一下",
                has_image=True,
                is_directly_requested=False,
            )
        )
        self.assertIn("catty_image_search", image_not_direct)
        # 主人 2026-10-26: 工具全量交给模型 — 改图工具不再靠 @/直呼才暴露
        self.assertIn("catty_nai_director", image_not_direct)

    def test_fadianji_nsfw_feature_gates_schema_and_executor(self) -> None:
        cfg = _config()
        fadianji = types.SimpleNamespace(
            name="fadianji",
            feature_disabled=lambda feature: feature == "nsfw_spark",
        )
        schemas = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=True,
                user_text="pixiv 色图",
                is_directly_requested=True,
                persona=fadianji,
            )
        )
        self.assertNotIn("catty_nsfw_search", schemas)

        calls: list[dict] = []

        async def fake_executor(args, _ctx):
            calls.append(args)
            return {"ok": True}

        ctx = types.SimpleNamespace(
            config=cfg,
            persona=fadianji,
            is_private=True,
            group_id="",
            input_image_urls=[],
            recent_image_urls=[],
            is_directly_requested=True,
            user_text="",
            event=None,
        )
        with patch.dict(
            _tools._EXECUTORS,
            {"catty_nsfw_search": fake_executor},
            clear=False,
        ):
            denied = asyncio.run(
                _tools.execute_tool_call(
                    "catty_nsfw_search",
                    '{"kind": "image", "query": "test"}',
                    ctx,
                    allowed_names={"catty_nsfw_search"},
                )
            )

        self.assertEqual(denied["error"], "tool_not_allowed")
        self.assertEqual(denied["reason"], "persona_feature_disabled")
        self.assertEqual(calls, [])

    def test_explicit_image_url_enables_search_but_not_director(self) -> None:
        schemas = _schema_names(
            _tools.available_tool_schemas(
                _config(),
                is_private=False,
                user_text=(
                    "帮我查作者 https://cdn.example.invalid/art/sample.webp?size=large"
                ),
                has_image=False,
                is_directly_requested=True,
            )
        )

        self.assertIn("catty_image_search", schemas)
        # 回切 intent-gate 模式仍按关键词命中; 全量暴露由 full_exposure 用例锁定
        self.assertNotIn("catty_nai_director", schemas)

        url_only = _schema_names(
            _tools.available_tool_schemas(
                _config(),
                is_private=False,
                user_text="https://cdn.example.invalid/art/sample.webp?size=large",
                has_image=False,
                is_directly_requested=True,
            )
        )
        self.assertIn("catty_image_search", url_only)
        self.assertNotIn("catty_nai_director", url_only)

    def test_execute_tool_call_rejects_allowlist_before_executor(self) -> None:
        calls: list[dict] = []

        async def fake_executor(args, _ctx):
            calls.append(args)
            return {"ok": True}

        ctx = _tools.ToolContext(config=_config(), memory_store=None, event=None)
        with patch.dict(_tools._EXECUTORS, {"catty_now": fake_executor}, clear=False):
            result = asyncio.run(
                _tools.execute_tool_call(
                    "catty_now",
                    "{}",
                    ctx,
                    allowed_names=set(),
                )
            )

        self.assertEqual(result["error"], "tool_not_allowed")
        self.assertEqual(result["reason"], "not_in_allowlist")
        self.assertEqual(calls, [])

    def test_execute_tool_call_rejects_disabled_write_tools_before_executor(self) -> None:
        for name, flag in (
            ("catty_profile_memory", "catty_profile_memory_enabled"),
            ("catty_scope_meme", "catty_scope_meme_enabled"),
            ("catty_timeline", "catty_timeline_enabled"),
            ("catty_adaptive_prompt", "catty_adaptive_prompt_enabled"),
        ):
            with self.subTest(tool=name):
                cfg = _config()
                setattr(cfg, flag, False)
                calls: list[dict] = []

                async def fake_executor(args, _ctx):
                    calls.append(args)
                    return {"ok": True}

                ctx = _tools.ToolContext(config=cfg, memory_store=None, event=None)
                with patch.dict(_tools._EXECUTORS, {name: fake_executor}, clear=False):
                    result = asyncio.run(
                        _tools.execute_tool_call(name, "{}", ctx, allowed_names={name})
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "config_disabled")
                self.assertEqual(calls, [])

    def test_nai_director_requires_general_and_nai_switches(self) -> None:
        cfg = _config()
        cfg.catty_imagegen_enabled = True
        cfg.catty_imagegen_nai_enabled = False
        disabled_names = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="帮我把这张图抠图",
                has_image=True,
                is_directly_requested=True,
            )
        )
        self.assertNotIn("catty_nai_director", disabled_names)

        calls: list[dict] = []

        async def fake_executor(args, _ctx):
            calls.append(args)
            return {"ok": True}

        ctx = _tools.ToolContext(
            config=cfg,
            memory_store=None,
            event=None,
            input_image_urls=["https://example.test/input.png"],
            is_directly_requested=True,
            user_text="帮我把这张图抠图",
        )
        with patch.dict(_tools._EXECUTORS, {"catty_nai_director": fake_executor}, clear=False):
            denied = asyncio.run(
                _tools.execute_tool_call(
                    "catty_nai_director",
                    '{"req_type": "bg-removal"}',
                    ctx,
                    allowed_names={"catty_nai_director"},
                )
            )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "config_disabled")
        self.assertEqual(calls, [])

        cfg.catty_imagegen_nai_enabled = True
        enabled_names = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="帮我把这张图抠图",
                has_image=True,
                is_directly_requested=True,
            )
        )
        self.assertIn("catty_nai_director", enabled_names)

    def test_non_catty_schema_and_capability_messages_are_neutral(self) -> None:
        persona = types.SimpleNamespace(
            name="fadianji",
            feature_disabled=lambda _name: False,
        )
        imagegen = _tools.available_tool_schemas(
            _config(),
            is_private=True,
            user_text="给我画一张头像",
            is_directly_requested=True,
            persona=persona,
        )
        social = _tools.available_tool_schemas(
            _config(),
            is_private=True,
            user_text="Steam 账号",
            is_directly_requested=True,
            persona=persona,
        )
        descriptions = "\n".join(
            str((schema.get("function") or {}).get("description") or "")
            for schema in [*imagegen, *social]
        )
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘"):
            self.assertNotIn(forbidden, descriptions)

        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=None,
            event=None,
            persona=persona,
            is_directly_requested=False,
        )
        denied = asyncio.run(
            _tools.execute_tool_call(
                "catty_nsfw_search",
                '{"kind": "image", "query": "test"}',
                ctx,
                allowed_names={"catty_nsfw_search"},
            )
        )
        self.assertEqual(denied["reason"], "private_only")
        self.assertNotIn("猫猫", denied["message"])

    def test_non_catty_full_schemas_are_neutral_without_copying_catty(self) -> None:
        fadianji = types.SimpleNamespace(
            name="fadianji",
            feature_disabled=lambda _name: False,
        )
        catty = types.SimpleNamespace(name="catty")
        forbidden = ("主人", "喵", "猫", "ฅ")

        for name, schema in _tools.ALL_TOOL_SCHEMAS.items():
            self.assertIs(
                _tools._schema_for_persona(
                    name,
                    schema,
                    lazy=False,
                    persona=catty,
                ),
                schema,
            )
            neutral = _tools._schema_for_persona(
                name,
                schema,
                lazy=False,
                persona=fadianji,
            )
            serialized = json.dumps(neutral, ensure_ascii=False)
            for term in forbidden:
                self.assertNotIn(term, serialized, name)

        emoji_properties = (
            _tools._schema_for_persona(
                "catty_emoji",
                _tools.ALL_TOOL_SCHEMAS["catty_emoji"],
                lazy=False,
                persona=fadianji,
            )["function"]["parameters"]["properties"]
        )
        for property_schema in emoji_properties.values():
            for term in forbidden:
                self.assertNotIn(term, property_schema.get("description", ""))

    def test_non_catty_image_search_failure_and_cooldown_guidance_are_neutral(self) -> None:
        persona = types.SimpleNamespace(
            name="fadianji",
            feature_disabled=lambda _name: False,
        )
        cfg = _config()
        no_image = asyncio.run(
            _tools._exec_image_search(
                {"kind": "artwork"},
                _tools.ToolContext(
                    config=cfg,
                    memory_store=None,
                    event=None,
                    persona=persona,
                ),
            )
        )
        cooldown_ctx = _tools.ToolContext(
            config=cfg,
            memory_store=None,
            event=None,
            persona=persona,
            input_image_urls=["https://cdn.example.invalid/image.png"],
        )
        with patch.dict(
            _tools._image_search_cooldowns,
            {"anonymous": _tools.time.monotonic()},
            clear=True,
        ):
            cooldown = asyncio.run(
                _tools._exec_image_search({"kind": "artwork"}, cooldown_ctx)
            )

        for result in (no_image, cooldown):
            serialized = json.dumps(result, ensure_ascii=False)
            for term in ("主人", "喵", "猫", "ฅ"):
                self.assertNotIn(term, serialized)

    def test_imagegen_planner_keeps_catty_and_unknown_personas_separate(self) -> None:
        cfg = _config()
        cfg.catty_ai_fallback_base_url = "https://planner.example.invalid/v1"
        cfg.catty_ai_fallback_api_key = "planner-key"
        cfg.catty_ai_fallback_model = "planner-model"
        prompts: list[str] = []
        routes: list[str] = []

        async def fake_raw(**kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            routes.append(kwargs["request_route"])
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "provider": "nai",
                                    "prompt": "anime illustration",
                                    "short_review": "画好了.",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        unknown = types.SimpleNamespace(name="unknown", imagegen=None)
        catty = types.SimpleNamespace(
            name="catty",
            imagegen=types.SimpleNamespace(
                girl_tags="should not be used",
                short_review_style="should not be used",
                planner_brief="should not be used",
            ),
        )
        with patch.object(_client, "_post_chat_completion_raw", fake_raw):
            asyncio.run(_tools._deepseek_imagegen_plan(cfg, "画一张图", persona=unknown))
            asyncio.run(_tools._deepseek_imagegen_plan(cfg, "画一张图", persona=catty))

        unknown_prompt, catty_prompt = prompts
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘", "wave", "emoji"):
            self.assertNotIn(forbidden, unknown_prompt)
        self.assertIn('"self_portrait": null', unknown_prompt)
        self.assertIn("QQ 群猫娘机器人『笨猫』", catty_prompt)
        self.assertIn("画好啦~看看喜不喜欢喵ฅฅ", catty_prompt)
        self.assertNotIn("主人", catty_prompt)
        self.assertEqual(routes, ["imagegen_plan", "imagegen_plan"])

    def test_catty_imagegen_planner_failure_uses_neutral_caption(self) -> None:
        ctx = _tools.ToolContext(
            config=_config(), memory_store=None, event=None,
            persona=types.SimpleNamespace(name="catty"), user_text="画一张图",
        )
        async def fake_plan_failure(*_args, **_kwargs):
            raise RuntimeError("planner unavailable")
        async def fake_nai(**_kwargs):
            return {"image_sent": True, "cost": 0}
        with (
            patch.object(_tools, "_deepseek_imagegen_plan", fake_plan_failure),
            patch.object(_tools, "_exec_imagegen_nai", fake_nai),
        ):
            result = asyncio.run(_tools._exec_imagegen_agent({}, ctx))
        self.assertEqual(result["_short_circuit_reply"], "画好啦喵~ ฅฅ")

    def test_catty_imagegen_rejects_owner_address_for_guest(self) -> None:
        cfg = _config()
        cfg.catty_owner_qq = "10001"
        persona = types.SimpleNamespace(name="catty")
        async def fake_plan(*_args, **_kwargs):
            return {"provider": "nai", "prompt": "cat", "short_review": "主人画好啦喵"}
        async def fake_nai(**_kwargs):
            return {"image_sent": True, "cost": 0}
        with (
            patch.object(_tools, "_deepseek_imagegen_plan", fake_plan),
            patch.object(_tools, "_exec_imagegen_nai", fake_nai),
        ):
            for user_id, expected in (("20002", "画好啦喵~ ฅฅ"), ("10001", "主人画好啦喵")):
                with self.subTest(user_id=user_id):
                    ctx = _tools.ToolContext(
                        config=cfg, memory_store=None, event=types.SimpleNamespace(user_id=user_id),
                        persona=persona, user_text="画一张图",
                    )
                    result = asyncio.run(_tools._exec_imagegen_agent({}, ctx))
                    self.assertEqual(result["_short_circuit_reply"], expected)

    def test_unknown_persona_imagegen_fallback_reply_is_neutral(self) -> None:
        unknown = types.SimpleNamespace(name="unknown", imagegen=None)
        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=None,
            event=None,
            persona=unknown,
            user_text="画一张图",
        )
        generated: list[dict] = []

        async def fake_plan_failure(*_args, **_kwargs):
            raise RuntimeError("planner unavailable")

        async def fake_nai(**kwargs):
            generated.append(kwargs)
            return {
                "image_sent": True,
                "cost": 5,
                "balance_after": 12,
            }

        with (
            patch.object(_tools, "_deepseek_imagegen_plan", fake_plan_failure),
            patch.object(_tools, "_exec_imagegen_nai", fake_nai),
        ):
            result = asyncio.run(_tools._exec_imagegen_agent({}, ctx))

        self.assertEqual(generated[0]["prompt"], "anime illustration, detailed, high quality")
        self.assertIsNone(generated[0]["local_reference_bytes_list"])
        self.assertEqual(
            result["_short_circuit_reply"],
            "画好了. 请查看 这张图消耗了 5 积分, 现在还剩 12 分",
        )
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘", "wave", "emoji"):
            self.assertNotIn(forbidden, result["_short_circuit_reply"])

    def test_unknown_persona_ignores_malicious_self_portrait_plan(self) -> None:
        unknown = types.SimpleNamespace(name="unknown", imagegen=None)
        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=None,
            event=None,
            persona=unknown,
            user_text="画一张图",
        )
        checked_paths: list[str] = []
        generated: list[dict] = []

        def fake_is_file(path: Path) -> bool:
            checked_paths.append(path.as_posix())
            return True

        async def fake_plan(_config, _user_text, *, persona=None, **kwargs):
            self.assertIs(persona, unknown)
            self.assertFalse(kwargs.get("allow_nsfw"))
            return {
                "provider": "nai",
                "prompt": "anime illustration",
                "aspect": "portrait",
                "self_portrait": "sfw",
                "short_review": "画好啦主人~喵ฅฅ",
            }

        async def fake_nai(**kwargs):
            generated.append(kwargs)
            return {"image_sent": True, "cost": 0}

        with (
            patch.object(Path, "is_file", fake_is_file),
            patch.object(Path, "read_bytes", return_value=b"catty-reference"),
            patch.object(_tools, "_deepseek_imagegen_plan", fake_plan),
            patch.object(_tools, "_exec_imagegen_nai", fake_nai),
        ):
            self.assertEqual(
                _tools._load_self_portrait_reference_bytes("sfw", persona=unknown),
                [],
            )
            result = asyncio.run(_tools._exec_imagegen_agent({}, ctx))

        self.assertEqual(checked_paths, [])
        self.assertIsNone(generated[0]["local_reference_bytes_list"])
        self.assertIsNone(result["_deepseek_plan"]["self_portrait"])
        self.assertEqual(result["_short_circuit_reply"], "画好了. 请查看")

        nameless = types.SimpleNamespace(imagegen=None)
        self.assertFalse(_tools._is_catty_persona(nameless))
        self.assertEqual(
            _tools._load_self_portrait_reference_bytes("sfw", persona=nameless),
            [],
        )

    def test_meme_result_uses_pending_segment_without_base64_payload(self) -> None:
        async def fake_search(_config, _keywords, *, max_results):
            self.assertGreaterEqual(max_results, 1)
            return ["https://example.invalid/meme.png"]

        async def fake_download(_config, _url, *, timeout):
            self.assertGreater(timeout, 0)
            return b"fake-image-bytes", "image/png"

        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=None,
            event=None,
            download_binary_fn=fake_download,
        )
        with patch.object(_tools, "search_image_urls", fake_search):
            result = asyncio.run(_tools._exec_meme_query({"keywords": "测试梗"}, ctx))

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("base64://", serialized)
        self.assertNotIn("image_uri", result)
        self.assertEqual(len(ctx.pending_image_segments), 1)


    def test_intelligence_core_is_always_exposed_and_heavy_tools_stay_gated(self) -> None:
        names = _schema_names(
            _tools.available_tool_schemas(
                _config(),
                is_private=False,
                user_text="普通闲聊",
                is_directly_requested=False,
            )
        )
        core = {
            "catty_recall", "catty_user_profile", "catty_recall_notes",
            "catty_web_search", "catty_meme_explain", "catty_now",
            "catty_profile_memory", "catty_scope_meme", "catty_timeline",
            "catty_adaptive_prompt",
        }
        self.assertTrue(core.issubset(names))
        exposed = {
            (schema.get("function") or {}).get("name"): schema
            for schema in _tools.available_tool_schemas(
                _config(),
                is_private=False,
                user_text="普通闲聊",
                is_directly_requested=False,
            )
        }
        for name in core:
            self.assertEqual(exposed[name], _tools.ALL_TOOL_SCHEMAS[name])
        self.assertNotIn("catty_imagegen", names)
        self.assertNotIn("catty_meme_query", names)

    def test_full_exposure_default_exposes_heavy_tools_without_intent(self) -> None:
        # 主人 2026-08-13: AI 优先不省 token — 全量暴露为默认行为.
        # 主人 2026-10-26: 画图/写文件/改图也不再要求先被点名, 全部交给模型决定。
        cfg = _config()
        cfg.catty_tools_full_exposure = True
        names = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="普通闲聊",
                is_directly_requested=False,
            )
        )
        self.assertIn("catty_meme_query", names)
        self.assertIn("catty_fetch_url", names)
        self.assertIn("catty_weather", names)
        self.assertIn("catty_read_file", names)
        # 2026-10-26 沙箱改造: catty_run_code 由 legacy 开关单独控制 (默认关)
        self.assertIn("catty_imagegen", names)
        self.assertIn("catty_write_file", names)
        self.assertIn("catty_scan_messages", names)
        self.assertIn("catty_look_image", names)

    def test_config_disabled_tools_are_not_exposed(self) -> None:
        cfg = _config()
        cfg.catty_timeline_enabled = False
        cfg.catty_web_search_enabled = False
        names = _schema_names(
            _tools.available_tool_schemas(
                cfg,
                is_private=False,
                user_text="普通闲聊",
            )
        )
        self.assertNotIn("catty_timeline", names)
        self.assertNotIn("catty_web_search", names)

    def test_new_write_executors_use_current_scope_and_persona(self) -> None:
        calls: list[tuple] = []

        class Memory:
            def set_preferred_name(self, user_id, value):
                calls.append(("name", user_id, value))
                return value

            def record_profile_fact(self, user_id, value, **kwargs):
                calls.append(("fact", user_id, value, kwargs))
                return {"ok": True, "id": "n1"}

        class Lore:
            def add_entry(self, scope, keys, content, **kwargs):
                calls.append(("meme", scope, keys, content, kwargs))
                return {"identifier": "m1"}

        class Timeline:
            def add_item(self, scope, **kwargs):
                calls.append(("timeline", scope, kwargs))
                return {"id": "t1"}

            def record_tool_activity(self, *args, **kwargs):
                calls.append(("activity", args, kwargs))

        class Adaptive:
            def upsert_prompt(self, scope, name, content, **kwargs):
                calls.append(("adaptive", scope, name, content, kwargs))
                return {"id": "a1", "name": name}

        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=Memory(),
            event=types.SimpleNamespace(user_id="u1"),
            persona=types.SimpleNamespace(name="fadianji"),
            scope_key="group:42",
            scope_lorebook_store=Lore(),
            timeline_store=Timeline(),
            adaptive_prompt_store=Adaptive(),
        )
        asyncio.run(_tools._exec_profile_memory({"preferred_name": "小周", "fact": "不吃香菜", "category": "preference"}, ctx))
        asyncio.run(_tools._exec_scope_meme({"keys": ["梗"], "content": "群内玩笑"}, ctx))
        asyncio.run(_tools._exec_timeline({"action": "add", "title": "今天的计划", "content": "整理直播素材"}, ctx))
        asyncio.run(_tools._exec_adaptive_prompt({"action": "upsert", "name": "style", "content": "短句"}, ctx))
        self.assertIn(("name", "u1", "小周"), calls)
        self.assertTrue(any(item[0] == "fact" and item[1:3] == ("u1", "不吃香菜") and item[3]["category"] == "preference" for item in calls))
        self.assertTrue(any(item[0] == "meme" and item[1] == "group:42" and item[4]["persona"] == "fadianji" for item in calls))
        self.assertTrue(any(item[0] == "timeline" and item[1] == "group:42" and item[2]["persona"] == "fadianji" and item[2]["details"] == "整理直播素材" for item in calls))
        self.assertTrue(any(item[0] == "adaptive" and item[1] == "group:42" and item[4]["persona"] == "fadianji" for item in calls))

        asyncio.run(_tools._record_timeline_tool_activity(ctx, "catty_web_search"))
        self.assertTrue(any(item[0] == "activity" and item[1] == ("group:42", "catty_web_search") and item[2]["persona"] == "fadianji" for item in calls))

    def test_profile_write_invalidates_recall_and_profile_caches(self) -> None:
        class Memory:
            def set_preferred_name(self, user_id, value):
                return None

            def record_profile_fact(self, user_id, value, **kwargs):
                return None

        _tools._recall_cache.put("r", {"old": True}, ttl=60)
        _tools._profile_cache.put("p", {"old": True}, ttl=60)
        ctx = _tools.ToolContext(
            config=_config(),
            memory_store=Memory(),
            event=types.SimpleNamespace(user_id="u1"),
        )
        asyncio.run(_tools._exec_profile_memory({"fact": "new"}, ctx))
        self.assertIsNone(_tools._recall_cache.get("r", ttl=60))
        self.assertIsNone(_tools._profile_cache.get("p", ttl=60))

    def test_write_executors_report_store_rejections(self) -> None:
        class DisabledMemory:
            enabled = False

        disabled_ctx = _tools.ToolContext(
            config=_config(), memory_store=DisabledMemory(), event=types.SimpleNamespace(user_id="u1")
        )
        self.assertFalse(asyncio.run(_tools._exec_profile_memory({"fact": "new"}, disabled_ctx))["ok"])

        class RejectMemory:
            enabled = True
            def set_preferred_name(self, _user_id, _value): return ""
            def record_profile_fact(self, _user_id, _value, **_kwargs): return {"ok": False}

        reject_memory_ctx = _tools.ToolContext(
            config=_config(), memory_store=RejectMemory(), event=types.SimpleNamespace(user_id="u1")
        )
        self.assertFalse(asyncio.run(_tools._exec_profile_memory({"preferred_name": "小周"}, reject_memory_ctx))["ok"])
        self.assertFalse(asyncio.run(_tools._exec_profile_memory({"fact": "new"}, reject_memory_ctx))["ok"])

        class RejectLore:
            def add_entry(self, *_args, **_kwargs): return None

        class RejectTimeline:
            def update_item(self, *_args, **_kwargs): return None
            def complete_item(self, *_args, **_kwargs): return None
            def remove_item(self, *_args, **_kwargs): return False

        class RejectAdaptive:
            def upsert_prompt(self, *_args, **_kwargs): return None

        base = dict(config=_config(), memory_store=None, event=None, scope_key="group:42", persona=types.SimpleNamespace(name="fadianji"))
        lore_ctx = _tools.ToolContext(**base, scope_lorebook_store=RejectLore())
        self.assertFalse(asyncio.run(_tools._exec_scope_meme({"keys": ["梗"], "content": "说明"}, lore_ctx))["ok"])

        timeline_ctx = _tools.ToolContext(**base, timeline_store=RejectTimeline())
        for args in (
            {"action": "update", "item_id": "missing", "title": "改名"},
            {"action": "complete", "item_id": "missing"},
            {"action": "remove", "item_id": "missing"},
        ):
            with self.subTest(action=args["action"]):
                self.assertFalse(asyncio.run(_tools._exec_timeline(args, timeline_ctx))["ok"])

        adaptive_ctx = _tools.ToolContext(**base, adaptive_prompt_store=RejectAdaptive())
        self.assertFalse(asyncio.run(_tools._exec_adaptive_prompt({"action": "upsert", "name": "style", "content": "短句"}, adaptive_ctx))["ok"])


class ToolLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        _client._reset_cloud_health_for_tests()
        self._original_raw = _client._post_chat_completion_raw

    def tearDown(self) -> None:
        _client._post_chat_completion_raw = self._original_raw
        _client._reset_cloud_health_for_tests()

    def test_scope_contextvar_set_and_reset(self) -> None:
        previous = _client.get_current_scope_key()
        token = _client.set_current_scope_key("private:sim-test")
        try:
            self.assertEqual(_client.get_current_scope_key(), "private:sim-test")
        finally:
            _client.reset_current_scope_key(token)
        self.assertEqual(_client.get_current_scope_key(), previous)

    @staticmethod
    def _tools() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def test_unexposed_tool_is_paired_but_not_executed(self) -> None:
        calls: list[dict] = []
        executed: list[str] = []

        async def fake_raw(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_forbidden",
                                        "type": "function",
                                        "function": {"name": "forbidden", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            tool_messages = [m for m in kwargs["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("tool_not_allowed", tool_messages[0]["content"])
            return {"choices": [{"message": {"content": "done"}}]}

        async def executor(name: str, _args: str) -> dict:
            executed.append(name)
            return {"ok": True}

        _client._post_chat_completion_raw = fake_raw
        result = asyncio.run(
            _client.chat_completion_with_tools(
                _config(),
                [{"role": "user", "content": "test"}],
                tools=self._tools(),
                tool_executor=executor,
                max_rounds=2,
            )
        )

        self.assertEqual(result, "done")
        self.assertEqual(executed, [])

    def test_truncated_calls_all_receive_tool_results(self) -> None:
        calls: list[dict] = []
        executed: list[str] = []

        async def fake_raw(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": "{}"},
                                    },
                                    {
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": "{}"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            assistant_calls = [
                call
                for message in kwargs["messages"]
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
            ]
            tool_messages = [m for m in kwargs["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(assistant_calls), 2)
            self.assertEqual(len(tool_messages), 2)
            self.assertIn("tool_call_truncated", tool_messages[1]["content"])
            return {"choices": [{"message": {"content": "done"}}]}

        async def executor(name: str, _args: str) -> dict:
            executed.append(name)
            return {"ok": True}

        _client._post_chat_completion_raw = fake_raw
        result = asyncio.run(
            _client.chat_completion_with_tools(
                _config(),
                [{"role": "user", "content": "test"}],
                tools=self._tools(),
                tool_executor=executor,
                max_rounds=2,
                max_calls_per_round=1,
            )
        )

        self.assertEqual(result, "done")
        self.assertEqual(executed, ["lookup"])

    def test_history_serialization_redacts_and_bounds_payload(self) -> None:
        content = _client._serialize_tool_result_for_history(
            {
                "image": "base64://" + "A" * 5000,
                "api_key": "super-secret",
                "items": list(range(100)),
                "text": "x" * 5000,
            }
        )

        self.assertLessEqual(len(content), _client._TOOL_HISTORY_MAX_JSON_CHARS)
        self.assertNotIn("super-secret", content)
        self.assertNotIn("base64://", content)
        self.assertIn("<redacted>", content)
        self.assertIn("truncated_items", content)

    def test_assistant_argument_history_is_sanitized_but_executor_gets_raw(self) -> None:
        calls: list[dict] = []
        executor_arguments: list[str] = []
        secret = "assistant-argument-secret"
        raw_arguments = json.dumps(
            {
                "query": "normal query",
                "api_key": secret,
                "image": "base64://" + "A" * 5000,
            },
            ensure_ascii=False,
        )

        async def fake_raw(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_sensitive",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": raw_arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            assistant = [
                message
                for message in kwargs["messages"]
                if message.get("role") == "assistant"
            ][-1]
            history_arguments = assistant["tool_calls"][0]["function"]["arguments"]
            self.assertLessEqual(
                len(history_arguments),
                _client._TOOL_HISTORY_MAX_ARGUMENT_JSON_CHARS,
            )
            self.assertNotIn(secret, history_arguments)
            self.assertNotIn("base64://", history_arguments)
            return {"choices": [{"message": {"content": "done"}}]}

        async def executor(_name: str, args: str) -> dict:
            executor_arguments.append(args)
            return {"ok": True}

        _client._post_chat_completion_raw = fake_raw
        result = asyncio.run(
            _client.chat_completion_with_tools(
                _config(),
                [{"role": "user", "content": "test"}],
                tools=self._tools(),
                tool_executor=executor,
                max_rounds=2,
            )
        )

        self.assertEqual(result, "done")
        self.assertEqual(executor_arguments, [raw_arguments])

    def test_cascade_filters_targets_outside_current_allowlist(self) -> None:
        result = {"matches": [{"user_id": "42", "text": "old message"}]}

        without_profile = _cascade.build_post_tool_hint(
            [("catty_recall", result)],
            allowed_tools={"catty_recall"},
        )
        with_profile = _cascade.build_post_tool_hint(
            [("catty_recall", result)],
            allowed_tools={"catty_recall", "catty_user_profile"},
        )

        self.assertNotIn("catty_user_profile", without_profile)
        self.assertIn("catty_user_profile", with_profile)


if __name__ == "__main__":
    unittest.main()
