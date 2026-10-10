import unittest

from astrbot_plugin_daily_reminder.models import parse_note
from astrbot_plugin_daily_reminder.render import STATUS_COLORS
from fixtures import DATA, ROLES, stamp
from test_nte import NTE_DATA, NTE_ROLE


class StatusTests(unittest.TestCase):
    def starrail(self, **fields):
        data = {**DATA["starrail"], "current_train_score": 500, "expeditions": [], **fields}
        note = parse_note(ROLES[1], data)
        note.observed_at = stamp("2026-10-02 23:00:00")
        return note

    def test_completed_daily_excludes_stamina_and_weekly_boss(self):
        note = self.starrail(current_stamina=240)
        self.assertEqual(note.summary, "⸜(๑'ᵕ'๑)⸝日常已完成，安心～")
        self.assertEqual(note.login_state, "active")

    def test_pending_daily_and_dispatch_reward(self):
        note = self.starrail(current_train_score=300, expeditions=[{"status": "Finished"}])
        self.assertIn("每日实训、探索派遣", note.summary)
        self.assertNotIn("宇宙", note.summary)
        self.assertNotIn("历战", note.summary)

    def test_sunday_adds_weekly_and_monday_removes_it(self):
        note = self.starrail()
        note.observed_at = stamp("2026-10-03 00:00:00")
        self.assertIn("模拟宇宙积分、差分宇宙积分", note.summary)
        self.assertNotIn("历战余响", note.summary)
        note.observed_at = stamp("2026-10-04 00:00:00")
        self.assertIn("模拟宇宙积分、差分宇宙积分", note.summary)
        self.assertNotIn("历战余响", note.summary)
        note.observed_at = stamp("2026-10-05 00:00:00")
        self.assertIn("日常已完成", note.summary)

    def test_unknown_progress_does_not_claim_completion(self):
        note = self.starrail(expeditions=None)
        self.assertIn("暂未获取", note.summary)
        self.assertNotIn("日常已完成", note.summary)

    def test_locked_weekly_is_not_a_todo(self):
        note = self.starrail(current_rogue_score=14000, rogue_tourn_weekly_unlocked=False)
        note.observed_at = stamp("2026-10-04 23:00:00")
        self.assertIn("日常已完成", note.summary)

    def test_genshin_encounter_points_and_reward(self):
        data = {
            **DATA["genshin"],
            "daily_task": {
                "total_num": 4,
                "finished_num": 0,
                "is_extra_task_reward_received": False,
                "attendance_rewards": [{"status": "AttendanceRewardStatusTakenAward"}] * 4,
            },
            "expeditions": [],
        }
        note = parse_note(ROLES[0], data)
        self.assertEqual(note.login_state, "active")
        self.assertIn("每日委托", note.summary)
        self.assertIn("奖励尚未领取", note.text())
        self.assertNotIn("每日奖励", [f.label for f in note.daily])

    def test_zzz_shop_and_scratch_todos(self):
        note = parse_note(
            ROLES[2],
            {**DATA["zzz"], "card_sign": "CardSignNo", "vhs_sale": {"sale_state": "SaleStateDone"}},
        )
        self.assertIn("刮刮卡、录像店", note.summary)
        note.observed_at = stamp("2026-10-04 23:00:00")
        self.assertIn("零号空洞悬赏", note.summary)

    def test_inactive_and_unknown_are_distinct(self):
        note = self.starrail(current_train_score=0, current_stamina=240)
        self.assertEqual(note.login_state, "inactive")
        self.assertEqual(note.summary, "｡ﾟ(ﾟ´ω`ﾟ)ﾟ｡今天似乎还没有登录哦～")
        self.assertNotEqual(STATUS_COLORS["inactive"], STATUS_COLORS["active"])
        note.stamina = 10
        self.assertEqual(note.login_state, "inactive")
        note.seen = True
        self.assertEqual(note.login_state, "inactive")
        note.progress = None
        note.seen = False
        self.assertEqual(note.login_state, "unknown")
        note.progress = 100
        note.activity = True
        self.assertEqual(note.login_state, "active")

    def test_nte_city_stamina_is_weekly_but_not_a_task(self):
        note = parse_note(NTE_ROLE, {**NTE_DATA, "dayvalue": 100})
        note.observed_at = stamp("2026-10-04 23:00:00")
        self.assertNotIn("都市活力", [f.label for f in note.daily])
        self.assertIn("都市活力", [f.label for f in note.weekly])
        self.assertIn("日常已完成", note.summary)
        self.assertNotIn("周本", note.summary)

    def test_genshin_week_activity_sunday_and_period_are_separate(self):
        note = parse_note(
            ROLES[0],
            {
                **DATA["genshin"],
                "week_active_progress": {
                    "unlock": True,
                    "is_active_period": True,
                    "progress_current": 2,
                    "progress_total": 3,
                    "period_progress_current": 1,
                    "period_progress_total": 6,
                },
            },
        )
        self.assertEqual(note.weekly[0].value, "2/3 · 本期 1/6 周")
        note.observed_at = stamp("2026-10-02 23:00:00")
        self.assertNotIn("砺行修远", note.summary)
        note.observed_at = stamp("2026-10-03 23:00:00")
        self.assertIn("砺行修远", note.summary)
        note.observed_at = stamp("2026-10-04 23:00:00")
        self.assertIn("砺行修远", note.summary)
        self.assertNotIn("周本树脂减半", note.summary)

    def test_week_activity_unknown_locked_and_closed(self):
        for fields, value, pending in [
            ({}, "暂未获取", None),
            ({"unlock": False}, "未解锁", False),
            ({"is_active_period": False}, "本期未开放", False),
        ]:
            fact = parse_note(ROLES[0], {"week_active_progress": fields}).weekly[0]
            self.assertEqual((fact.value, fact.pending), (value, pending))

    def test_zzz_prefers_current_season_over_legacy(self):
        note = parse_note(
            ROLES[2],
            {
                **DATA["zzz"],
                "s2_bounty_commission": {"num": 1, "total": 2},
                "s1_bounty_commission": {"num": 0, "total": 4},
            },
        )
        self.assertEqual(note.weekly[1].value, "1/2")
        self.assertNotIn("零号空洞调查点", [f.label for f in note.weekly])

    def test_zzz_uses_previous_season_when_current_is_empty(self):
        note = parse_note(
            ROLES[2], {"s2_bounty_commission": {}, "s1_bounty_commission": {"num": 2, "total": 2}}
        )
        self.assertEqual(note.weekly[1].value, "2/2")
        self.assertFalse(note.weekly[1].pending)

    def test_commissions_combine_encounters_and_reward_without_duplicate_rows(self):
        note = parse_note(ROLES[0], DATA["genshin"])
        self.assertEqual([f.label for f in note.daily], ["树脂", "每日委托", "探索派遣"])
        self.assertIn("3/4", note.daily[1].value)
        self.assertIn("1 项历练点可领取", note.daily[1].value)
        self.assertTrue(note.daily[1].pending)
