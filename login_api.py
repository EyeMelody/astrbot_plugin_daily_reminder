"""MiYouShe QR protocol adapted from gsuid_core and 星铁助手 (NOTICE.md).

This module only exchanges credentials; auth.py owns persistence and selection.
No authentication payload is logged.
"""

import asyncio
import io
import json
import re
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse

import aiohttp

from .api import (
    MAX_RESPONSE,
    MYS_DEVICE_ID,
    MYS_USER_AGENT,
    MYS_VERSION,
    ApiError,
    normalize_cookie,
)

PASSPORT = "https://passport-api.mihoyo.com"
CREATE = PASSPORT + "/account/ma-cn-passport/app/createQRLogin"
STATUS = PASSPORT + "/account/ma-cn-passport/app/queryQRLoginStatus"
EXCHANGE = PASSPORT + "/account/auth/api/getCookieAccountInfoBySToken"
ROLES = "https://api-takumi-record.mihoyo.com/game_record/card/wapi/getGameRecordCard"


def token_value(value, field_name="凭据", *, cookie=True):
    """Treat server tokens as opaque, not Base64/UUIDs.

    Cookie values use RFC 6265 cookie-octet; JSON tickets only need bounded,
    non-control text. Error messages never include values or token fragments.
    """
    if value is None or value == "":
        reason = "缺失或为空"
    elif not isinstance(value, str):
        reason = "不是字符串"
    elif len(value) > 4096:
        reason = "长度超限"
    elif cookie and any(
        not (
            ord(c) == 0x21
            or 0x23 <= ord(c) <= 0x2B
            or 0x2D <= ord(c) <= 0x3A
            or 0x3C <= ord(c) <= 0x5B
            or 0x5D <= ord(c) <= 0x7E
        )
        for c in value
    ):
        reason = "包含 Cookie 不允许的字符"
    elif not cookie and any(ord(c) < 32 or ord(c) == 127 for c in value):
        reason = "包含控制字符"
    else:
        return value
    raise ApiError(
        f"{field_name} {reason}，请重新扫码；若重复出现，请反馈此字段名称（不要发送凭据）。",
        "login_schema",
    )


def render_qr(url):
    # Pure Python encoder, no Pillow; import only when a QR is requested.
    import segno

    output = io.BytesIO()
    segno.make_qr(url, error="m").save(output, kind="png", scale=6, border=4)
    return output.getvalue()


class LoginApi:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def hyp_headers(device):
        return {
            "x-rpc-device_id": MYS_DEVICE_ID,
            "User-Agent": "HYPContainer/1.3.3.182",
            "x-rpc-app_id": "ddxf5dufpuyo",
            "x-rpc-client_type": "3",
        }

    @staticmethod
    def account_headers(device):
        return {
            "x-rpc-device_id": MYS_DEVICE_ID,
            "x-rpc-app_version": MYS_VERSION,
            "x-rpc-client_type": "5",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Referer": "https://webstatic.mihoyo.com/",
            "Origin": "https://webstatic.mihoyo.com/",
            "User-Agent": MYS_USER_AGENT,
        }

    async def request(self, method, url, *, stage="登录", allow_empty=False, **kwargs):
        try:
            async with self.session.request(
                method, url, allow_redirects=False, **kwargs
            ) as response:
                if response.status != 200:
                    raise ApiError(
                        f"登录接口暂不可用（HTTP {response.status}），请稍后重试。",
                        "login_http",
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise ApiError("登录接口响应异常过大，已停止。", "login_size")
                payload = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ApiError("登录接口连接失败或超时，请稍后重新扫码。", "login_network") from None
        except (ValueError, UnicodeError):
            raise ApiError("登录接口返回了无效数据。", "login_schema") from None
        if not isinstance(payload, dict):
            raise ApiError("登录接口返回格式异常，请重新扫码。", "login_schema")
        if payload.get("retcode") != 0:
            code = payload.get("retcode")
            code = str(code) if type(code) is int else "unknown"
            raise ApiError(
                f"{stage}接口未完成（错误码 {code}），请重试；持续失败请重新扫码或在官方应用检查验证。",
                "login_retcode",
            )
        if allow_empty and payload.get("data") is None:
            return {}
        if not isinstance(payload.get("data"), dict):
            raise ApiError("登录接口返回格式异常，请重新扫码。", "login_schema")
        return payload["data"]

    async def create(self):
        device = MYS_DEVICE_ID
        data = await self.request("POST", CREATE, headers=self.hyp_headers(device), json={})
        ticket = token_value(data.get("ticket"), "二维码票据 ticket", cookie=False)
        url = data.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            raise ApiError("二维码链接格式异常。", "login_schema")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not any(
            host == h or host.endswith("." + h)
            for h in ("mihoyo.com", "miyoushe.com", "hoyoverse.com")
        ):
            raise ApiError("二维码链接不是米哈游登录地址，已停止。", "login_url")
        return {"ticket": ticket, "url": url, "device_id": device}

    async def status(self, code):
        return await self.request(
            "POST",
            STATUS,
            headers=self.hyp_headers(code["device_id"]),
            json={"ticket": code["ticket"]},
        )

    async def credentials(self, login_data, device):
        info = login_data.get("user_info") or {}
        tokens = login_data.get("tokens") or []
        if not isinstance(info, dict) or not isinstance(tokens, list):
            raise ApiError("扫码确认后未取得账号信息。", "login_schema")
        aid = str(info.get("aid") or info.get("uid") or info.get("account_id") or "")
        if not re.fullmatch(r"[0-9]{1,20}", aid):
            raise ApiError("扫码确认后未取得账号 ID。", "login_schema")
        mid = token_value(info.get("mid"), "账号 MID")
        # HYP responses may identify SToken by token_type=1 without a name.
        # Do not use the upstream fallback tokens[0]: it may be another token type.
        stoken = next(
            (
                t.get("token")
                for name in ("stoken_v2", "stoken")
                for t in tokens
                if isinstance(t, dict) and t.get("name") == name and t.get("token")
            ),
            None,
        )
        if stoken is None:
            stoken = next(
                (
                    t.get("token")
                    for t in tokens
                    if isinstance(t, dict)
                    and str(t.get("token_type")) == "1"
                    and t.get("name") in (None, "", "stoken", "stoken_v2")
                    and t.get("token")
                ),
                None,
            )
        stoken = token_value(stoken, "登录令牌 SToken")
        headers = self.account_headers(device)
        headers["Cookie"] = f"stuid={aid};stoken={stoken};mid={mid}"
        data = await self.request(
            "GET",
            EXCHANGE,
            headers=headers,
            params={"stoken": stoken, "uid": aid, "mid": mid},
        )
        cookie_token = token_value(data.get("cookie_token"), "CookieToken")
        cookie = normalize_cookie(f"account_id={aid};cookie_token={cookie_token}")
        return aid, cookie, headers["Cookie"]

    async def refresh_cookie(self, app_cookie, device):
        jar = SimpleCookie()
        try:
            jar.load(app_cookie)
            aid = jar["stuid"].value
            stoken = token_value(jar["stoken"].value, "SToken")
            mid = token_value(jar["mid"].value, "MID")
            if not re.fullmatch(r"[0-9]{1,20}", aid):
                raise ValueError
        except (CookieError, KeyError, ValueError):
            raise ApiError("已保存的 SToken 信息不完整，请重新扫码。", "refresh_schema") from None
        headers = self.account_headers(device)
        headers["Cookie"] = app_cookie
        data = await self.request(
            "GET",
            EXCHANGE,
            stage="刷新 Cookie",
            headers=headers,
            params={"stoken": stoken, "uid": aid, "mid": mid},
        )
        token = token_value(data.get("cookie_token"), "CookieToken")
        return normalize_cookie(f"account_id={aid};cookie_token={token}")
