from pathlib import Path
import importlib.util
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "persona_prompts.py"
_SPEC = importlib.util.spec_from_file_location("persona_prompts", _MODULE_PATH)
_persona_prompts = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_persona_prompts)


class PersonaPromptsTests(unittest.TestCase):
    def test_self_check_mentions_intent_and_markers(self) -> None:
        prompt = _persona_prompts.build_reply_self_check_prompt("NOPE", "SPLIT")

        self.assertIn("禁止把笨猫当成第三个人", prompt)
        self.assertIn("脱离人格倾向", prompt)
        self.assertIn("主回复智能策略", prompt)
        self.assertIn("NSFW处理", prompt)
        self.assertIn("重读主 prompt", prompt)
        self.assertIn("用户真实意图", prompt)
        self.assertIn("猫系动作", prompt)
        self.assertIn("现场感", prompt)
        self.assertIn("删掉一句最像客服", prompt)
        self.assertIn("NOPE", prompt)
        self.assertIn("SPLIT", prompt)

    def test_examples_use_given_no_reply_marker(self) -> None:
        prompt = _persona_prompts.build_catgirl_examples_prompt("NOPE")

        self.assertIn("先理解，再回应", prompt)
        self.assertIn("怎么还没好", prompt)
        self.assertIn("这个能不能别硬编码", prompt)
        self.assertIn("一点都不可爱", prompt)
        self.assertIn("(ฅ>ω<*ฅ)", prompt)
        self.assertIn("ヾ(≧▽≦*)o", prompt)
        self.assertIn("好回复：NOPE", prompt)
        self.assertNotIn("<<<CATTY_NO_REPLY>>>", prompt)

    def test_persona_memory_anchors_identity_without_copying_full_prompt(self) -> None:
        prompt = _persona_prompts.build_persona_memory_prompt("你现在是米雪儿，长设定。")

        self.assertIn("笨猫人格记忆", prompt)
        self.assertIn("米雪儿、笨猫、猫猫都指你本人", prompt)
        self.assertIn("不要把笨猫当第三个人", prompt)
        self.assertIn("重新阅读主 prompt", prompt)
        self.assertIn("角色同步确认", prompt)

    def test_reply_intelligence_prompt_guides_context_use(self) -> None:
        prompt = _persona_prompts.build_reply_intelligence_prompt("NOPE")

        self.assertIn("主回复智能策略", prompt)
        self.assertIn("任务类型", prompt)
        self.assertIn("主语和指代", prompt)
        self.assertIn("不要只因为出现关键词", prompt)
        self.assertIn("不要把记忆整段背出来", prompt)
        self.assertIn("必须覆盖用户提出的具体目标", prompt)
        self.assertIn("方式/方案/怎么做", prompt)
        self.assertIn("先回答 B", prompt)
        self.assertIn("不要说只看到几个词", prompt)
        self.assertIn("NOPE", prompt)

    def test_group_meme_literacy_prompt_guides_abstract_group_chat(self) -> None:
        prompt = _persona_prompts.build_group_meme_literacy_prompt()

        self.assertIn("群友抽象语境理解", prompt)
        self.assertIn("玩梗", prompt)
        self.assertIn("难绷", prompt)
        self.assertIn("不要长篇科普", prompt)


if __name__ == "__main__":
    unittest.main()
