from pathlib import Path
import importlib.util
import json
import tempfile
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "export_reply_gate_dataset.py"
_SPEC = importlib.util.spec_from_file_location("export_reply_gate_dataset", _MODULE_PATH)
_exporter = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_exporter)


class ReplyGateTrainingExportTests(unittest.TestCase):
    def test_export_reply_gate_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            samples_path = root / "local_critic_samples.jsonl"
            dataset_path = root / "training" / "reply_gate_dataset.jsonl"
            config_path.write_text(
                json.dumps(
                    {
                        "local_critic": {"training_samples_path": "local_critic_samples.jsonl"},
                        "local_training": {"dataset_path": "training/reply_gate_dataset.jsonl"},
                    }
                ),
                encoding="utf-8",
            )
            samples_path.write_text(
                json.dumps(
                    {
                        "event": {
                            "message_type": "group",
                            "user_message": "猫猫你看看",
                            "plain_text": "猫猫你看看",
                            "mentioned": False,
                            "replied_to_self": False,
                            "used_prefix": False,
                            "directed": True,
                            "directed_strength": "direct_address",
                            "directly_requested": True,
                            "opportunistic": False,
                        },
                        "critic": {
                            "reply_gate": {
                                "should_reply": True,
                                "confidence": 95,
                                "reason": "直接喊猫猫",
                                "training_tags": ["direct_address"],
                            }
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            count, exported_path = _exporter.export_dataset(config_path)

            self.assertEqual(count, 1)
            self.assertEqual(exported_path, dataset_path)
            exported = json.loads(dataset_path.read_text(encoding="utf-8").strip())
            self.assertEqual(exported["messages"][-1]["role"], "assistant")
            self.assertIn('"should_reply": true', exported["messages"][-1]["content"])

    def test_export_assistant_reply_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            samples_path = root / "training" / "assistant_reply_samples.jsonl"
            dataset_path = root / "training" / "assistant_reply_dataset.jsonl"
            config_path.write_text(
                json.dumps(
                    {
                        "local_training": {
                            "assistant_samples_path": "training/assistant_reply_samples.jsonl",
                            "assistant_dataset_path": "training/assistant_reply_dataset.jsonl",
                        }
                    }
                ),
                encoding="utf-8",
            )
            samples_path.parent.mkdir(parents=True, exist_ok=True)
            samples_path.write_text(
                json.dumps(
                    {
                        "kind": "assistant_reply",
                        "messages": [
                            {"role": "system", "content": "你是笨猫。"},
                            {"role": "user", "content": "猫猫在吗"},
                        ],
                        "final_reply": "在啦主人，喵～",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            count, exported_path = _exporter.export_assistant_reply_dataset(config_path)

            self.assertEqual(count, 1)
            self.assertEqual(exported_path, dataset_path)
            exported = json.loads(dataset_path.read_text(encoding="utf-8").strip())
            self.assertEqual(exported["messages"][-1], {"role": "assistant", "content": "在啦主人，喵～"})


if __name__ == "__main__":
    unittest.main()
