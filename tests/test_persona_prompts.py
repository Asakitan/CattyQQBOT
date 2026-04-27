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


if __name__ == "__main__":
    unittest.main()
