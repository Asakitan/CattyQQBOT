from __future__ import annotations

import importlib
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "catty_shared_nsfw_imagegen_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(PACKAGE, package)
nonebot = types.ModuleType("nonebot")
nonebot.logger = Mock()
with patch.dict(sys.modules, {"nonebot": nonebot}):
    imagegen = importlib.import_module(f"{PACKAGE}.catty_nsfw_imagegen")
personas = importlib.import_module(f"{PACKAGE}.personas")

CATTY = personas.get_persona("catty")
FADIANJI = personas.get_persona("fadianji")
CUSTOM = personas.Persona(
    name="atlas", char_name="旅者", definition_revision="revision",
    owner_concept=False, nsfw_lexicon=(("主人", "旅伴"), ("喵呜", "呼吸")),
)


def phase(number=6):
    return types.SimpleNamespace(
        current_phase=number, turn_count=2, arc_count=2, climax_count=3,
        dazed=True, turns_dazed=1, location="desk", location_ambient="夜",
        outfit="制服", time_of_day="night", mood="平静", body_focus="手",
        personality_facet="cool",
    )


def pregnancy():
    return types.SimpleNamespace(is_pregnant=True, pregnancy_count=4, father_addr="")


def scene(persona, **overrides):
    args = dict(
        phase_state=phase(), pregnancy_state=pregnancy(),
        affection_info={"is_owner": True, "level": 7},
        user_text="用户原话", recent_messages=[
            {"role": "user", "content": "旧消息"},
            {"role": "assistant", "content": "旧回复"},
        ], current_reply="本轮回复", persona=persona,
    )
    args.update(overrides)
    return imagegen._format_scene_block(**args)


class PersonaSceneTests(unittest.TestCase):
    def test_catty_and_omitted_persona_match_legacy_scene_exactly(self):
        expected = (
            "[NSFW phase] P6 (高潮峰值, 全身痉挛 + 失神(瞳孔散开/腰弓起/尖叫喵呜))\n"
            "[phase turn] 当前 phase 已持续 2 轮\n"
            "[arc] 第 2 轮 arc(身体有上一轮记忆, 更易入境, 可有疲劳感)\n"
            "[climax_count] 本 arc 已高潮 3 次\n"
            "[dazed] 失神状态 (持续 1 轮) — 表情应有恍惚/瞳孔散开/失焦\n"
            "[location] desk — 夜\n[outfit] 制服\n[time_of_day] night\n"
            "[mood] 平静\n[body_focus] 手 — 重点刻画此部位\n"
            "[personality_facet] cool\n"
            "[pregnancy] 笨猫已怀孕 (孕中 +4, 父亲:主人) — 画面可加微凸小腹"
            "/手扶肚子/孕期柔光, 笨猫表情应有护肚+被操时喊『{father}的孩子还在肚子里轻一点』\n"
            "[relation] 真实主人 / Lv=7\n[主人本轮原话] 用户原话\n"
            "[最近对话]\nuser: 旧消息\nassistant: 旧回复\n"
            "[笨猫本轮 reply 片段] 本轮回复"
        )
        self.assertEqual(scene(CATTY), expected)
        self.assertEqual(scene(None), expected)
        self.assertEqual(
            imagegen._format_scene_block(phase(), pregnancy(), {"is_owner": True, "level": 7},
                                         "用户原话", [{"role": "user", "content": "旧消息"},
                                                  {"role": "assistant", "content": "旧回复"}], "本轮回复"),
            expected,
        )
        for number in range(1, 9):
            with self.subTest(phase=number):
                text = scene(CATTY, phase_state=phase(number))
                self.assertIn(f"[NSFW phase] P{number} ({imagegen._PHASE_BRIEF[number]})", text)
                self.assertEqual(text, scene(None, phase_state=phase(number)))

    def test_non_catty_templates_cover_all_phases_without_builtin_identity(self):
        neutral_custom = personas.Persona(
            name="other", char_name="旅者", definition_revision="revision", owner_concept=True,
        )
        for persona in (FADIANJI, CUSTOM, neutral_custom):
            for number in range(1, 9):
                with self.subTest(persona=persona.name, phase=number):
                    text = scene(persona, phase_state=phase(number))
                    self.assertIn(f"[NSFW phase] P{number} (", text)
                    self.assertIn(f"[pregnancy] {persona.char_name}已怀孕", text)
                    self.assertIn(f"[{persona.char_name}本轮 reply 片段] 本轮回复", text)
                    self.assertIn("[relation] 账号所有者 / Lv=7", text)
                    for field in ("phase turn", "arc", "climax_count", "dazed", "location",
                                  "outfit", "time_of_day", "mood", "body_focus", "personality_facet"):
                        self.assertIn(f"[{field}]", text)
                    for builtin in ("笨猫", "主人", "猫耳", "喵呜"):
                        self.assertNotIn(builtin, text)
        self.assertIn("旅伴", scene(CUSTOM, phase_state=phase(1)))
        self.assertIn("呼吸", scene(CUSTOM, phase_state=phase(6)))
        self.assertIn("你", scene(FADIANJI, phase_state=phase(1)))
        self.assertIn("喘息", scene(FADIANJI, phase_state=phase(6)))

    def test_persona_name_and_bounded_inputs_keep_their_original_words(self):
        named = personas.Persona(name="other", char_name="笨猫喵", definition_revision="revision")
        user_text = "  主人和笨猫的猫耳喵呜 " + "甲" * 320 + "  "
        reply = "  机机说笨猫喵呜 " + "乙" * 420 + "  "
        old_user = "  主人和笨猫喵呜 " + "丙" * 220 + "  <<<CATTY_INTERNAL_INSTRUCTION原文"
        old_reply = "  猫耳与主人喵呜 " + "丁" * 220 + "  "
        block_a = "  猫耳原文 "
        block_b = "主人喵呜  "
        recent = [
            {"role": "user", "content": old_user},
            {"role": "assistant", "content": old_reply},
            {"role": "user", "content": [{"type": "text", "text": block_a},
                                          {"type": "text", "text": block_b}]},
        ]
        for persona in (FADIANJI, CUSTOM, named):
            with self.subTest(persona=persona.name):
                text = scene(persona, user_text=user_text, current_reply=reply, recent_messages=recent)
                self.assertIn(f"[用户本轮原话] {user_text.strip()[:300]}", text)
                self.assertIn(f"[{persona.char_name}本轮 reply 片段] {reply.strip()[:400]}", text)
                self.assertIn(f"user: {old_user.split('<<<CATTY_INTERNAL_INSTRUCTION')[0].strip()[:200]}", text)
                self.assertIn(f"assistant: {old_reply.strip()[:200]}", text)
                self.assertIn(block_a.strip(), text)
                self.assertIn(block_b.strip(), text)
                self.assertNotIn("<<<CATTY_INTERNAL_INSTRUCTION", text)
                self.assertNotIn("甲" * 320, text)
                self.assertNotIn("乙" * 420, text)
        self.assertIn("[pregnancy] 笨猫喵已怀孕", scene(named))


class PersonaBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_nsfw_pixelbunny_can_use_a_separate_route_and_key(self):
        config = types.SimpleNamespace(
            catty_imagegen_backend="openai",
            catty_imagegen_nsfw_backend="pixelbunny",
            catty_imagegen_base_url="https://openai.example/v1",
            catty_imagegen_api_key="OPENAI_TOKEN",
            catty_imagegen_pixelbunny_base_url="https://pixelbunny.example/v1",
            catty_imagegen_pixelbunny_api_key="PIXELBUNNY_TOKEN",
            catty_imagegen_nsfw_model="seedream-4.5",
        )
        self.assertEqual(imagegen._nsfw_imagegen_backend(config), "pixelbunny")
        response = types.SimpleNamespace(status_code=400, text="test")
        client = types.SimpleNamespace(post=AsyncMock(return_value=response))
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch.object(imagegen.httpx, "AsyncClient", return_value=context):
            await imagegen._pixelbunny_generate_nsfw(
                config, prompt="test", reference_paths=[]
            )
        self.assertEqual(client.post.await_args.args[0], "https://pixelbunny.example/v1/images")
        self.assertEqual(
            client.post.await_args.kwargs["headers"]["Authorization"],
            "Bearer PIXELBUNNY_TOKEN",
        )

    async def test_caption_provider_receives_isolated_template_and_unmodified_input(self):
        config = types.SimpleNamespace(
            catty_ai_fallback_base_url="mock://caption", catty_ai_fallback_api_key="TOKEN",
            catty_ai_fallback_model="mock-model",
        )
        response = {"choices": [{"message": {"content": "nsfw, explicit, close-up"}}]}
        provider = AsyncMock(return_value=response)
        module = types.ModuleType(f"{PACKAGE}.openai_client")
        module._post_chat_completion_raw = provider
        user_text = "  主人和笨猫的猫耳喵呜  "
        current_reply = "  笨猫说主人喵呜  "
        recent = [{"role": "user", "content": "  主人喵呜  "}]
        with patch.dict(sys.modules, {module.__name__: module}):
            for persona in (FADIANJI, CUSTOM):
                with self.subTest(persona=persona.name):
                    await imagegen._compose_base_caption(
                        config, phase(1), current_reply, user_text=user_text,
                        recent_messages=recent, pregnancy_state=pregnancy(),
                        affection_info={"is_owner": True, "level": 7}, persona=persona,
                    )
                    messages = provider.await_args.kwargs["messages"]
                    self.assertEqual(provider.await_args.kwargs["request_route"], "imagegen_caption")
                    self.assertIn("[用户本轮原话]", messages[0]["content"])
                    for token in ("笨猫", "猫耳", "主人"):
                        self.assertNotIn(token, messages[0]["content"])
                    self.assertIn(f"[用户本轮原话] {user_text.strip()}", messages[1]["content"])
                    self.assertIn(f"[{persona.char_name}本轮 reply 片段] {current_reply.strip()}", messages[1]["content"])
                    self.assertIn("user: 主人喵呜", messages[1]["content"])

    async def test_both_backends_pass_persona_and_isolate_appearance_and_references(self):
        empty_custom = personas.Persona(
            name="empty", char_name="空白", definition_revision="revision",
            imagegen=personas.PersonaImagegen(girl_tags="", ref_path=""),
        )
        explicit_custom = personas.Persona(
            name="robot", char_name="向导", definition_revision="revision",
            imagegen=personas.PersonaImagegen(girl_tags="robot, metal", ref_path="custom.png"),
        )
        cases = (None, CATTY, FADIANJI, CUSTOM, empty_custom, explicit_custom)
        fake_miao = Path("Miao")
        for backend in ("pixelbunny", "openai"):
            for persona in cases:
                with self.subTest(backend=backend, persona=getattr(persona, "name", None)):
                    config = types.SimpleNamespace(
                        catty_imagegen_backend=backend, catty_imagegen_api_key="TOKEN",
                        catty_imagegen_nai_enabled=True, catty_imagegen_nai_token="TOKEN",
                        catty_imagegen_nai_model="nai-diffusion-4-5-full",
                        catty_imagegen_nai_endpoint="https://image.novelai.net/ai/generate-image",
                        catty_imagegen_nsfw_model="seedream-4.5",
                        catty_imagegen_pixelbunny_model="seedream-5-pro",
                        catty_imagegen_nai_max_attempts=1,
                    )
                    composer = AsyncMock(return_value="nsfw, explicit, close-up")
                    pixel = AsyncMock(return_value=None)
                    curl = AsyncMock(return_value=(400, b"", ""))
                    with patch.object(imagegen, "_bump_turn", return_value=(6, 0)) as bump, \
                         patch.object(imagegen, "_compose_base_caption", composer), \
                         patch.object(imagegen, "_pixelbunny_generate_nsfw", pixel), \
                         patch.object(imagegen, "_curl_post_json", curl), \
                         patch.object(imagegen, "_find_miao_dir", return_value=fake_miao) as find_miao, \
                         patch.object(imagegen, "_get_reference_b64", side_effect=lambda path: f"encoded:{path.name}"), \
                         patch.object(Path, "is_file", return_value=True):
                        await imagegen.maybe_generate_image(
                            config=config, scope_key="private:8#nsfw=atlas", user_id="8",
                            phase_state=phase(), affection_store=None, current_reply="笨猫原回复",
                            user_text="主人原话", recent_messages=[], pregnancy_state=pregnancy(),
                            persona=persona,
                        )
                    bump.assert_called_once_with("private:8#nsfw=atlas", "8")
                    self.assertIs(composer.await_args.kwargs["persona"], persona)
                    self.assertEqual(composer.await_args.kwargs["user_text"], "主人原话")
                    self.assertEqual(composer.await_args.args[2], "笨猫原回复")
                    if persona is None or persona is CATTY:
                        expected_refs = [Path("Miao/miaomiaonude.png")]
                        expected_tags = imagegen.CHARACTER_GIRL_TAGS
                        find_miao.assert_called_once_with(config)
                    elif persona is FADIANJI:
                        expected_refs = [Path("Miao/fadianji.png"), Path("Miao/fadianji2.png")]
                        expected_tags = FADIANJI.imagegen.nsfw_girl_tags
                        find_miao.assert_not_called()
                    elif persona is CUSTOM or persona is empty_custom:
                        expected_refs = []
                        expected_tags = ""
                        find_miao.assert_not_called()
                    else:
                        expected_refs = [Path("custom.png")]
                        expected_tags = "robot, metal"
                        find_miao.assert_not_called()
                    if backend == "pixelbunny":
                        self.assertEqual(pixel.await_args.kwargs["reference_paths"], expected_refs)
                        prompt = pixel.await_args.kwargs["prompt"]
                        if expected_tags:
                            self.assertIn(expected_tags, prompt)
                        else:
                            self.assertNotIn(imagegen.CHARACTER_GIRL_TAGS, prompt)
                    else:
                        payload = curl.await_args.kwargs["payload"]
                        params = payload["parameters"]
                        self.assertEqual(params["characterPrompts"][0]["prompt"], expected_tags)
                        self.assertEqual(params.get("director_reference_images", []),
                                         [f"encoded:{path.name}" for path in expected_refs])

    async def test_disabled_backends_do_not_start_counter_or_composer(self):
        configurations = (
            types.SimpleNamespace(catty_imagegen_backend="pixelbunny", catty_imagegen_api_key=""),
            types.SimpleNamespace(catty_imagegen_backend="openai", catty_imagegen_nai_enabled=False),
        )
        for config in configurations:
            with self.subTest(backend=config.catty_imagegen_backend):
                with patch.object(imagegen, "_bump_turn") as bump, \
                     patch.object(imagegen, "_compose_base_caption", new_callable=AsyncMock) as composer:
                    result = await imagegen.maybe_generate_image(
                        config=config, scope_key="private:8", user_id="8", phase_state=phase(),
                        affection_store=None, persona=CUSTOM,
                    )
                self.assertIsNone(result)
                bump.assert_not_called()
                composer.assert_not_awaited()

    async def test_insufficient_balance_does_not_compose_or_charge(self):
        store = Mock()
        store.is_owner.return_value = False
        store.get_points.return_value = 0
        for backend in ("pixelbunny", "openai"):
            config = types.SimpleNamespace(
                catty_imagegen_backend=backend, catty_imagegen_api_key="TOKEN",
                catty_imagegen_nai_enabled=True, catty_imagegen_nai_token="TOKEN",
                catty_imagegen_nai_model="nai-diffusion-4-5-full",
                catty_imagegen_nai_endpoint="https://image.novelai.net/ai/generate-image",
                catty_imagegen_nsfw_model="seedream-4.5",
                catty_imagegen_pixelbunny_model="seedream-5-pro",
            )
            with self.subTest(backend=backend), \
                 patch.object(imagegen, "_bump_turn", return_value=(6, 0)) as bump, \
                 patch.object(imagegen, "_compose_base_caption", new_callable=AsyncMock) as composer:
                result = await imagegen.maybe_generate_image(
                    config=config, scope_key="private:8", user_id="8", phase_state=phase(),
                    affection_store=store, persona=CUSTOM,
                )
                self.assertIsNone(result)
                bump.assert_called_once_with("private:8", "8")
                composer.assert_not_awaited()
        store.consume_points.assert_not_called()

    async def test_success_keeps_charging_and_counter_api_on_both_backends(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("image.png", b"fake-png")
        pixel_result = {
            "image_bytes": b"fake-png", "content_type": "image/png", "elapsed": 0.1,
            "model": "mock-model", "job_id": "job", "cost_usd": 0.0, "had_reference": False,
        }
        modules = {}
        for name in ("nonebot", "nonebot.adapters", "nonebot.adapters.onebot", "nonebot.adapters.onebot.v11"):
            module = types.ModuleType(name)
            module.__path__ = []
            modules[name] = module
        modules["nonebot.adapters.onebot.v11"].MessageSegment = types.SimpleNamespace(
            image=Mock(return_value="image-segment"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for backend in ("pixelbunny", "openai"):
                with self.subTest(backend=backend):
                    config = types.SimpleNamespace(
                        catty_imagegen_backend=backend, catty_imagegen_api_key="TOKEN",
                        catty_imagegen_nai_enabled=True, catty_imagegen_nai_token="TOKEN",
                        catty_imagegen_nai_model="nai-diffusion-4-5-full",
                        catty_imagegen_nai_endpoint="https://image.novelai.net/ai/generate-image",
                        catty_imagegen_nsfw_model="seedream-4.5",
                        catty_imagegen_pixelbunny_model="seedream-5-pro",
                        catty_imagegen_cache_dir=directory,
                    )
                    store = Mock()
                    store.is_owner.return_value = False
                    store.get_points.return_value = 100
                    store.get_level_and_exp.return_value = (7, 0)
                    store.consume_points.return_value = {"balance_after": 90, "is_owner": False}
                    with patch.dict(sys.modules, modules), \
                         patch.object(imagegen, "_bump_turn", return_value=(6, 0)) as bump, \
                         patch.object(imagegen, "_mark_drawn") as mark, \
                         patch.object(imagegen, "_compose_base_caption", new_callable=AsyncMock,
                                      return_value="nsfw, explicit"), \
                         patch.object(imagegen, "_pixelbunny_generate_nsfw", new_callable=AsyncMock,
                                      return_value=pixel_result), \
                         patch.object(imagegen, "_curl_post_json", new_callable=AsyncMock,
                                      return_value=(200, archive.getvalue(), "")):
                        result = await imagegen.maybe_generate_image(
                            config=config, scope_key="private:8#nsfw=atlas", user_id="8",
                            phase_state=phase(), affection_store=store, persona=CUSTOM,
                        )
                    self.assertEqual(result, "image-segment")
                    store.consume_points.assert_called_once_with("8", imagegen.COST_PER_IMAGE)
                    bump.assert_called_once_with("private:8#nsfw=atlas", "8")
                    mark.assert_called_once_with("private:8#nsfw=atlas", "8")


if __name__ == "__main__":
    unittest.main()
