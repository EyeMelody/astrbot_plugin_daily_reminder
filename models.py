"""Game-specific meaning stays here; renderers receive display-ready facts."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
GAMES = {"genshin": "原神", "starrail": "星铁", "zzz": "绝区零", "nte": "异环"}
ALIASES = {
    **{v: k for k, v in GAMES.items()},
    **{k: k for k in GAMES},
    "崩坏星穹铁道": "starrail",
    "星穹铁道": "starrail",
    "崩坏：星穹铁道": "starrail",
}


def game_day(stamp):
    return (datetime.fromtimestamp(stamp, CN) - timedelta(hours=4)).date().isoformat()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
        return value if 0 <= value <= 100000000 else None
    except (ValueError, TypeError, OverflowError):
        return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def sequence(value):
    return value[:100] if isinstance(value, list) else []


@dataclass(frozen=True)
class Role:
    game: str
    uid: str
    region: str
    name: str


@dataclass
class Fact:
    label: str
    value: str
    current: int | None = None
    maximum: int | None = None
    pending: bool | None = False


@dataclass
class Note:
    role: Role
    daily: list[Fact] = field(default_factory=list)
    weekly: list[Fact] = field(default_factory=list)
    stamina: int | None = None
    stamina_max: int | None = None
    progress: int | None = None
    daily_complete: bool = False
    activity: bool = False
    error: str = ""
    error_code: str = ""
    transient: bool = False
    observed_at: float = 0
    seen: bool = False

    @property
    def login_state(self):
        if self.error:
            return "unknown"
        if self.progress == 0:
            return "inactive"
        if self.daily_complete or self.activity or self.seen:
            return "active"
        return "unknown"

    @property
    def reminder_facts(self):
        sunday = self.observed_at > 0 and datetime.fromtimestamp(
            self.observed_at, CN
        ).weekday() in (5, 6)
        return self.daily + (self.weekly if sunday else [])

    @property
    def summary(self):
        if self.error:
            return self.error
        if self.login_state == "active":
            pending = [f.label for f in self.reminder_facts if f.pending is True]
            if pending:
                return "(´･ω･`) 今天登过咯～但还有 " + "、".join(pending) + " 没完成"
            if any(f.pending is None for f in self.reminder_facts):
                return "(´･ω･`) 今天登过咯～部分任务状态暂未获取"
            return "⸜(๑'ᵕ'๑)⸝日常已完成，安心～"
        if self.login_state == "inactive":
            return "｡ﾟ(ﾟ´ω`ﾟ)ﾟ｡今天似乎还没有登录哦～"
        return "是否已上线暂时无法确认"

    def text(self):
        title = f"{GAMES[self.role.game]} · {self.role.name} · UID {self.role.uid}"
        rows = [title, self.summary]
        rows.extend(f"{f.label}：{f.value}" for f in self.daily)
        rows.extend(f"{f.label}：{f.value}" for f in self.weekly)
        return "\n".join(rows)


def progress(label, cur, maximum, *, unlocked=None, remaining=False):
    if unlocked is False:
        return Fact(label, "未解锁")
    cur, maximum = number(cur), number(maximum)
    if cur is None or maximum is None or maximum <= 0:
        return Fact(label, "暂未获取")
    value = f"剩余 {cur}/{maximum} 次" if remaining else f"{cur}/{maximum}"
    return Fact(label, value, cur, maximum)


def energy(note, data, label, current, maximum):
    fact = progress(label, data.get(current), data.get(maximum))
    note.stamina, note.stamina_max = fact.current, fact.maximum
    if fact.maximum and fact.current >= fact.maximum:
        fact.value += " · 已满"
    elif fact.maximum and fact.current * 10 >= fact.maximum * 9:
        fact.value += " · 快满了"
    note.daily.append(fact)


def expeditions(data):
    rows = data.get("expeditions")
    if not isinstance(rows, list):
        return Fact("探索派遣", "暂未获取")
    if not rows:
        return Fact("探索派遣", "暂无派遣")
    valid = [mapping(r) for r in rows]
    if any(r.get("status") not in ("Finished", "Ongoing") for r in valid):
        return Fact("探索派遣", "暂未获取")
    finished = sum(r["status"] == "Finished" for r in valid)
    return Fact(
        "探索派遣", f"{finished}/{len(rows)} 已完成" if finished else f"{len(rows)} 项进行中"
    )


def parse_note(role, data):
    note = Note(role)
    if role.game == "nte":
        energy(note, data, "本性像素", "staminaValue", "staminaMaxValue")
        note.weekly.append(
            progress("都市活力", data.get("citystaminaValue"), data.get("citystaminaMaxValue"))
        )
        active = progress("每日活跃度", data.get("dayvalue"), 100)
        note.progress = active.current
        note.activity = bool(active.current)
        note.daily_complete = bool(active.maximum and active.current >= active.maximum)
        if active.current is not None and active.current < 100:
            active.value += f" · 还差 {100 - active.current}"
        note.daily.append(active)
        weekly_remaining = next(
            (
                number(data.get(key))
                for key in ("weekcopiesremainCnt", "weekCopiesRemainCnt")
                if number(data.get(key)) is not None
            ),
            None,
        )
        note.weekly.append(progress("周本奖励", weekly_remaining, 3, remaining=True))
    elif role.game == "genshin":
        energy(note, data, "树脂", "current_resin", "max_resin")
        tasks = mapping(data.get("daily_task"))
        total = number(tasks.get("total_num", data.get("total_task_num")))
        done = number(tasks.get("finished_num", data.get("finished_task_num")))
        reward = tasks.get(
            "is_extra_task_reward_received", data.get("is_extra_task_reward_received")
        )
        task_rows = sequence(tasks.get("task_rewards"))
        attendance = sequence(tasks.get("attendance_rewards"))
        # A slot may be completed by either a commission or encounter points.
        # Do not add the two arrays, which would count the same slot twice.
        slot_count = max(len(task_rows), len(attendance))
        slots = sum(
            (
                mapping(task_rows[i]).get("status")
                in ("TaskRewardStatusFinished", "TaskRewardStatusTakenAward")
                if i < len(task_rows)
                else False
            )
            or (
                mapping(attendance[i]).get("status") == "AttendanceRewardStatusTakenAward"
                if i < len(attendance)
                else False
            )
            for i in range(slot_count)
        )
        if slot_count and total:
            done = min(total, max(done or 0, slots))
        if reward is True and total:
            done = total
        note.progress = done
        note.activity = reward is True or bool(done)
        note.daily_complete = reward is True
        commissions = progress("每日委托", done, total)
        available = sum(
            mapping(r).get("status") == "AttendanceRewardStatusWaitTaken" for r in attendance
        )
        if reward is True:
            commissions.value += " · 奖励已领取"
            commissions.pending = False
        else:
            if available:
                commissions.value += f" · {available} 项历练点可领取"
            elif reward is False and total and done is not None and done >= total:
                commissions.value += " · 奖励尚未领取"
            elif reward is None:
                commissions.value += " · 领奖状态暂未获取"
            commissions.pending = (
                True
                if available or reward is False or (total and done is not None and done < total)
                else None
            )
        note.daily.append(commissions)
        note.daily.append(expeditions(data))
        active = mapping(data.get("week_active_progress"))
        if active.get("unlock") is False:
            weekly_activity = Fact("砺行修远", "未解锁")
        elif active.get("is_active_period") is False:
            weekly_activity = Fact("砺行修远", "本期未开放")
        else:
            weekly_activity = progress(
                "砺行修远", active.get("progress_current"), active.get("progress_total")
            )
            weekly_activity.pending = (
                weekly_activity.current < weekly_activity.maximum
                if weekly_activity.maximum and weekly_activity.current is not None
                else None
            )
            period_current = number(active.get("period_progress_current"))
            period_total = number(active.get("period_progress_total"))
            if period_current is not None and period_total:
                weekly_activity.value += f" · 本期 {period_current}/{period_total} 周"
        note.weekly.append(weekly_activity)
        note.weekly.append(
            progress(
                "周本树脂减半",
                data.get("remain_resin_discount_num"),
                data.get("resin_discount_num_limit"),
                remaining=True,
            )
        )
    elif role.game == "starrail":
        energy(note, data, "开拓力", "current_stamina", "max_stamina")
        training = progress(
            "每日实训", data.get("current_train_score"), data.get("max_train_score")
        )
        note.progress = training.current
        note.activity = bool(training.current)
        note.daily_complete = bool(training.maximum and training.current >= training.maximum)
        if training.maximum and training.current < training.maximum:
            training.value += f" · 还差 {training.maximum - training.current}"
        note.daily.extend([training, expeditions(data)])
        note.weekly.extend(
            [
                progress(
                    "模拟宇宙积分", data.get("current_rogue_score"), data.get("max_rogue_score")
                ),
                progress(
                    "差分宇宙积分",
                    data.get("rogue_tourn_weekly_cur"),
                    data.get("rogue_tourn_weekly_max"),
                    unlocked=data.get("rogue_tourn_weekly_unlocked"),
                ),
                progress(
                    "历战余响奖励",
                    data.get("weekly_cocoon_cnt"),
                    data.get("weekly_cocoon_limit"),
                    remaining=True,
                ),
            ]
        )
    elif role.game == "zzz":
        battery = mapping(data.get("energy"))
        energy(note, mapping(battery.get("progress")) or battery, "电量", "current", "max")
        vitality = mapping(data.get("vitality"))
        daily = progress("每日活跃度", vitality.get("current"), vitality.get("max"))
        note.progress = daily.current
        scratch = data.get("card_sign")
        store = mapping(data.get("vhs_sale")).get("sale_state", data.get("video_store_state"))
        note.activity = bool(daily.current) or scratch == "CardSignDone"
        note.daily_complete = bool(
            daily.maximum
            and daily.current >= daily.maximum
            and scratch == "CardSignDone"
            and store == "SaleStateDoing"
        )
        note.daily.extend(
            [
                daily,
                Fact(
                    "刮刮卡",
                    {
                        "CardSignDone": "已刮",
                        "CardSignNo": "还没刮",
                        "CardSignNotDone": "还没刮",
                    }.get(scratch, "暂未获取"),
                ),
                Fact(
                    "录像店",
                    {
                        "SaleStateNo": "尚未营业",
                        "SaleStateDoing": "营业中",
                        "SaleStateDone": "收益可领取",
                    }.get(store, "暂未获取"),
                ),
            ]
        )
        weekly = mapping(data.get("weekly_task"))
        hollow = mapping(data.get("hollow_zero"))
        bounty = next(
            (
                mapping(data.get(key))
                for key in ("s2_bounty_commission", "s1_bounty_commission", "bounty_commission")
                if mapping(data.get(key))
            ),
            mapping(hollow.get("bounty_commission")),
        )
        note.weekly.extend(
            [
                progress(
                    "周常进度",
                    weekly.get("cur_point"),
                    weekly.get("max_point"),
                    unlocked=weekly.get("unlock"),
                ),
                progress(
                    "零号空洞悬赏",
                    bounty.get("num"),
                    bounty.get("total"),
                    unlocked=bounty.get("unlock"),
                ),
            ]
        )
    # Only task progress counts as a to-do. Energy pools and weekly bosses
    # remain informational, including NTE's city stamina in the weekly area.
    progress_tasks = {
        "每日实训",
        "每日活跃度",
        "模拟宇宙积分",
        "差分宇宙积分",
        "周常进度",
        "零号空洞悬赏",
    }
    for fact in note.daily + note.weekly:
        if fact.label in progress_tasks:
            if fact.value == "未解锁":
                fact.pending = False
            elif fact.current is None or not fact.maximum:
                fact.pending = None
            else:
                fact.pending = fact.current < fact.maximum
        elif fact.label in {"刮刮卡", "录像店", "探索派遣"}:
            if fact.value == "暂未获取":
                fact.pending = None
            else:
                fact.pending = fact.value in {"尚未领取", "还没刮", "尚未营业", "收益可领取"}
                if fact.label == "探索派遣":
                    fact.pending = "已完成" in fact.value
    return note
