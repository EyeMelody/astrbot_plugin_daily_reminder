import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_daily_reminder.api import MAX_RESPONSE, ApiError, Client
from astrbot_plugin_daily_reminder.login_api import LoginApi
from fixtures import account


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_rate_and_server_errors_are_retryable(self):
        for status in (429, 500, 503):
            client = Client(SimpleNamespace(get=Mock(return_value=Response(b"", status))))
            with self.subTest(status=status), self.assertRaises(ApiError) as caught:
                await client.request("https://example.invalid", account(), {})
            self.assertTrue(caught.exception.transient)

    async def test_timeout_is_retryable_without_secret_leak(self):
        client = Client(SimpleNamespace(get=Mock(side_effect=asyncio.TimeoutError("SECRET"))))
        with self.assertRaises(ApiError) as caught:
            await client.request("https://example.invalid", account(), {})
        self.assertTrue(caught.exception.transient)
        self.assertNotIn("SECRET", str(caught.exception))

    async def test_size_limit_and_non_json(self):
        for body in (b"x" * (MAX_RESPONSE + 1), b"<html>error</html>"):
            client = Client(SimpleNamespace(get=Mock(return_value=Response(body))))
            with self.assertRaises(ApiError) as caught:
                await client.request("https://example.invalid", account(), {})
            self.assertEqual(caught.exception.code, "schema")

    async def test_query_is_encoded_in_signature_order_and_redirects_disabled(self):
        getter = Mock(
            return_value=Response(json.dumps({"retcode": 0, "data": {"ok": True}}).encode())
        )
        client = Client(SimpleNamespace(get=getter))
        self.assertTrue(
            (
                await client.request(
                    "https://example.invalid",
                    account(),
                    {"server": "cn_gf01", "role_id": "100000001"},
                )
            )["ok"]
        )
        kwargs = getter.call_args.kwargs
        self.assertEqual(kwargs["params"], "role_id=100000001&server=cn_gf01")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn("DS", kwargs["headers"])

    async def test_qr_url_must_be_official_https(self):
        for url in (
            "https://evil.invalid/login",
            "http://mihoyo.com/login",
            "https://mihoyo.com.evil.invalid/",
        ):
            payload = {"retcode": 0, "data": {"ticket": "ticket", "url": url}}
            api = LoginApi(
                SimpleNamespace(request=Mock(return_value=Response(json.dumps(payload).encode())))
            )
            with self.subTest(url=url), self.assertRaises(ApiError):
                await api.create()
