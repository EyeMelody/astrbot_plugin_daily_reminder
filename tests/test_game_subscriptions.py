import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_daily_reminder.api import ApiError
from astrbot_plugin_daily_reminder.models import parse_note
from astrbot_plugin_daily_reminder.render import account_entries, draw_accounts
from astrbot_plugin_daily_reminder.service import Service
from astrbot_plugin_daily_reminder.store import Store
from fixtures import DATA, account, stamp
from PIL import Image
from test_nte import nte_account


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="daily-subscription-tests-"))
        self.path = self.temp / "test.sqlite3"
        self.store = Store(self.path)
        self.store.save_account("owner", account())

    async def asyncTearDown(self):
        self.store.close()

    async def test_each_session_gets_only_its_games_and_shared_queries(self):
        self.store.subscribe("owner", "a", "", "23:00", stamp(), ["genshin"])
        self.store.subscribe("owner", "b", "", "23:00", stamp(), ["genshin", "zzz"])
        client = Mock()
        client.fetch = AsyncMock(side_effect=lambda a, r: parse_note(r, DATA[r.game]))
        send = AsyncMock(return_value=True)
        service = Service(
            self.store, client, Mock(), send, Mock(), clock=lambda: stamp("2026-10-03 23:00:00")
        )
        await service.tick()
        self.assertEqual(client.fetch.await_count, 2)
        result = {
            call.args[0]["origin"]: [n.role.game for n in call.args[1]]
            for call in send.await_args_list
        }
        self.assertEqual(result, {"a": ["genshin"], "b": ["genshin", "zzz"]})
        await service.tick()
        self.assertEqual(send.await_count, 2)

    async def test_legacy_migration_snapshots_games_and_preserves_delivery(self):
        self.store.close()
        legacy = self.temp / "old.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript(
                "CREATE TABLE accounts(owner TEXT PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE subscriptions(owner TEXT,origin TEXT,mention TEXT,clock TEXT,start_day TEXT,last_sent TEXT DEFAULT '',next_attempt REAL DEFAULT 0,PRIMARY KEY(owner,origin));"
            )
            payload = account()
            payload["selected"] = {r["game"]: r["uid"] for r in payload["roles"]}
            db.execute("INSERT INTO accounts VALUES(?,?)", ("owner", json.dumps(payload)))
            db.execute(
                "INSERT INTO subscriptions VALUES('owner','a','','23:00','2026-10-01','2026-10-03',0)"
            )
        db.close()
        self.store = Store(legacy)
        self.store.save_nte("owner", nte_account())
        row = self.store.subscription("owner", "a")
        self.assertEqual(row["games"], ["genshin", "starrail", "zzz"])
        self.assertEqual(row["last_sent"], "2026-10-03")
        self.store.close()
        self.store = Store(legacy)
        self.assertNotIn("nte", self.store.subscription("owner", "a")["games"])

    async def test_all_is_a_snapshot_and_unbind_removes_only_nte(self):
        self.store.subscribe("owner", "a", "", "23:00", stamp())
        self.store.save_nte("owner", nte_account())
        self.assertNotIn("nte", self.store.subscription("owner", "a")["games"])
        self.store.subscribe("owner", "b", "", "23:00", stamp(), ["nte"])
        self.store.subscribe("owner", "c", "", "23:00", stamp(), ["nte", "genshin"])
        self.store.unbind_nte("owner")
        self.assertIsNone(self.store.subscription("owner", "b"))
        self.assertEqual(self.store.subscription("owner", "c")["games"], ["genshin"])

    async def test_account_card_adds_only_available_games(self):
        before = draw_accounts(self.store.account("owner"))
        self.store.save_nte("owner", nte_account())
        after = draw_accounts(self.store.account("owner"))
        with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
            self.assertEqual(b.height - a.height, 158)

    async def test_account_reminder_badges_cover_all_sessions_and_isolate_users(self):
        self.store.subscribe("owner", "private", "", "23:00", stamp(), ["genshin"])
        self.store.subscribe("owner", "group", "11", "23:00", stamp(), ["genshin", "starrail"])
        self.store.save_account("other", account())
        self.store.subscribe("other", "group", "22", "23:00", stamp(), ["zzz"])
        counts = self.store.reminder_counts("owner")
        self.assertEqual(counts, {"genshin": 2, "starrail": 1})
        entries = account_entries(self.store.account("owner"), counts)
        self.assertEqual(
            [(r["name"], r["uid"], count) for r, _, count in entries],
            [("旅行者", "100000001", 2), ("开拓者", "100000002", 1), ("绳匠", "10000003", 0)],
        )
        self.store.unsubscribe("owner", "group")
        self.assertEqual(self.store.reminder_counts("owner"), {"genshin": 1})

    async def test_unselected_role_is_not_shown_as_subscribed(self):
        data = self.store.account("owner")
        data["roles"].append(
            {"game": "genshin", "uid": "100000009", "region": "cn_gf01", "name": "另一个角色"}
        )
        entries = account_entries(data, {"genshin": 1})
        other = next(entry for entry in entries if entry[0]["uid"] == "100000009")
        self.assertEqual(other[1:], (False, 0))

    async def test_other_session_failure_does_not_delay_success_or_reset_retries(self):
        self.store.subscribe("owner", "a", "", "23:00", stamp(), ["genshin"])
        self.store.subscribe("owner", "b", "", "23:00", stamp(), ["starrail"])
        now = [stamp("2026-10-03 23:00:00")]

        async def fetch(a, role):
            if role.game == "starrail":
                raise ApiError("网络超时", "network", transient=True)
            return parse_note(role, DATA[role.game])

        client, send = Mock(), AsyncMock(return_value=True)
        client.fetch = AsyncMock(side_effect=fetch)
        service = Service(self.store, client, Mock(), send, Mock(), clock=lambda: now[0])
        await service.tick()
        self.assertEqual([c.args[0]["origin"] for c in send.await_args_list], ["a"])
        now[0] += 60
        await service.tick()
        self.assertEqual(send.await_count, 1)
        now[0] += 300
        await service.tick()
        self.assertEqual([c.args[0]["origin"] for c in send.await_args_list], ["a", "b"])
        self.assertEqual(client.fetch.await_count, 4)
