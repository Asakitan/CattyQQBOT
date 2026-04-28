from pathlib import Path
import asyncio
import base64
from io import BytesIO
import importlib.util
import sys
import types
import unittest

from PIL import Image


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "openai_client.py"
_SPEC = importlib.util.spec_from_file_location("catty_qq_ai.openai_client", _MODULE_PATH)
_client = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _client
_SPEC.loader.exec_module(_client)


class OpenAIClientTests(unittest.TestCase):
    def test_ollama_route_and_options(self) -> None:
        extra_body = {"keep_alive": "30m", "options": {"num_ctx": 1024}}

        self.assertEqual(_client._ollama_chat_url("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434/api/chat")
        self.assertTrue(_client._looks_like_ollama_route("http://127.0.0.1:11434/v1", "ollama", extra_body))
        self.assertEqual(
            _client._ollama_options(temperature=0.1, max_tokens=16, extra_body=extra_body),
            {"num_ctx": 1024, "temperature": 0.1, "num_predict": 16},
        )

    def test_empty_image_analysis_reply_is_not_treated_as_valid_analysis(self) -> None:
        self.assertEqual(_client._image_analysis_from_reply(""), {})

    def test_data_gif_is_converted_to_first_frame_png_for_vision(self) -> None:
        image = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
        output = BytesIO()
        image.save(output, format="GIF")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        config = types.SimpleNamespace(catty_vision_request_timeout=1, catty_request_timeout=1, catty_http_proxy="")

        converted = asyncio.run(_client._vision_image_url(config, f"data:image/gif;base64,{encoded}"))

        self.assertTrue(converted.startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
