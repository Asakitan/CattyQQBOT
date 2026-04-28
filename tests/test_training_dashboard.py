from pathlib import Path
import importlib.util
import json
import tempfile
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "catty_training_dashboard.py"
_SPEC = importlib.util.spec_from_file_location("catty_training_dashboard", _MODULE_PATH)
_dashboard = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_dashboard)


class TrainingDashboardTests(unittest.TestCase):
    def test_snapshot_reads_progress_audit_suggestions_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            dataset_path = root / "training" / "reply_gate_dataset.jsonl"
            output_dir = root / "training" / "reply_gate_lora"
            output_dir.mkdir(parents=True, exist_ok=True)
            dataset_path.write_text("{}\n{}\n", encoding="utf-8")
            (root / "training" / "local_training.log").write_text("line1\nline2\n", encoding="utf-8")
            (output_dir / "last_idle_status.json").write_text(
                json.dumps(
                    {
                        "task": "reply_gate",
                        "mode": "idle",
                        "status": "completed",
                        "created_at": 10,
                        "artifact_audit": {
                            "status": "approved",
                            "allow_apply": True,
                            "allow_merge": False,
                            "risk_level": "low",
                            "next_suggestions": ["collect more directed messages"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "audit_ai": {"model": "GLM-5.1"},
                        "local_training": {
                            "enabled": True,
                            "dataset_path": "training/reply_gate_dataset.jsonl",
                            "output_dir": "training/reply_gate_lora",
                            "artifact_audit_temperature": 0.5,
                            "progress_log_path": "training/local_training.log",
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = _dashboard.build_snapshot(config_path)

            self.assertEqual(snapshot["audit_model"], "GLM-5.1")
            self.assertEqual(snapshot["audit_temperature"], 0.5)
            self.assertEqual(snapshot["datasets"]["reply_gate"]["samples"], 2)
            self.assertEqual(snapshot["suggestions"]["reply_gate"], ["collect more directed messages"])
            self.assertIn("line2", snapshot["log_tail"])

    def test_model_test_prompt_and_score_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"system_prompt": "你是主 AI 笨猫。"},
                        "local_critic": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:1.5b"},
                        "local_training": {"model_test_scores_path": "training/scores.jsonl"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            messages = _dashboard.build_model_test_messages(config_path, "测试一下")
            self.assertEqual(messages[0]["content"], "你是主 AI 笨猫。")
            self.assertIn("笨猫人格记忆", str(messages[1]["content"]))
            self.assertIn("主回复智能策略", str(messages[2]["content"]))
            self.assertIn("回复前请在心里完成一次自检", str(messages[3]["content"]))
            self.assertIn("主线程模拟请求", str(messages[5]["content"]))
            self.assertIn("测试窗口模拟记忆", str(messages[6]["content"]))
            self.assertEqual(messages[-1]["content"], "测试一下")
            self.assertEqual(messages[7]["content"], "/no_think")
            thinking_messages = _dashboard.build_model_test_messages(config_path, "测试一下", thinking=True)
            self.assertTrue(any("禁止把笨猫当成第三个人" in str(message.get("content")) for message in thinking_messages))
            self.assertTrue(any("Thinking 测试模式" in str(message.get("content")) for message in thinking_messages))
            self.assertNotIn("/no_think", [message["content"] for message in thinking_messages])
            route = _dashboard._route_config(_dashboard._load_json(config_path))
            self.assertEqual(route["extra_body"], {"think": False})
            self.assertEqual(route["thinking_max_tokens"], 96)
            self.assertEqual(route["thinking_timeout"], 20.0)
            self.assertEqual(_dashboard._ollama_chat_url(route["base_url"]), "http://127.0.0.1:11434/api/chat")
            self.assertTrue(_dashboard._looks_like_ollama_route(route["base_url"], route["api_key"], route["extra_body"]))
            self.assertEqual(
                _dashboard._ollama_options(temperature=0.1, max_tokens=96, extra_body=route["extra_body"])["num_predict"],
                96,
            )

            saved = _dashboard.save_model_eval(
                config_path,
                {
                    "model": "qwen2.5:1.5b",
                    "elapsed_seconds": 1.23,
                    "prompt": "测试一下",
                    "response": "喵～",
                },
                score=4,
                note="good",
            )

            self.assertEqual(saved, root / "training" / "scores.jsonl")
            records = _dashboard.latest_model_evals(config_path)
            self.assertEqual(records[0]["score"], 4)
            self.assertEqual(records[0]["note"], "good")


if __name__ == "__main__":
    unittest.main()
