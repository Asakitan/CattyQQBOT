from argparse import Namespace
from pathlib import Path
import importlib.util
import json
import tempfile
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "local_lora_train.py"
_SPEC = importlib.util.spec_from_file_location("local_lora_train", _MODULE_PATH)
_trainer = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_trainer)


class LocalLoraTrainTests(unittest.TestCase):
    def test_safe_wrapper_skips_without_backend_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            dataset_path = root / "training" / "assistant_reply_dataset.jsonl"
            output_dir = root / "training" / "assistant_reply_lora"
            dataset_path.parent.mkdir(parents=True, exist_ok=True)
            dataset_path.write_text(json.dumps({"messages": []}) + "\n", encoding="utf-8")
            config_path.write_text(json.dumps({"local_training": {}}), encoding="utf-8")

            code = _trainer.run(
                Namespace(
                    dataset=str(dataset_path),
                    output_dir=str(output_dir),
                    config=str(config_path),
                    task="assistant_reply",
                    mode="busy",
                )
            )

            self.assertEqual(code, _trainer.SKIPPED_EXIT_CODE)
            status = json.loads((output_dir / "last_busy_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "skipped")
            self.assertEqual(status["sample_count"], 1)

    def test_wrapper_records_adapter_artifact_after_backend_training(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            dataset_path = root / "training" / "reply_gate_dataset.jsonl"
            output_dir = root / "training" / "reply_gate_lora"
            dataset_path.parent.mkdir(parents=True, exist_ok=True)
            dataset_path.write_text(json.dumps({"messages": []}) + "\n", encoding="utf-8")
            command = (
                '"{python}" -c "from pathlib import Path; '
                "p=Path(r'{output_dir}'); p.mkdir(parents=True, exist_ok=True); "
                "(p/'adapter_model.safetensors').write_text('ok', encoding='utf-8')\""
            )
            config_path.write_text(
                json.dumps({"local_training": {"backend_command": command, "artifact_audit_enabled": False}}),
                encoding="utf-8",
            )

            code = _trainer.run(
                Namespace(
                    dataset=str(dataset_path),
                    output_dir=str(output_dir),
                    config=str(config_path),
                    task="reply_gate",
                    mode="idle",
                )
            )

            self.assertEqual(code, 0)
            status = json.loads((output_dir / "last_idle_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")
            self.assertTrue(status["artifact"]["has_artifact"])
            self.assertEqual(status["apply_adapter"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
