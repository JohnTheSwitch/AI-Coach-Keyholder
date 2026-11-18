"""
visuals/cards.py

Owner, 2026-11-17 ("I want all of these"): the pictures, drawn locally with Pillow; nothing is sent anywhere to be drawn.

- `render(card)`: PNG bytes for a card spec from an EmbedSpec (`{"type": "task", ...}` or `{"type": "week", ...}`); None for anything
  it cannot draw, so a bad spec costs the picture, never the message.
- The bot's Discord avatar and banner are the owner's own design and are never set by code (owner, 2026-11-18).

Never drawn, by design: the Trust score or level, or anything about when the release comes.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from visuals.style import COLORS

__all__ = ["render", "STATE_COLORS"]

logger = logging.getLogger("ai_coach_keyholder.visuals")

BG_TOP, BG_BOTTOM = (34, 35, 48), (22, 22, 31)
TEXT, MUTED, TRACK = (240, 240, 245), (160, 162, 180), (60, 62, 80)
GOLD = (241, 196, 15)
STATE_COLORS = {"clean": (46, 204, 113), "missed": (231, 76, 60), "vacation": (149, 165, 166), "none": None}
_FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"))
_FONT_FILES = {True: ("segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"), False: ("segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf")}


def _rgb(name: str | None) -> tuple[int, int, int]:
    value = COLORS.get(name or "", COLORS["blue"])
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for folder in _FONT_DIRS:
        for name in _FONT_FILES[bold]:
            path = folder / name
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
    return ImageFont.load_default(size=size)


def _canvas(width: int, height: int, top=BG_TOP, bottom=BG_BOTTOM):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (width, height), bottom)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line([(0, y), (width, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return image, draw


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _padlock(draw, cx: float, cy: float, size: float, color, *, keyhole=BG_BOTTOM, open_: bool = False) -> None:
    """A padlock centred on (cx, cy), `size` tall."""
    body_w, body_h = size * 0.78, size * 0.55
    body_top = cy - size / 2 + size * 0.45
    shackle_w, thick = size * 0.52, max(2, size * 0.1)
    left = cx - shackle_w / 2
    lift = size * 0.14 if open_ else 0
    draw.arc([left, cy - size / 2 - lift, left + shackle_w, cy - size / 2 + shackle_w - lift], 180, 360, fill=color, width=round(thick))
    draw.line([(left + thick / 2, cy - size / 2 + shackle_w / 2 - lift), (left + thick / 2, body_top + 2 - (lift if open_ else 0))],
              fill=color, width=round(thick))
    right = left + shackle_w - thick / 2
    draw.line([(right, cy - size / 2 + shackle_w / 2 - lift), (right, body_top + 2)], fill=color, width=round(thick))
    draw.rounded_rectangle([cx - body_w / 2, body_top, cx + body_w / 2, body_top + body_h], radius=size * 0.08, fill=color)
    r = size * 0.07
    hole_y = body_top + body_h * 0.42
    draw.ellipse([cx - r, hole_y - r, cx + r, hole_y + r], fill=keyhole)
    draw.polygon([(cx - r * 0.55, hole_y), (cx + r * 0.55, hole_y), (cx + r * 0.9, hole_y + body_h * 0.33), (cx - r * 0.9, hole_y + body_h * 0.33)],
                 fill=keyhole)


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and " ".join(lines) != " ".join(words):
        while lines[-1] and draw.textlength(lines[-1] + "...", font=font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


# ---------------------------------------------------------------------------------------------------------------------- task card

def _task(card: dict) -> bytes:
    width, height = 900, 260
    image, draw = _canvas(width, height)
    accent = _rgb(card.get("color"))
    draw.rectangle([0, 0, 12, height], fill=accent)
    draw.text((48, 34), str(card.get("label") or "TASK")[:40], font=_font(22, bold=True), fill=accent)
    headline = _font(40, bold=True)
    y = 74
    for line in _wrap(draw, str(card.get("headline") or ""), headline, 640, 2):
        draw.text((48, y), line, font=headline, fill=TEXT)
        y += 52
    note = str(card.get("note") or "")[:60]
    if note:
        font = _font(22, bold=True)
        w = draw.textlength(note, font=font)
        draw.rounded_rectangle([48, height - 66, 48 + w + 32, height - 26], radius=20, fill=(60, 52, 20))
        draw.text((64, height - 61), note, font=font, fill=GOLD)
    _padlock(draw, width - 110, height / 2, 130, accent)
    return _png(image)


# ---------------------------------------------------------------------------------------------------------------------- week card

def _week(card: dict) -> bytes:
    days = list(card.get("days") or [])[:7]
    tiles = list(card.get("tiles") or [])[:5]
    width, height = 900, 400 if tiles else 230
    image, draw = _canvas(width, height)
    accent = _rgb("blue")
    draw.rectangle([0, 0, width, 8], fill=accent)
    draw.text((40, 30), str(card.get("title") or "Your week")[:60], font=_font(34, bold=True), fill=TEXT)
    label_font, step = _font(20, bold=True), (width - 80) / 7
    for i, day in enumerate(days):
        cx, cy, r = 40 + step * i + step / 2, 140, 30
        color = STATE_COLORS.get(day.get("state"))
        if color is None:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TRACK, width=4)
        else:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        label = str(day.get("label") or "")[:3]
        draw.text((cx - draw.textlength(label, font=label_font) / 2, cy + r + 8), label, font=label_font, fill=MUTED)
    if tiles:
        tile_w, gap, top = (width - 80 - 16 * (len(tiles) - 1)) / len(tiles), 16, 250
        name_font, value_font = _font(15, bold=True), _font(34, bold=True)
        for i, tile in enumerate(tiles):
            x = 40 + i * (tile_w + gap)
            draw.rounded_rectangle([x, top, x + tile_w, top + 120], radius=14, fill=(42, 44, 60))
            draw.text((x + 16, top + 12), str(tile.get("label") or "")[:16].upper(), font=name_font, fill=MUTED)
            done, total = tile.get("done"), tile.get("total")
            value = str(tile.get("value") if tile.get("value") is not None else f"{done}/{total}")[:10]
            draw.text((x + 16, top + 38), value, font=value_font, fill=TEXT)
            if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
                frac = max(0.0, min(1.0, done / total))
                bar_top = top + 92
                draw.rounded_rectangle([x + 16, bar_top, x + tile_w - 16, bar_top + 10], radius=5, fill=TRACK)
                if frac > 0:
                    color = STATE_COLORS["clean"] if frac >= 0.999 else (GOLD if frac >= 0.5 else STATE_COLORS["missed"])
                    draw.rounded_rectangle([x + 16, bar_top, x + 16 + (tile_w - 32) * frac, bar_top + 10], radius=5, fill=color)
    return _png(image)


_RENDERERS = {"task": _task, "week": _week}


def render(card: dict | None) -> bytes | None:
    if not isinstance(card, dict) or card.get("type") not in _RENDERERS:
        return None
    try:
        return _RENDERERS[card["type"]](card)
    except Exception:
        logger.exception("A %s card could not be drawn; the message goes without it.", card.get("type"))
        return None


