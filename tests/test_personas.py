import asyncio
from pathlib import Path
import importlib
import importlib.util
import re
import sys
import tempfile
import types
import unittest

# nonebot stub — 本地 venv 无 nonebot, 但 prompt_manager→affection 链会 from nonebot import logger
_nonebot_stub = types.ModuleType("nonebot")
_nonebot_stub.logger = types.SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
    exception=lambda *args, **kwargs: None,
)
sys.modules.setdefault("nonebot", _nonebot_stub)


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_personas_test"
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


character_card = _load_module("character_card")
_personas = importlib.import_module(f"{_PACKAGE_NAME}.personas")
_override_store_mod = _load_module("persona_override_store")
PersonaOverrideStore = _override_store_mod.PersonaOverrideStore


class _FakeConfig:
    def __init__(self, group_personas=None, default="catty"):
        self.catty_group_personas = group_personas or {}
        self.catty_default_persona = default


class PersonaRegistryTests(unittest.TestCase):
    def test_catty_persona_is_pure_default(self) -> None:
        """catty 实例所有内容字段必须为 None — 保证 prompt 管线走老路径字节不变。"""
        catty = _personas.get_persona("catty")
        self.assertEqual(catty.name, "catty")
        self.assertEqual(catty.char_name, "笨猫")
        self.assertTrue(catty.owner_concept)
        for attr in (
            "core_persona", "group_silence", "first_mes", "mes_example",
            "style_examples", "disambiguation_examples", "character_book",
            "persona_reminder_text", "reply_gate_style", "imagegen", "chat_rhythm",
            "trigger_prefixes", "directed_keywords", "model_override", "reply_catalog",
        ):
            self.assertIsNone(getattr(catty, attr), f"catty.{attr} 必须是 None")
        self.assertEqual(catty.disabled_prompt_segments, frozenset())
        self.assertEqual(catty.disabled_features, frozenset())
        self.assertEqual(catty.conversation_prompts, ())
        # 主人 2026-08-10: catty 保持旧触发/续聊行为
        self.assertFalse(catty.mention_only_trigger)
        self.assertIsNone(catty.followup_reply_budget)
        self.assertIsNone(catty.emoji_root)
        self.assertIsNone(catty.emoji_manifest_path)
        self.assertIsNone(catty.emoji_download_dir)
        self.assertIsNone(catty.emoji_allow_downloads)
        self.assertIsNone(catty.emoji_guidance)
        self.assertFalse(catty.cache_full_character_book)
        self.assertFalse(catty.semantic_harness_enabled)

    def test_catty_prompt_keeps_character_and_contextual_rules(self) -> None:
        core = _load_module("catty_core_persona")
        reminder = _load_module("catty_persona_reminder")
        catty = _personas.get_persona("catty")
        manager_module = _load_module("prompt_manager")
        manager = manager_module.PromptManager()
        manager_module.register_catty_persona(
            manager,
            {"config": _FakeConfig(), "persona": catty, "scope": "group:10001", "is_group": True},
        )
        joined = "\n".join(m["content"] for m in manager.build_messages() if m.get("role") == "system")
        self.assertIn(core.CATTY_CORE_PERSONA, joined)
        self.assertIn(core.CATTY_GROUP_SILENCE, joined)
        for anchor in (
            "18 岁猫亚人萌妹", "只对真实主人", "≤50 字",
            "先接当前发言者和话题", "样本只学口吻", "未实际查到或发送不要说已经完成",
            "动作描写和颜文字按情绪可选",
        ):
            self.assertIn(anchor, core.CATTY_CORE_PERSONA)
        self.assertIn("当前发言者的相关续聊窗口", core.CATTY_GROUP_SILENCE)
        self.assertIn("(动作) 和颜文字可选", reminder.build_reminder_text(catty))
        self.assertNotIn("每条回复必备", core.CATTY_CORE_PERSONA)

    def test_user_details_use_current_owner_and_persona(self) -> None:
        details_module = _load_module("user_details_store")
        manager_module = _load_module("prompt_manager")
        details = {"favorite_foods": ["烤鱼"]}
        store = types.SimpleNamespace(get_details=lambda _user_id: details)
        for persona_name, is_owner, expected in (
            ("catty", True, "主人之前不是说"),
            ("catty", False, "你之前不是说"),
            ("fadianji", True, "你之前不是说"),
            ("fadianji", False, "你之前不是说"),
        ):
            with self.subTest(persona=persona_name, is_owner=is_owner):
                persona = _personas.get_persona(persona_name)
                prompt = details_module.build_user_details_prompt(details, persona=persona, is_owner=is_owner)
                self.assertIn(expected, prompt)
                manager = manager_module.PromptManager()
                manager_module.register_catty_persona(manager, {
                    "config": _FakeConfig(), "persona": persona, "scope": "group:1",
                    "user_id": "123", "user_display": "小明", "is_owner": is_owner,
                    "user_details_store": store,
                })
                registered = next(m["content"] for m in manager.build_messages() if "【已知小明的细节】" in m["content"])
                self.assertIn(expected, registered)
                if expected.startswith("你"):
                    self.assertNotIn("主人", registered)
        self.assertIn("你之前不是说", details_module.build_user_details_prompt(details))

    def test_adaptive_drift_skeleton_points_to_actual_vibe(self) -> None:
        text = _load_module("author_note").build_adaptive_drift_skeleton()
        self.assertIn("vibe:", text)
        self.assertIn("vibe=tag", text)
        self.assertNotIn("catty_adaptive_drift_pointer", text)

    def test_get_persona_fallback(self) -> None:
        self.assertEqual(_personas.get_persona("nonexistent").name, "catty")
        self.assertEqual(_personas.get_persona("").name, "catty")
        self.assertEqual(_personas.get_persona(None).name, "catty")
        self.assertEqual(_personas.get_persona("FADIANJI").name, "fadianji")

    def test_normalize_aliases(self) -> None:
        self.assertEqual(_personas.normalize_persona_name("机机"), "fadianji")
        self.assertEqual(_personas.normalize_persona_name("小机"), "fadianji")
        self.assertEqual(_personas.normalize_persona_name("发电机"), "fadianji")
        self.assertEqual(_personas.normalize_persona_name("不稳定发电机"), "fadianji")
        self.assertEqual(_personas.normalize_persona_name("笨猫"), "catty")
        self.assertEqual(_personas.normalize_persona_name("猫猫"), "catty")
        self.assertEqual(_personas.normalize_persona_name("catty"), "catty")
        self.assertEqual(_personas.normalize_persona_name("FADIANJI"), "fadianji")
        self.assertIsNone(_personas.normalize_persona_name("不存在的人格"))
        self.assertIsNone(_personas.normalize_persona_name(""))

    def test_fadianji_content_pack(self) -> None:
        fdj = _personas.get_persona("fadianji")
        self.assertEqual(fdj.char_name, "机机")
        self.assertFalse(fdj.owner_concept)
        # 核心内容都要有
        for attr in ("core_persona", "group_silence", "first_mes", "mes_example",
                     "persona_reminder_text", "reply_gate_style"):
            self.assertTrue(getattr(fdj, attr), f"fadianji.{attr} 不能为空")
        self.assertTrue(fdj.character_book)
        self.assertIsNotNone(fdj.imagegen)
        self.assertEqual(fdj.imagegen.ref_path, "Miao/fadianji.png")
        self.assertTrue(fdj.chat_rhythm)
        self.assertEqual(len(fdj.conversation_prompts), 1)
        # NSFW 深水: 主人 2026-10-25 只放开私聊, 群聊仍禁; 笨猫周边必须禁用
        self.assertNotIn("nsfw_spark", fdj.disabled_features)
        self.assertEqual(fdj.nsfw_scopes, frozenset({"private"}))
        self.assertTrue(fdj.nsfw_allowed(is_private=True))
        self.assertFalse(fdj.nsfw_allowed(is_private=False))
        # 2026-10-25: 机机也怀孕, 但只记私聊 (pregnancy_private_only) — 群里不说肚子里的孩子
        self.assertNotIn("pregnancy", fdj.disabled_features)
        self.assertTrue(fdj.pregnancy_private_only)
        self.assertFalse(_personas.get_persona("catty").pregnancy_private_only)
        # 2026-10-25: 机机私聊要接住 NSFW — gate 不再整段禁用, 换成机机骨架 + 只在私聊注册
        self.assertNotIn("catty_nsfw_gate_skeleton", fdj.disabled_prompt_segments)
        self.assertNotIn("catty_nsfw_gate_params", fdj.disabled_prompt_segments)
        self.assertTrue(fdj.nsfw_gate_private_only)
        self.assertTrue(fdj.nsfw_gate_skeleton)
        self.assertIn("max_stage≥5", fdj.nsfw_gate_skeleton or "")
        self.assertIn("100 积分", fdj.nsfw_gate_skeleton or "")
        # 2026-10-25: NSFW 阶段/场景词表分开 — 机机有替换表, catty 为空
        self.assertTrue(fdj.nsfw_lexicon)
        lexicon = dict(fdj.nsfw_lexicon)
        self.assertEqual(lexicon.get("猫耳"), "翼耳")
        self.assertEqual(lexicon.get("笨猫"), "机机")
        self.assertEqual(_personas.get_persona("catty").nsfw_lexicon, ())
        # NSFW 自由发挥 (去掉笨猫 phase 推进表/格式铁律) — 2026-10-25 起 catty 也走自由发挥
        self.assertTrue(fdj.nsfw_freeform)
        self.assertTrue(_personas.get_persona("catty").nsfw_freeform)
        self.assertIn("catty_daily_life", fdj.disabled_prompt_segments)
        # 唤醒词整组替换 + 形象/语气 constant 段常驻
        self.assertIn("机机", fdj.trigger_prefixes)
        self.assertNotIn("猫猫", fdj.directed_keywords)
        self.assertTrue(any(e.constant for e in fdj.character_book))
        # 主人 2026-08-10: model_override 已取消 — 机机主回复跟随 config 主模型 (gpt-5.6-luna)
        self.assertIsNone(fdj.model_override)
        # 机机显式唤醒 + 两条相关续聊；旧猫娘冒泡仍独立禁用。
        self.assertFalse(fdj.mention_only_trigger)
        self.assertEqual(fdj.followup_reply_budget, 2)
        self.assertTrue(fdj.feature_disabled("proactive_bubble"))
        self.assertNotIn("只有**真 @", fdj.group_silence)
        self.assertEqual(fdj.emoji_root, "fadianji/emoji")
        self.assertEqual(fdj.emoji_manifest_path, "fadianji/emoji/manifest.json")
        self.assertEqual(fdj.emoji_download_dir, "fadianji/emoji/downloaded")
        self.assertFalse(fdj.emoji_allow_downloads)
        self.assertTrue(fdj.emoji_guidance)
        self.assertTrue(fdj.cache_full_character_book)
        self.assertTrue(fdj.semantic_harness_enabled)

    def test_fadianji_full_character_book_skeleton(self) -> None:
        fdj = _personas.get_persona("fadianji")
        skeleton = character_card.build_character_book_skeleton(
            fdj.character_book, char_name=fdj.char_name, include_all=True,
        )
        self.assertIn("关键词段 (完整缓存, 命中时只激活)", skeleton)
        for entry in fdj.character_book:
            self.assertIn(entry.identifier, skeleton)
            self.assertIn(entry.content, skeleton)

    def test_fadianji_core_persona_and_rhythm_contract(self) -> None:
        fdj = _personas.get_persona("fadianji")
        core_persona = fdj.core_persona or ""
        for anchor in (
            "默认反应核",
            "73.4% 的发言簇只有一条",
            "18.5% 两条",
            "3-4 条 7.2%",
            "「我去」是长期主惊讶词",
            "「我靠」其次",
            "「我超」只在当前真兴奋/阳光档",
            "感觉 / 真的 / 我觉得 / 怎么说呢",
            "不知道啊 / 没看懂 / 这个得问本体",
            "图片、语音、表情包约占全部消息 15-17%",
            "Unicode emoji 是低频签名而非硬禁",
            "不把问候、技术、身体不适、认真倾诉强行黄化",
            "FORWARD 只给一句即时反应",
            "AV 标题/简介只学钩子",
            "DRAW 长动态",
            "不替本体宣布直播/行程/身体/报价/钱款/账号/关系/承诺",
            "口癖绑定与冷却",
            "同一口癖一轮最多一次",
            "装自信→破防四步链",
            "事实类问题（时间/搜索/百科/技术排错）不准装懂",
            "回调（callback）规则",
            "情绪状态与输出形态",
            "破防（被拆穿/连续翻车/被戳痛处）",
            "工具旁白",
            "不要照抄固定开场",
            "<<<CATTY_REPLY_SPLIT>>>",
            "语料补充·默认现场件",
            "被夸时的三种真实落点",
            "熟人互损门控",
            "发癫峰值门控",
            "不知道时的真实短答",
            "成人梗处理优先级",
            # 2026-08-15 主人: 机机要会写小黄文 — 创作模式段进稳定 core
            "成人创作模式 (小黄文写作)",
            "直接写，不推脱",
            "有 story 有情景",
            "标点与 emoji 的真实比例",
            "长度门控",
            "称呼按事件切换",
        ):
            self.assertIn(anchor, core_persona, f"机机核心人格缺少语域锚点: {anchor}")
        # 2026-10-25: 私聊放行档 (Lv5+/100 积分) 不许划走
        self.assertIn("不许划走", core_persona)
        self.assertIn("max_stage ≥5", core_persona)
        self.assertIn("100 积分援交", core_persona)
        # reminder 是高 recency trailing system, 同样要钉住放行档
        self.assertIn("max_stage≥5", fdj.persona_reminder_text or "")
        # 2026-08-10 四轮语料复核: 跳脸词表必须是语料实证写法, 零证据词表禁止回潮
        self.assertIn("滚啊", core_persona)
        for zero_evidence_word in ("想得美", "自己拉去", "你谁啊", "你行你上"):
            self.assertNotIn(zero_evidence_word, core_persona)
        # 2026-08-15: 全量场景库退出稳定 prompt，只保留本轮动态证据指引。
        self.assertIn("STYLE_EXAMPLE/口吻母本", core_persona)
        self.assertNotIn("## 8. 场景精选", core_persona)
        self.assertNotIn("群友: nb\n机机: 我靠", core_persona)
        self.assertIn("即时反应", core_persona)
        self.assertIn("熟人群雌小鬼", core_persona)
        self.assertIn("分身口径", core_persona)
        # 五轮: 分块式连续表达 (U=C₁⊕…⊕Cₖ) + 爆粗分档 + 括号心情低频
        self.assertIn("分块式连续表达", core_persona)
        self.assertIn("Complete(", core_persona)
        self.assertIn("普通空格不算消息边界", core_persona)
        self.assertIn("不写标记就是一条消息", core_persona)
        self.assertIn("爆粗分档", core_persona)
        self.assertIn("你他妈有病吧", core_persona)
        self.assertIn("（）心情/动作基本不用", core_persona)
        # 五轮修: 真人式回复 — 禁「草（心里活动）」式单字感叹+括号注解
        self.assertIn("绝不配括号注解", core_persona)
        self.assertNotIn("3670608232", core_persona)
        self.assertNotIn("922298923", core_persona)
        self.assertNotIn("好兄弟互损", core_persona)
        self.assertNotIn("约 48% 一条、29% 两条、20% 三到四条", core_persona)

        voice_guide = next(
            entry.content
            for entry in fdj.character_book
            if entry.identifier == "fdj_voice_guide"
        )
        for anchor in (
            "群聊文本中位4字/P90约10字",
            "惊讶优先我去>我靠>>我超",
            "帮忙/送礼可谢谢宝宝",
            "专业协作可老师",
            "私聊专属称呼只由当前会话专属层控制",
        ):
            self.assertIn(anchor, voice_guide, f"机机常驻语气段称呼规则缺少: {anchor}")

        rhythm_guidance = "\n".join(
            text for text in (fdj.core_persona, fdj.persona_reminder_text, fdj.chat_rhythm) if text
        )
        self.assertRegex(
            rhythm_guidance,
            r"(?:即时)?短(?:回|句)[\s\S]{0,48}(?:默认|常态)|(?:默认|常态)[\s\S]{0,48}(?:即时)?短(?:回|句)",
            "reminder/chat_rhythm 必须将即时短回明确为默认节奏",
        )
        self.assertRegex(
            rhythm_guidance,
            r"(?:DRAW\s*)?长动态[\s\S]{0,80}(?:例外|不(?:是|当|作)[\s\S]{0,16}默认|默认模板)|(?:例外|不(?:是|当|作)[\s\S]{0,16}默认|默认模板)[\s\S]{0,80}(?:DRAW\s*)?长动态",
            "reminder/chat_rhythm 必须把长动态限定为例外而非默认模板",
        )
        self.assertRegex(
            rhythm_guidance,
            r"(?:复读|叠字)[\s\S]{0,96}(?:有情绪|真情绪|不能常驻|不机械)|(?:有情绪|真情绪|不能常驻|不机械)[\s\S]{0,96}(?:复读|叠字)",
            "reminder/chat_rhythm 必须避免把复读固化为口头禅",
        )
        self.assertIn("实际输出不要写斜杠或普通空格假装边界", fdj.chat_rhythm or "")
        self.assertIn("catty_adaptive_drift_skeleton", fdj.disabled_prompt_segments)

    def test_fadianji_ordinary_chat_context_and_historical_examples(self) -> None:
        core = _personas.get_persona("fadianji").core_persona or ""
        default_reactions = core.split("## 1. 默认反应核", 1)[1].split("## 2. 语气和标点", 1)[0]
        for anchor in (
            "真实聊天对是口吻参考", "只借反应、节奏和分寸",
            "不照抄旧事件或把样本当本轮事实", "先接当前话题、说话人和意图",
            "有相关记忆就自然接续", "玩笑顺着接",
            "认真求助或安慰时切认真、给有用回应", "口癖随语境变化, 不反复套同一句",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, default_reactions)
        self.assertNotIn("原文优先", default_reactions)
        registers = core.split("## 3. 条件语域", 1)[1].split("## 4. 分块式", 1)[0]
        peak = registers.split("- 发癫 (", 1)[1].split("- 不知道:", 1)[0]
        self.assertIn("招牌, 情绪峰值", peak)
        self.assertIn("癫完优先回短软句", peak)
        self.assertNotIn("招牌, 高频", peak)
        examples = core.split("## 25. 常驻口吻母本", 1)[1].split("## 27. 聪明办事", 1)[0]
        self.assertIn("只借反应节奏, 不照抄旧事件", examples)
        self.assertIn("样本里的直播、画稿等状态不是本轮事实", examples)
        self.assertIn("适用于当前时点的同 scope 可靠记忆/工具结果明确确认", examples)

    def test_fadianji_private_conversation_upgrade_isolation(self) -> None:
        fdj = _personas.get_persona("fadianji")
        private_prompt = fdj.conversation_prompt_for("private:3670608232")

        self.assertIn("当前私聊专属升级层", private_prompt)
        self.assertIn("兄弟 / 哥们", private_prompt)
        self.assertIn("老师 / 无名老师", private_prompt)
        self.assertIn("约 48% 一条、29% 两条、20% 三到四条", private_prompt)
        self.assertIn("一律留在本会话", private_prompt)
        # 2026-10-25: 私聊 NSFW 放行档破例 (Lv5+/100 积分), 平时默认边界不动
        self.assertIn("唯一例外", private_prompt)
        self.assertIn("max_stage ≥5", private_prompt)
        self.assertIn("越顺从", private_prompt)
        # 私聊互损词表同样走语料实证 (妈的/神经病/傻逼/弱智/滚/爬/捅死你 级别)
        self.assertIn("捅死你", private_prompt)
        self.assertNotIn("想得美", private_prompt)
        # 2026-08-15: 私聊只保留蒸馏规则和少量节奏样本，完整实录按 scope 动态取 top-3。
        self.assertIn("动态私聊母本", private_prompt)
        self.assertIn("STYLE_EXAMPLE/口吻母本", private_prompt)
        self.assertNotIn("私聊场景精选", private_prompt)
        self.assertNotIn("对方: 生日快乐🎂发电机", private_prompt)
        self.assertNotIn("3670608232", private_prompt)
        self.assertNotIn("922298923", private_prompt)

        # 922298923 的记录已经蒸馏进通用 core，不再单独注入群 prompt。
        self.assertEqual(fdj.conversation_prompt_for("group:922298923"), "")
        self.assertEqual(fdj.conversation_prompt_for("private:1"), "")
        self.assertEqual(fdj.conversation_prompt_for("group:1"), "")
        self.assertEqual(
            fdj.conversation_prompt_for("group:922298923:user:3670608232"),
            "",
        )

    def test_prompt_manager_caches_full_group_scene_bank(self) -> None:
        """完整群聊母本常驻稳定前缀；显式低预算时可裁，core 仍受保护。"""
        prompt_manager = _load_module("prompt_manager")
        fdj = _personas.get_persona("fadianji")

        manager = prompt_manager.PromptManager()
        prompt_manager.register_catty_persona(
            manager,
            {
                "config": _FakeConfig(),
                "persona": fdj,
                "scope": "group:922298923",
                "is_group": True,
            },
        )
        full_messages = manager.build_messages()
        full_joined = "\n".join(
            m["content"] for m in full_messages if m.get("role") == "system"
        )
        self.assertIn("【机机·完整群聊口吻母本】", full_joined)
        self.assertIn("## 8. 场景精选", full_joined)
        self.assertIn("群友: nb\n机机: 我靠", full_joined)
        self.assertNotIn("对方: 生日快乐🎂发电机", full_joined)

        messages = manager.build_messages(max_tokens=500)
        joined = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        self.assertNotIn("【机机·完整群聊口吻母本】", joined)
        self.assertIn(fdj.core_persona, joined, "core_persona 全文受保护不被 trim")
        trimmed = getattr(manager, "last_trim_report", {}).get("trimmed_identifiers", [])
        self.assertIn("fadianji_static_scene_bank", trimmed)
        self.assertNotIn("catty_core_persona", trimmed)

        private_manager = prompt_manager.PromptManager()
        prompt_manager.register_catty_persona(
            private_manager,
            {
                "config": _FakeConfig(),
                "persona": fdj,
                "scope": "private:3670608232",
                "is_group": False,
            },
        )
        private_joined = "\n".join(
            m["content"] for m in private_manager.build_messages() if m.get("role") == "system"
        )
        self.assertNotIn("【机机·完整群聊口吻母本】", private_joined)
        self.assertNotIn("群友: nb\n机机: 我靠", private_joined)

    def test_fadianji_dynamic_hits_activate_cached_full_entry(self) -> None:
        prompt_manager = _load_module("prompt_manager")
        fdj = _personas.get_persona("fadianji")
        manager = prompt_manager.PromptManager()
        prompt_manager.register_catty_persona(
            manager,
            {
                "config": _FakeConfig(),
                "persona": fdj,
                "scope": "group:922298923",
                "is_group": True,
                "user_text": "钢板",
            },
        )
        messages = manager.build_messages()
        skeleton = next(
            message["content"]
            for message in messages
            if "角色私货库 character_book" in message["content"]
        )
        dynamic = next(
            message["content"]
            for message in messages
            if "character_book·本轮命中 (已缓存全文, 仅激活)" in message["content"]
        )
        full_content = next(
            entry.content
            for entry in fdj.character_book
            if entry.identifier == "fdj_gangban"
        )
        self.assertIn("fdj_gangban", dynamic)
        self.assertIn("摘录:", dynamic)
        self.assertNotIn(full_content, dynamic)
        self.assertIn(full_content, skeleton)

    def test_fadianji_mes_example_shape_and_key_scenarios(self) -> None:
        fdj = _personas.get_persona("fadianji")
        scenarios = [
            block.strip()
            for block in (fdj.mes_example or "").split("<START>")
            if block.strip()
        ]
        reply_line_counts = []
        for scenario in scenarios:
            lines = [line.strip() for line in scenario.splitlines() if line.strip()]
            char_line_index = next(
                (index for index, line in enumerate(lines) if line.startswith("{{char}}:")),
                None,
            )
            if char_line_index is None:
                self.fail(f"示例场景缺少 {{char}} 回复: {scenario!r}")
            inline_reply = lines[char_line_index].partition(":")[2].strip()
            reply_lines = [inline_reply] if inline_reply else []
            reply_lines.extend(lines[char_line_index + 1:])
            reply_line_counts.append(len(reply_lines))

        self.assertEqual(len(scenarios), 12)
        self.assertTrue(all(reply_line_counts), f"每个场景都应有机机回复: {reply_line_counts}")
        self.assertEqual(
            sum(line_count == 1 for line_count in reply_line_counts),
            11,
            f"普通示例都应默认单条: {reply_line_counts}",
        )
        # 2026-08-15 主人: 机机要会写小黄文 — 创作模式示例是多行散文, 单条门控豁免
        self.assertEqual(
            sum(line_count > 1 for line_count in reply_line_counts),
            1,
            f"成人创作示例只应有一个多行散文场景: {reply_line_counts}",
        )

        mes_example = fdj.mes_example or ""
        for scenario_word in (
            "在吗",
            "好可爱",
            "这图你看懂了吗",
            "对不起刚才",
            "今天播吗",
            "今天好难过",
            "你是真的机机吗",
            # 2026-08-12 主人: 机机要更癫 — 示例里必须有怪叫系发癫场景
            "矮人至高女王",
            # 2026-08-15 主人: 机机要会写小黄文 — 示例里必须有成人创作场景
            "机机给我写一段小黄文",
        ):
            self.assertIn(scenario_word, mes_example)
        self.assertTrue(
            any(keyword in mes_example for keyword in ("config", "报错", "配置", "排错")),
            "示例必须包含一个技术/排错场景",
        )

    def test_fadianji_no_cat_leakage(self) -> None:
        """机机人格文本不能混入猫娘文案(喵/笨猫/米雪儿/卡拉彼丘/猫耳)。"""
        fdj = _personas.get_persona("fadianji")
        texts = [
            fdj.core_persona, fdj.group_silence, fdj.first_mes,
            fdj.mes_example, fdj.persona_reminder_text, fdj.reply_gate_style,
        ]
        texts.extend(prompt for _, prompt in fdj.conversation_prompts)
        texts.extend(e.content for e in fdj.character_book)
        blob = "\n".join(t for t in texts if t)
        # "喵"只能出现在明确的 Catty 专属硬禁规则中。
        for line in blob.splitlines():
            for word in ("笨猫", "米雪儿", "卡拉彼丘", "猫耳", "猫尾"):
                self.assertNotIn(word, line, f"机机文本混入猫娘词 {word!r}: {line!r}")
            if "喵" in line:
                self.assertTrue(
                    any(marker in line for marker in (
                        "Catty 专属", "硬禁", "完全禁用", "任何场景都不输出",
                    )),
                    f"机机文本出现非禁用规则的'喵': {line!r}",
                )


class PersonaReplyCatalogTests(unittest.TestCase):
    def test_catty_reply_catalog_is_module_fallback_and_generic_is_neutral(self) -> None:
        catty = _personas.get_persona("catty")
        self.assertIsNone(catty.reply_catalog)
        self.assertIs(_personas.get_reply_catalog(catty), _personas.CATTY_REPLY_CATALOG)

        bare_persona = _personas.Persona(name="bare", char_name="bare")
        self.assertIs(_personas.get_reply_catalog(bare_persona), _personas.NEUTRAL_REPLY_CATALOG)
        neutral_blob = "\n".join((
            *_personas.NEUTRAL_REPLY_CATALOG.slow_reply_placeholders,
            *_personas.NEUTRAL_REPLY_CATALOG.no_reply_fallbacks,
            _personas.NEUTRAL_REPLY_CATALOG.api_timeout_reply,
            _personas.NEUTRAL_REPLY_CATALOG.api_transport_reply,
        ))
        for forbidden in ("主人", "喵", "笨猫", "猫猫"):
            self.assertNotIn(forbidden, neutral_blob)

        self.assertTrue(_personas.PersonaReplyCatalog.__dataclass_params__.frozen)
        self.assertTrue(_personas.PersonaReplyContext.__dataclass_params__.frozen)
        self.assertIn("slow_reply_placeholders", _personas.PersonaReplyCatalog.__slots__)
        self.assertIn("persona", _personas.PersonaReplyContext.__slots__)

    def test_fadianji_reply_catalog_is_explicit_and_complete(self) -> None:
        fdj = _personas.get_persona("fadianji")
        catalog = fdj.reply_catalog
        self.assertIs(catalog, _personas.FADIANJI_REPLY_CATALOG)
        self.assertIs(_personas.get_reply_catalog(fdj), catalog)
        self.assertTrue(catalog.slow_reply_placeholders)
        self.assertEqual(catalog.slow_reply_owner_placeholders, ())
        self.assertTrue(catalog.force_reply_instruction)
        self.assertEqual(len(catalog.no_reply_fallbacks), 4)
        self.assertTrue(all(catalog.no_reply_fallbacks))
        self.assertTrue(catalog.api_timeout_reply)
        self.assertTrue(catalog.api_transport_reply)
        self.assertTrue(catalog.tool_result_follow_up_instruction)

    def test_reply_context_owner_address_and_rendering(self) -> None:
        catty_owner = _personas.build_persona_reply_context(
            _personas.get_persona("catty"), is_owner=True,
        )
        catty_guest = _personas.build_persona_reply_context(
            _personas.get_persona("catty"), is_owner=False,
        )
        fdj_owner = _personas.build_persona_reply_context(
            _personas.get_persona("fadianji"), is_owner=True,
        )

        self.assertTrue(catty_owner.is_owner)
        self.assertTrue(catty_owner.owner_concept)
        self.assertTrue(catty_owner.owner_address_allowed)
        self.assertFalse(catty_guest.owner_address_allowed)
        self.assertTrue(fdj_owner.is_owner)
        self.assertFalse(fdj_owner.owner_concept)
        self.assertFalse(fdj_owner.owner_address_allowed)
        self.assertFalse(fdj_owner.feature_disabled("nsfw_spark"))
        self.assertTrue(fdj_owner.persona.nsfw_allowed(is_private=True))
        self.assertFalse(fdj_owner.persona.nsfw_allowed(is_private=False))
        self.assertFalse(fdj_owner.feature_disabled("unknown_feature"))
        self.assertEqual(
            catty_owner.render("{char}/{char_name}/{owner_address}/{topic}", topic="好"),
            "笨猫/笨猫/主人/好",
        )
        self.assertEqual(fdj_owner.render("{char}/{owner_address}"), "机机/你")
        with self.assertRaises(ValueError):
            fdj_owner.render("{owner_address}", owner_address="主人")
        with self.assertRaises(KeyError):
            fdj_owner.render("{missing}")
        self.assertEqual(
            catty_owner.placeholder_pool,
            _personas.CATTY_REPLY_CATALOG.slow_reply_placeholders
            + _personas.CATTY_REPLY_CATALOG.slow_reply_owner_placeholders,
        )
        self.assertEqual(fdj_owner.placeholder_pool, _personas.FADIANJI_REPLY_CATALOG.slow_reply_placeholders)

    def test_fadianji_reply_catalog_has_no_cat_or_decorative_leakage(self) -> None:
        catalog = _personas.FADIANJI_REPLY_CATALOG
        texts = (
            *catalog.slow_reply_placeholders,
            *catalog.slow_reply_owner_placeholders,
            catalog.force_reply_instruction,
            *catalog.no_reply_fallbacks,
            catalog.api_timeout_reply,
            catalog.api_transport_reply,
            catalog.tool_result_follow_up_instruction,
        )
        blob = "\n".join(texts)
        for forbidden in ("主人", "喵", "笨猫", "~", "～", "〜", "〰", "颜文字", "emoji"):
            self.assertNotIn(forbidden, blob)
        self.assertIsNone(re.search(r"[\U0001F300-\U0001FAFF]", blob))
        self.assertTrue(all(len(line) <= 8 for line in catalog.slow_reply_placeholders))
        self.assertTrue(all(len(line) <= 16 for line in catalog.no_reply_fallbacks))


class TokenBillingPersonaPromptTests(unittest.TestCase):
    def test_ai_gate_reply_uses_clean_fadianji_brief_and_preserves_catty_guidance(self) -> None:
        token_billing = _load_module("token_billing")
        captured_messages = []

        async def fake_chat_completion_codex_instant(config, messages, max_tokens):
            captured_messages.append(messages)
            return "拦截文案"

        openai_client_name = f"{_PACKAGE_NAME}.openai_client"
        previous_openai_client = sys.modules.get(openai_client_name)
        openai_client = types.ModuleType(openai_client_name)
        openai_client.chat_completion_codex_instant = fake_chat_completion_codex_instant
        sys.modules[openai_client_name] = openai_client
        try:
            asyncio.run(
                token_billing.ai_gate_reply(
                    _FakeConfig(), _personas.get_persona("fadianji"), "broke", "在吗"
                )
            )
            asyncio.run(
                token_billing.ai_gate_reply(
                    _FakeConfig(), _personas.get_persona("catty"), "broke", "在吗"
                )
            )
            asyncio.run(
                token_billing.ai_gate_reply(
                    _FakeConfig(),
                    _personas.Persona(
                        name="unknown",
                        char_name="unknown",
                        core_persona="未声明的完整人格不得进入计费拦截提示。",
                    ),
                    "broke",
                    "在吗",
                )
            )
        finally:
            if previous_openai_client is None:
                sys.modules.pop(openai_client_name, None)
            else:
                sys.modules[openai_client_name] = previous_openai_client

        fadianji_system = "\n".join(
            message["content"]
            for message in captured_messages[0]
            if message["role"] == "system"
        )
        self.assertEqual(
            captured_messages[0][0]["content"], token_billing._FADIANJI_BILLING_BRIEF
        )
        self.assertNotIn(_personas.get_persona("fadianji").core_persona, fadianji_system)
        for forbidden in (
            "主人", "喵", "笨猫", "猫猫", "猫娘", "~", "～", "〜", "〰", "波浪号", "颜文字", "emoji",
        ):
            self.assertNotIn(forbidden, fadianji_system)

        catty_system = "\n".join(
            message["content"]
            for message in captured_messages[1]
            if message["role"] == "system"
        )
        self.assertEqual(captured_messages[1][0]["content"], token_billing._CATTY_BRIEF)
        self.assertIn("一只住在 QQ 里的猫娘", catty_system)
        self.assertIn("撒娇/耍赖", catty_system)
        self.assertIn("对方不是你的主人绝对不要叫他主人", catty_system)

        self.assertEqual(captured_messages[2][0]["content"], token_billing._NEUTRAL_BRIEF)


class PersonaResolveTests(unittest.TestCase):
    def test_resolve_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PersonaOverrideStore(Path(directory) / "memory.json")
            cfg = _FakeConfig(group_personas={"123": "fadianji"})
            # config 群映射
            self.assertEqual(
                _personas.resolve_persona_name("group:123", config=cfg, override_store=store),
                "fadianji",
            )
            # per-user 群 scope 也取 gid
            self.assertEqual(
                _personas.resolve_persona_name("group:123:user:9", config=cfg, override_store=store),
                "fadianji",
            )
            # 未映射群 → default
            self.assertEqual(
                _personas.resolve_persona_name("group:456", config=cfg, override_store=store),
                "catty",
            )
            # override 优先于 config
            store.set("group:123", "catty", set_by="99999")
            self.assertEqual(
                _personas.resolve_persona_name("group:123", config=cfg, override_store=store),
                "catty",
            )
            # per-user 群会话 key 也要命中群级 override (/人格 存 group:{gid}, history key 带 :user:)
            store.set("group:456", "fadianji")
            self.assertEqual(
                _personas.resolve_persona_name("group:456:user:9", config=cfg, override_store=store),
                "fadianji",
            )
            store.clear("group:456")
            # 私聊 override
            store.set("private:9", "fadianji")
            self.assertEqual(
                _personas.resolve_persona_name("private:9", config=cfg, override_store=store),
                "fadianji",
            )
            # 清除后回落
            store.clear("group:123")
            self.assertEqual(
                _personas.resolve_persona_name("group:123", config=cfg, override_store=store),
                "fadianji",
            )

    def test_resolve_group_mapping_accepts_alias(self) -> None:
        cfg = _FakeConfig(group_personas={"777": "机机"})
        self.assertEqual(_personas.resolve_persona_name("group:777", config=cfg), "fadianji")

    def test_resolve_default_accepts_alias_after_group_mapping_miss(self) -> None:
        cfg = _FakeConfig(group_personas={"777": "notexist"}, default="小机")
        self.assertEqual(_personas.resolve_persona_name("group:777", config=cfg), "fadianji")

    def test_resolve_unknown_names_fall_back(self) -> None:
        cfg = _FakeConfig(group_personas={"1": "notexist"}, default="alsonotexist")
        self.assertEqual(_personas.resolve_persona_name("group:1", config=cfg), "catty")
        self.assertEqual(_personas.resolve_persona_name("private:2", config=cfg), "catty")
        self.assertEqual(_personas.resolve_persona_name("", config=None), "catty")


class PersonaOverrideStoreTests(unittest.TestCase):
    def test_roundtrip_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            store = PersonaOverrideStore(memory_path)
            store.set("group:1", "fadianji", set_by="42")
            self.assertEqual(store.get("group:1"), "fadianji")
            # 新实例从盘上恢复
            reloaded = PersonaOverrideStore(memory_path)
            self.assertEqual(reloaded.get("group:1"), "fadianji")
            self.assertEqual(reloaded.all_overrides(), {"group:1": "fadianji"})
            self.assertTrue(reloaded.clear("group:1"))
            self.assertFalse(reloaded.clear("group:1"))
            self.assertIsNone(PersonaOverrideStore(memory_path).get("group:1"))

    def test_ignores_bad_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            (Path(directory) / "persona_overrides.json").write_text(
                '{"group:1": {"persona": ""}, "group:2": "junk", "group:3": {"persona": "x"}}',
                encoding="utf-8",
            )
            store = PersonaOverrideStore(memory_path)
            self.assertIsNone(store.get("group:1"))
            self.assertIsNone(store.get("group:2"))
            self.assertEqual(store.get("group:3"), "x")


if __name__ == "__main__":
    unittest.main()
