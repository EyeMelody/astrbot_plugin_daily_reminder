"""Dark, asset-light status cards with bounded layout and a text fallback."""

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .models import CN, GAMES

COLORS = {"genshin": "#CBA968", "starrail": "#A99CEF", "zzz": "#C8DE6A", "nte": "#78CDD8"}
FONT = Path(__file__).parent / "assets" / "MemoCJK-Regular.otf"
STATUS_COLORS = {"active": "#85D9A2", "inactive": "#F08086", "unknown": "#A8B3C3"}


def text_summary(notes):
    latest = max((n.observed_at for n in notes), default=0)
    stamp = datetime.fromtimestamp(latest, CN).strftime("%m-%d %H:%M")
    return (
        "今日日常\n\n"
        + "\n\n".join(n.text() for n in notes)
        + f"\n\n查询于 {stamp} · 北京时间\n便笺可能有延迟，以游戏内状态为准。"
    )


def draw_card(notes):
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    fonts = {size: ImageFont.truetype(str(FONT), size) for size in (18, 20, 23, 24, 28, 34)}
    fallback = {
        "latin": ImageFont.truetype(str(FONT.parent / "MemoSymbols.ttf"), 23),
        "thai": ImageFont.truetype(str(FONT.parent / "MemoThai.ttf"), 23),
    }
    width, margin = 960, 32
    canvas = Image.new("RGB", (width, 4096), "#10151E")
    draw = ImageDraw.Draw(canvas)

    def runs(text, size):
        current, chunk = None, ""
        for char in text:
            font = fonts[size]
            if size == 23:
                if char in "⸜⸝ᵕ":
                    font = fallback["latin"]
                elif char == "๑":
                    font = fallback["thai"]
            if current is not font and chunk:
                yield chunk, current
                chunk = ""
            current = font
            chunk += char
        if chunk:
            yield chunk, current

    def length(text, size):
        return sum(draw.textlength(chunk, font=font) for chunk, font in runs(text, size))

    def fit(text, size, max_width):
        text = str(text).replace("\n", " ")
        if length(text, size) <= max_width:
            return text
        while text and length(text + "…", size) > max_width:
            text = text[:-1]
        return text + "…"

    def put(x, y, text, size=23, color="#E6EBF2", limit=850):
        for chunk, font in runs(fit(text, size, limit), size):
            draw.text((x, y + size * 1.2), chunk, font=font, fill=color, anchor="ls")
            x += draw.textlength(chunk, font=font)

    def wrap(text, size, max_width):
        result, line = [], ""
        for char in text:
            if length(line + char, size) > max_width:
                result.append(line)
                line = ""
            line += char
        if line:
            result.append(line)
        return result

    put(margin, 24, "今日日常", 34)
    put(margin, 78, "二游日常备忘姬", 18, "#8F9BAD")
    y = 124
    for note in notes[:4]:
        color = COLORS[note.role.game]
        facts = [(False, f) for f in note.daily] + [(True, f) for f in note.weekly]
        summary_lines = wrap(note.summary, 23, 820)
        name_lines = wrap(note.role.name, 28, 705)
        extra_header = max(0, len(name_lines) - 1) * 36
        height = (
            212
            + extra_header
            + len(summary_lines) * 34
            + len(facts) * 46
            + (38 if note.weekly else 0)
        )
        draw.rounded_rectangle((margin, y, width - margin, y + height), radius=20, fill="#1C2432")
        draw.rounded_rectangle((margin, y + 26, margin + 5, y + 121), radius=2, fill=color)
        icon_path = FONT.parent / "icons" / f"{note.role.game}.jpg"
        if icon_path.is_file():
            with Image.open(icon_path) as source:
                icon = ImageOps.fit(
                    source.convert("RGB"), (96, 96), method=Image.Resampling.LANCZOS
                )
            mask = Image.new("L", (96, 96))
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, 95, 95), radius=20, fill=255)
            canvas.paste(icon, (58, y + 26), mask)
            icon.close()
            mask.close()
        for index, line in enumerate(name_lines):
            put(178, y + 32 + index * 36, line, 28, limit=705)
        put(178, y + 80 + extra_header, f"UID {note.role.uid}", 24, "#A1ADBD", limit=705)
        row_y = y + 164 + extra_header
        for line in summary_lines:
            put(58, row_y, line, 23, "#F1C481" if note.error else STATUS_COLORS[note.login_state])
            row_y += 34
        row_y += 12
        weekly_started = False
        for weekly, fact in facts:
            if weekly and not weekly_started:
                draw.line((58, row_y, 902, row_y), fill="#344054")
                put(58, row_y + 6, "本周", 18, "#8F9BAD")
                row_y += 38
                weekly_started = True
            put(58, row_y, fact.label, 20, "#A8B3C3", limit=250)
            put(330, row_y, fact.value, 23, limit=565)
            if fact.maximum and fact.current is not None:
                draw.rounded_rectangle((330, row_y + 35, 895, row_y + 39), radius=2, fill="#394558")
                bar_width = int(565 * min(1, max(0, fact.current / fact.maximum)))
                if bar_width > 0:
                    draw.rounded_rectangle(
                        (330, row_y + 35, 330 + bar_width, row_y + 39), radius=2, fill=color
                    )
            row_y += 46
        stamp = datetime.fromtimestamp(note.observed_at, CN).strftime("%m-%d %H:%M")
        put(58, y + height - 30, f"查询于 {stamp}", 18, "#738198")
        y += height + 20
    put(margin, y + 2, "北京时间 · 便笺可能有延迟，以游戏内状态为准", 18, "#8F9BAD")
    output = io.BytesIO()
    canvas.crop((0, 0, width, y + 55)).save(output, format="PNG", optimize=True)
    canvas.close()
    return output.getvalue()


def account_entries(account, reminders=None):
    reminders = reminders or {}
    result = []
    for game in GAMES:
        provider = account.get("nte", {}) if game == "nte" else account
        selected = provider.get("selected", {}).get(game)
        roles = [r for r in provider.get("roles", []) if r["game"] == game]
        for role in sorted(roles, key=lambda r: r["uid"] != selected):
            chosen = role["uid"] == selected
            result.append((role, chosen, reminders.get(game, 0) if chosen else 0))
    return result


def draw_accounts(account, reminders=None):
    """Same role header as the daily card, plus per-user reminder status."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    title_font = ImageFont.truetype(str(FONT), 32)
    font = ImageFont.truetype(str(FONT), 28)
    uid_font = ImageFont.truetype(str(FONT), 24)
    small = ImageFont.truetype(str(FONT), 20)
    rows = account_entries(account, reminders)
    canvas = Image.new("RGB", (960, 260 + len(rows) * 230), "#10151E")
    draw = ImageDraw.Draw(canvas)
    draw.text((32, 24), "已绑定的游戏", font=title_font, fill="#E6EBF2")
    y = 90
    for role, selected, count in rows:
        game = role["game"]
        name_lines, line = [], ""
        for char in role["name"].replace("\n", " "):
            if draw.textlength(line + char, font=font) > 485:
                name_lines.append(line)
                line = ""
            line += char
        name_lines.append(line)
        extra = max(0, len(name_lines) - 1) * 36
        height = 158 + extra
        draw.rounded_rectangle((32, y, 928, y + height - 10), radius=16, fill="#1C2432")
        draw.rounded_rectangle((32, y + 26, 37, y + 121), radius=2, fill=COLORS[game])
        with Image.open(FONT.parent / "icons" / f"{game}.jpg") as source:
            icon = ImageOps.fit(source.convert("RGB"), (96, 96), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (96, 96))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 95, 95), radius=20, fill=255)
        canvas.paste(icon, (58, y + 26), mask)
        mask.close()
        icon.close()
        for index, line in enumerate(name_lines):
            draw.text(
                (178, y + 32 + index * 36 + 28 * 1.2), line, font=font, fill="#E6EBF2", anchor="ls"
            )
        draw.text(
            (178, y + 80 + extra + 24 * 1.2),
            "UID " + role["uid"],
            font=uid_font,
            fill="#A1ADBD",
            anchor="ls",
        )
        label = "已设每日提醒" if count else "未设每日提醒" if selected else "待选择角色"
        color = "#85D9A2" if count else "#A8B3C3" if selected else "#F1C481"
        draw.rounded_rectangle(
            (706, y + 39, 904, y + 79), radius=20, fill="#253A36" if count else "#2A3342"
        )
        draw.ellipse((720, y + 55, 728, y + 63), fill=color)
        draw.text((740, y + 43), label, font=small, fill=color)
        if count:
            draw.text((740, y + 88), f"{count} 个会话", font=small, fill="#A8B3C3")
        y += height
    draw.text((32, y + 16), "提醒标志统计你在所有会话设置的每日提醒", font=small, fill="#A8B3C3")
    draw.text((32, y + 50), "多个角色时：/备忘姬 角色 游戏 UID", font=small, fill="#A8B3C3")
    output = io.BytesIO()
    canvas.crop((0, 0, 960, y + 100)).save(output, format="PNG")
    canvas.close()
    return output.getvalue()


class Renderer:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily-card")
        self.lock = asyncio.Lock()
        self.closed = False

    async def render(self, notes):
        return await self._render(draw_card, notes)

    async def accounts(self, account, reminders=None):
        return await self._render(lambda value: draw_accounts(value, reminders), account)

    async def help(self):
        from .ui import draw_help

        return await self._render(draw_help, None)

    async def status(self, account, subscription):
        from .ui import draw_status

        return await self._render(draw_status, (account, subscription))

    async def _render(self, draw, value):
        async with self.lock:
            if self.closed:
                raise RuntimeError("renderer closed")
            future = asyncio.get_running_loop().run_in_executor(self.pool, draw, value)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future
                raise

    async def close(self):
        async with self.lock:
            self.closed = True
            self.pool.shutdown(wait=True, cancel_futures=True)
