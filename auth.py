"""Short-lived QR sessions; successful login replaces the platform account."""

import asyncio
import time

from .api import MYS_DEVICE_ID, ApiError, account_id, normalize_cookie
from .login_api import render_qr
from .models import GAMES
from .store import Store


def account_text(account):
    lines = [f"米游社账号 {account['aid']}"] if account.get("aid") else []
    for game, name in GAMES.items():
        roles = [r for r in account["roles"] if r["game"] == game]
        for role in roles:
            selected = account["selected"].get(game) == role["uid"]
            lines.append(
                f"{name} · {role['name']} · {role['uid']}" + ("（已选）" if selected else "")
            )
        if len(roles) > 1 and not account["selected"].get(game):
            lines.append(f"请选择：/备忘姬 角色 {name} UID")
    nte = account.get("nte")
    if nte:
        lines.append(f"塔吉多账号 {nte['aid']}")
        for role in nte["roles"]:
            chosen = nte.get("selected", {}).get("nte") == role["uid"]
            lines.append(f"异环 · {role['name']} · {role['uid']}" + ("（已选）" if chosen else ""))
        if not nte.get("selected"):
            lines.append("请选择：/备忘姬 角色 异环 UID")
    lines.append(
        "订阅推送：/备忘姬 订阅推送 23:00 原神 星铁；或 /备忘姬 订阅推送 23:00 全部\n查询订阅：/备忘姬 今日日常"
    )
    return "\n".join(lines)


class Auth:
    def __init__(self, api, client, service, send, logger, clock=time.time):
        self.api, self.client, self.service = api, client, service
        self.send, self.logger, self.clock = send, logger, clock
        self.tasks = {}
        self.closed = False

    async def commit(self, owner, account):
        async with self.service.locked(owner):
            old = self.service.store.account(owner)
            changed = old and old.get("aid") and old["aid"] != account["aid"]
            self.service.store.save_account(owner, account, replace=True)
            self.service.invalidate(owner)
            message = "米游社账号已更换，旧订阅已清除，请重新设置。" if changed else "绑定成功。"
            return message + "\n" + account_text(self.service.store.account(owner))

    async def manual(self, owner, value):
        # Cancel any QR session first; it must not overwrite the new manual login.
        await self.cancel(owner)
        cookie = normalize_cookie(value)
        device = MYS_DEVICE_ID
        account = Store.login_account(account_id(cookie), cookie, device, "", [])
        roles = await self.client.roles(account)
        account = Store.login_account(account["aid"], cookie, device, "", roles)
        return await self.commit(owner, account)

    async def start(self, owner, origin):
        if self.closed:
            return "插件正在关闭，请稍后重试。"
        await self.cancel(owner)
        if len(self.tasks) >= 3:
            return "当前扫码人数较多，请稍后重试。"
        task = asyncio.create_task(self.run(owner, origin), name="daily-reminder-login")
        self.tasks[owner] = task
        task.add_done_callback(
            lambda done: self.tasks.pop(owner, None) if self.tasks.get(owner) is done else None
        )
        return "正在生成二维码，请用米游社 App 扫码。"

    async def cancel(self, owner):
        task = self.tasks.pop(owner, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def report(self, origin, text, png=None):
        async with asyncio.timeout(20):
            if await self.send(origin, text, png) is False:
                raise ApiError("登录消息发送失败，请检查机器人连接", "send")

    async def run(self, owner, origin):
        try:
            async with asyncio.timeout(180):
                code = await self.api.create()
                await self.report(
                    origin,
                    "用米游社 App 扫码并确认，二维码 3 分钟内有效。确认后将绑定账号，用于查询原神、星铁和绝区零日常。请勿转发二维码。",
                    render_qr(code["url"]),
                )
                scanned = False
                while True:
                    await asyncio.sleep(2)
                    data = await self.api.status(code)
                    if data.get("status") == "Confirmed":
                        break
                    if data.get("status") == "Scanned":
                        if not scanned:
                            await self.report(origin, "已扫码，请在米游社 App 确认。")
                            scanned = True
                    elif data.get("status") != "Created":
                        raise ApiError("二维码已失效，请重新发送 /备忘姬 米游社扫码登录", "expired")
                aid, cookie, stoken = await self.api.credentials(data, code["device_id"])
                account = Store.login_account(aid, cookie, code["device_id"], stoken, [])
                roles = await self.client.roles(account)
                account = Store.login_account(aid, cookie, code["device_id"], stoken, roles)
                message = await self.commit(owner, account)
                await self.report(origin, message)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                message = "扫码已超时。若已确认，请用 /备忘姬 账号 查看；否则重新发送 /备忘姬 米游社扫码登录。"
            elif isinstance(exc, (ApiError, ValueError)):
                message = str(exc)
            else:
                message = "扫码暂时失败，请稍后重新发送 /备忘姬 米游社扫码登录。"
            self.logger.warning(f"[二游日常备忘姬] 登录失败：{type(exc).__name__}")
            try:
                await self.report(origin, message)
            except Exception:
                self.logger.warning("[二游日常备忘姬] 登录通知未能送达")

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
