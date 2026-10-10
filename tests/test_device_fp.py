import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_daily_reminder.api import MYS_DEVICE_FP, MYS_DEVICE_ID, ApiError, Client
from astrbot_plugin_daily_reminder.login_api import LoginApi
from fixtures import account
from test_network import Response


def reply(data):
    return Response(json.dumps(data).encode())


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_identity_ignores_legacy_cache_without_network_lookup(self):
        for old_device in ("old-device", "another-device"):
            acc = account()
            acc["device_id"] = old_device
            acc["device_fp_state"] = {"device_id": old_device, "fp": "legacyfp"}
            session = self.session((0, 0, 0))
            for game in ("genshin", "starrail", "zzz"):
                await Client(session).request("https://example.invalid", acc, {}, game)
            session.post.assert_not_called()
            for call in session.get.call_args_list:
                headers = call.kwargs["headers"]
                self.assertEqual(headers["x-rpc-device_id"], MYS_DEVICE_ID)
                self.assertEqual(headers["x-rpc-device_fp"], MYS_DEVICE_FP)
            self.assertEqual(LoginApi.hyp_headers(old_device)["x-rpc-device_id"], MYS_DEVICE_ID)
            self.assertEqual(LoginApi.account_headers(old_device)["x-rpc-device_id"], MYS_DEVICE_ID)

    def session(self, codes=(0,)):
        return SimpleNamespace(
            post=Mock(
                return_value=reply(
                    {"retcode": 0, "data": {"code": 200, "device_fp": "abcdef0123456"}}
                )
            ),
            get=Mock(side_effect=[reply({"retcode": code, "data": {}}) for code in codes]),
        )

    async def test_captcha_retries_once_per_game(self):
        for game, number in (("genshin", "2"), ("starrail", "6"), ("zzz", "8")):
            for code in (1034, 10035, 5003, 10041, 10053):
                session = self.session((code, code))
                with self.assertRaises(ApiError) as caught:
                    await Client(session).request("https://example.invalid", account(), {}, game)
                self.assertEqual(session.get.call_count, 2)
                self.assertIn(str(code), str(caught.exception))
                self.assertFalse(caught.exception.transient)
                first, second = [c.kwargs["headers"] for c in session.get.call_args_list]
                self.assertNotIn("x-rpc-challenge_game", first)
                self.assertEqual(second["x-rpc-challenge_game"], number)
                self.assertIn("x-rpc-tool-verison", second)
                for headers in (first, second):
                    self.assertEqual("DS" in headers, game != "zzz")
                    self.assertEqual("x-rpc-client_type" in headers, game != "zzz")
                if game == "zzz":
                    self.assertEqual(first["Origin"], "https://act.mihoyo.com")
                if game == "starrail":
                    self.assertEqual(first["x-rpc-device_model"], "PHK110")
                    self.assertEqual(first["x-rpc-sys_version"], "13")

    async def test_adjusted_retry_can_succeed(self):
        session = self.session((1034, 0))
        self.assertEqual(
            await Client(session).request("https://example.invalid", account(), {}, "genshin"), {}
        )
        self.assertEqual(session.get.call_count, 2)

    async def test_other_errors_do_not_retry_or_binding_get_fp(self):
        for code in (-100, 10101, 10102):
            session = self.session((code,))
            with self.assertRaises(ApiError):
                await Client(session).request("https://example.invalid", account(), {}, "genshin")
            self.assertEqual(session.get.call_count, 1)
        session = self.session((1034,))
        with self.assertRaises(ApiError):
            await Client(session).request("https://example.invalid", account(), {})
        session.post.assert_not_called()
        self.assertEqual(session.get.call_count, 1)
