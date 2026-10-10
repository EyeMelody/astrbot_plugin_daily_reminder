"""NTEUID-compatible Laohu/Tajiduo login and read-only NTE daily data.

Protocol constants and signatures are adapted from tyql688/NTEUID (GPLv3).
See NOTICE.md. Raw responses and credentials must never enter logs.
"""

import hashlib
import json
import re
import secrets
import string
import time
import uuid
from base64 import b64encode
from dataclasses import asdict

import aiohttp

from .api import MAX_RESPONSE, ApiError
from .models import Role, mapping, parse_note

TAJIDUO = "https://bbs-api.tajiduo.com"
LAOHU = "https://user.laohu.com"
APP_VERSION = "1.2.4"
# Public SDK protocol key, not a user credential.
APP_KEY = "89155cc4e8634ec5b1b6364013b23e3e"
GAME_ID = "1289"


def device_id():
    return "HT" + uuid.uuid4().hex[:14].upper()


def token(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise ValueError("令牌格式不正确，请提交完整的单行令牌。")
    return value


def identifier(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,24}", value):
        raise ApiError("塔吉多返回的账号或角色 ID 无效", "nte_schema")
    return value


def ds_headers(device, access=""):
    stamp = str(int(time.time()))
    nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    digest = hashlib.md5(f"{stamp}{nonce}{APP_VERSION}pUds3dfMkl".encode()).hexdigest()
    return {
        "User-Agent": "okhttp/4.12.0",
        "platform": "android",
        "deviceid": device,
        "appversion": APP_VERSION,
        "uid": "0",
        "authorization": access,
        "ds": f"{stamp},{nonce},{digest}",
    }


def sms_fields(device, millis=False):
    result = {
        "appId": "10550",
        "channelId": "1",
        "deviceId": device,
        "deviceType": "Pixel 6",
        "deviceModel": "Pixel 6",
        "deviceName": "Pixel 6",
        "deviceSys": "Android 14",
        "adm": device,
        "idfa": "",
        "sdkVersion": "4.273.0",
        "bid": "com.pwrd.htassistant",
        "t": str(int(time.time() * (1000 if millis else 1))),
    }
    result.update({"version": "12", "mac": ""} if millis else {"versionCode": "12", "imei": ""})
    return result


def encrypt_sms(value):
    # ECB is required by the upstream SDK wire protocol, not used for local storage.
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    return b64encode(
        AES.new(APP_KEY[-16:].encode(), AES.MODE_ECB).encrypt(pad(value.encode(), AES.block_size))
    ).decode()


class NteClient:
    def __init__(self, session):
        self.session = session

    async def request(
        self, path, account, *, body=None, params=None, laohu=False, keep_empty=False
    ):
        root = LAOHU if laohu else TAJIDUO
        if laohu:
            headers = {"User-Agent": "okhttp/4.9.0"}
            body = dict(body or {})
            body["sign"] = hashlib.md5(
                ("".join(body[k] for k in sorted(body)) + APP_KEY).encode()
            ).hexdigest()
            if not keep_empty:
                body = {k: v for k, v in body.items() if v != ""}
        else:
            headers = ds_headers(account["device_id"], account.get("access_token", ""))
        try:
            async with self.session.request(
                "POST" if body is not None else "GET",
                root + path,
                headers=headers,
                data=body,
                params=params,
                allow_redirects=False,
            ) as response:
                if response.status in (401, 402, 403):
                    raise ApiError(
                        "塔吉多短信登录已失效，请私聊 /备忘姬 塔吉多短信登录", "nte_auth", auth=True
                    )
                if response.status != 200:
                    raise ApiError(
                        f"异环接口暂不可用（HTTP {response.status}）",
                        "nte_http",
                        transient=response.status == 429 or response.status >= 500,
                    )
                content = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ApiError("异环接口数据过大", "nte_schema")
                payload = json.loads(content)
        except (aiohttp.ClientError, TimeoutError):
            raise ApiError("异环接口连接超时，请稍后再试", "nte_network", transient=True) from None
        except (ValueError, UnicodeError):
            raise ApiError("异环接口返回了无效数据", "nte_schema") from None
        if not isinstance(payload, dict):
            raise ApiError("异环接口数据格式有变化", "nte_schema")
        code = payload.get("code")
        if str(code) in ("401", "402", "403"):
            raise ApiError(
                "塔吉多短信登录已失效，请私聊 /备忘姬 塔吉多短信登录", "nte_auth", auth=True
            )
        if type(code) not in (int, str) or str(code) != "0":
            safe = (
                str(code)
                if type(code) is int
                or isinstance(code, str)
                and re.fullmatch(r"-?[0-9]{1,8}", code)
                else "未知"
            )
            raise ApiError(
                f"异环接口未完成（错误码 {safe}），请检查登录或验证码；如需验证请在官方应用处理",
                "nte_retcode",
                transient=str(code) in ("429", "500", "502", "503", "504"),
            )
        return payload.get("result" if laohu else "data") or {}

    async def send_sms(self, phone, device):
        body = sms_fields(device)
        body.update(cellphone=phone, areaCodeId="1", type="16")
        await self.request("/m/newApi/sendPhoneCaptchaWithOutLogin", {}, body=body, laohu=True)

    async def sms_login(self, phone, code, device):
        check = sms_fields(device)
        check.update(cellphone=phone, captcha=code)
        await self.request("/m/newApi/checkPhoneCaptchaWithOutLogin", {}, body=check, laohu=True)
        body = sms_fields(device, True)
        body.update(
            cellphone=encrypt_sms(phone), captcha=encrypt_sms(code), areaCodeId="1", type="16"
        )
        raw = mapping(
            await self.request("/openApi/sms/new/login", {}, body=body, laohu=True, keep_empty=True)
        )
        account = {
            "device_id": device,
            "laohu_uid": identifier(raw.get("userId")),
            "laohu_token": token(raw.get("token")),
        }
        account.update(await self.exchange(account))
        return await self.complete_account(account)

    async def exchange(self, account):
        data = mapping(
            await self.request(
                "/usercenter/api/login",
                {**account, "access_token": ""},
                body={
                    "token": account["laohu_token"],
                    "userIdentity": account["laohu_uid"],
                    "appId": "10551",
                },
            )
        )
        result = self.session_data(data)
        result["aid"] = identifier(data.get("uid"))
        if account.get("aid") and result["aid"] != account["aid"]:
            raise ApiError("异环续期返回了不同账号，请重新绑定", "nte_identity")
        return result

    @staticmethod
    def session_data(data):
        try:
            return {
                "access_token": token(data.get("accessToken")),
                "refresh_token": token(data.get("refreshToken")),
                "updated_at": time.time(),
            }
        except ValueError:
            raise ApiError("塔吉多没有返回完整登录凭据", "nte_schema") from None

    async def renew(self, account):
        if account.get("refresh_token"):
            try:
                data = mapping(
                    await self.request(
                        "/usercenter/api/refreshToken",
                        {**account, "access_token": account["refresh_token"]},
                        body={},
                    )
                )
                if data.get("uid") is not None and identifier(data["uid"]) != account["aid"]:
                    raise ApiError("异环续期返回了不同账号", "nte_identity")
                return self.session_data(data)
            except ApiError as exc:
                if not exc.auth or not account.get("laohu_token"):
                    raise
        if account.get("laohu_token"):
            return await self.exchange(account)
        raise ApiError("accessToken 无法自动续期，请私聊重新绑定异环", "nte_auth", auth=True)

    async def access_login(self, value):
        return await self.complete_account(
            {"device_id": device_id(), "access_token": token(value), "updated_at": time.time()}
        )

    async def complete_account(self, account):
        info = mapping(await self.request("/usercenter/api/getUserFullInfo", account))
        aid = identifier(mapping(info.get("user")).get("uid"))
        if account.get("aid") and account["aid"] != aid:
            raise ApiError("塔吉多账号信息不一致，未保存绑定", "nte_identity")
        account = {**account, "aid": aid}
        rows = []
        for path, params in (
            ("/apihub/api/getGameBindRole", {"uid": aid, "gameId": GAME_ID}),
            ("/usercenter/api/v2/getGameRoles", {"gameId": GAME_ID}),
        ):
            try:
                data = await self.request(path, account, params=params)
                if path.endswith("getGameBindRole"):
                    rows.append(mapping(data))
                else:
                    values = data if isinstance(data, list) else mapping(data).get("roles", [])
                    if isinstance(values, list):
                        rows.extend(values[:100])
            except ApiError as exc:
                if exc.auth or exc.transient:
                    raise
        roles = self.parse_roles(rows)
        if not roles:
            cards = await self.request(
                "/apihub/api/getGameRecordCard", account, params={"uid": aid}
            )
            if isinstance(cards, list):
                roles = self.parse_roles(
                    [
                        mapping(c).get("bindRoleInfo")
                        for c in cards[:100]
                        if str(mapping(c).get("gameId")) == GAME_ID
                    ]
                )
        if not roles:
            raise ApiError("未找到异环角色，请先在塔吉多绑定异环国服角色", "nte_roles")
        account["roles"] = [asdict(r) for r in roles]
        return account

    @staticmethod
    def parse_roles(rows):
        result = {}
        for row in rows:
            row = mapping(row)
            try:
                uid = identifier(row.get("roleId"))
            except ApiError:
                continue
            result[uid] = Role(
                "nte", uid, "cn", " ".join(str(row.get("roleName") or "异环角色").split())[:40]
            )
        return list(result.values())

    async def fetch(self, account, role):
        data = await self.request("/apihub/awapi/yh/roleHome", account, params={"roleId": role.uid})
        if not isinstance(data, dict):
            raise ApiError("异环便笺格式有变化", "nte_schema")
        if data.get("roleid") is not None and str(data["roleid"]) != role.uid:
            raise ApiError("异环返回的角色与所选角色不一致", "nte_identity")
        return parse_note(role, data)
