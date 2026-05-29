"""S5.6k (主人 2026-05-29): Ollama 独立控制面板.

简单 Tkinter GUI:
- Start/Stop Ollama daemon (含 affinity 0xF = 4 核 0-3)
- 选模型 (qwen2.5:1.5b / 3b / 7b) load 到内存 (keep_alive 30m)
- 查看 ps / list / 内存占用
- 实时日志

用法 (远端):
    D:/CattyQQAI/.venv/Scripts/python.exe scripts/ollama_gui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from urllib import request


ROOT = Path(__file__).resolve().parent.parent
OLLAMA_EXE = ROOT / "tools" / "ollama" / "ollama.exe"
OLLAMA_MODELS = ROOT / "models" / "ollama"
OLLAMA_URL = "http://127.0.0.1:11434"
AFFINITY_MASK = 0xF  # 核 0-3 (4 核)


def _http_get(path: str, timeout: float = 5.0) -> dict | None:
    try:
        with request.urlopen(f"{OLLAMA_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _http_post(path: str, body: dict, timeout: float = 60.0) -> dict | None:
    try:
        req = request.Request(
            f"{OLLAMA_URL}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"_error": str(exc)}


def _set_affinity_via_powershell(pid: int, mask: int) -> bool:
    """Windows: 通过 PowerShell 设进程 affinity."""
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Id {pid}).ProcessorAffinity = [System.IntPtr]::new({mask})",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


class OllamaGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Ollama 控制面板 (S5.6k)")
        root.geometry("720x540")

        # 状态栏
        self.status_var = tk.StringVar(value="未启动")
        ttk.Label(root, textvariable=self.status_var, foreground="blue").pack(pady=4)

        # 控制按钮
        ctrl = ttk.Frame(root)
        ctrl.pack(pady=4)
        ttk.Button(ctrl, text="Start Ollama (affinity 4 核)", command=self.start_ollama, width=22).grid(row=0, column=0, padx=4)
        ttk.Button(ctrl, text="Stop Ollama", command=self.stop_ollama, width=14).grid(row=0, column=1, padx=4)
        ttk.Button(ctrl, text="Refresh", command=self.refresh, width=10).grid(row=0, column=2, padx=4)

        # 模型选择 + load
        model_frame = ttk.Frame(root)
        model_frame.pack(pady=4)
        ttk.Label(model_frame, text="Load 模型:").grid(row=0, column=0, padx=4)
        self.model_var = tk.StringVar(value="qwen2.5:3b")
        self.model_combo = ttk.Combobox(
            model_frame,
            textvariable=self.model_var,
            values=["qwen2.5:1.5b", "qwen2.5:3b", "qwen2.5:7b", "qwen3:1.7b", "qwen3:4b"],
            width=18,
            state="readonly",
        )
        self.model_combo.grid(row=0, column=1, padx=4)
        ttk.Button(model_frame, text="Load (keep_alive 30m)", command=self.load_model, width=22).grid(row=0, column=2, padx=4)
        ttk.Button(model_frame, text="Unload", command=self.unload_model, width=8).grid(row=0, column=3, padx=4)

        # 测试 prompt
        test_frame = ttk.Frame(root)
        test_frame.pack(pady=4, fill="x", padx=8)
        ttk.Button(test_frame, text="Test 笨猫 (warm latency)", command=self.test_catnify, width=24).pack(side="left", padx=4)

        # 日志
        ttk.Label(root, text="日志 / 状态:").pack(anchor="w", padx=8)
        self.log = tk.Text(root, height=22, width=88, bg="#1e1e1e", fg="#d4d4d4")
        self.log.pack(padx=8, pady=4, fill="both", expand=True)

        # 启动时刷新一次
        self.refresh()

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")
        self.root.update_idletasks()

    def start_ollama(self) -> None:
        if not OLLAMA_EXE.exists():
            self._log(f"ERROR: {OLLAMA_EXE} not found")
            return
        OLLAMA_MODELS.mkdir(parents=True, exist_ok=True)
        # 先 kill 旧的
        subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True)
        time.sleep(2)

        env = os.environ.copy()
        env.update({
            "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": str(OLLAMA_MODELS),
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_KEEP_ALIVE": "30m",
            "OLLAMA_NUM_THREAD": "4",
        })
        proc = subprocess.Popen(
            [str(OLLAMA_EXE), "serve"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
        )
        self._log(f"Started ollama PID={proc.pid}")
        # 设 affinity
        time.sleep(1)
        ok = _set_affinity_via_powershell(proc.pid, AFFINITY_MASK)
        self._log(f"Set affinity 0x{AFFINITY_MASK:X} (cores 0-3): {'OK' if ok else 'FAIL'}")
        time.sleep(3)
        self.refresh()

    def stop_ollama(self) -> None:
        r = subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True, text=True)
        self._log(f"Stop ollama: {r.stdout.strip() or r.stderr.strip()}")
        self.refresh()

    def load_model(self) -> None:
        model = self.model_var.get()
        self._log(f"Loading {model} (keep_alive 30m, may take 30-60s cold)...")

        def _bg():
            t0 = time.monotonic()
            r = _http_post("/api/generate", {
                "model": model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_thread": 4, "num_predict": 5},
            }, timeout=120)
            ms = (time.monotonic() - t0) * 1000.0
            if r and "_error" in r:
                self._log(f"  load FAIL after {ms:.0f}ms: {r['_error']}")
            elif r:
                self._log(f"  load OK after {ms:.0f}ms, response={r.get('response','')!r}")
            self.refresh()

        threading.Thread(target=_bg, daemon=True).start()

    def unload_model(self) -> None:
        model = self.model_var.get()
        r = _http_post("/api/generate", {"model": model, "keep_alive": 0}, timeout=10)
        self._log(f"Unload {model}: {r}")
        self.refresh()

    def test_catnify(self) -> None:
        model = self.model_var.get()
        self._log(f"Testing 笨猫 with {model}...")

        def _bg():
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是笨猫(米雪儿). 改写候选成笨猫语气. 必带喵~. 主人称『主人』. 自称『人家』."},
                    {"role": "user", "content": "用户: 早安笨猫\nCPU候选: 早上好~\n改写:"},
                ],
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_thread": 4, "num_predict": 80, "temperature": 0.7},
            }
            t0 = time.monotonic()
            r = _http_post("/api/chat", body, timeout=30)
            ms = (time.monotonic() - t0) * 1000.0
            if r and "_error" in r:
                self._log(f"  catnify FAIL after {ms:.0f}ms: {r['_error']}")
            elif r:
                msg = r.get("message", {}).get("content", "")
                prefill = r.get("prompt_eval_duration", 0) / 1e6
                decode = r.get("eval_duration", 0) / 1e6
                self._log(f"  catnify OK {ms:.0f}ms (prefill {prefill:.0f}ms + decode {decode:.0f}ms)")
                self._log(f"  output: {msg}")

        threading.Thread(target=_bg, daemon=True).start()

    def refresh(self) -> None:
        tags = _http_get("/api/tags", timeout=3)
        ps = _http_get("/api/ps", timeout=3)
        if tags is None:
            self.status_var.set("❌ Ollama 未启动 / API 不可用")
        else:
            n_models = len(tags.get("models", []))
            loaded = ps.get("models", []) if ps else []
            if loaded:
                loaded_str = ", ".join(m["name"] for m in loaded)
                self.status_var.set(f"✅ Ollama OK | 磁盘 {n_models} 模型 | 内存中: {loaded_str}")
            else:
                self.status_var.set(f"⚠️ Ollama OK | 磁盘 {n_models} 模型 | 内存无加载")


def main() -> int:
    root = tk.Tk()
    OllamaGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
