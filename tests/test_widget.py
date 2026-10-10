import asyncio
import copy
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_daily_reminder.api import (
    MYS_DEVICE_FP,
    MYS_DEVICE_ID,
    SR_WIDGET,
    WIDGET_ROUTES,
    ApiError,
    Client,
    make_web_ds,
)
from astrbot_plugin_daily_reminder.models import Role
from fixtures import DATA, ROLES, account
from test_network import Response


def response(data, code=0):
    return Response(json.dumps({"retcode": code, "data": data}).encode())


def widget_data():
    return {
        k: copy.deepcopy(v)
        for k, v in DATA["starrail"].items()
        if k
        in (
            "current_stamina",
            "max_stamina",
            "current_train_score",
            "max_train_score",
            "expeditions",
            "current_rogue_score",
            "max_rogue_score",
        )
    }


def widget_account():
    acc = account()
    acc["stoken"] = "stuid=12345;stoken=FAKE_STOKEN;mid=FAKE_MID"
    acc["device_fp_state"] = {"device_id": acc["device_id"], "fp": "abcdef0123456"}
    return acc


class WidgetTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses):
        session = SimpleNamespace(get=Mock(side_effect=responses), post=Mock())
        return Client(session), session

    async def test_widget_success_uses_stoken_native_headers_and_no_uid_params(self):
        client, session = self.client(response(widget_data()))
        note = await client.fetch(widget_account(), ROLES[1])
        self.assertEqual(note.stamina, 228)
        self.assertFalse(note.error)
        self.assertEqual(session.get.call_count, 1)
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], SR_WIDGET)
        self.assertEqual(kwargs["params"], "")
        self.assertFalse(kwargs["allow_redirects"])
        headers = kwargs["headers"]
        self.assertIn("stoken=FAKE_STOKEN", headers["Cookie"])
        self.assertNotIn("cookie_token", headers["Cookie"])
        self.assertEqual(headers["User-Agent"], "okhttp/4.8.0")
        self.assertEqual(headers["Referer"], "https://app.mihoyo.com")
        self.assertEqual(headers["x-rpc-channel"], "beta")
        self.assertNotIn("Origin", headers)
        self.assertEqual(headers["x-rpc-device_fp"], MYS_DEVICE_FP)
        self.assertEqual(headers["x-rpc-device_id"], MYS_DEVICE_ID)
        session.post.assert_not_called()

    async def test_no_stoken_goes_directly_to_note(self):
        acc = widget_account()
        acc["stoken"] = ""
        client, session = self.client(response(DATA["starrail"]))
        await client.fetch(acc, ROLES[1])
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(session.get.call_args.args[0].endswith("/api/note"))

    async def test_widget_errors_fall_back_to_cookie(self):
        for code in (-100, 1034, 10041, 10035, 10101):
            with self.subTest(code=code):
                client, session = self.client(response({}, code), response(DATA["starrail"]))
                note = await client.fetch(widget_account(), ROLES[1])
                self.assertEqual(note.stamina, 228)
                self.assertEqual(session.get.call_count, 2)
                self.assertIn("cookie_token", session.get.call_args.kwargs["headers"]["Cookie"])

    async def test_timeout_and_invalid_schema_fall_back(self):
        for first in (
            asyncio.TimeoutError("SECRET"),
            response({}),
            response({"current_stamina": True, "max_stamina": 240}),
        ):
            client, session = self.client(first, response(DATA["starrail"]))
            note = await client.fetch(widget_account(), ROLES[1])
            self.assertEqual(note.stamina, 228)
            self.assertEqual(session.get.call_count, 2)

    async def test_both_interfaces_restricted_have_bounded_requests(self):
        client, session = self.client(response({}, 10041), response({}, 1034), response({}, 1034))
        with self.assertRaises(ApiError) as caught:
            await client.fetch(widget_account(), ROLES[1])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(caught.exception.code, "captcha")
        self.assertNotIn("FAKE", str(caught.exception))

    async def test_missing_widget_weeklies_remain_unknown(self):
        client, _ = self.client(response(widget_data()))
        note = await client.fetch(widget_account(), ROLES[1])
        text = str(note)
        self.assertIn("暂未获取", text)
        self.assertFalse(note.error)

    async def test_multiple_roles_require_response_identity(self):
        acc = widget_account()
        acc["roles"].append(asdict(Role("starrail", "500000001", "prod_qd_cn", "另一个角色")))
        for link, expected in (
            (None, 2),
            ("https://webstatic.mihoyo.com/?role_id=100000002&server=prod_gf_cn", 1),
            ("https://webstatic.mihoyo.com/#/rpg?role_id=100000002&server=prod_gf_cn", 1),
            ("https://webstatic.mihoyo.com/?role_id=500000001&server=prod_qd_cn", 2),
        ):
            data = widget_data()
            if link:
                data["note_url"] = link
            client, session = self.client(response(data), response(DATA["starrail"]))
            await client.fetch(acc, ROLES[1])
            self.assertEqual(session.get.call_count, expected)

    async def test_explicit_wrong_role_rejected_even_for_single_binding(self):
        for fields in ({"role_id": "500000001"}, {"server": "prod_qd_cn"}):
            client, session = self.client(
                response({**widget_data(), **fields}), response(DATA["starrail"])
            )
            await client.fetch(widget_account(), ROLES[1])
            self.assertEqual(session.get.call_count, 2)

    async def test_invalid_stoken_identity_not_sent(self):
        for token in ("stuid=999;stoken=FAKE;mid=FAKE", "stuid=12345;stoken=FAKE"):
            acc = widget_account()
            acc["stoken"] = token
            client, session = self.client(response(DATA["starrail"]))
            await client.fetch(acc, ROLES[1])
            self.assertEqual(session.get.call_count, 1)
            self.assertNotEqual(session.get.call_args.args[0], SR_WIDGET)

    async def test_other_games_use_their_own_widget(self):
        for role in (ROLES[0], ROLES[2]):
            client, session = self.client(response(DATA[role.game]))
            await client.fetch(widget_account(), role)
            self.assertEqual(session.get.call_args.args[0], WIDGET_ROUTES[role.game])
            headers = session.get.call_args.kwargs["headers"]
            self.assertIn("stoken=", headers["Cookie"])
            if role.game == "genshin":
                self.assertEqual(session.get.call_args.kwargs["params"], "game_id=2")
                self.assertEqual(headers["x-rpc-channel"], "miyousheluodi")
                self.assertIn("DS", headers)
            else:
                self.assertEqual(session.get.call_args.kwargs["params"], "")
                self.assertNotIn("DS", headers)
                self.assertNotIn("x-rpc-client_type", headers)
                self.assertEqual(headers["Origin"], "https://act.mihoyo.com")

    def test_genshin_web_signature_uses_web_salt_without_query_body(self):
        import hashlib

        stamp, nonce, digest = make_web_ds().split(",")
        expected = hashlib.md5(
            f"salt=yBh10ikxtLPoIhgwgPZSv5dmfaOTSJ6a&t={stamp}&r={nonce}".encode()
        ).hexdigest()
        self.assertEqual(digest, expected)
        self.assertEqual(len(nonce), 6)

    async def test_genshin_widget_missing_encounter_points_and_weeklies(self):
        data = {
            k: copy.deepcopy(v)
            for k, v in DATA["genshin"].items()
            if k
            not in (
                "daily_task",
                "week_active_progress",
                "remain_resin_discount_num",
                "resin_discount_num_limit",
            )
        }
        for claimed in (False, True):
            data["is_extra_task_reward_received"] = claimed
            client, session = self.client(response(data))
            note = await client.fetch(widget_account(), ROLES[0])
            commissions = next(f for f in note.daily if f.label == "每日委托")
            self.assertEqual(note.daily_complete, claimed)
            self.assertEqual(commissions.pending, not claimed)
            if claimed:
                self.assertEqual(commissions.current, 4)
            else:
                self.assertIn("历练点暂未获取", commissions.value)
            self.assertTrue(all(f.value == "暂未获取" for f in note.weekly))
            self.assertEqual(session.get.call_count, 1)

    async def test_genshin_widget_preserves_encounter_reward_slots(self):
        data = copy.deepcopy(DATA["genshin"])
        client, _ = self.client(response(data))
        note = await client.fetch(widget_account(), ROLES[0])
        commissions = next(f for f in note.daily if f.label == "每日委托")
        self.assertEqual(commissions.current, 3)
        self.assertIn("1 项历练点可领取", commissions.value)

    async def test_new_widgets_fall_back_on_failure_or_wrong_role(self):
        for role in (ROLES[0], ROLES[2]):
            for first in (
                response({}, 10035),
                response({}),
                response({**DATA[role.game], "role_id": "wrong"}),
            ):
                client, session = self.client(first, response(DATA[role.game]))
                note = await client.fetch(widget_account(), role)
                self.assertFalse(note.error)
                self.assertEqual(session.get.call_count, 2)
                self.assertNotEqual(session.get.call_args.args[0], WIDGET_ROUTES[role.game])

    async def test_zzz_widget_preserves_new_bounty_and_unknown_missing_weeklies(self):
        data = copy.deepcopy(DATA["zzz"])
        data["s2_bounty_commission"] = {"num": 0, "total": 3}
        client, _ = self.client(response(data))
        note = await client.fetch(widget_account(), ROLES[2])
        self.assertEqual(note.weekly[1].current, 0)
        for key in ("s2_bounty_commission", "bounty_commission", "weekly_task", "hollow_zero"):
            data.pop(key, None)
        client, _ = self.client(response(data))
        note = await client.fetch(widget_account(), ROLES[2])
        self.assertTrue(all(f.value == "暂未获取" for f in note.weekly))
