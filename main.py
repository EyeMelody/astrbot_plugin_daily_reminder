"""AstrBot commands and transport for the daily reminder plugin."""

import asyncio
import json
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .api import ApiError, Client
from .auth import Auth, account_text
from .login_api import LoginApi
from .models import ALIASES, GAMES
from .nte import NteClient
from .nte_auth import NteAuth
from .render import Renderer, text_summary
from .service import Service
from .store import Store, private_origin
from .ui import help_text, status_text

PRIVATE = {
    "米游社扫码登录",
    "米游社Cookie登录",
    "账号",
    "角色",
    "解绑",
}
PRIVATE.update(
    {
        "塔吉多短信登录",
        "塔吉多验证码",
        "塔吉多AccessToken登录",
        "塔吉多解绑",
    }
)
PRIVATE.update({"帮助", "查询", "提醒"})
HELP = help_text()


@register(
    "astrbot_plugin_daily_reminder",
    "EyeMelody",
    "原神、星铁、绝区零、异环日常查询与定时提醒。",
    "0.5.6",
)
class DailyReminder(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = self.session = self.service = self.auth = self.renderer = self.task = None
        self.nte_auth = None
        self.closing = False
        self.inflight = set()

    async def initialize(self):
        """Open plugin-owned resources and start one reminder loop."""
        if self.task:
            return
        self.closing = False
        try:
            path = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "astrbot_plugin_daily_reminder"
                / "daily.sqlite3"
            )
            self.store = Store(path, private_only=True)
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=3),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
            client, login = Client(self.session), LoginApi(self.session)
            self.renderer = Renderer()
            nte = NteClient(self.session)
            self.service = Service(self.store, client, login, self.push, logger, nte_client=nte)
            self.auth = Auth(login, client, self.service, self.login_message, logger)
            self.nte_auth = NteAuth(nte, self.service)
            self.task = asyncio.create_task(self.run(), name="daily-reminder-scheduler")
        except BaseException:
            await self.terminate()
            raise

    async def run(self):
        while True:
            try:
                self.nte_auth.purge()
                await self.service.tick()
            except Exception as exc:
                logger.warning(f"[二游日常备忘姬] 定时检查失败：{type(exc).__name__}")
            await asyncio.sleep(60)

    async def login_message(self, origin, text, png=None):
        if not private_origin(origin):
            raise ValueError("仅支持私聊发送。")
        chain = MessageChain().message(text)
        if png:
            chain.chain.append(Comp.Image.fromBytes(png))
        return await self.context.send_message(origin, chain)

    async def components(self, notes, mention=""):
        parts = [Comp.At(qq=mention)] if mention else []
        try:
            parts.append(Comp.Image.fromBytes(await self.renderer.render(notes)))
        except Exception as exc:
            logger.warning(f"[二游日常备忘姬] 图片生成失败，改发文字：{type(exc).__name__}")
            parts.append(Comp.Plain(text_summary(notes)))
        return parts

    async def push(self, row, notes):
        if row["mention"] or not private_origin(row["origin"], row["owner"]):
            raise ValueError("每日提醒仅支持本人私聊。")
        chain = MessageChain()
        chain.chain = await self.components(notes, row["mention"])
        return await self.context.send_message(row["origin"], chain)

    @staticmethod
    def owner(event):
        return json.dumps([event.get_platform_id(), str(event.get_sender_id())])

    async def subscription_result(self, event, owner, origin):
        account, row = self.store.account(owner), self.store.subscription(owner, origin)
        try:
            image = await self.renderer.status(account, row)
            return event.chain_result([Comp.Image.fromBytes(image)])
        except Exception as exc:
            logger.warning(f"[二游日常备忘姬] 订阅图片生成失败：{type(exc).__name__}")
            return event.plain_result(status_text(account, row))

    @staticmethod
    def parse_games(value, available):
        names = value.replace("，", " ").replace(",", " ").split()
        if names == ["全部"]:
            return list(available)
        games = [ALIASES.get(name) for name in names]
        if not games or any(g is None for g in games):
            raise ValueError("请填写游戏，可多选：原神、星铁、绝区零、异环，或填写“全部”。")
        return list(dict.fromkeys(games))

    async def execute(self, event, action, value=""):
        event.stop_event()
        if event.get_platform_name() != "aiocqhttp":
            return event.plain_result("目前支持 QQ NapCat / OneBot。")
        if not event.is_private_chat():
            return event.plain_result("二游日常备忘姬仅支持私聊，请私聊机器人使用 /备忘姬 帮助。")
        if self.closing or self.service is None:
            return event.plain_result("插件正在初始化或关闭，请稍后重试。")
        owner, origin = self.owner(event), event.unified_msg_origin
        task = asyncio.current_task()
        self.inflight.add(task)
        try:
            async with self.service.locked("command:" + owner):
                if action == "帮助":
                    try:
                        return event.chain_result(
                            [Comp.Image.fromBytes(await self.renderer.help())]
                        )
                    except Exception as exc:
                        logger.warning(f"[二游日常备忘姬] 帮助图片生成失败：{type(exc).__name__}")
                        result = HELP
                elif action in ("获取米游社Cookie", "获取塔吉多AccessToken"):
                    async with self.service.locked(owner):
                        account = self.store.account(owner) or {}
                        secret = (
                            account.get("cookie")
                            if action == "获取米游社Cookie"
                            else account.get("nte", {}).get("access_token")
                        )
                    result = (
                        ("仅供本人使用，请勿转发。\n" + secret)
                        if secret
                        else "尚未登录对应账号，或未保存该凭据。"
                    )
                elif action == "米游社登出":
                    await self.auth.cancel(owner)
                    async with self.service.locked(owner):
                        self.store.unbind_mys(owner)
                        self.service.invalidate(owner)
                    result = "已删除米游社凭据、角色及对应订阅。塔吉多账号和异环订阅保留。"
                elif action == "订阅状态":
                    return await self.subscription_result(event, owner, origin)
                elif action == "今日日常":
                    row = self.store.subscription(owner, origin)
                    if not row:
                        return await self.subscription_result(event, owner, origin)
                    notes = await self.service.query(owner, games=row["games"])
                    return event.chain_result(await self.components(notes))
                elif action in ("订阅推送", "取消推送", "取消全部推送"):
                    async with self.service.locked(owner):
                        row = self.store.subscription(owner, origin)
                        existing = row["games"] if row else []
                        if action == "订阅推送":
                            args = value.split(maxsplit=1)
                            if len(args) != 2:
                                raise ValueError("例如：/备忘姬 订阅推送 23:00 原神 星铁")
                            available = [r.game for r in self.store.selected_roles(owner)]
                            added = self.parse_games(args[1], available)
                            games = list(dict.fromkeys(existing + added))
                            self.store.subscribe(
                                owner, origin, "", args[0], self.service.clock(), games=games
                            )
                        elif action == "取消全部推送":
                            self.store.unsubscribe(owner, origin)
                        else:
                            removed = self.parse_games(value, existing)
                            self.store.set_subscription_games(
                                owner, origin, [g for g in existing if g not in removed]
                            )
                        self.service.invalidate(owner)
                    return await self.subscription_result(event, owner, origin)
                elif action == "米游社扫码登录":
                    result = await self.auth.start(owner, origin)
                elif action == "米游社Cookie登录":
                    result = await self.auth.manual(owner, value)
                elif action == "塔吉多短信登录":
                    result = await self.nte_auth.send_code(owner, value)
                elif action == "塔吉多验证码":
                    result = await self.nte_auth.verify(owner, value)
                elif action == "塔吉多AccessToken登录":
                    result = await self.nte_auth.access(owner, value)
                elif action in ("塔吉多登出", "塔吉多解绑"):
                    self.nte_auth.cancel(owner)
                    async with self.service.locked(owner):
                        self.store.unbind_nte(owner)
                        self.service.invalidate(owner)
                    result = "已删除塔吉多账号及凭据，并从提醒中移除异环。米游社账号不受影响，已无游戏的提醒同时关闭。"
                elif action in ("全部登出", "解绑"):
                    await self.auth.cancel(owner)
                    self.nte_auth.cancel(owner)
                    async with self.service.locked(owner):
                        self.store.unbind(owner)
                        self.service.invalidate(owner)
                    result = "已解绑，账号凭据和全部提醒订阅已删除。"
                elif action == "账号":
                    account = self.store.account(owner)
                    reminders = self.store.reminder_counts(owner)
                    if account:
                        try:
                            png = await self.renderer.accounts(account, reminders)
                            return event.chain_result([Comp.Image.fromBytes(png)])
                        except Exception as exc:
                            logger.warning(
                                f"[二游日常备忘姬] 账号图片生成失败：{type(exc).__name__}"
                            )
                    result = (
                        account_text(account)
                        if account
                        else "还没有绑定账号，请私聊 /备忘姬 米游社扫码登录。"
                    )
                    if account:
                        result += "\n每日提醒（所有会话）：" + (
                            "、".join(
                                f"{GAMES[g]} {count} 个会话" for g, count in reminders.items()
                            )
                            if reminders
                            else "尚未设置"
                        )
                elif action == "角色":
                    args = value.split()
                    if len(args) != 2 or args[0] not in ALIASES:
                        raise ValueError("例如：/备忘姬 角色 原神 123456789")
                    game = ALIASES[args[0]]
                    async with self.service.locked(owner):
                        self.store.select(owner, game, args[1])
                        self.service.invalidate(owner)
                    result = f"{GAMES[game]}已选择 UID {args[1]}。"
                else:
                    result = HELP
                return event.plain_result(result)
        except (ValueError, ApiError) as exc:
            return event.plain_result(str(exc))
        except Exception as exc:
            logger.warning(f"[二游日常备忘姬] 指令失败：{type(exc).__name__}")
            return event.plain_result("操作暂时失败，请稍后重试。")
        finally:
            self.inflight.discard(task)

    @filter.command_group("备忘姬")
    def reminder(self):
        """原神、星铁、绝区零、异环日常查询与提醒。"""
        pass

    @reminder.command("帮助")
    async def help_command(self, event: AstrMessageEvent):
        """查看指令用法。"""
        yield await self.execute(event, "帮助")

    @reminder.command("米游社扫码登录")
    async def login_command(self, event: AstrMessageEvent):
        """私聊扫码绑定米游社。"""
        yield await self.execute(event, "米游社扫码登录")

    @reminder.command("米游社Cookie登录", alias={"米游社cookie登录"})
    async def cookie_command(self, event: AstrMessageEvent, cookie: GreedyStr):
        """私聊提交完整 Cookie。"""
        yield await self.execute(event, "米游社Cookie登录", str(cookie))

    @reminder.command("账号")
    async def account_command(self, event: AstrMessageEvent):
        """私聊查看绑定与角色。"""
        yield await self.execute(event, "账号")

    @reminder.command("角色")
    async def role_command(self, event: AstrMessageEvent, role: GreedyStr):
        """私聊选择游戏角色，例如：原神 123456789。"""
        yield await self.execute(event, "角色", str(role).strip())

    @reminder.command("今日日常")
    async def query_command(self, event: AstrMessageEvent):
        """查询当前订阅的日常和周常。"""
        yield await self.execute(event, "今日日常")

    @reminder.command("订阅推送")
    async def schedule_command(self, event: AstrMessageEvent, setting: GreedyStr):
        """设置时间与游戏列表、查看提醒，或填写“关闭”。"""
        yield await self.execute(event, "订阅推送", str(setting).strip())

    @reminder.command("全部登出")
    async def unbind_command(self, event: AstrMessageEvent):
        """私聊删除账号及全部提醒订阅。"""
        yield await self.execute(event, "全部登出")

    @reminder.command("塔吉多短信登录")
    async def nte_login_command(self, event: AstrMessageEvent, phone: str = ""):
        """私聊发送塔吉多短信登录短信，不填手机号时查看说明。"""
        yield await self.execute(event, "塔吉多短信登录", phone.strip())

    @reminder.command("塔吉多验证码")
    async def nte_code_command(self, event: AstrMessageEvent, code: str = ""):
        """私聊提交登录短信验证码。"""
        yield await self.execute(event, "塔吉多验证码", code.strip())

    @reminder.command("塔吉多AccessToken登录", alias={"塔吉多accesstoken登录"})
    async def nte_token_command(self, event: AstrMessageEvent, access: GreedyStr):
        """私聊提交塔吉多 accessToken。"""
        yield await self.execute(event, "塔吉多AccessToken登录", str(access).strip())

    @reminder.command("塔吉多登出")
    async def nte_unbind_command(self, event: AstrMessageEvent):
        """只删除异环账号，保留米游社账号。"""
        yield await self.execute(event, "塔吉多登出")

    @reminder.command("米游社登出")
    async def mys_logout_command(self, event: AstrMessageEvent):
        yield await self.execute(event, "米游社登出")

    @reminder.command("获取米游社Cookie")
    async def export_cookie_command(self, event: AstrMessageEvent):
        yield await self.execute(event, "获取米游社Cookie")

    @reminder.command("获取塔吉多AccessToken")
    async def export_access_command(self, event: AstrMessageEvent):
        yield await self.execute(event, "获取塔吉多AccessToken")

    @reminder.command("订阅状态")
    async def status_command(self, event: AstrMessageEvent):
        yield await self.execute(event, "订阅状态")

    @reminder.command("取消推送")
    async def cancel_games_command(self, event: AstrMessageEvent, games: GreedyStr):
        yield await self.execute(event, "取消推送", str(games).strip())

    @reminder.command("取消全部推送")
    async def cancel_push_command(self, event: AstrMessageEvent):
        yield await self.execute(event, "取消全部推送")

    async def terminate(self):
        """Cancel owned work before closing storage and network resources."""
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        running = [t for t in self.inflight if t is not asyncio.current_task()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        if self.auth:
            await self.auth.close()
            self.auth = None
        if self.nte_auth:
            self.nte_auth.close()
            self.nte_auth = None
        if self.renderer:
            await self.renderer.close()
            self.renderer = None
        if self.session:
            await self.session.close()
            self.session = None
        if self.store:
            self.store.close()
            self.store = None
        self.service = None
