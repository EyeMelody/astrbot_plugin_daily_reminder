"""Bounded CN record requests; protocol adapted from gsuid_core (NOTICE.md)."""

import hashlib
import json
import random
import re
import string
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

MAX_RESPONSE = 512 * 1024
MYS_VERSION = "2.102.1"
MYS_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; PHK110) AppleWebKit/537.36 "
    f"Chrome/126.0.6478.133 Mobile Safari/537.36 miHoYoBBS/{MYS_VERSION}"
)
ROOT = "https://api-takumi-record.mihoyo.com"
# Fixed identifiers follow astrbot_eryou_daily; see NOTICE.md.
MYS_DEVICE_ID = "AFA5DBD7-D027-402B-9522-1D9A4A5EFB85"
MYS_DEVICE_FP = "38d7f349e93d8"
SR_WIDGET = ROOT + "/game_record/app/hkrpg/aapi/widget"
WIDGET_ROUTES = {
    "genshin": ROOT + "/game_record/genshin/aapi/widget/v2",
    "starrail": SR_WIDGET,
    "zzz": ROOT + "/event/game_record_zzz/api/zzz/widget",
}
CHALLENGE = {
    "genshin": ("2", "v4.1.5-ys_#ys", "v4.1.5-ys"),
    "starrail": ("6", "v1.4.1-rpg_#/rpg", "v1.4.1-rpg"),
    "zzz": ("8", "v1.0.14_#/zzz", "v1.0.14-zzz"),
}
ROUTES = {
    "genshin": "/game_record/app/genshin/api/dailyNote",
    "starrail": "/game_record/app/hkrpg/api/note",
    "zzz": "/event/game_record_zzz/api/zzz/note",
}
GAME_BIZ = {"genshin": "hk4e_cn", "starrail": "hkrpg_cn", "zzz": "nap_cn"}
REGIONS = {
    "genshin": {"cn_gf01", "cn_qd01"},
    "starrail": {"prod_gf_cn", "prod_qd_cn"},
    "zzz": {"prod_gf_cn"},
}


class ApiError(Exception):
    def __init__(self, message, code="network", auth=False, transient=False):
        super().__init__(message)
        self.code, self.auth, self.transient = code, auth, transient


def normalize_cookie(value):
    value = value.strip()
    if not value or len(value) > 8192 or any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError("Cookie 格式不正确，请粘贴完整的单行 Cookie。")
    jar = SimpleCookie()
    try:
        jar.load(value)
    except CookieError:
        raise ValueError("Cookie 格式不正确。") from None
    if not any(jar.get(k) for k in ("cookie_token", "cookie_token_v2", "ltoken", "ltoken_v2")):
        raise ValueError("Cookie 缺少 cookie_token 或 ltoken，不能只填写 UID。")
    ids = [jar[k].value for k in ("account_id", "account_id_v2", "ltuid", "ltuid_v2") if k in jar]
    if not ids or any(not re.fullmatch(r"[0-9]{1,20}", i) for i in ids) or len(set(ids)) != 1:
        raise ValueError("Cookie 缺少账号 ID，或包含不一致的账号 ID。")
    return "; ".join(f"{k}={v.value}" for k, v in jar.items())


def account_id(cookie):
    jar = SimpleCookie()
    jar.load(normalize_cookie(cookie))
    return next(
        jar[k].value for k in ("account_id_v2", "account_id", "ltuid_v2", "ltuid") if k in jar
    )


def make_ds(query, overseas=False):
    stamp, nonce = str(int(time.time())), str(random.randint(100000, 200000))
    raw = f"salt=xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs&t={stamp}&r={nonce}&b=&q={query}"
    return f"{stamp},{nonce},{hashlib.md5(raw.encode()).hexdigest()}"


def make_web_ds():
    stamp = str(int(time.time()))
    nonce = "".join(random.sample(string.ascii_lowercase + string.digits, 6))
    raw = f"salt=yBh10ikxtLPoIhgwgPZSv5dmfaOTSJ6a&t={stamp}&r={nonce}"
    return f"{stamp},{nonce},{hashlib.md5(raw.encode()).hexdigest()}"


def widget_matches_role(account, role, data):
    """The widget uses the account's active role, not a requested UID.

    Prefer explicit identity or the official note link. An anonymous response
    is usable only for a single authenticated CN binding of this game.
    """
    roles = [r for r in account.get("roles", []) if r.get("game") == role.game]
    if not any(r.get("uid") == role.uid and r.get("region") == role.region for r in roles):
        return False
    uids, servers = set(), set()
    for key in ("uid", "role_id", "game_uid"):
        if data.get(key) is not None:
            uids.add(str(data[key]))
    for key in ("server", "region"):
        if data.get(key) is not None:
            servers.add(str(data[key]))
    for key in ("note_url", "home_url"):
        link = data.get(key)
        if not isinstance(link, str):
            continue
        try:
            parsed = urlsplit(link)
        except ValueError:
            return False
        for query in (parsed.query, parsed.fragment.partition("?")[2]):
            params = parse_qs(query)
            for field in ("uid", "role_id", "game_uid"):
                uids.update(params.get(field, []))
            for field in ("server", "region"):
                servers.update(params.get(field, []))
    if (uids and uids != {role.uid}) or (servers and servers != {role.region}):
        return False
    if uids and servers:
        return True
    candidates = {(r.get("uid"), r.get("region")) for r in roles}
    return candidates == {(role.uid, role.region)}


class Client:
    def __init__(self, session, clock=time.time):
        self.session = session
        self.clock = clock

    @staticmethod
    async def read_json(response):
        if response.status != 200:
            raise ApiError(
                f"接口暂不可用（HTTP {response.status}）",
                "http",
                transient=response.status == 429 or response.status >= 500,
            )
        body = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise ApiError("接口返回数据过大", "schema")
        return json.loads(body)

    async def request(self, url, account, params, game=None):
        query = urlencode(sorted(params.items()))
        headers = {
            "Cookie": account["cookie"],
            "DS": make_ds(query),
            "x-rpc-device_id": MYS_DEVICE_ID,
            "x-rpc-app_version": MYS_VERSION,
            "x-rpc-client_type": "5",
            "User-Agent": MYS_USER_AGENT,
            "Referer": "https://webstatic.mihoyo.com/",
            "Origin": "https://webstatic.mihoyo.com",
            "x-rpc-language": "zh-cn",
            "X-Requested-With": "com.mihoyo.hyperion",
        }
        if game:
            headers["x-rpc-device_fp"] = MYS_DEVICE_FP
        if game == "starrail":
            headers.update({"x-rpc-device_model": "PHK110", "x-rpc-sys_version": "13"})
        if game == "zzz":
            headers.pop("DS")
            headers.pop("x-rpc-client_type")
            headers.update(
                {
                    "x-rpc-page": CHALLENGE[game][1],
                    "x-rpc-platform": "2",
                    "Referer": "https://act.mihoyo.com/",
                    "Origin": "https://act.mihoyo.com",
                }
            )
        for attempt in range(2):
            try:
                return await self._request_once(url, query, headers)
            except ApiError as exc:
                if exc.code != "captcha" or game not in CHALLENGE or attempt:
                    raise
                challenge, page, version = CHALLENGE[game]
                headers = {
                    **headers,
                    "x-rpc-challenge_game": challenge,
                    "x-rpc-page": page,
                    "x-rpc-tool-verison": version,
                }
                if "DS" in headers:
                    headers["DS"] = make_ds(query)

    async def _request_once(self, url, query, headers):
        try:
            async with self.session.get(
                url, params=query, headers=headers, allow_redirects=False
            ) as response:
                payload = await self.read_json(response)
        except (aiohttp.ClientError, TimeoutError):
            raise ApiError("连接超时，请稍后再试", transient=True) from None
        except (ValueError, UnicodeError):
            raise ApiError("接口返回了无效数据", "schema") from None
        return self.decode(payload)

    @staticmethod
    def decode(payload):
        if not isinstance(payload, dict):
            raise ApiError("接口数据格式有变化", "schema")
        code = payload.get("retcode")
        if code in (-100, 10001, 10002, 10103):
            raise ApiError("登录已失效，请私聊 /备忘姬 米游社扫码登录", "auth", auth=True)
        if code in (1034, 10035, 5003, 10041, 10053):
            raise ApiError(f"米游社暂时限制查询（错误码 {code}）", "captcha")
        if code in (10102, 50001):
            raise ApiError("请先在米游社开启实时便笺并检查角色绑定", "private")
        if code in (10101, 1008, -110):
            raise ApiError("查询过于频繁，请稍后再试", "rate", transient=True)
        if type(code) is not int or code != 0:
            label = str(code) if type(code) is int else "未知"
            raise ApiError(f"查询失败（错误码 {label}）", "retcode")
        if not isinstance(payload.get("data"), dict):
            raise ApiError("接口数据格式有变化", "schema")
        return payload["data"]

    async def roles(self, account):
        """Use authenticated bindings, not public profile cards, as ownership proof."""
        from .models import Role

        result = []
        for game, biz in GAME_BIZ.items():
            data = await self.request(
                "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie",
                account,
                {"game_biz": biz},
            )
            rows = data.get("list")
            if not isinstance(rows, list):
                raise ApiError("无法读取绑定角色，请稍后重试", "schema")
            for row in rows[:100]:
                if not isinstance(row, dict):
                    continue
                uid, region = str(row.get("game_uid", "")), row.get("region")
                if not re.fullmatch(r"[0-9]{6,12}", uid) or region not in REGIONS[game]:
                    continue
                role = Role(
                    game, uid, region, " ".join(str(row.get("nickname") or "旅行者").split())[:40]
                )
                if role not in result:
                    result.append(role)
        if not result:
            raise ApiError("没有找到原神、星铁或绝区零的国服角色", "roles")
        return result

    async def fetch(self, account, role):
        from .models import parse_note

        widget_error = None
        if role.game in WIDGET_ROUTES and account.get("stoken"):
            try:
                data = await self.widget(account, role)
                note = parse_note(role, data)
                if (
                    role.game == "genshin"
                    and not data.get("daily_task")
                    and data.get("is_extra_task_reward_received") is not True
                ):
                    commissions = next(f for f in note.daily if f.label == "每日委托")
                    commissions.value += " · 历练点暂未获取"
                return note
            except ApiError as exc:
                # A widget failure does not imply that the normal Cookie expired.
                # Fall back once; the regular note keeps its existing bounded retry.
                widget_error = exc
        try:
            data = await self.request(
                ROOT + ROUTES[role.game],
                account,
                {"role_id": role.uid, "server": role.region},
                game=role.game,
            )
        except ApiError as exc:
            if widget_error is None:
                raise
            raise ApiError(
                f"小组件：{widget_error}；便笺：{exc}",
                exc.code,
                auth=exc.auth,
                transient=exc.transient,
            ) from None
        return parse_note(role, data)

    async def widget(self, account, role):
        """Per-game CN widget protocols; no assumed support for selecting a UID."""
        from .login_api import token_value

        jar = SimpleCookie()
        try:
            jar.load(account["stoken"])
            aid = jar["stuid"].value
            if aid != account["aid"] or not re.fullmatch(r"[0-9]{1,20}", aid):
                raise ValueError
            token = token_value(jar["stoken"].value, "SToken")
            mid = token_value(jar["mid"].value, "MID")
        except (CookieError, KeyError, TypeError, ValueError):
            raise ApiError("小组件凭据不完整", "widget_credentials") from None
        headers = {
            "Cookie": f"stuid={aid};stoken={token};mid={mid}",
            "DS": make_ds(""),
            "x-rpc-app_version": MYS_VERSION,
            "x-rpc-client_type": "5",
            "x-rpc-channel": "beta",
            "x-rpc-device_id": MYS_DEVICE_ID,
            "x-rpc-device_model": "PHK110",
            "x-rpc-sys_version": "13",
            "x-rpc-language": "zh-cn",
            "Referer": "https://app.mihoyo.com",
            "User-Agent": "okhttp/4.8.0",
            "X-Requested-With": "com.mihoyo.hyperion",
        }
        headers["x-rpc-device_fp"] = MYS_DEVICE_FP
        query = ""
        if role.game in ("genshin", "zzz"):
            headers.pop("x-rpc-sys_version")
            headers.pop("x-rpc-device_model")
            headers.pop("x-rpc-channel")
            headers.update(
                {
                    "User-Agent": MYS_USER_AGENT,
                    "Referer": "https://webstatic.mihoyo.com/",
                    "Origin": "https://webstatic.mihoyo.com/",
                }
            )
        if role.game == "genshin":
            query = "game_id=2"
            headers.update({"DS": make_web_ds(), "x-rpc-channel": "miyousheluodi"})
        elif role.game == "zzz":
            headers.pop("DS")
            headers.pop("x-rpc-client_type")
            headers.update(
                {
                    "x-rpc-page": CHALLENGE["zzz"][1],
                    "x-rpc-platform": "2",
                    "Referer": "https://act.mihoyo.com/",
                    "Origin": "https://act.mihoyo.com",
                }
            )
        data = await self._request_once(WIDGET_ROUTES[role.game], query, headers)
        if not widget_matches_role(account, role, data):
            raise ApiError("小组件角色与所选角色无法对应", "widget_role")
        # Reject empty/changed schemas instead of displaying an all-unknown success.
        if role.game == "zzz":
            battery = data.get("energy")
            progress = battery.get("progress") if isinstance(battery, dict) else None
            current, maximum = (
                (progress.get("current"), progress.get("max"))
                if isinstance(progress, dict)
                else (None, None)
            )
        else:
            suffix = "resin" if role.game == "genshin" else "stamina"
            current, maximum = data.get("current_" + suffix), data.get("max_" + suffix)
        if type(current) is not int or current < 0 or type(maximum) is not int or maximum <= 0:
            raise ApiError("小组件数据格式有变化", "schema")
        return data
