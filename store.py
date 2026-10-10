"""Small, transactional SQLite store, separate from all other plugins."""

import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .models import CN, GAMES, Role, game_day


def private_origin(origin, owner=None):
    """AstrBot 4 session format; unknown/legacy non-private destinations fail closed."""
    parts = origin.split(":", 2)
    if len(parts) != 3 or parts[1] != "FriendMessage" or not parts[0] or not parts[2]:
        return False
    if owner is None:
        return True
    try:
        return json.loads(owner) == [parts[0], parts[2]]
    except (ValueError, TypeError):
        return False


class Store:
    def __init__(self, path, *, private_only=False):
        self.private_only = private_only
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(
                owner TEXT REFERENCES accounts(owner) ON DELETE CASCADE,
                origin TEXT, mention TEXT NOT NULL, clock TEXT NOT NULL, start_day TEXT NOT NULL,
                last_sent TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(owner,origin));
            CREATE TABLE IF NOT EXISTS observations(
                owner TEXT REFERENCES accounts(owner) ON DELETE CASCADE,
                game TEXT, uid TEXT, day TEXT, stamina INTEGER, seen INTEGER, stamp REAL,
                PRIMARY KEY(owner,game));
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(subscriptions)")}
        if "games" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN games TEXT NOT NULL DEFAULT '[]'"
                )
                for row in self.db.execute("SELECT owner,origin FROM subscriptions").fetchall():
                    try:
                        games = [r.game for r in self.selected_roles(row["owner"])]
                    except ValueError:
                        games = []
                    self.db.execute(
                        "UPDATE subscriptions SET games=? WHERE owner=? AND origin=?",
                        (json.dumps(games), row["owner"], row["origin"]),
                    )

    def close(self):
        self.db.close()

    def account(self, owner):
        row = self.db.execute("SELECT payload FROM accounts WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_account(self, owner, account, *, replace=False):
        old = self.account(owner)
        changed = old and old.get("aid") and old["aid"] != account["aid"]
        if changed and not replace:
            raise ValueError("需要确认更换米游社账号。")
        selected = {}
        for game in GAMES:
            choices = [r for r in account["roles"] if r["game"] == game]
            previous = old.get("selected", {}).get(game) if old and not changed else None
            if previous and any(r["uid"] == previous for r in choices):
                selected[game] = previous
            elif len(choices) == 1:
                selected[game] = choices[0]["uid"]
        account = {**account, "selected": selected}
        if old and old.get("nte"):
            account["nte"] = old["nte"]
        with self.db:
            if changed:
                self.db.execute("DELETE FROM subscriptions WHERE owner=?", (owner,))
            self.db.execute(
                "INSERT INTO accounts VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (owner, json.dumps(account)),
            )
            # Login refresh may change the automatic selection; clear stale observations.
            self.db.execute("DELETE FROM observations WHERE owner=?", (owner,))

    def update_cookie(self, owner, cookie):
        account = self.account(owner)
        if account:
            account["cookie"] = cookie
            with self.db:
                self.db.execute(
                    "UPDATE accounts SET payload=? WHERE owner=?", (json.dumps(account), owner)
                )

    def unbind(self, owner):
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE owner=?", (owner,))

    def nte_account(self, owner):
        return (self.account(owner) or {}).get("nte")

    def unbind_mys(self, owner):
        account = self.account(owner)
        if not account:
            return
        if not account.get("nte"):
            self.unbind(owner)
            return
        shell = self.login_account("", "", "", "", [])
        shell["selected"] = {}
        shell["nte"] = account["nte"]
        with self.db:
            self.db.execute(
                "UPDATE accounts SET payload=? WHERE owner=?", (json.dumps(shell), owner)
            )
            self.db.execute("DELETE FROM observations WHERE owner=? AND game!='nte'", (owner,))
            for row in self.db.execute(
                "SELECT origin,games FROM subscriptions WHERE owner=?", (owner,)
            ).fetchall():
                self.set_subscription_games(
                    owner, row["origin"], [g for g in json.loads(row["games"]) if g == "nte"]
                )

    def set_subscription_games(self, owner, origin, games):
        """Removal preserves schedule and delivery history for the remaining games."""
        with self.db:
            if games:
                self.db.execute(
                    "UPDATE subscriptions SET games=? WHERE owner=? AND origin=?",
                    (json.dumps(games), owner, origin),
                )
            else:
                self.db.execute(
                    "DELETE FROM subscriptions WHERE owner=? AND origin=?", (owner, origin)
                )

    def save_nte(self, owner, nte, *, replace=False):
        account = self.account(owner) or self.login_account("", "", "", "", [])
        account.setdefault("selected", {})
        old = account.get("nte")
        changed = old and old["aid"] != nte["aid"]
        if changed and not replace:
            raise ValueError("需要确认更换塔吉多账号。")
        selected = old.get("selected", {}) if old and not changed else {}
        choices = nte["roles"]
        uid = selected.get("nte")
        if uid and any(r["uid"] == uid for r in choices):
            selection = {"nte": uid}
        else:
            selection = {"nte": choices[0]["uid"]} if len(choices) == 1 else {}
        account["nte"] = {**nte, "selected": selection}
        with self.db:
            self.db.execute(
                "INSERT INTO accounts VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (owner, json.dumps(account)),
            )
            if changed:
                self.db.execute("DELETE FROM subscriptions WHERE owner=?", (owner,))
            self.db.execute("DELETE FROM observations WHERE owner=? AND game='nte'", (owner,))

    def update_nte_session(self, owner, session):
        account = self.account(owner)
        if account and account.get("nte"):
            account["nte"].update(session)
            with self.db:
                self.db.execute(
                    "UPDATE accounts SET payload=? WHERE owner=?", (json.dumps(account), owner)
                )

    def unbind_nte(self, owner):
        account = self.account(owner)
        if not account:
            return
        if not account.get("aid"):
            self.unbind(owner)
            return
        account.pop("nte", None)
        with self.db:
            self.db.execute(
                "UPDATE accounts SET payload=? WHERE owner=?", (json.dumps(account), owner)
            )
            self.db.execute("DELETE FROM observations WHERE owner=? AND game='nte'", (owner,))
            for row in self.db.execute(
                "SELECT origin,games FROM subscriptions WHERE owner=?", (owner,)
            ).fetchall():
                games = [g for g in json.loads(row["games"]) if g != "nte"]
                if games:
                    self.db.execute(
                        "UPDATE subscriptions SET games=? WHERE owner=? AND origin=?",
                        (json.dumps(games), owner, row["origin"]),
                    )
                else:
                    self.db.execute(
                        "DELETE FROM subscriptions WHERE owner=? AND origin=?",
                        (owner, row["origin"]),
                    )

    def selected_roles(self, owner, game=""):
        account = self.account(owner)
        if not account:
            raise ValueError(
                "还没有绑定账号，请私聊 /备忘姬 米游社扫码登录 或 /备忘姬 塔吉多短信登录。"
            )
        roles = [
            Role(**r)
            for r in account["roles"]
            if account["selected"].get(r["game"]) == r["uid"] and (not game or r["game"] == game)
        ]
        nte = account.get("nte", {})
        roles.extend(
            Role(**r)
            for r in nte.get("roles", [])
            if nte.get("selected", {}).get("nte") == r["uid"] and game in ("", "nte")
        )
        if not roles:
            raise ValueError("还没有选好角色，请私聊 /备忘姬 账号 查看。")
        return roles

    def select(self, owner, game, uid):
        account = self.account(owner)
        provider = (account or {}).get("nte") if game == "nte" else account
        if not provider or not any(
            r["game"] == game and r["uid"] == uid for r in provider["roles"]
        ):
            raise ValueError("只能选择自己已绑定账号中的角色。")
        provider["selected"][game] = uid
        with self.db:
            self.db.execute(
                "UPDATE accounts SET payload=? WHERE owner=?", (json.dumps(account), owner)
            )
            self.db.execute("DELETE FROM observations WHERE owner=? AND game=?", (owner, game))

    def subscribe(self, owner, origin, mention, clock, now, games=None):
        if self.private_only and (mention or not private_origin(origin, owner)):
            raise ValueError("每日提醒只支持本人私聊，请私聊机器人设置。")
        available = {r.game for r in self.selected_roles(owner)}
        games = list(dict.fromkeys(games if games is not None else available))
        if not games or any(game not in available for game in games):
            raise ValueError("只能选择已绑定且已选好角色的游戏，请先私聊 /备忘姬 账号 查看。")
        games = [g for g in GAMES if g in games]
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
            raise ValueError("时间请写成 HH:MM，例如 /备忘姬 订阅推送 23:00 原神 星铁。")
        old = self.subscription(owner, origin)
        count = sum(
            not self.private_only or private_origin(row["origin"], owner)
            for row in self.db.execute("SELECT origin FROM subscriptions WHERE owner=?", (owner,))
        )
        if not old and count >= 10:
            raise ValueError("最多设置 10 个会话，请先关闭不再使用的提醒。")
        local = datetime.fromtimestamp(now, CN)
        hour, minute = map(int, clock.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        start = (local.date() + timedelta(days=int(scheduled.timestamp() <= now))).isoformat()
        with self.db:
            self.db.execute(
                """INSERT INTO subscriptions(owner,origin,mention,clock,start_day,games) VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner,origin) DO UPDATE SET mention=excluded.mention,clock=excluded.clock,
                start_day=excluded.start_day,games=excluded.games,next_attempt=0""",
                (owner, origin, mention, clock, start, json.dumps(games)),
            )
        return start

    def subscription(self, owner, origin):
        if self.private_only and not private_origin(origin, owner):
            return None
        row = self.db.execute(
            "SELECT * FROM subscriptions WHERE owner=? AND origin=?", (owner, origin)
        ).fetchone()
        return {**dict(row), "games": json.loads(row["games"])} if row else None

    def unsubscribe(self, owner, origin):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE owner=? AND origin=?", (owner, origin))

    def reminder_counts(self, owner):
        """Count this user's subscribed sessions per game, never other users'."""
        counts = {}
        for row in self.db.execute("SELECT * FROM subscriptions WHERE owner=?", (owner,)):
            if self.private_only and (row["mention"] or not private_origin(row["origin"], owner)):
                continue
            for game in set(json.loads(row["games"])):
                if game in GAMES:
                    counts[game] = counts.get(game, 0) + 1
        return counts

    def due(self, now):
        local = datetime.fromtimestamp(now, CN)
        # Include yesterday for a 23:59 reminder retried just after midnight.
        rows = self.db.execute(
            "SELECT * FROM subscriptions WHERE next_attempt<=?", (now,)
        ).fetchall()
        result = []
        for row in rows:
            if self.private_only and (
                row["mention"] or not private_origin(row["origin"], row["owner"])
            ):
                continue
            for date in (local.date() - timedelta(days=1), local.date()):
                day = date.isoformat()
                hour, minute = map(int, row["clock"].split(":"))
                due = (
                    datetime.combine(date, datetime.min.time(), CN)
                    .replace(hour=hour, minute=minute)
                    .timestamp()
                )
                if row["start_day"] <= day and row["last_sent"] < day and 0 <= now - due <= 1800:
                    result.append(
                        {**dict(row), "games": json.loads(row["games"]), "day": day, "due": due}
                    )
        return result

    def delivered(self, row):
        with self.db:
            self.db.execute(
                "UPDATE subscriptions SET last_sent=?,next_attempt=0 WHERE owner=? AND origin=?",
                (row["day"], row["owner"], row["origin"]),
            )

    def retry_send(self, row, now):
        with self.db:
            self.db.execute(
                "UPDATE subscriptions SET next_attempt=? WHERE owner=? AND origin=?",
                (now + 60, row["owner"], row["origin"]),
            )

    def observe(self, owner, note, now):
        if note.error:
            return
        if note.role.game == "nte":
            # The upstream does not document the reset hour. Use the API's
            # current daily activity, never cumulative login days or a guessed boundary.
            note.seen = note.activity
            return
        old = self.db.execute(
            "SELECT * FROM observations WHERE owner=? AND game=?", (owner, note.role.game)
        ).fetchone()
        day = game_day(now)
        same = old and old["uid"] == note.role.uid and old["day"] == day
        consumed = (
            same
            and old["stamina"] is not None
            and note.stamina is not None
            and note.stamina < old["stamina"]
        )
        note.seen = bool(note.activity or consumed or (same and old["seen"]))
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?)",
                (owner, note.role.game, note.role.uid, day, note.stamina, int(note.seen), now),
            )

    @staticmethod
    def login_account(aid, cookie, device, stoken, roles):
        return {
            "aid": aid,
            "cookie": cookie,
            "device_id": device,
            "stoken": stoken,
            "roles": [asdict(r) for r in roles],
        }
