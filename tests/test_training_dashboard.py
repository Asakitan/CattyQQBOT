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


if __name__ == "__main__":
    unittest.main()
