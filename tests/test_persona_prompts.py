"""persona_prompts 单测 (2026-06-02 重写版).

主人 2026-05~06 期间把 persona_prompts.py 多段 prompt 改成连排条目 + 去标题行,
旧测试 assert 字面已对不上新代码. 主人裁定"代码 = 真相, 测试要跟着代码走",
本文件全部按 dump_persona_prompts.py 输出的真实字面重写 assertIn / assertNotIn,
不动 _persona_prompts 模块本身, 不动业务逻辑.
"""
from pathlib import Path
import importlib.util
from types import SimpleNamespace
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "persona_prompts.py"
_SPEC = importlib.util.spec_from_file_location("persona_prompts", _MODULE_PATH)
_persona_prompts = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_persona_prompts)


class PersonaPromptsTests(unittest.TestCase):
    def test_self_check_mentions_intent_and_markers(self) -> None:
        prompt = _persona_prompts.build_reply_self_check_prompt("NOPE", "SPLIT")

        # 主人多次精简后, 自检 prompt 改成连排条目 (砍光"禁止把笨猫当成第三个人"
        # /"脱离人格倾向"/"主回复智能策略"等独立标题). 锁骨架: 自检开头 / 拆句 /
        # 脱戏自纠 / 心里重读人格 / 猫系动作 / 现场感 / 删客服 / 不复读 / NOPE / SPLIT.
        self.assertIn("【演员心态自检", prompt)
        self.assertIn("拆 user 句", prompt)
        self.assertIn("脱戏自纠", prompt)
        self.assertIn("心里重读人格", prompt)
        self.assertIn("猫系动作收尾", prompt)
        self.assertIn("技术翻译现场感", prompt)
        self.assertIn("输出前删一句客服", prompt)
        self.assertIn("不复读上轮", prompt)
        self.assertIn("不该回只输出 NOPE", prompt)
        self.assertIn("NSFW处理", prompt)
        self.assertIn("NOPE", prompt)
        self.assertIn("SPLIT", prompt)

    def test_examples_use_given_no_reply_marker(self) -> None:
        prompt = _persona_prompts.build_catgirl_examples_prompt("NOPE")

        # 主人改写为 5 模式覆盖 (单段/反差链/技术分步/拍板/NSFW) + 末尾
        # "不该回: NOPE" 行. 旧的"怎么还没好/这个能不能别硬编码/一点都不可爱"
        # 单例被删, 这里只锁结构性骨架 + NOPE marker 替换正确.
        self.assertIn("猫娘回复范例", prompt)
        self.assertIn("5 模式覆盖", prompt)
        self.assertIn("# 模式 1 (单段 - 闲聊)", prompt)
        self.assertIn("# 模式 2 (反差链 - 摸头/暧昧)", prompt)
        self.assertIn("# 模式 3 (技术分步)", prompt)
        self.assertIn("# 模式 4 (拍板比较)", prompt)
        self.assertIn("# 模式 5 (NSFW 主人触发)", prompt)
        self.assertIn("# 不该回: NOPE", prompt)
        self.assertIn("ฅฅ", prompt)
        self.assertIn("NOPE", prompt)
        # marker 替换确实把传入的 NOPE 用上了, 没漏到老 CATTY marker
        self.assertNotIn("<<<CATTY_NO_REPLY>>>", prompt)

    def test_persona_memory_anchors_identity_without_copying_full_prompt(self) -> None:
        prompt = _persona_prompts.build_persona_memory_prompt("你现在是米雪儿，长设定。")

        # 主人改为人格记忆一段连排条目, "角色同步确认"独立标题被吸收进条目.
        # 仍存在的稳定骨架: 笨猫人格记忆开头 / 米雪儿-笨猫-猫猫同指 / 不要当第三人
        # / 重新阅读主 prompt / 第一人称回应 / NSFW处理.
        self.assertIn("笨猫人格记忆", prompt)
        self.assertIn("米雪儿、笨猫、猫猫都指你本人", prompt)
        self.assertIn("不要把笨猫当第三个人", prompt)
        self.assertIn("重新阅读主 prompt", prompt)
        self.assertIn("第一人称回应", prompt)
        self.assertIn("NSFW处理", prompt)

    def test_reply_intelligence_prompt_guides_context_use(self) -> None:
        # 主人进一步精简为 4 条编号要点, 去掉了记忆使用/长短判定/信息不足/
        # 情绪优先等子标题. 这里只锁住 4 条骨架 + 关键 ≤50 字硬规则.
        prompt = _persona_prompts.build_reply_intelligence_prompt("NOPE")

        self.assertIn("**回复策略**", prompt)
        self.assertIn("拆 user 句 4 件事", prompt)
        self.assertIn("结论再最小步骤", prompt)
        self.assertIn("默认短", prompt)
        self.assertIn("≤50 字", prompt)
        self.assertIn("拍板给推荐项", prompt)
        # 已被砍掉的旧子句不应再出现
        self.assertNotIn("不要把记忆整段背出来", prompt)
        self.assertNotIn("必须覆盖用户提出的具体目标", prompt)
        self.assertNotIn("先回答 B", prompt)
        self.assertNotIn("不要说只看到几个词", prompt)

    def test_reply_intelligence_prompt_accepts_default_marker(self) -> None:
        # 精简后 NO_REPLY marker 不再嵌入此 prompt, 调用时即使不传参数也应正常返回.
        prompt = _persona_prompts.build_reply_intelligence_prompt()
        self.assertIn("**回复策略**", prompt)

    def test_group_meme_literacy_prompt_guides_abstract_group_chat(self) -> None:
        prompt = _persona_prompts.build_group_meme_literacy_prompt()

        # 主人改写为单段连排, "群友抽象语境理解"标题变为"群友抽象语境:"前缀.
        # 关键骨架: 梗词清单 (难绷) / 玩梗/反讽/抽象 / 短促接梗不长篇科普.
        self.assertIn("群友抽象语境", prompt)
        self.assertIn("玩梗", prompt)
        self.assertIn("难绷", prompt)
        self.assertIn("短促接梗", prompt)
        self.assertIn("不要按字面硬解释", prompt)

    def test_conversation_flow_prompt_covers_multi_speaker_and_anti_repeat(self) -> None:
        prompt = _persona_prompts.build_conversation_flow_prompt()

        # 主人改写为 5 条编号要点, "群聊上下文与对话流感知"标题改为"群聊对话流:".
        # 保留: 输入格式 / 多人混聊识别 / 冷场不硬救 / 签到短路.
        self.assertIn("群聊对话流", prompt)
        self.assertIn("昵称(QQ): 正文", prompt)
        self.assertIn("先识别真在叫你的", prompt)
        self.assertIn("找不到清晰指向就短问", prompt)
        self.assertIn("冷场不硬救", prompt)
        self.assertIn("不站队不递刀", prompt)
        self.assertIn("签到", prompt)
        # 旧子句不应再出现
        self.assertNotIn("不要把别人之间的对话当成在跟你说话", prompt)
        self.assertNotIn("不要原样复读", prompt)
        self.assertNotIn("不要重复同一句口头禅", prompt)

    def test_semantic_perception_prompt_covers_irony_and_short_signals(self) -> None:
        prompt = _persona_prompts.build_semantic_perception_prompt()

        # 主人改写为 5 条编号要点, "语义感知和情绪辨识"标题变为"语义感知:"前缀.
        # 关键骨架: 反讽夸 / 短句单字情绪信号 / 缩写 yyds / 矛盾信号 (傲娇) / 擦边.
        self.assertIn("语义感知", prompt)
        self.assertIn("反讽夸", prompt)
        self.assertIn("短句单字", prompt)
        self.assertIn("yyds", prompt)
        self.assertIn("矛盾信号", prompt)
        self.assertIn("按傲娇体察", prompt)
        # 旧标题/子句不应再出现
        self.assertNotIn("阴阳怪气", prompt)
        self.assertNotIn("不要照单全收", prompt)

    def test_image_literacy_prompt_distinguishes_image_functions(self) -> None:
        prompt = _persona_prompts.build_image_literacy_prompt()

        # 这条没改过, 仍稳定. 锁骨架: 图文区分 / 递梗 / 不原样念 / 文字是主语义锚点
        # / 截图类型 / NSFW 上下文 / 不硬猜长篇.
        self.assertIn("图片/表情/梗图理解", prompt)
        self.assertIn("递梗", prompt)
        self.assertIn("不要原样念出来", prompt)
        self.assertIn("文字是主语义锚点", prompt)
        self.assertIn("代码/报错截图", prompt)
        self.assertIn("NSFW", prompt)
        self.assertIn("不要硬猜出长篇分析", prompt)

    def test_image_literacy_prompt_uses_current_catty_context(self) -> None:
        prompt = _persona_prompts.build_image_literacy_prompt()
        self.assertEqual(
            prompt,
            _persona_prompts.build_image_literacy_prompt(SimpleNamespace(name="catty")),
        )
        self.assertIn("先结合语义感知和当前聊天语境判断", prompt)
        self.assertNotIn("第 16 条", prompt)

    def test_catty_scene_rules_allow_related_followup(self) -> None:
        prompt = _persona_prompts.build_scene_discrimination_prompt("NOPE")
        self.assertIn("当前发言者已有相关续聊", prompt)
        self.assertIn("没有相关续聊或主动任务", prompt)
        self.assertIn("猫系动作按语境可选", prompt)
        self.assertIn("NOPE", prompt)

    def test_image_literacy_prompt_is_neutral_for_non_catty_personas(self) -> None:
        anchors = (
            "当前人格",
            "图在本轮对话里的'功能'",
            "vision 给的图像描述",
            "背景信息",
            "文字是主语义锚点",
            "代码/报错截图",
            "不确定图意思",
            "同一张图/同一个表情包",
        )
        forbidden = (
            "主人",
            "喵",
            "笨猫",
            "猫猫",
            "猫娘",
            "wave",
            "emoji",
            "一镜到底",
            "快速妥协",
        )

        for label, persona in (
            ("fadianji", SimpleNamespace(name="fadianji")),
            ("unknown-name", SimpleNamespace(name="unknown")),
            ("unknown-object", object()),
        ):
            with self.subTest(persona=label):
                prompt = _persona_prompts.build_image_literacy_prompt(persona)
                for anchor in anchors:
                    self.assertIn(anchor, prompt)
                for text in forbidden:
                    self.assertNotIn(text, prompt)

    def test_prompt_manager_passes_persona_to_image_literacy_builder(self) -> None:
        manager_path = _MODULE_PATH.with_name("prompt_manager.py")
        source = manager_path.read_text(encoding="utf-8")

        self.assertIn(
            "content_fn=lambda: _pp.build_image_literacy_prompt(persona),",
            source,
        )

    def test_scene_discrimination_prompt_has_six_step_pipeline(self) -> None:
        prompt = _persona_prompts.build_scene_discrimination_prompt("NOPE")

        # 主人改写为 5 步 + 末尾"误判避坑"段, 砍掉了方括号编号格式. 实际只有 5 步,
        # 但因为功能是"接话前心里过几步"清单, 仍按 test 名字里的"six_step"保留.
        self.assertIn("接话前心里过几步", prompt)
        self.assertIn("1) 是不是叫你", prompt)
        self.assertIn("2) 关系", prompt)
        self.assertIn("3) 任务类型", prompt)
        self.assertIn("4) 情绪诚意", prompt)
        self.assertIn("5) 输出决策", prompt)
        self.assertIn("误判避坑", prompt)
        # 旧方括号编号格式不应再出现
        self.assertNotIn("[第一步 是否在叫你]", prompt)
        self.assertNotIn("[常见误判清单 自查避坑]", prompt)
        self.assertNotIn("反讽夸被当真夸", prompt)
        self.assertNotIn("陌生人被当主人", prompt)
        self.assertIn("NOPE", prompt)

    def test_disambiguation_examples_prompt_shows_context_pairs(self) -> None:
        prompt = _persona_prompts.build_disambiguation_examples_prompt("NOPE")

        # 主人精简到 3 个对照 (单字? / 随便 / 帮我查一下), 砍掉了"真有你的/
        # 你-猫猫-到底/你好烦"等案例. 保留: 情景消歧对照例句 + 3 个 # 对照段 +
        # 风格要点末尾总结.
        self.assertIn("情景消歧对照例句", prompt)
        self.assertIn("# 对照: 单字 '?'", prompt)
        self.assertIn("# 对照: '随便'", prompt)
        self.assertIn("# 对照: '帮我查一下'", prompt)
        self.assertIn("先看上下文判定场景再选反应", prompt)
        self.assertIn("NOPE", prompt)
        # 旧对照案例不应再出现
        self.assertNotIn("# 对照 1：'真有你的'", prompt)
        self.assertNotIn("# 对照 11：'你好烦'", prompt)

    def test_scenario_playbook_prompt_covers_key_scenes(self) -> None:
        prompt = _persona_prompts.build_scenario_playbook_prompt("NOPE")

        # 主人改写为 `- 场景:` 连排短行, 砍掉了方括号格式和"多人热聊/有人吵架/
        # 反讽夸你/已经说过的内容"等场景. 保留: 情景应对前缀 + 6 类关键场景.
        self.assertIn("情景应对", prompt)
        self.assertIn("- 被夸:", prompt)
        self.assertIn("- 被反撩/暧昧:", prompt)
        self.assertIn("- 主人催:", prompt)
        self.assertIn("- 越界/敏感:", prompt)
        self.assertIn("- NSFW/性暗示:", prompt)
        self.assertIn("- 不是叫你/不该插话:", prompt)
        self.assertIn("NOPE", prompt)
        # 旧方括号格式不应再出现
        self.assertNotIn("[被夸]", prompt)
        self.assertNotIn("[多人热聊不问你]", prompt)
        self.assertNotIn("[已经说过的内容/复读自己]", prompt)


if __name__ == "__main__":
    unittest.main()
