"""Private-chat login state. Phone numbers and SMS codes are never persisted."""

import re
import time

from .nte import device_id

GUIDE = """异环需要单独绑定塔吉多账号。
私聊 /备忘姬 塔吉多短信登录 手机号 — 发送登录短信（中国大陆号码）
私聊 /备忘姬 塔吉多验证码 验证码 — 完成登录
也可私聊 /备忘姬 塔吉多AccessToken登录 accessToken 直接绑定。
短信登录会保存续期凭据；单独的 accessToken 失效后需要重新提交。
绑定后请发送 /备忘姬 订阅推送 23:00 异环 设置推送。"""


class NteAuth:
    def __init__(self, client, service, clock=time.time):
        self.client, self.service, self.clock = client, service, clock
        self.sms, self.cooldown = {}, {}

    def purge(self):
        now = self.clock()
        self.sms = {k: v for k, v in self.sms.items() if v["expires"] > now}
        self.cooldown = {k: v for k, v in self.cooldown.items() if v > now}

    def cancel(self, owner):
        self.sms.pop(owner, None)

    def close(self):
        self.sms.clear()
        self.cooldown.clear()

    async def send_code(self, owner, phone):
        if not phone:
            return GUIDE
        if not re.fullmatch(r"1[3-9][0-9]{9}", phone):
            raise ValueError("请填写中国大陆手机号，例如：/备忘姬 塔吉多短信登录 手机号。")
        self.purge()
        now = self.clock()
        if (
            self.cooldown.get(("owner", owner), 0) > now
            or self.cooldown.get(("phone", phone), 0) > now
        ):
            return "短信发送间隔至少 60 秒，请稍后再试。"
        if len(self.sms) >= 20 or len(self.cooldown) >= 100:
            return "当前登录人数较多，请稍后再试。"
        # Reserve cooldown before I/O, including uncertain SMS-send outcomes.
        self.cooldown[("owner", owner)] = self.cooldown[("phone", phone)] = now + 60
        self.cancel(owner)
        device = device_id()
        await self.client.send_sms(phone, device)
        self.sms[owner] = {"phone": phone, "device": device, "expires": now + 300, "attempts": 0}
        return "短信已发送，请在 5 分钟内私聊 /备忘姬 塔吉多验证码 验证码。"

    async def verify(self, owner, code):
        self.purge()
        state = self.sms.get(owner)
        if not state:
            return "没有等待验证的短信，或已超时，请先发送 /备忘姬 塔吉多短信登录 手机号。"
        if not re.fullmatch(r"[0-9]{4,8}", code):
            raise ValueError("验证码应为 4 至 8 位数字。")
        state["attempts"] += 1
        if state["attempts"] >= 5:
            self.sms.pop(owner, None)
        account = await self.client.sms_login(state["phone"], code, state["device"])
        self.sms.pop(owner, None)
        return await self.commit(owner, account)

    async def access(self, owner, value):
        self.cancel(owner)
        account = await self.client.access_login(value)
        return await self.commit(owner, account)

    async def commit(self, owner, account):
        async with self.service.locked(owner):
            self.purge()
            old = self.service.store.nte_account(owner)
            changed = old and old["aid"] != account["aid"]
            self.service.store.save_nte(owner, account, replace=True)
            self.service.invalidate(owner)
            message = "塔吉多账号已更换，旧订阅已清除，请重新设置。\n" if changed else ""
            return message + self.success(owner)

    def success(self, owner):
        account = self.service.store.nte_account(owner)
        lines = ["异环绑定成功。"]
        lines.extend(f"{r['name']} · UID {r['uid']}" for r in account["roles"])
        if not account["selected"]:
            lines.append("有多个角色，请发送 /备忘姬 角色 异环 UID 选择。")
        lines.append("订阅异环：/备忘姬 订阅推送 23:00 异环\n查询订阅：/备忘姬 今日日常")
        return "\n".join(lines)
