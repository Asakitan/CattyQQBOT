"""Pack 20 changed files into payload.zip and push to remote via HTTP /file/upload_zip."""
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile

TOKEN = "5ba40db9bedc61d2a21d3e36759cc3eb732ce55420d1c9aa55322e56d4cb921a"
FILE_BASE = "http://x2.sjcmc.cn:15089"
HOST_HEADER = "localhost:9820"
ORIGIN_HEADER = "http://localhost:9820"
REMOTE_DIR = "D:/CattyQQAI"

FILES = [
    "README.md",
    "catty_integrations.py",
    "config.example.json",
    "config.json",
    "scripts/auto_train_reply_gate.py",
    "scripts/mc_idle_ping.py",
    "src/catty_qq_ai/__init__.py",
    "src/catty_qq_ai/config.py",
    "src/catty_qq_ai/features.py",
    "src/catty_qq_ai/legs_picker.py",
    "src/catty_qq_ai/message_utils.py",
    "src/catty_qq_ai/nsfw_search.py",
    "src/catty_qq_ai/owner_forward.py",
    "src/catty_qq_ai/persona_prompts.py",
    "src/catty_qq_ai/session_cache.py",
    "tests/test_integrations.py",
    "tests/test_mc_idle.py",
    "tests/test_owner_forward.py",
    "tests/test_persona_prompts.py",
    "tests/test_session_cache.py",
]


def build_zip(root: str, zip_path: str) -> tuple[int, int]:
    raw_total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in FILES:
            full = os.path.join(root, rel.replace("/", os.sep))
            if not os.path.isfile(full):
                raise FileNotFoundError(full)
            zf.write(full, arcname=rel)
            raw_total += os.path.getsize(full)
    return len(FILES), raw_total


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_zip(zip_path: str, remote_dir: str) -> dict:
    size = os.path.getsize(zip_path)
    params = {
        "token": TOKEN,
        "path": remote_dir,
        "overwrite": "true",
        "sha256": sha256_of(zip_path),
    }
    url = f"{FILE_BASE}/file/upload_zip?{urllib.parse.urlencode(params)}"
    headers = {
        "Host": HOST_HEADER,
        "Origin": ORIGIN_HEADER,
        "Content-Type": "application/zip",
        "Content-Length": str(size),
    }
    with open(zip_path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # noqa: F841
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {body}")


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(os.environ.get("LOCALAPPDATA", root), "Temp", "catty_push")
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "payload.zip")

    count, raw = build_zip(root, zip_path)
    zsize = os.path.getsize(zip_path)
    print(f"packed {count} files: {raw} raw → {zsize} zip bytes")
    print(f"zip path: {zip_path}")

    result = upload_zip(zip_path, REMOTE_DIR)
    print("upload result:", json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
