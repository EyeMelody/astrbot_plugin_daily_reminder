import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_daily_reminder.store import Store
from fixtures import account


class PrivateStoreTests(unittest.TestCase):
    def test_upgrade_preserves_private_history_and_excludes_old_groups(self):
        path = Path(tempfile.mkdtemp()) / "daily.sqlite3"
        owner = json.dumps(["bot", "11"])
        private = "bot:FriendMessage:11"
        now = 1791532800  # fixed timestamp; all dates below come from the store
        legacy = Store(path)
        legacy.save_account(owner, account())
        for origin, mention in (
            (private, ""),
            ("bot:GroupMessage:55", "11"),
            ("unknown", ""),
            ("bot:FriendMessage:22", ""),
        ):
            legacy.subscribe(owner, origin, mention, "23:00", now, ["genshin"])
        legacy.close()
        store = Store(path, private_only=True)
        try:
            self.assertIsNotNone(store.account(owner))
            self.assertEqual(store.reminder_counts(owner), {"genshin": 1})
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 4
            )
            rows = []
            for minute in range(24 * 60):
                rows += store.due(now + 86400 + minute * 60)
            self.assertTrue(rows)
            self.assertTrue(all(row["origin"] == private for row in rows))
            store.delivered(rows[0])
            expected_day = rows[0]["day"]
            store.close()
            store = Store(path, private_only=True)
            self.assertEqual(store.subscription(owner, private)["last_sent"], expected_day)
            self.assertEqual(store.due(rows[0]["due"]), [])
            for origin in ("bot:GroupMessage:55", "unknown", "bot:FriendMessage:22"):
                with self.assertRaises(ValueError):
                    store.subscribe(owner, origin, "", "23:00", now, ["genshin"])
        finally:
            store.close()

    def test_ten_old_groups_do_not_block_new_private_reminder(self):
        path = Path(tempfile.mkdtemp()) / "daily.sqlite3"
        owner = json.dumps(["bot", "11"])
        legacy = Store(path)
        legacy.save_account(owner, account())
        for i in range(10):
            legacy.subscribe(owner, f"bot:GroupMessage:{i}", "11", "23:00", 0, ["genshin"])
        legacy.close()
        store = Store(path, private_only=True)
        try:
            store.subscribe(owner, "bot:FriendMessage:11", "", "23:00", 0, ["genshin"])
            self.assertEqual(store.reminder_counts(owner), {"genshin": 1})
        finally:
            store.close()
