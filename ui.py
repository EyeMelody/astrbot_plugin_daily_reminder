"""Local, fixed-text help and subscription cards; no remote artwork or credentials."""

import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import GAMES

ROOT = Path(__file__).parent
FONT = ROOT / "assets/NotoSansCJKsc-Regular.otf"
VERSION = re.search(
    r"(?m)^version:\s*['\"]?(v?[\d.]+)", (ROOT / "metadata.yaml").read_text(encoding="utf-8")
).group(1)
VERSION = "v" + VERSION.removeprefix("v")
SECTIONS = [
    (
        "账号登录",
        [
            ("米游社扫码登录", "米游社扫码登录"),
            ("米游社Cookie登录", "米游社Cookie登录 完整Cookie"),
            ("获取米游社Cookie", "获取米游社Cookie"),
            ("米游社登出", "米游社登出"),
            ("塔吉多短信登录", "塔吉多短信登录 手机号"),
            ("提交短信验证码", "塔吉多验证码 验证码"),
            ("塔吉多AccessToken登录", "塔吉多AccessToken登录 AccessToken"),
            ("获取塔吉多AccessToken", "获取塔吉多AccessToken"),
            ("塔吉多登出", "塔吉多登出"),
            ("全部登出", "全部登出"),
        ],
    ),
    (
        "日常与推送",
        [
            ("今日日常", "今日日常"),
            ("订阅状态", "订阅状态"),
            ("订阅推送", "订阅推送 23:00 原神 星铁"),
            ("取消推送", "取消推送 原神 星铁"),
            ("取消全部推送", "取消全部推送"),
        ],
    ),
    ("其他", [("帮助", "帮助")]),
]


def help_text():
    lines = [f"备忘姬帮助 · {VERSION}"]
    for title, entries in SECTIONS:
        lines.append(title)
        lines.extend("/备忘姬 " + command for _, command in entries)
    lines.extend(
        [
            "支持原神、星铁、绝区零、异环，也可填写“全部”。重复订阅追加游戏并更新统一时间。",
            "今日日常只查询当前订阅。周六、周日将周常加入待办，周本不计入。",
            "便笺只能推测当天是否有活动，不能确认登录记录。获取的凭据请勿转发。",
        ]
    )
    return "\n".join(lines)


def wrap(draw, text, font, width):
    rows, line = [], ""
    for char in text:
        if line and draw.textlength(line + char, font=font) > width:
            rows.append(line)
            line = ""
        line += char
    return rows + [line]


def png(canvas, height):
    output = io.BytesIO()
    canvas.crop((0, 0, canvas.width, height)).save(output, format="PNG")
    canvas.close()
    return output.getvalue()


def draw_help(_=None):
    canvas = Image.new("RGB", (1080, 2600), "#10131C")
    draw = ImageDraw.Draw(canvas)
    title = ImageFont.truetype(str(FONT), 42)
    heading = ImageFont.truetype(str(FONT), 30)
    label = ImageFont.truetype(str(FONT), 25)
    small = ImageFont.truetype(str(FONT), 21)
    draw.rounded_rectangle((24, 24, 1056, 180), radius=26, fill="#242234")
    with Image.open(ROOT / "logo.png") as source:
        icon = ImageOps.fit(source.convert("RGB"), (124, 124))
    canvas.paste(icon, (906, 40))
    icon.close()

    def centered_text(x, cy, text, font, fill, bold=False):
        bounds = draw.textbbox((0, 0), text, font=font)
        draw.text(
            (x, cy - (bounds[1] + bounds[3]) / 2),
            text,
            font=font,
            fill=fill,
            stroke_width=1 if bold else 0,
            stroke_fill=fill,
        )

    centered_text(48, 102, "备忘姬帮助", title, "#F5F3FA", bold=True)
    badge_x = 48 + draw.textlength("备忘姬帮助", font=title) + 24
    badge_width = draw.textlength(VERSION, font=small) + 28
    draw.rounded_rectangle((badge_x, 83, badge_x + badge_width, 121), radius=10, fill="#703452")
    centered_text(badge_x + 14, 102, VERSION, small, "#FFD8E6")
    y = 206
    for section, entries in SECTIONS:
        draw.rounded_rectangle((28, y, 1052, y + 53), radius=15, fill="#262534")
        draw.rounded_rectangle((28, y, 270, y + 53), radius=15, fill="#D92C69")
        centered_text(47, y + 26.5, section, heading, "white", bold=True)
        y += 71
        for offset in range(0, len(entries), 2):
            pair = entries[offset : offset + 2]
            commands = [wrap(draw, "/备忘姬 " + cmd, small, 455) for _, cmd in pair]
            height = 64 + max(map(len, commands)) * 30
            for column, ((name, _), lines) in enumerate(zip(pair, commands)):
                x = 28 + column * 522
                draw.rounded_rectangle(
                    (x, y, x + 502, y + height),
                    radius=18,
                    fill="#202534",
                    outline="#703452",
                    width=2,
                )
                draw.rounded_rectangle((x + 16, y + 19, x + 21, y + 44), radius=2, fill="#FF6291")
                draw.text((x + 34, y + 13), name, font=label, fill="#F4F5FA")
                for i, line in enumerate(lines):
                    draw.text((x + 24, y + 52 + i * 30), line, font=small, fill="#B7C1D5")
            y += height + 16
        y += 14
    for text in (
        "游戏：原神 / 星铁 / 绝区零 / 异环；也可填写“全部”。",
        "订阅会追加游戏并更新统一时间；今日日常只查询已订阅游戏。",
        "周六、周日检查周常待办，周本不计入。便笺仅用于推测活动。",
        "获取的 Cookie / AccessToken 仅供本人使用，请勿转发。",
    ):
        draw.text((32, y), text, font=small, fill="#9FAABD")
        y += 35
    return png(canvas, y + 26)


def status_rows(account, subscription):
    account = account or {}
    games = set((subscription or {}).get("games", []))
    rows = []
    for game in GAMES:
        provider = account.get("nte", {}) if game == "nte" else account
        roles = [r for r in provider.get("roles", []) if r["game"] == game]
        selected = next(
            (r for r in roles if provider.get("selected", {}).get(game) == r["uid"]), None
        )
        state = (
            "已订阅"
            if game in games and selected
            else "待选择角色"
            if roles and not selected
            else "未订阅"
            if selected
            else "未登录或无角色"
        )
        rows.append((game, selected, state))
    return rows


def status_text(account, subscription):
    lines = [
        "日常订阅状态",
        f"将在每天{subscription['clock']}点进行一次日常状态推送" if subscription else "尚未设置推送",
    ]
    for game, role, state in status_rows(account, subscription):
        lines.append(
            f"{GAMES[game]} · {state}" + (f" · {role['name']} · UID {role['uid']}" if role else "")
        )
    return "\n".join(lines)


def draw_status(value):
    from .render import COLORS

    account, subscription = value
    canvas = Image.new("RGB", (960, 1800), "#10151E")
    draw = ImageDraw.Draw(canvas)
    title = ImageFont.truetype(str(FONT), 32)
    label = ImageFont.truetype(str(FONT), 28)
    small = ImageFont.truetype(str(FONT), 20)
    uid_font = ImageFont.truetype(str(FONT), 24)
    draw.text((32, 24), "日常订阅状态", font=title, fill="#E6EBF2")
    clock = f"将在每天{subscription['clock']}点进行一次日常状态推送" if subscription else "尚未设置推送"
    draw.text((32, 72), clock, font=small, fill="#A8B3C3")
    y = 116
    for game, role, state in status_rows(account, subscription):
        names = wrap(draw, role["name"] if role else GAMES[game], label, 485)
        extra = 36 * (len(names) - 1)
        height = 148 + extra
        draw.rounded_rectangle((32, y, 928, y + height), radius=16, fill="#1C2432")
        with Image.open(ROOT / "assets/icons" / f"{game}.jpg") as source:
            icon = ImageOps.fit(source.convert("RGB"), (96, 96), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (96, 96))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 95, 95), radius=20, fill=255)
        canvas.paste(icon, (58, y + 26), mask)
        mask.close()
        icon.close()
        draw.rounded_rectangle((32, y + 26, 37, y + 121), radius=2, fill=COLORS[game])
        for i, name in enumerate(names):
            draw.text(
                (178, y + 32 + i * 36 + 28 * 1.2), name, font=label, fill="#E6EBF2", anchor="ls"
            )
        draw.text(
            (178, y + 80 + extra + 24 * 1.2),
            "UID " + role["uid"] if role else "—",
            font=uid_font,
            fill="#A1ADBD",
            anchor="ls",
        )
        active = state == "已订阅"
        color = "#85D9A2" if active else "#A8B3C3"
        draw.rounded_rectangle(
            (706, y + 39, 905, y + 80), radius=21, fill="#253D36" if active else "#293241"
        )
        draw.ellipse((721, y + 55, 729, y + 63), fill=color)
        badge = {"已订阅": "已设每日提醒", "未订阅": "未设每日提醒"}.get(state, state)
        bounds = draw.textbbox((0, 0), badge, font=small)
        draw.text((740, y + 59.5 - (bounds[1] + bounds[3]) / 2), badge, font=small, fill=color)
        y += height + 10
    draw.text(
        (32, y + 16), "今日日常仅查询已订阅游戏 · 全部完成也会推送", font=small, fill="#A8B3C3"
    )
    return png(canvas, y + 70)
