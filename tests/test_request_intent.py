"""Offline contracts for the shared context guide; no NoneBot/model initialization."""
from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "catty_qq_ai"
SPEC = importlib.util.spec_from_file_location("_request_intent_test", SOURCE / "request_intent.py")
assert SPEC is not None and SPEC.loader is not None
intent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(intent)


class ContextRequestPromptTests(unittest.TestCase):
    def test_combines_current_history_references_corrections_and_receipts(self) -> None:
        for text in ("当前消息", "最近历史", "引用对象", "补充/纠正", "已成功的工具回执", "不要只凭最后一句"):
            with self.subTest(text=text):
                self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_distinguishes_execute_reply_modify_continue_and_preserves_requirements(self) -> None:
        for text in (
            "实际执行", "直接回复/讨论", "修改已有结果", "继续未完成", "同一目标", "范围", "格式", "语气",
            "只替换对应旧要求", "就换刚才那个", "把第二段改短", "不从头重做", "用户明确换目标",
        ):
            with self.subTest(text=text):
                self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_examples_and_temporary_roleplay_do_not_imply_persistence(self) -> None:
        for text in ("举例", "临时角色扮演", "不自动意味着永久保存", "这轮扮演它", "才将对应设定保存"):
            self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_receipts_prevent_repeating_success_or_fabricating_mutation(self) -> None:
        for text in (
            "保存成功不等于已切换", "不等于已交付", "成功回执时不重复执行", "只执行尚未满足的变更",
            "失败、不可用或缺回执不编造成功", "不要把缺少目标当成重置、清空或修改全部",
        ):
            self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_only_material_missing_details_are_asked_without_cot_or_second_planner(self) -> None:
        for text in (
            "能唯一确定", "不反复追问", "会实质影响结果的关键细节", "无法消解的多目标歧义",
            "主模型直接从上下文填写 definition", "不再请求第二个规划/生成模型", "不输出内部推理过程",
        ):
            self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_base_requires_authoritative_research_without_inventing_a_cutoff(self) -> None:
        prompt = intent.CONTEXT_REQUEST_INTENT_PROMPT
        for text in (
            "知识库无可靠依据", "知识截止之后", "版本/API/价格", "不能靠记忆猜",
            "本轮实际提供的联网工具", "官方或一手资料", "已给权威 URL 时优先抓正文",
            "知识截止日期未提供时不得编造日期", "旧技能中的时效信息仍需重新核实",
        ):
            with self.subTest(text=text):
                self.assertIn(text, prompt)
        self.assertNotRegex(prompt, r"\b(?:19|20)\d{2}(?:[-/.年]\d{1,2}|年)")

    def test_base_does_not_confuse_search_dates_or_failures_with_verification(self) -> None:
        for text in (
            "搜索摘要不等于核实正文", "抓取日期不等于发布日期", "无联网能力、失败或证据不足",
            "区分已知与未核实", "不伪造事实、来源或验证记录", "没查过不能自称查过",
        ):
            with self.subTest(text=text):
                self.assertIn(text, intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_pure_module_has_no_runtime_model_or_harness_dependencies(self) -> None:
        tree = ast.parse((SOURCE / "request_intent.py").read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertTrue(all(isinstance(node, ast.ImportFrom) and node.module == "__future__" for node in imports))
        self.assertFalse(any(isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree)))
        for name in ("openai", "completion", "harness_prompt", "nsfw"):
            self.assertNotIn(name, ast.dump(tree).lower())

    def test_static_tool_hint_is_only_a_short_pointer_and_preserves_harness_pointer(self) -> None:
        tree = ast.parse((SOURCE / "tools.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "tools_system_hint")
        namespace = {"Any": object}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "tools_system_hint", "exec"), namespace)
        for harness in (False, True):
            hint = namespace["tools_system_hint"](SimpleNamespace(name="custom", char_name="角色", semantic_harness_enabled=harness))
            self.assertEqual(hint.count(intent.CONTEXT_REQUEST_INTENT_HEADER), 1)
            pointer = next(line for line in hint.splitlines() if intent.CONTEXT_REQUEST_INTENT_HEADER in line)
            self.assertLess(len(pointer), 80)
            self.assertNotIn(intent.CONTEXT_REQUEST_INTENT_PROMPT, hint)
            self.assertNotIn("把当前消息、最近历史", hint)
            self.assertEqual("【角色·证据使用指南】" in hint, harness)
            self.assertNotIn("【机机·证据使用指南】", hint)


class ContextRequestInjectionTests(unittest.TestCase):
    def test_empty_list_and_non_harness_messages_always_receive_guide(self) -> None:
        for messages in ([], [{"role": "system", "content": "普通人格"}, {"role": "user", "content": "你好"}]):
            before = copy.deepcopy(messages)
            self.assertIsNone(intent.append_context_request_hint(messages))
            self.assertEqual(messages[:-1], before)
            self.assertEqual(messages[-1], {"role": "system", "content": intent.CONTEXT_REQUEST_INTENT_PROMPT})

    def test_after_boundary_before_assistant_prefill_and_idempotent(self) -> None:
        boundary = {"role": "system", "content": "cache boundary", "cache_control": {"type": "ephemeral"}}
        prefill = {"role": "assistant", "content": "（"}
        messages = [boundary, {"role": "user", "content": "就换刚才那个"}, prefill]
        originals = list(messages)
        intent.append_context_request_hint(messages)
        once = copy.deepcopy(messages)
        intent.append_context_request_hint(messages)
        self.assertEqual(messages, once)
        self.assertIs(messages[0], boundary)
        self.assertIs(messages[-1], prefill)
        self.assertEqual(messages[-2]["content"], intent.CONTEXT_REQUEST_INTENT_PROMPT)
        for before, after in zip(originals, messages[:2] + messages[-1:]):
            self.assertIs(before, after)

    def test_duplicate_stale_guides_are_refreshed_and_moved_after_boundary(self) -> None:
        boundary = {"role": "system", "content": "boundary"}
        messages = [
            {"role": "system", "content": intent.CONTEXT_REQUEST_INTENT_HEADER + "\nold"},
            boundary,
            {"role": "system", "content": intent.CONTEXT_REQUEST_INTENT_HEADER},
            {"role": "user", "content": "继续"},
        ]
        intent.append_context_request_hint(messages)
        self.assertEqual(len(messages), 3)
        self.assertIs(messages[0], boundary)
        self.assertEqual(messages[-1]["content"], intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_other_guides_user_header_multimodal_content_and_embedded_header_are_preserved(self) -> None:
        messages = [
            {"role": "system", "content": "【机机·证据使用指南】\n保留已有 v2 教程"},
            {"role": "user", "content": intent.CONTEXT_REQUEST_INTENT_HEADER + "\n用户引用"},
            {"role": "system", "content": [{"type": "text", "text": intent.CONTEXT_REQUEST_INTENT_HEADER}]},
            {"role": "system", "content": "参考：" + intent.CONTEXT_REQUEST_INTENT_HEADER},
            {"role": "tool", "tool_call_id": "p1", "content": '{"ok":true,"status":"saved"}'},
        ]
        originals = list(messages)
        before = copy.deepcopy(messages)
        intent.append_context_request_hint(messages)
        self.assertEqual(messages[:-1], before)
        self.assertTrue(all(old is new for old, new in zip(originals, messages)))

    def test_assistant_tool_call_is_not_mistaken_for_prefill(self) -> None:
        assistant = {"role": "assistant", "content": "", "tool_calls": [{"id": "p1", "type": "function"}]}
        messages = [assistant]
        intent.append_context_request_hint(messages)
        self.assertIs(messages[0], assistant)
        self.assertEqual(messages[-1]["role"], "system")

    def test_empty_prefill_remains_last_after_refresh(self) -> None:
        prefill = {"role": "assistant", "content": "", "tool_calls": []}
        messages = [prefill]
        intent.append_context_request_hint(messages)
        intent.append_context_request_hint(messages)
        self.assertEqual(len(messages), 2)
        self.assertIs(messages[-1], prefill)


class FileCapabilityHintTests(unittest.TestCase):
    def guide(self, names):
        messages = [{"role": "user", "content": "把刚才那个整理好给我"}]
        intent.append_context_request_hint(messages, names)
        return messages[-1]["content"]

    def test_write_file_is_available_without_requiring_literal_file_keyword(self):
        guide = self.guide({"catty_write_file"})
        for fragment in ("catty_write_file", "完整代码", "文档", "当前会话", "短示例", "可选能力", "staged"):
            self.assertIn(fragment, guide)
        self.assertNotIn("catty_python_run", guide)
        self.assertNotIn("必须调用", guide)

    def test_python_hint_only_when_run_tool_is_available(self):
        guide = self.guide({"catty_python_run"})
        self.assertIn("catty_python_run", guide)
        self.assertNotIn("catty_write_file", guide)
        self.assertNotIn("catty_python_install", guide)

    def test_no_file_capability_keeps_base_guide(self):
        for names in ((), ("catty_python_install",), ("catty_write_files",), ("catty_web_search_extra",)):
            self.assertEqual(self.guide(names), intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_refresh_removes_withdrawn_capability_and_preserves_prefill(self):
        prefill = {"role": "assistant", "content": "（"}
        messages = [{"role": "user", "content": "继续"}, prefill]
        intent.append_context_request_hint(messages, {"catty_write_file"})
        intent.append_context_request_hint(messages)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-2]["content"], intent.CONTEXT_REQUEST_INTENT_PROMPT)
        self.assertIs(messages[-1], prefill)

    def test_tool_order_duplicates_and_unknown_names_do_not_change_guidance(self):
        self.assertEqual(
            self.guide(("catty_write_file", "catty_python_run")),
            self.guide(("catty_python_run", "unknown", "catty_write_file", "catty_write_file")),
        )

    def test_sandbox_prompt_does_not_reserve_all_file_creation_for_python(self):
        tree = ast.parse((SOURCE / "code_sandbox.py").read_text(encoding="utf-8"))
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "CODE_SANDBOX_SYSTEM_PROMPT" for t in n.targets))
        prompt = ast.literal_eval(assignment.value)
        self.assertNotIn("需要写/跑 Python、生成文件", prompt)
        self.assertIn("无需运行", prompt)
        self.assertIn("Python 工具生成的文件", prompt)

    def test_production_and_simulation_use_available_names_without_forced_choice(self):
        for filename in ("__init__.py", "catty_sim_chat.py"):
            tree = ast.parse((SOURCE / filename).read_text(encoding="utf-8"))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "append_context_request_hint"]
            self.assertTrue(any("_allowed_tool_names" in ast.dump(n) for n in calls), filename)
            choices = [n for n in ast.walk(tree) if isinstance(n, ast.AnnAssign)
                       and isinstance(n.target, ast.Name) and n.target.id in {"_forced_tool_choice", "_tool_choice"}]
            self.assertTrue(choices, filename)
            self.assertTrue(all(isinstance(n.value, ast.Constant) and n.value.value == "auto" for n in choices))


class SkillResearchHintTests(unittest.TestCase):
    TOOL_NAMES = (
        "catty_skill_search", "catty_skill_load", "catty_skill_save",
        "catty_web_search", "catty_fetch_url",
    )

    def guide(self, names=()):
        messages = [{"role": "user", "content": "继续处理刚才的问题"}]
        intent.append_context_request_hint(messages, names)
        return messages[-1]["content"]

    def test_no_tools_install_and_similar_names_do_not_claim_skills(self):
        for names in (
            (), ("catty_python_install",), ("catty_skill_install",),
            ("catty_skill_search_extra", "catty_skill_loader", "catty_skill_save_all"),
            ("search_skills", "Catty_skill_search", "catty_fetch_urls", "catty_web_search_extra"),
        ):
            with self.subTest(names=names):
                self.assertEqual(intent.build_skill_research_hint(names), "")
                self.assertEqual(self.guide(names), intent.CONTEXT_REQUEST_INTENT_PROMPT)

    def test_each_exact_name_only_describes_that_available_entry(self):
        for name in self.TOOL_NAMES:
            with self.subTest(name=name):
                hint = intent.build_skill_research_hint((name,))
                self.assertIn("本轮提供 " + name, hint)
                for unavailable in set(self.TOOL_NAMES) - {name}:
                    self.assertNotIn(unavailable, hint)
                self.assertEqual(self.guide((name,)), intent.CONTEXT_REQUEST_INTENT_PROMPT + "\n\n" + hint)

    def test_first_round_search_is_candidates_only_without_catalog_body_or_future_schema(self):
        hint = intent.build_skill_research_hint(("catty_skill_search", "private-skill-id-sentinel"))
        for text in (
            "用 query", "只返回候选 metadata，不是正文", "不预先列出全库技能清单或正文",
            "不把候选摘要当作已加载", "检索成功后系统才在下一轮提供候选加载能力",
            "未开放前不调用、不声称已读正文",
        ):
            self.assertIn(text, hint)
        for text in (
            "catty_skill_load", "catty_skill_save", "receipt_ids", "existing_id",
            "private-skill-id-sentinel", '"parameters"', '"properties"',
        ):
            self.assertNotIn(text, hint)

    def test_substantial_tasks_search_then_select_then_read_not_every_greeting(self):
        hint = intent.build_skill_research_hint(("catty_skill_search", "catty_skill_load"))
        for text in (
            "有实质任务先查适合的技能", "没有精确匹配就查相邻领域", "先检索、再从候选中选择",
            "选后读正文再做", "普通问候或闲聊不必强行搜索", "用 id 加载本轮检索命中的已选候选",
            "不猜造 id", "失败不声称已加载",
        ):
            self.assertIn(text, hint)
        self.assertLess(hint.index("先检索"), hint.index("再从候选中选择"))
        self.assertLess(hint.index("再从候选中选择"), hint.index("选后读正文再做"))

    def test_search_without_fetch_does_not_promise_body_verification(self):
        guide = self.guide(("catty_web_search",))
        for text in (
            "用 query 查找官方或一手公共资料", "搜索结果只用于定位来源",
            "本轮未提供网页正文抓取入口", "不能把搜索摘要说成已核实正文",
            "没查过不能自称查过",
        ):
            self.assertIn(text, guide)
        self.assertNotIn("catty_fetch_url", guide)
        self.assertNotIn("catty_skill_save", guide)

    def test_fetch_uses_given_url_and_receipt_without_premature_save_entry(self):
        guide = self.guide(("catty_fetch_url",))
        for text in (
            "用 url 抓取公共网页正文", "已有权威 URL 就直接抓取", "skill_receipt_id",
            "不把抓取成功本身当成事实已核实", "失败不得伪造回执",
            "后续轮次提供知识保存能力", "当前没有该入口时不宣称已入库",
        ):
            self.assertIn(text, guide)
        self.assertNotIn("catty_web_search", guide)
        self.assertNotIn("catty_skill_save", guide)
        self.assertNotIn("receipt_ids", guide)

    def test_confirmed_research_learns_only_public_reusable_knowledge(self):
        for names in (("catty_fetch_url",), ("catty_skill_save",), self.TOOL_NAMES):
            with self.subTest(names=names):
                hint = intent.build_skill_research_hint(names)
                for text in (
                    "查证得到可复用公共知识后", "自动通过实际开放的保存能力",
                    "适用条件、步骤、验证和来源", "供所有用户共享检索", "私人信息不入库",
                    "不保存用户聊天、身份或凭据",
                ):
                    self.assertIn(text, hint)

    def test_save_requires_current_receipts_and_only_loaded_existing_id(self):
        hint = intent.build_skill_research_hint(("catty_skill_save",))
        for text in (
            "只提交本轮成功抓取回执的 receipt_ids", "系统内部隔离生成通用知识",
            "不接受任意聊天 body", "可选 existing_id 仅限本轮已加载的技能", "没有则省略",
            "只有成功保存回执才能确认入库", "失败不得声称已保存",
        ):
            self.assertIn(text, hint)
        self.assertNotIn("catty_skill_load", hint)

    def test_order_duplicates_unknown_names_and_generator_are_equivalent(self):
        names = ("catty_write_file", "catty_python_run", *self.TOOL_NAMES)
        expected = self.guide(names)
        self.assertEqual(self.guide((*reversed(names), "unknown", *names)), expected)
        self.assertEqual(self.guide(name for name in names), expected)
        self.assertEqual(
            intent.build_skill_research_hint(iter(self.TOOL_NAMES)),
            intent.build_skill_research_hint(self.TOOL_NAMES),
        )

    def test_same_system_keeps_file_hint_before_research_and_preserves_other_messages(self):
        boundary = {"role": "system", "content": "boundary", "cache_control": {"type": "ephemeral"}}
        user = {"role": "user", "content": "查官方资料，给我整理成文件"}
        prefill = {"role": "assistant", "content": "（"}
        messages = [boundary, user, prefill]
        names = ("catty_write_file", *self.TOOL_NAMES)
        intent.append_context_request_hint(messages, names)
        self.assertEqual(len(messages), 4)
        self.assertIs(messages[0], boundary)
        self.assertIs(messages[1], user)
        self.assertIs(messages[-1], prefill)
        guide = messages[-2]["content"]
        self.assertEqual(messages[-2]["role"], "system")
        self.assertEqual(
            guide,
            intent.CONTEXT_REQUEST_INTENT_PROMPT + "\n\n" + intent.build_file_delivery_hint(names)
            + "\n\n" + intent.build_skill_research_hint(names),
        )
        before = copy.deepcopy(messages)
        intent.append_context_request_hint(messages, names)
        self.assertEqual(messages, before)

    def test_refresh_removes_all_withdrawn_entries_and_returns_to_base(self):
        prefill = {"role": "assistant", "content": "（"}
        user = {"role": "user", "content": "继续"}
        messages = [user, prefill]
        intent.append_context_request_hint(messages, ("catty_write_file", *self.TOOL_NAMES))
        intent.append_context_request_hint(messages, ("catty_skill_search",))
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-2]["content"], self.guide(("catty_skill_search",)))
        for name in ("catty_write_file", *self.TOOL_NAMES[1:]):
            self.assertNotIn(name, messages[-2]["content"])
        intent.append_context_request_hint(messages)
        self.assertEqual(len(messages), 3)
        self.assertIs(messages[0], user)
        self.assertIs(messages[-1], prefill)
        self.assertEqual(messages[-2]["content"], intent.CONTEXT_REQUEST_INTENT_PROMPT)
        self.assertNotIn("【本轮技能与联网能力】", messages[-2]["content"])
        self.assertNotIn("本轮提供", messages[-2]["content"])


if __name__ == "__main__":
    unittest.main()