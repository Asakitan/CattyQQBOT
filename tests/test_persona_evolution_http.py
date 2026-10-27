from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import test_openai_client

client = test_openai_client._client


class PersonaEvolutionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_evolution_sends_only_one_request_on_server_error(self):
        requests = []
        real_client = client.httpx.AsyncClient

        def respond(request):
            requests.append(request)
            return client.httpx.Response(503, text="unavailable")

        transport = client.httpx.MockTransport(respond)
        with patch.object(client.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with patch.object(client, "_get_runtime_config", return_value=None), patch.object(client.asyncio, "sleep", new_callable=AsyncMock):
                with self.assertRaises(client.OpenAICompatibleError):
                    await client._post_chat_completion_raw(
                        base_url="https://example.test/v1", api_key="fixture", model="fixture",
                        messages=[{"role": "user", "content": "fixture"}], timeout=1, proxy="",
                        temperature=0.3, max_tokens=4096, extra_headers={}, extra_body={},
                        request_route="persona_evolution", max_attempts=1,
                    )
        self.assertEqual(len(requests), 1)

    async def test_other_routes_keep_four_attempts(self):
        requests = []
        real_client = client.httpx.AsyncClient

        def respond(request):
            requests.append(request)
            return client.httpx.Response(503, text="unavailable")

        transport = client.httpx.MockTransport(respond)
        with patch.object(client.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with patch.object(client, "_get_runtime_config", return_value=None), patch.object(client.asyncio, "sleep", new_callable=AsyncMock):
                with self.assertRaises(client.OpenAICompatibleError):
                    await client._post_chat_completion_raw(
                        base_url="https://example.test/v1", api_key="fixture", model="fixture",
                        messages=[{"role": "user", "content": "fixture"}], timeout=1, proxy="",
                        temperature=0.3, max_tokens=4096, extra_headers={}, extra_body={},
                    )
        self.assertEqual(len(requests), 4)


if __name__ == "__main__":
    unittest.main()
