"""Minute scheduler with non-blocking retries and per-user serialization."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .api import ApiError
from .models import Note


@dataclass
class Batch:
    started: float
    notes: list
    attempts: int = 0
    next_attempt: float = 0


class Service:
    def __init__(self, store, client, login_api, send, logger, clock=time.time, nte_client=None):
        self.store, self.client, self.login_api = store, client, login_api
        self.send, self.logger, self.clock = send, logger, clock
        self.locks, self.batches, self.cache = {}, {}, {}
        self.nte_client = nte_client

    async def fetch_nte(self, owner, role):
        account = self.store.nte_account(owner)
        if not account or self.nte_client is None:
            raise ApiError("请先私聊 /备忘姬 塔吉多短信登录", "nte_auth", auth=True)
        renewed = False
        if account.get("refresh_token") and self.clock() - account.get("updated_at", 0) >= 3600:
            session = await self.nte_client.renew(account)
            account.update(session)
            self.store.update_nte_session(owner, session)
            renewed = True
        try:
            return await self.nte_client.fetch(account, role)
        except ApiError as exc:
            if (
                not exc.auth
                or renewed
                or not (account.get("refresh_token") or account.get("laohu_token"))
            ):
                raise
            session = await self.nte_client.renew(account)
            account.update(session)
            self.store.update_nte_session(owner, session)
            return await self.nte_client.fetch(account, role)

    @asynccontextmanager
    async def locked(self, owner):
        # Reference counting includes waiters, so an unlocked owner can be evicted.
        entry = self.locks.setdefault(owner, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.locks.pop(owner, None)

    def invalidate(self, owner):
        self.batches.pop(owner, None)
        self.cache.pop(owner, None)

    async def fetch(self, owner, roles):
        account = self.store.account(owner)
        notes, refreshed, auth_error = [], False, None
        for role in roles:
            try:
                if auth_error and role.game != "nte":
                    raise auth_error
                try:
                    note = (
                        await self.fetch_nte(owner, role)
                        if role.game == "nte"
                        else await self.client.fetch(account, role)
                    )
                except ApiError as exc:
                    if role.game == "nte" or not exc.auth or refreshed or not account.get("stoken"):
                        raise
                    refreshed = True
                    try:
                        cookie = await self.login_api.refresh_cookie(
                            account["stoken"], account["device_id"]
                        )
                    except ApiError:
                        raise exc from None
                    account["cookie"] = cookie
                    self.store.update_cookie(owner, cookie)
                    note = await self.client.fetch(account, role)
            except ApiError as exc:
                if exc.auth and role.game != "nte":
                    auth_error = exc
                note = Note(role, error=str(exc), error_code=exc.code, transient=exc.transient)
            except Exception as exc:
                # Payloads and credentials must never enter logs.
                self.logger.warning(f"[二游日常备忘姬] 查询失败：{type(exc).__name__}")
                note = Note(role, error="查询暂时失败，请稍后再试", error_code="internal")
            note.observed_at = self.clock()
            self.store.observe(owner, note, note.observed_at)
            notes.append(note)
        return notes

    async def query(self, owner, game="", *, games=None):
        async with self.locked(owner):
            roles = self.store.selected_roles(owner, game)
            if games is not None:
                roles = [r for r in roles if r.game in games]
                if not roles:
                    raise ValueError("还没有可查询的订阅角色，请先查看订阅状态。")
            now = self.clock()
            cached = self.cache.get(owner)
            fresh = {n.role: n for n in cached.notes if now - n.observed_at < 30} if cached else {}
            missing = [r for r in roles if r not in fresh]
            if missing:
                fresh.update({n.role: n for n in await self.fetch(owner, missing)})
            self.cache[owner] = Batch(self.clock(), list(fresh.values()))
            return [fresh[r] for r in roles]

    async def tick(self):
        now = self.clock()
        grouped = {}
        for row in self.store.due(now):
            grouped.setdefault(row["owner"], []).append(row)
        for owner, rows in grouped.items():
            async with self.locked(owner):
                # Re-read after acquiring the lock: unbind/close may have run.
                rows = [r for r in self.store.due(self.clock()) if r["owner"] == owner]
                if not rows:
                    continue
                try:
                    games = {g for row in rows for g in row["games"]}
                    roles = [r for r in self.store.selected_roles(owner) if r.game in games]
                except ValueError:
                    # A fresh login can leave several roles awaiting selection.
                    # Do not let that user's old subscriptions block everyone else.
                    continue
                batch = self.batches.get(owner)
                if not roles:
                    continue
                if (
                    batch is None
                    or batch.started < max(r["due"] for r in rows)
                    or not set(roles).issubset({n.role for n in batch.notes})
                ):
                    batch = Batch(self.clock(), await self.fetch(owner, roles))
                    batch.next_attempt = self.clock() + 60
                    self.batches[owner] = batch
                elif (
                    any(n.transient for n in batch.notes)
                    and batch.attempts < 2
                    and self.clock() >= batch.next_attempt
                ):
                    retried = await self.fetch(
                        owner, [n.role for n in batch.notes if n.transient and n.role in roles]
                    )
                    fresh = {n.role: n for n in retried}
                    batch.notes = [fresh.get(n.role, n) for n in batch.notes]
                    batch.attempts += 1
                    batch.next_attempt = self.clock() + 300
                self.cache[owner] = Batch(self.clock(), batch.notes)
                for row in rows:
                    selected = [n for n in batch.notes if n.role.game in row["games"]]
                    if not selected or (any(n.transient for n in selected) and batch.attempts < 2):
                        continue
                    if self.clock() - row["due"] > 1800:
                        continue
                    try:
                        async with asyncio.timeout(30):
                            sent = await self.send(row, selected)
                    except Exception as exc:
                        self.logger.warning(f"[二游日常备忘姬] 发送失败：{type(exc).__name__}")
                        sent = False
                    if sent is not False:
                        self.store.delivered(row)
                    else:
                        self.store.retry_send(row, self.clock())
            await asyncio.sleep(0)
        # No permanent accumulation of cached accounts or image data.
        self.batches = {k: v for k, v in self.batches.items() if now - v.started <= 1800}
        self.cache = {k: v for k, v in self.cache.items() if now - v.started <= 30}

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.logger.warning(f"[二游日常备忘姬] 本轮检查失败：{type(exc).__name__}")
            await asyncio.sleep(60)
