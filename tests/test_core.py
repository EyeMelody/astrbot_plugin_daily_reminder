import asyncio
import copy
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_daily_reminder.api import ApiError, Client, account_id, normalize_cookie
from astrbot_plugin_daily_reminder.auth import Auth
from astrbot_plugin_daily_reminder.models import Note, Role, game_day, parse_note
from astrbot_plugin_daily_reminder.render import Renderer, draw_card
from astrbot_plugin_daily_reminder.service import Service
from astrbot_plugin_daily_reminder.store import Store
from fixtures import DATA, ROLES, account, notes, stamp
from PIL import Image


class ParsingTests(unittest.TestCase):
    def test_three_games(self):
        result = notes()
        self.assertEqual([n.stamina for n in result], [180, 228, 240])
        self.assertEqual([n.progress for n in result], [3, 300, 400])
        self.assertTrue(all(n.weekly for n in result))

    def test_encounter_points_do_not_double_count_commissions(self):
        data = copy.deepcopy(DATA["genshin"])
        data["daily_task"]["task_rewards"] = [{"status": "TaskRewardStatusTakenAward"}] * 2
        note = parse_note(ROLES[0], data)
        self.assertEqual(note.progress, 3)
        self.assertFalse(note.daily_complete)

    def test_all_encounters_claimed_reward_pending(self):
        data = copy.deepcopy(DATA["genshin"])
        data["daily_task"]["attendance_rewards"] = [
            {"status": "AttendanceRewardStatusTakenAward"}
        ] * 4
        note = parse_note(ROLES[0], data)
        self.assertEqual(note.progress, 4)
        self.assertFalse(note.daily_complete)
        self.assertIn("尚未领取", note.text())

    def test_reward_claimed_means_daily_done(self):
        note = parse_note(ROLES[0], {"total_task_num": 4, "is_extra_task_reward_received": True})
        self.assertEqual(note.progress, 4)
        self.assertTrue(note.daily_complete)

    def test_missing_fields_are_unknown_not_zero(self):
        for role in ROLES:
            with self.subTest(game=role.game):
                note = parse_note(role, {})
                self.assertIsNone(note.stamina)
                self.assertIsNone(note.progress)
                self.assertNotIn("似乎还没有登录", note.summary)
                self.assertIn("暂未获取", note.text())

    def test_unlocked_false_is_distinct(self):
        note = parse_note(ROLES[1], {"rogue_tourn_weekly_unlocked": False})
        self.assertEqual(note.weekly[1].value, "未解锁")

    def test_zzz_unlocked_false(self):
        note = parse_note(ROLES[2], {"bounty_commission": {"unlock": False}})
        self.assertEqual(note.weekly[1].value, "未解锁")

    def test_zero_daily_progress_overrides_activity_observation(self):
        note = parse_note(
            ROLES[1],
            {
                "current_stamina": 240,
                "max_stamina": 240,
                "current_train_score": 0,
                "max_train_score": 500,
            },
        )
        self.assertIn("似乎还没有登录", note.summary)
        note.seen = True
        self.assertIn("似乎还没有登录", note.summary)

    def test_four_am_and_week_rollover(self):
        self.assertEqual(game_day(stamp("2026-10-05 03:59:59")), "2026-10-04")
        self.assertEqual(game_day(stamp("2026-10-05 04:00:00")), "2026-10-05")

    def test_invalid_numeric_values(self):
        note = parse_note(
            ROLES[1], {"current_stamina": True, "max_stamina": -1, "current_train_score": "bad"}
        )
        self.assertIsNone(note.stamina)
        self.assertIsNone(note.progress)

    def test_cookies_do_not_accept_conflicting_ids(self):
        with self.assertRaises(ValueError):
            normalize_cookie("account_id=1;ltuid=2;cookie_token=test")
        self.assertEqual(account_id("account_id_v2=123;cookie_token_v2=test"), "123")

    def test_error_mapping(self):
        for code, kind, retry in [
            (10001, "auth", False),
            (1034, "captcha", False),
            (10102, "private", False),
            (10101, "rate", True),
        ]:
            with self.subTest(code=code), self.assertRaises(ApiError) as caught:
                Client.decode({"retcode": code, "message": "SECRET"})
            self.assertEqual(caught.exception.code, kind)
            self.assertEqual(caught.exception.transient, retry)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_captcha_codes_remain_visible_without_raw_server_message(self):
        for code in (1034, 10035, 5003, 10041):
            with self.assertRaises(ApiError) as caught:
                Client.decode({"retcode": code, "message": "SECRET"})
            self.assertIn(str(code), str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertFalse(caught.exception.transient)

    def test_invalid_payload(self):
        for payload in ([], {}, {"retcode": 0, "data": []}, {"retcode": "0", "data": {}}):
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                Client.decode(payload)

    def test_preview_long_name_and_error(self):
        sample = notes()
        sample[0].role = Role("genshin", "100000001", "cn_gf01", "很长的旅行者名字" * 6)
        sample[1] = Note(
            ROLES[1], error="米游社要求验证，请先在官方应用完成验证", observed_at=stamp()
        )
        with Image.open(io.BytesIO(draw_card(sample))) as im:
            self.assertEqual(im.width, 960)
            self.assertLess(im.height, 4096)
            self.assertGreater(im.height, 800)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="daily-tests-")) / "test.sqlite3"
        self.store = Store(self.path)
        self.store.save_account("owner", account())

    def tearDown(self):
        self.store.close()

    def test_unique_role_auto_selection(self):
        self.assertEqual(len(self.store.selected_roles("owner")), 3)

    def test_multiple_roles_require_selection(self):
        roles = [ROLES[0], Role("genshin", "500000001", "cn_qd01", "另一角色")]
        self.store.save_account("new", account(roles=roles))
        with self.assertRaises(ValueError):
            self.store.selected_roles("new")
        self.store.select("new", "genshin", "500000001")
        self.assertEqual(self.store.selected_roles("new")[0].uid, "500000001")

    def test_cross_user_role_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.select("stranger", "genshin", ROLES[0].uid)
        with self.assertRaises(ValueError):
            self.store.select("owner", "genshin", "500000001")

    def test_relogin_preserves_subscriptions(self):
        self.store.subscribe("owner", "group", "1", "23:00", stamp())
        self.store.save_account("owner", account())
        self.assertIsNotNone(self.store.subscription("owner", "group"))

    def test_replace_requires_confirmation(self):
        self.store.subscribe("owner", "group", "1", "23:00", stamp())
        with self.assertRaises(ValueError):
            self.store.save_account("owner", account("54321"))
        self.store.save_account("owner", account("54321"), replace=True)
        self.assertIsNone(self.store.subscription("owner", "group"))

    def test_unbind_cascades(self):
        self.store.subscribe("owner", "group", "1", "23:00", stamp())
        self.store.observe("owner", notes()[0], stamp())
        self.store.unbind("owner")
        self.assertIsNone(self.store.account("owner"))
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0
        )
        self.assertIsNone(self.store.subscription("owner", "group"))

    def test_set_after_time_starts_next_day(self):
        start = self.store.subscribe("owner", "group", "1", "22:00", stamp())
        self.assertEqual(start, "2026-10-04")
        self.assertEqual(self.store.due(stamp()), [])

    def test_due_window_and_restart_deduplication(self):
        self.store.subscribe("owner", "group", "1", "23:00", stamp())
        rows = self.store.due(stamp("2026-10-03 23:29:59"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.due(stamp("2026-10-03 23:30:01")), [])
        self.store.delivered(rows[0])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.due(stamp("2026-10-03 23:30:00")), [])

    def test_midnight_catchup_uses_schedule_date(self):
        self.store.subscribe("owner", "group", "1", "23:59", stamp())
        row = self.store.due(stamp("2026-10-04 00:05:00"))[0]
        self.assertEqual(row["day"], "2026-10-03")

    def test_max_ten_sessions_but_updates_allowed(self):
        for i in range(10):
            self.store.subscribe("owner", str(i), "1", "23:00", stamp())
        self.store.subscribe("owner", "0", "1", "23:15", stamp())
        with self.assertRaises(ValueError):
            self.store.subscribe("owner", "10", "1", "23:00", stamp())

    def test_invalid_times(self):
        for value in ("24:00", "9:00", "23:60", "-1:00", "23:00 xx"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.subscribe("owner", "group", "1", value, stamp())

    def test_stamina_drop_does_not_cross_game_day(self):
        first = notes()[1]
        self.store.observe("owner", first, stamp("2026-10-03 03:59:00"))
        second = parse_note(
            ROLES[1], {"current_stamina": 10, "max_stamina": 240, "current_train_score": 0}
        )
        self.store.observe("owner", second, stamp("2026-10-03 04:00:00"))
        self.assertFalse(second.seen)
        third = parse_note(
            ROLES[1], {"current_stamina": 5, "max_stamina": 240, "current_train_score": 0}
        )
        self.store.observe("owner", third, stamp("2026-10-03 04:01:00"))
        self.assertTrue(third.seen)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="daily-async-")) / "test.sqlite3")
        self.store.save_account("owner", account())
        self.now = stamp("2026-10-03 23:00:00")
        self.client = SimpleNamespace(
            fetch=AsyncMock(side_effect=lambda a, r: parse_note(r, DATA[r.game]))
        )
        self.login = SimpleNamespace(
            refresh_cookie=AsyncMock(return_value="account_id=12345;cookie_token=REFRESHED")
        )
        self.send = AsyncMock(return_value=True)
        self.service = Service(
            self.store, self.client, self.login, self.send, Mock(), clock=lambda: self.now
        )
        self.store.subscribe("owner", "group", "1", "23:00", stamp())

    async def asyncTearDown(self):
        self.store.close()

    async def test_two_sessions_share_queries_and_do_not_repeat(self):
        self.store.subscribe("owner", "private", "", "23:00", stamp())
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(self.client.fetch.await_count, 3)
        self.assertEqual(self.send.await_count, 2)

    async def test_transient_retries_only_failed_game(self):
        attempts = {r.game: 0 for r in ROLES}

        async def fetch(a, r):
            attempts[r.game] += 1
            if r.game == "starrail":
                raise ApiError("暂时失败", transient=True)
            return parse_note(r, DATA[r.game])

        self.client.fetch.side_effect = fetch
        await self.service.tick()
        self.assertEqual(self.send.await_count, 0)
        self.now += 60
        await self.service.tick()
        self.assertEqual(self.send.await_count, 0)
        self.now += 299
        await self.service.tick()
        self.assertEqual(attempts["starrail"], 2)
        self.now += 1
        await self.service.tick()
        self.assertEqual(attempts, {"genshin": 1, "starrail": 3, "zzz": 1})
        self.assertEqual(self.send.await_count, 1)
        self.assertTrue(self.send.call_args.args[1][1].error)

    async def test_auth_failure_is_not_retried_for_each_game(self):
        self.client.fetch.side_effect = ApiError("重新登录", "auth", auth=True)
        await self.service.tick()
        self.assertEqual(self.client.fetch.await_count, 1)
        self.assertEqual(self.send.await_count, 1)
        self.assertTrue(all(n.error for n in self.send.call_args.args[1]))

    async def test_cookie_refresh_once_then_success(self):
        acc = account()
        acc["stoken"] = "synthetic-stoken"
        self.store.save_account("owner", acc)

        async def fetch(a, r):
            if "REFRESHED" not in a["cookie"]:
                raise ApiError("过期", "auth", auth=True)
            return parse_note(r, DATA[r.game])

        self.client.fetch.side_effect = fetch
        await self.service.tick()
        self.assertEqual(self.login.refresh_cookie.await_count, 1)
        self.assertEqual(self.send.await_count, 1)

    async def test_send_failure_reuses_result(self):
        self.send.side_effect = [False, True]
        await self.service.tick()
        self.assertEqual(self.store.subscription("owner", "group")["last_sent"], "")
        self.now += 60
        await self.service.tick()
        self.assertEqual(self.client.fetch.await_count, 3)
        self.assertEqual(self.store.subscription("owner", "group")["last_sent"], "2026-10-03")

    async def test_manual_query_cache(self):
        await self.service.query("owner")
        await self.service.query("owner", "starrail")
        self.assertEqual(self.client.fetch.await_count, 3)

    async def test_alternating_games_keep_independent_cache(self):
        await self.service.query("owner", "genshin")
        await self.service.query("owner", "starrail")
        await self.service.query("owner", "genshin")
        self.assertEqual(self.client.fetch.await_count, 2)

    async def test_unselected_account_does_not_block_other_subscribers(self):
        # Model a same-account relogin with previously selected roles removed.
        self.store.save_account("owner", account(roles=[]))
        self.store.save_account("other", account())
        self.store.subscribe("other", "private", "", "23:00", stamp())
        await self.service.tick()
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.send.call_args.args[0]["owner"], "other")

    async def test_qr_success_saves_verified_roles(self):
        api = SimpleNamespace(
            create=AsyncMock(
                return_value={"url": "https://mihoyo.com/login", "device_id": "device"}
            ),
            status=AsyncMock(return_value={"status": "Confirmed"}),
            credentials=AsyncMock(
                return_value=("12345", "account_id=12345;cookie_token=test", "stoken=test")
            ),
        )
        self.client.roles = AsyncMock(return_value=ROLES)
        auth = Auth(api, self.client, self.service, self.send, Mock())
        await auth.run("owner", "private")
        saved = self.store.account("owner")
        self.assertEqual(saved["stoken"], "stoken=test")
        self.assertEqual(len(saved["selected"]), 3)
        self.assertIn("绑定成功", self.send.call_args.args[1])

    async def test_completed_daily_is_still_sent(self):
        self.client.fetch.side_effect = lambda a, r: Note(r, daily_complete=True)
        await self.service.tick()
        self.assertEqual(self.send.await_count, 1)

    async def test_login_replaces_account_directly(self):
        auth = Auth(
            self.login, self.client, self.service, self.send, Mock(), clock=lambda: self.now
        )
        message = await auth.commit("owner", account("54321"))
        self.assertIn("已更换", message)
        self.assertEqual(self.store.account("owner")["aid"], "54321")
        self.assertIsNone(self.store.subscription("owner", "group"))
        self.assertFalse(hasattr(auth, "pending"))

    async def test_qr_close_cancels_tasks(self):
        entered = asyncio.Event()

        async def create():
            entered.set()
            await asyncio.Event().wait()

        auth = Auth(SimpleNamespace(create=create), self.client, self.service, self.send, Mock())
        await auth.start("owner", "private")
        await entered.wait()
        await auth.close()
        self.assertEqual(auth.tasks, {})

    async def test_renderer_closes(self):
        renderer = Renderer()
        self.assertTrue(await renderer.render(notes()))
        await renderer.close()
        with self.assertRaises(RuntimeError):
            await renderer.render(notes())

    async def test_ownership_comes_from_authenticated_roles(self):
        client = Client(None)
        client.request = AsyncMock(
            side_effect=[
                {
                    "list": [
                        {"game_uid": "100000001", "region": "cn_gf01", "nickname": "test"},
                        {"game_uid": "600000001", "region": "os_usa"},
                    ]
                },
                {"list": []},
                {"list": []},
            ]
        )
        result = await client.roles(account())
        self.assertEqual(len(result), 1)
        self.assertIn("getUserGameRolesByCookie", client.request.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
