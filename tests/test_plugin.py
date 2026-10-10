import asyncio
import importlib
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fixtures import account, notes


def fake_host(root):
    """Offline transport shim, explicitly not an actual AstrBot instance."""
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
            "astrbot.api.message_components",
            "astrbot.core",
            "astrbot.core.star",
            "astrbot.core.star.filter",
            "astrbot.core.star.filter.command",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        )
    }

    class Group:
        def __init__(self, fn):
            self.fn = fn
            self.commands = {}

        def command(self, name, **kwargs):
            def decorate(fn):
                self.commands[name] = fn.__name__
                return fn

            return decorate

    class Chain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(("text", text))
            return self

    class Star:
        def __init__(self, context):
            self.context = context

    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = Chain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(command_group=lambda name: Group)
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = lambda *a: lambda cls: cls
    comp = modules["astrbot.api.message_components"]
    comp.At = lambda qq: ("at", qq)
    comp.Plain = lambda text: ("text", text)
    comp.Image = types.SimpleNamespace(fromBytes=lambda data: ("image", data))
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: root
    return modules


class Event:
    def __init__(self, private=True, sender="11", platform="aiocqhttp", instance="bot"):
        self.private, self.sender, self.platform, self.instance = (
            private,
            sender,
            platform,
            instance,
        )
        self.unified_msg_origin = (
            instance + (":FriendMessage:" if private else ":GroupMessage:") + sender
        )
        self.stopped = False

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.instance

    def get_sender_id(self):
        return self.sender

    def is_private_chat(self):
        return self.private

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(tempfile.mkdtemp(prefix="daily-host-shim-"))
        self.patch = patch.dict(sys.modules, fake_host(root))
        self.patch.start()
        name = "astrbot_plugin_daily_reminder.main"
        sys.modules.pop(name, None)
        self.main = importlib.import_module(name)
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin = self.main.DailyReminder(self.context)
        await self.plugin.initialize()
        self.event = Event()
        self.owner = self.plugin.owner(self.event)
        self.plugin.store.save_account(self.owner, account())

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.patch.stop()
        sys.modules.pop("astrbot_plugin_daily_reminder.main", None)

    async def test_native_command_group_has_all_commands(self):
        self.assertEqual(
            set(self.plugin.reminder.commands),
            {
                "帮助",
                "米游社扫码登录",
                "米游社Cookie登录",
                "账号",
                "角色",
                "今日日常",
                "订阅状态",
                "订阅推送",
                "取消推送",
                "取消全部推送",
                "全部登出",
                "米游社登出",
                "获取米游社Cookie",
                "获取塔吉多AccessToken",
                "塔吉多短信登录",
                "塔吉多验证码",
                "塔吉多AccessToken登录",
                "塔吉多登出",
            },
        )

    async def test_greedy_parameters_retain_annotation_in_astrbot_428(self):
        # 4.28.1 substitutes a parameter's default for its annotation. GreedyStr
        # therefore must not have a default, or only the first word is parsed.
        for name, parameter in (
            ("cookie_command", "cookie"),
            ("role_command", "role"),
            ("nte_token_command", "access"),
            ("schedule_command", "setting"),
        ):
            item = inspect.signature(getattr(self.plugin, name)).parameters[parameter]
            self.assertIs(item.default, inspect.Parameter.empty)

    async def test_private_operations_are_rejected_in_group(self):
        event = Event(private=False)
        for action in self.main.PRIVATE:
            with self.subTest(action=action):
                result = await self.plugin.execute(event, action, "account_id=secret")
                self.assertIn("私聊", result)
                self.assertNotIn("secret", result)
        self.assertTrue(event.stopped)

    async def test_group_query_and_reminders_do_not_execute(self):
        self.plugin.service.query = AsyncMock()
        self.plugin.auth.start = AsyncMock()
        for action in ("帮助", "查询", "提醒", "米游社扫码登录", "不存在"):
            result = await self.plugin.execute(Event(private=False), action, "23:00 原神")
            self.assertIn("仅支持私聊", result)
        self.plugin.service.query.assert_not_called()
        self.plugin.auth.start.assert_not_called()
        self.assertEqual(self.plugin.store.reminder_counts(self.owner), {})

    async def test_push_rejects_group_and_other_user_destinations(self):
        for origin, mention in (
            ("bot:GroupMessage:11", ""),
            ("bot:FriendMessage:22", ""),
            ("bot:FriendMessage:11", "11"),
        ):
            with self.assertRaises(ValueError):
                await self.plugin.push(
                    {"owner": self.owner, "origin": origin, "mention": mention}, notes()
                )
        self.context.send_message.assert_not_called()

    async def test_private_push_has_no_mention(self):
        await self.plugin.push(
            {"owner": self.owner, "origin": self.event.unified_msg_origin, "mention": ""}, notes()
        )
        args = self.context.send_message.call_args.args
        self.assertEqual(args[0], self.event.unified_msg_origin)
        self.assertFalse(any(p[0] == "at" for p in args[1].chain))

    async def test_platform_is_enforced(self):
        result = await self.plugin.execute(Event(platform="qq_official"), "提醒", "23:00")
        self.assertIn("OneBot", result)

    async def test_user_and_bot_instance_isolation(self):
        for event in (Event(sender="22"), Event(instance="other-bot")):
            result = await self.plugin.execute(event, "账号")
            self.assertIn("还没有绑定", result)

    async def test_image_fallback_keeps_group_mention(self):
        self.plugin.renderer.render = AsyncMock(side_effect=RuntimeError("test"))
        parts = await self.plugin.components(notes(), "11")
        self.assertEqual(parts[0], ("at", "11"))
        self.assertEqual(parts[1][0], "text")
        self.assertIn("原神", parts[1][1])

    async def test_schedule_and_unbind(self):
        self.assertEqual(
            (await self.plugin.execute(self.event, "订阅推送", "23:00 原神 星穹铁道"))[0][0],
            "image",
        )
        self.assertEqual(
            self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)["clock"],
            "23:00",
        )
        await self.plugin.execute(self.event, "全部登出")
        self.assertIsNone(self.plugin.store.account(self.owner))
        self.assertIsNone(self.plugin.store.subscription(self.owner, self.event.unified_msg_origin))

    async def test_schedule_requires_games_and_validates_binding(self):
        for args in ("23:00", "23:00 异环", "23:00 未知游戏", "23:00 全部 原神"):
            await self.plugin.execute(self.event, "订阅推送", args)
            self.assertIsNone(
                self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)
            )
        result = await self.plugin.execute(self.event, "订阅推送", "23:00 原神，星穹铁道")
        self.assertEqual(result[0][0], "image")
        self.assertEqual(
            self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)["games"],
            ["genshin", "starrail"],
        )
        result = await self.plugin.execute(self.event, "订阅状态")
        self.assertEqual(result[0][0], "image")

    async def test_account_image_and_text_fallback(self):
        result = await self.plugin.execute(self.event, "账号")
        self.assertEqual(result[0][0], "image")
        self.assertTrue(result[0][1].startswith(b"\x89PNG"))
        self.plugin.renderer.accounts = AsyncMock(side_effect=RuntimeError("test"))
        self.assertIn("100000001", await self.plugin.execute(self.event, "账号"))

    async def test_initialize_is_idempotent_and_reload_cleans_resources(self):
        task, session = self.plugin.task, self.plugin.session
        await self.plugin.initialize()
        self.assertIs(self.plugin.task, task)
        await self.plugin.terminate()
        self.assertTrue(task.done())
        self.assertTrue(session.closed)
        await self.plugin.initialize()
        self.assertIsNot(self.plugin.task, task)
        self.assertIsNotNone(self.plugin.store.account(self.owner))

    async def test_new_subscriptions_add_remove_and_render(self):
        result = await self.plugin.execute(self.event, "订阅推送", "23:00 原神 星铁")
        self.assertEqual(result[0][0], "image")
        await self.plugin.execute(self.event, "订阅推送", "22:00 绝区零")
        row = self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)
        self.assertEqual(row["games"], ["genshin", "starrail", "zzz"])
        self.assertEqual(row["clock"], "22:00")
        await self.plugin.execute(self.event, "取消推送", "原神 绝区零")
        remaining = self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)
        self.assertEqual(remaining["games"], ["starrail"])
        self.assertEqual(remaining["start_day"], row["start_day"])
        self.plugin.service.query = AsyncMock(return_value=notes()[1:2])
        await self.plugin.execute(self.event, "今日日常")
        self.plugin.service.query.assert_awaited_once_with(self.owner, games=["starrail"])
        await self.plugin.execute(self.event, "取消全部推送")
        self.assertIsNone(self.plugin.store.subscription(self.owner, self.event.unified_msg_origin))
        self.plugin.service.query.reset_mock()
        self.assertEqual((await self.plugin.execute(self.event, "今日日常"))[0][0], "image")
        self.plugin.service.query.assert_not_called()

    async def test_subscription_invalid_games_are_atomic(self):
        await self.plugin.execute(self.event, "订阅推送", "23:00 原神")
        old = self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)
        for command, args in (
            ("订阅推送", "24:00 星铁"),
            ("订阅推送", "23:00 异环"),
            ("取消推送", "原神 不存在"),
        ):
            result = await self.plugin.execute(self.event, command, args)
            self.assertIsInstance(result, str)
            self.assertEqual(
                self.plugin.store.subscription(self.owner, self.event.unified_msg_origin), old
            )

    async def test_credentials_export_is_private_and_owner_only(self):
        from test_nte import nte_account

        nte = nte_account()
        nte["access_token"] = "SYNTHETIC_ACCESS"
        self.plugin.store.save_nte(self.owner, nte)
        for action, secret in (
            ("获取米游社Cookie", "cookie_token=SYNTHETIC"),
            ("获取塔吉多AccessToken", "SYNTHETIC_ACCESS"),
        ):
            self.assertIn(secret, await self.plugin.execute(self.event, action))
            self.assertNotIn(secret, await self.plugin.execute(Event(sender="22"), action))
            self.assertNotIn(secret, await self.plugin.execute(Event(private=False), action))

    async def test_provider_logout_preserves_other_provider_and_subscription(self):
        from test_nte import nte_account

        self.plugin.store.save_nte(self.owner, nte_account())
        await self.plugin.execute(self.event, "订阅推送", "23:00 全部")
        await self.plugin.execute(self.event, "米游社登出")
        acc = self.plugin.store.account(self.owner)
        self.assertFalse(acc["cookie"])
        self.assertTrue(acc["nte"])
        self.assertEqual(
            self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)["games"],
            ["nte"],
        )
        await self.plugin.execute(self.event, "塔吉多登出")
        self.assertIsNone(self.plugin.store.account(self.owner))

    async def test_help_status_render_and_safe_fallback(self):
        result = await self.plugin.execute(self.event, "帮助")
        self.assertEqual(result[0][0], "image")
        self.plugin.renderer.help = AsyncMock(side_effect=RuntimeError())
        text = await self.plugin.execute(self.event, "帮助")
        self.assertIn("订阅推送 23:00 原神 星铁", text)
        self.plugin.renderer.status = AsyncMock(side_effect=RuntimeError())
        text = await self.plugin.execute(self.event, "订阅状态")
        self.assertIn("未订阅", text)
        self.assertNotIn("cookie_token", text)

    async def test_today_fetches_only_subscribed_role(self):
        self.plugin.service.client.fetch = AsyncMock(return_value=notes()[1])
        await self.plugin.execute(self.event, "订阅推送", "23:00 星铁")
        await self.plugin.execute(self.event, "今日日常")
        self.assertEqual(self.plugin.service.client.fetch.await_count, 1)
        self.assertEqual(self.plugin.service.client.fetch.call_args.args[1].game, "starrail")

    async def test_nte_logout_keeps_mys_and_all_logout_clears_everything(self):
        from test_nte import nte_account

        self.plugin.store.save_nte(self.owner, nte_account())
        await self.plugin.execute(self.event, "订阅推送", "23:00 全部")
        await self.plugin.execute(self.event, "塔吉多登出")
        self.assertTrue(self.plugin.store.account(self.owner)["cookie"])
        self.assertNotIn(
            "nte",
            self.plugin.store.subscription(self.owner, self.event.unified_msg_origin)["games"],
        )
        await self.plugin.execute(self.event, "全部登出")
        self.assertIsNone(self.plugin.store.account(self.owner))
        self.assertEqual(self.plugin.store.reminder_counts(self.owner), {})

    async def test_unload_cancels_inflight_queries(self):
        entered = asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.plugin.service.query = wait
        await self.plugin.execute(self.event, "订阅推送", "23:00 原神")
        task = asyncio.create_task(self.plugin.execute(self.event, "今日日常"))
        await entered.wait()
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
