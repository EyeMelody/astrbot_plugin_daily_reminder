import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_daily_reminder.api import ApiError
from astrbot_plugin_daily_reminder.models import Role, parse_note
from astrbot_plugin_daily_reminder.nte import APP_KEY, NteClient, ds_headers, encrypt_sms, token
from astrbot_plugin_daily_reminder.nte_auth import NteAuth
from astrbot_plugin_daily_reminder.render import draw_card
from astrbot_plugin_daily_reminder.service import Service
from astrbot_plugin_daily_reminder.store import Store
from fixtures import account, notes, stamp
from PIL import Image
from test_network import Response

NTE_ROLE = Role("nte", "1234567890", "cn", "异象猎人")
NTE_DATA = {
    "roleid": "1234567890",
    "staminaValue": 110,
    "staminaMaxValue": 120,
    "citystaminaValue": 80,
    "citystaminaMaxValue": 120,
    "dayvalue": 60,
    "weekcopiesremainCnt": 2,
    "roleloginDays": 99,
}


def nte_account(aid="8001", roles=None):
    return {
        "aid": aid,
        "device_id": "HTTESTDEVICE1234",
        "access_token": "SYNTHETIC_ACCESS",
        "roles": [asdict(r) for r in (roles or [NTE_ROLE])],
        "updated_at": stamp(),
    }


class NteModelTests(unittest.TestCase):
    def test_weekly_camel_case_and_zero_are_supported(self):
        for value in (0, 1, 3):
            note = parse_note(NTE_ROLE, {"weekCopiesRemainCnt": value})
            self.assertEqual(note.weekly[1].value, f"剩余 {value}/3 次")
        note = parse_note(NTE_ROLE, {"weekcopiesremainCnt": None, "weekCopiesRemainCnt": 2})
        self.assertEqual(note.weekly[1].current, 2)

    def test_weekly_absent_or_invalid_never_defaults_to_zero(self):
        for data in ({}, {"weekCopiesRemainCnt": None}, {"weekCopiesRemainCnt": "bad"}):
            note = parse_note(NTE_ROLE, data)
            self.assertIsNone(note.weekly[1].current)
            self.assertEqual(note.weekly[1].value, "暂未获取")

    def test_daily_and_weekly_have_correct_meaning(self):
        note = parse_note(NTE_ROLE, NTE_DATA)
        self.assertEqual(note.stamina, 110)
        self.assertEqual(note.progress, 60)
        self.assertTrue(note.activity)
        self.assertIn("还差 40", note.text())
        self.assertEqual(note.weekly[1].value, "剩余 2/3 次")

    def test_login_days_are_not_daily_login_evidence(self):
        data = {**NTE_DATA, "dayvalue": 0, "staminaValue": 120}
        note = parse_note(NTE_ROLE, data)
        self.assertFalse(note.activity)
        self.assertIn("似乎还没有登录", note.summary)

    def test_missing_values_do_not_become_zero(self):
        note = parse_note(NTE_ROLE, {})
        self.assertIsNone(note.progress)
        self.assertIsNone(note.stamina)
        self.assertEqual(note.weekly[0].value, "暂未获取")
        self.assertNotIn("似乎还没有登录", note.summary)

    def test_completed_daily(self):
        self.assertTrue(parse_note(NTE_ROLE, {**NTE_DATA, "dayvalue": 100}).daily_complete)

    def test_four_games_are_visible(self):
        three = notes()
        four = [*three, parse_note(NTE_ROLE, NTE_DATA)]
        with (
            Image.open(io.BytesIO(draw_card(three))) as a,
            Image.open(io.BytesIO(draw_card(four))) as b,
        ):
            self.assertGreater(b.height, a.height + 200)
            self.assertLess(b.height, 4096)

    def test_signature_follows_upstream_protocol(self):
        headers = ds_headers("device", "SYNTHETIC")
        ts, nonce, digest = headers["ds"].split(",")
        self.assertEqual(digest, hashlib.md5(f"{ts}{nonce}1.2.4pUds3dfMkl".encode()).hexdigest())
        self.assertEqual(headers["authorization"], "SYNTHETIC")
        self.assertEqual(len(nonce), 8)

    def test_aes_wire_payload_is_reversible_with_upstream_key(self):
        from base64 import b64decode

        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        value = encrypt_sms("123456")
        self.assertNotIn("123456", value)
        self.assertEqual(
            unpad(AES.new(APP_KEY[-16:].encode(), AES.MODE_ECB).decrypt(b64decode(value)), 16),
            b"123456",
        )

    def test_bad_tokens_are_rejected_without_echo(self):
        for value in ("", "SECRET\r\nheader", "Bearer SECRET", None):
            with self.assertRaises(ValueError) as caught:
                token(value)
            self.assertNotIn("SECRET", str(caught.exception))


class NteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="nte-tests-")) / "test.sqlite3"
        self.store = Store(self.path)
        self.now = stamp("2026-10-03 23:00:00")
        self.client = SimpleNamespace(
            fetch=AsyncMock(side_effect=lambda a, r: next(n for n in notes() if n.role == r))
        )
        self.nte = SimpleNamespace(
            fetch=AsyncMock(side_effect=lambda a, r: parse_note(r, NTE_DATA)), renew=AsyncMock()
        )
        self.send = AsyncMock(return_value=True)
        self.service = Service(
            self.store,
            self.client,
            SimpleNamespace(refresh_cookie=AsyncMock()),
            self.send,
            Mock(),
            clock=lambda: self.now,
            nte_client=self.nte,
        )

    async def asyncTearDown(self):
        self.store.close()

    async def test_nte_only_account_can_subscribe(self):
        self.store.save_nte("user", nte_account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        await self.service.tick()
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.client.fetch.await_count, 0)
        self.assertEqual(self.send.call_args.args[1][0].role.game, "nte")

    async def test_existing_mys_data_survives_nte_binding_and_restart(self):
        self.store.save_account("user", account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        self.store.save_nte("user", nte_account())
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.selected_roles("user")), 4)
        self.assertIsNotNone(self.store.subscription("user", "group"))
        self.assertEqual(self.store.account("user")["cookie"], account()["cookie"])

    async def test_mys_binding_does_not_replace_nte_shell(self):
        self.store.save_nte("user", nte_account())
        self.store.save_account("user", account())
        self.assertEqual(len(self.store.selected_roles("user")), 4)

    async def test_mys_replacement_preserves_nte_but_clears_subscriptions(self):
        self.store.save_account("user", account())
        self.store.save_nte("user", nte_account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        self.store.save_account("user", account("1111"), replace=True)
        self.assertEqual(self.store.nte_account("user")["aid"], "8001")
        self.assertIsNone(self.store.subscription("user", "group"))

    async def test_nte_login_replaces_account_directly(self):
        self.store.save_account("user", account())
        self.store.save_nte("user", nte_account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        auth = NteAuth(self.nte, self.service, clock=lambda: self.now)
        self.assertIn("已更换", await auth.commit("user", nte_account("8002")))
        self.assertEqual(self.store.nte_account("user")["aid"], "8002")
        self.assertEqual(self.store.account("user")["aid"], "12345")
        self.assertIsNone(self.store.subscription("user", "group"))

    async def test_nte_expiry_does_not_hide_mys_data(self):
        self.store.save_account("user", account())
        self.store.save_nte("user", nte_account())
        self.nte.fetch.side_effect = ApiError("NTE expired", "nte_auth", auth=True)
        result = await self.service.query("user")
        self.assertEqual(len(result), 4)
        self.assertTrue(result[-1].error)
        self.assertFalse(any(n.error for n in result[:3]))

    async def test_mys_expiry_does_not_hide_nte_data(self):
        self.store.save_account("user", account())
        self.store.save_nte("user", nte_account())
        self.client.fetch.side_effect = ApiError("MYS expired", "auth", auth=True)
        result = await self.service.query("user")
        self.assertFalse(result[-1].error)
        self.assertTrue(all(n.error for n in result[:3]))

    async def test_nte_refresh_updates_saved_tokens(self):
        acc = {**nte_account(), "refresh_token": "old", "updated_at": self.now - 3601}
        self.store.save_nte("user", acc)
        self.nte.renew.return_value = {
            "access_token": "new",
            "refresh_token": "new_refresh",
            "updated_at": self.now,
        }
        await self.service.query("user")
        self.assertEqual(self.nte.renew.await_count, 1)
        self.assertEqual(self.store.nte_account("user")["refresh_token"], "new_refresh")

    async def test_unbind_nte_preserves_mys_and_its_subscriptions(self):
        self.store.save_account("user", account())
        self.store.save_nte("user", nte_account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        self.store.unbind_nte("user")
        self.assertIsNone(self.store.nte_account("user"))
        self.assertEqual(len(self.store.selected_roles("user")), 3)
        self.assertIsNotNone(self.store.subscription("user", "group"))

    async def test_unbind_only_nte_cleans_subscriptions(self):
        self.store.save_nte("user", nte_account())
        self.store.subscribe("user", "group", "11", "23:00", stamp())
        self.store.unbind_nte("user")
        self.assertIsNone(self.store.account("user"))
        self.assertIsNone(self.store.subscription("user", "group"))

    async def test_multiple_nte_roles_require_selection_and_enforce_ownership(self):
        second = Role("nte", "22222", "cn", "another")
        self.store.save_nte("user", nte_account(roles=[NTE_ROLE, second]))
        with self.assertRaises(ValueError):
            self.store.selected_roles("user")
        with self.assertRaises(ValueError):
            self.store.select("other", "nte", "22222")
        self.store.select("user", "nte", "22222")
        self.assertEqual(self.store.selected_roles("user")[0], second)

    async def test_sms_cooldown_expiry_and_success(self):
        self.nte.send_sms = AsyncMock()
        self.nte.sms_login = AsyncMock(return_value=nte_account())
        auth = NteAuth(self.nte, self.service, clock=lambda: self.now)
        await auth.send_code("user", "13800000000")
        self.assertIn("60", await auth.send_code("other", "13800000000"))
        self.assertEqual(self.nte.send_sms.await_count, 1)
        self.assertIn("绑定成功", await auth.verify("user", "123456"))
        self.assertEqual(auth.sms, {})
        self.assertNotIn("13800000000", json.dumps(self.store.account("user")))
        self.now += 61
        await auth.send_code("user", "13800000000")
        self.now += 301
        self.assertIn("超时", await auth.verify("user", "123456"))

    async def test_sms_attempt_limit(self):
        self.nte.send_sms = AsyncMock()
        self.nte.sms_login = AsyncMock(side_effect=ApiError("验证码错误"))
        auth = NteAuth(self.nte, self.service, clock=lambda: self.now)
        await auth.send_code("user", "13800000000")
        for _ in range(5):
            with self.assertRaises(ApiError):
                await auth.verify("user", "123456")
        self.assertNotIn("user", auth.sms)


class NteNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_codes_are_sanitized_and_classified(self):
        for code in (401, "402", 403, 429):
            session = SimpleNamespace(
                request=Mock(
                    return_value=Response(json.dumps({"code": code, "msg": "SECRET"}).encode())
                )
            )
            with self.assertRaises(ApiError) as caught:
                await NteClient(session).request("/test", nte_account())
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(caught.exception.auth, str(code) != "429")

    async def test_laohu_signature_includes_empty_fields_before_filtering(self):
        session = SimpleNamespace(request=Mock(return_value=Response(b'{"code":0,"result":{}}')))
        await NteClient(session).request("/test", {}, body={"x": "hello", "z": ""}, laohu=True)
        data = session.request.call_args.kwargs["data"]
        self.assertEqual(data["sign"], hashlib.md5(("hello" + APP_KEY).encode()).hexdigest())
        self.assertNotIn("z", data)
        self.assertNotIn("authorization", session.request.call_args.kwargs["headers"])

    async def test_authentication_roles_and_old_list_shape(self):
        client = NteClient(None)
        client.request = AsyncMock(
            side_effect=[
                {"user": {"uid": 8001}},
                {"roleId": 0},
                [{"roleId": "1234567890", "roleName": "test"}],
            ]
        )
        result = await client.access_login("SYNTHETIC_ACCESS")
        self.assertEqual(result["aid"], "8001")
        self.assertEqual(result["roles"][0]["uid"], "1234567890")
        self.assertEqual(client.request.call_args.kwargs["params"], {"gameId": "1289"})

    async def test_record_card_fallback_only_includes_nte(self):
        client = NteClient(None)
        client.request = AsyncMock(
            side_effect=[
                {"user": {"uid": 8001}},
                {},
                {"bindRole": 0, "roles": []},
                [
                    {"gameId": 1256, "bindRoleInfo": {"roleId": 44}},
                    {"gameId": 1289, "bindRoleInfo": {"roleId": 55, "roleName": "test"}},
                ],
            ]
        )
        result = await client.access_login("SYNTHETIC_ACCESS")
        self.assertEqual([r["uid"] for r in result["roles"]], ["55"])

    async def test_refresh_failure_falls_back_to_laohu(self):
        client = NteClient(None)
        client.request = AsyncMock(
            side_effect=[
                ApiError("expired", auth=True),
                {"accessToken": "new", "refreshToken": "newrefresh", "uid": 8001},
            ]
        )
        result = await client.renew(
            {
                **nte_account(),
                "refresh_token": "expired",
                "laohu_token": "laohutoken",
                "laohu_uid": "9001",
            }
        )
        self.assertEqual(result["access_token"], "new")
        self.assertEqual(client.request.call_args.args[0], "/usercenter/api/login")

    async def test_network_refresh_failure_does_not_retry_login(self):
        client = NteClient(None)
        client.request = AsyncMock(side_effect=ApiError("timeout", transient=True))
        with self.assertRaises(ApiError):
            await client.renew({**nte_account(), "refresh_token": "old", "laohu_token": "token"})
        self.assertEqual(client.request.await_count, 1)

    async def test_sms_flow_encrypts_login_fields(self):
        client = NteClient(None)
        client.request = AsyncMock(
            side_effect=[
                {},
                {"userId": 9001, "token": "laohutoken"},
                {"accessToken": "access", "refreshToken": "refresh", "uid": 8001},
                {"user": {"uid": 8001}},
                {"roleId": 12345, "roleName": "test"},
                {"roles": []},
            ]
        )
        result = await client.sms_login("13800000000", "123456", "HTDEVICE")
        self.assertEqual(result["laohu_uid"], "9001")
        login = client.request.call_args_list[1]
        self.assertNotEqual(login.kwargs["body"]["cellphone"], "13800000000")
        self.assertTrue(login.kwargs["keep_empty"])

    async def test_note_role_mismatch_rejected(self):
        client = NteClient(None)
        client.request = AsyncMock(return_value={**NTE_DATA, "roleid": "other"})
        with self.assertRaises(ApiError) as caught:
            await client.fetch(nte_account(), NTE_ROLE)
        self.assertEqual(caught.exception.code, "nte_identity")
