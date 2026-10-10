"""Synthetic records only; no live account data."""

from datetime import datetime

from astrbot_plugin_daily_reminder.models import CN, Role, parse_note
from astrbot_plugin_daily_reminder.store import Store

ROLES = [
    Role("genshin", "100000001", "cn_gf01", "旅行者"),
    Role("starrail", "100000002", "prod_gf_cn", "开拓者"),
    Role("zzz", "10000003", "prod_gf_cn", "绳匠"),
]
DATA = {
    "genshin": {
        "current_resin": 180,
        "max_resin": 200,
        "finished_task_num": 0,
        "total_task_num": 4,
        "is_extra_task_reward_received": False,
        "daily_task": {
            "total_num": 4,
            "finished_num": 0,
            "is_extra_task_reward_received": False,
            "attendance_rewards": [{"status": "AttendanceRewardStatusTakenAward"}] * 3
            + [{"status": "AttendanceRewardStatusWaitTaken"}],
        },
        "expeditions": [{"status": "Finished"}, {"status": "Ongoing"}],
        "remain_resin_discount_num": 2,
        "resin_discount_num_limit": 3,
        "week_active_progress": {
            "unlock": True,
            "is_active_period": True,
            "progress_current": 2,
            "progress_total": 3,
            "period_progress_current": 1,
            "period_progress_total": 6,
        },
    },
    "starrail": {
        "current_stamina": 228,
        "max_stamina": 240,
        "current_train_score": 300,
        "max_train_score": 500,
        "expeditions": [{"status": "Finished"}] * 4,
        "current_rogue_score": 9000,
        "max_rogue_score": 14000,
        "rogue_tourn_weekly_cur": 600,
        "rogue_tourn_weekly_max": 1000,
        "rogue_tourn_weekly_unlocked": True,
        "weekly_cocoon_cnt": 3,
        "weekly_cocoon_limit": 3,
    },
    "zzz": {
        "energy": {"progress": {"current": 240, "max": 240}},
        "vitality": {"current": 400, "max": 400},
        "card_sign": "CardSignDone",
        "vhs_sale": {"sale_state": "SaleStateDoing"},
        "weekly_task": {"cur_point": 300, "max_point": 400},
        "bounty_commission": {"num": 2, "total": 3, "unlock": True},
        "survey_points": {"num": 6000, "total": 8000},
    },
}


def stamp(value="2026-10-03 22:59:00"):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp()


def account(aid="12345", roles=None):
    return Store.login_account(
        aid,
        f"account_id={aid}; cookie_token=SYNTHETIC",
        "test-device",
        "",
        ROLES if roles is None else roles,
    )


def notes():
    result = [parse_note(r, DATA[r.game]) for r in ROLES]
    for n in result:
        n.observed_at = stamp("2026-10-03 23:00:00")
    return result
