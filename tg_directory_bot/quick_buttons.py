"""快捷发布按钮：批量输入解析、指定位置、布局计算（纯函数，便于测试）。

输入格式（与说明一致）：
    按钮文字 - https://链接
    同一行多个按钮用 | 分隔，换行 = 新的一行
    可选颜色后缀：按钮文字 - https://链接 - 绿色（蓝色/绿色/红色/默认）
    按钮文字最前面的会员自定义表情会作为按钮图标
位置：在面板里点选一个已有按钮，再选 加在它前面/后面/上方新一行/下方新一行
（parse_position 仅作内部备用）
"""

from __future__ import annotations

import re

MAX_BUTTONS = 100
MAX_PER_ROW = 8

COLOR_WORDS = {
    "默认": "default", "蓝色": "primary", "蓝": "primary", "绿色": "success", "绿": "success",
    "红色": "danger", "红": "danger",
}

_URL_RE = re.compile(r"^(?P<text>.*?)\s*[-—－]\s*(?P<url>(?:https?|tg)://\S+)(?:\s*[-—－]\s*(?P<color>\S+))?\s*$", re.S)

FORMAT_HINT = (
    "格式：按钮文字 - https://链接\n"
    "同一行多个按钮用 | 分隔，换行表示新的一行，例如：\n"
    "官网 - https://example.com | 客服 - https://t.me/xxx\n"
    "频道 - https://t.me/yyy - 绿色\n"
    "可选颜色：蓝色/绿色/红色；按钮文字最前面的会员表情会作为图标。"
)
POSITION_HINT = (
    "发送位置：底部 / 顶部 / 第2行后（新起一行）/ 第2行（加到该行末尾）/ 第2行第1个（插到该行第1个位置，也可写 2-1）"
)


def looks_bulk(text: str) -> bool:
    return bool(re.search(r"[-—－]\s*(?:https?|tg)://", str(text or "")))


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse_buttons(text: str, entities=None) -> list[list[dict]]:
    """Return rows of {'text','url','color','custom_emoji_id'}; raise ValueError on bad input."""
    if not str(text or "").strip():
        raise ValueError("请发送按钮，" + FORMAT_HINT)
    # custom emoji entities by UTF-16 offset
    emoji_at: dict[int, tuple[int, str]] = {}
    for entity in entities or ():
        if str(getattr(entity, "type", "")) in {"custom_emoji", "MessageEntityType.CUSTOM_EMOJI"} and getattr(entity, "custom_emoji_id", None):
            emoji_at[int(entity.offset)] = (int(entity.length), str(entity.custom_emoji_id))
    rows: list[list[dict]] = []
    cursor = 0  # python index in text
    for line in str(text).split("\n"):
        line_start = cursor
        cursor += len(line) + 1
        if not line.strip():
            continue
        row = []
        seg_start = line_start
        for segment in line.split("|"):
            seg_offset = seg_start
            seg_start += len(segment) + 1
            stripped = segment.strip()
            if not stripped:
                continue
            match = _URL_RE.match(stripped)
            if not match:
                raise ValueError(f"无法识别：{stripped[:40]}\n" + FORMAT_HINT)
            label = match.group("text").strip()
            color_word = (match.group("color") or "").strip()
            if color_word and color_word not in COLOR_WORDS:
                raise ValueError(f"颜色只能是 蓝色/绿色/红色/默认：{color_word}")
            lead = seg_offset + (len(segment) - len(segment.lstrip()))
            custom = ""
            hit = emoji_at.get(_utf16_len(str(text)[:lead]))
            if hit:
                length, custom = hit
                # strip the fallback emoji characters (UTF-16 length) from the label
                consumed, i = 0, 0
                while i < len(label) and consumed < length:
                    consumed += _utf16_len(label[i])
                    i += 1
                label = label[i:].strip() or " "
            if not label.strip() and not custom:
                raise ValueError("按钮文字不能为空")
            if len(label) > 64:
                raise ValueError("按钮文字最多64个字")
            row.append({
                "text": label, "url": match.group("url"),
                "color": COLOR_WORDS.get(color_word, "default"), "custom_emoji_id": custom,
            })
        if len(row) > MAX_PER_ROW:
            raise ValueError(f"每行最多 {MAX_PER_ROW} 个按钮")
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("请发送按钮，" + FORMAT_HINT)
    return rows


_NUM = r"(\d+)"


def parse_position(text: str) -> tuple:
    """('bottom',) | ('top',) | ('after', row) | ('at', row, col|None). Rows/cols are 1-based."""
    raw = str(text or "").strip().replace(" ", "")
    if raw in {"", "底部", "最后", "末尾", "底部新行", "最下面"}:
        return ("bottom",)
    if raw in {"顶部", "最前", "顶部新行", "最上面", "开头"}:
        return ("top",)
    m = re.fullmatch(r"第?" + _NUM + r"行[之以]?后(?:新行|新起一行)?", raw)
    if m:
        return ("after", int(m.group(1)))
    m = re.fullmatch(r"第?" + _NUM + r"行第?" + _NUM + r"个?", raw) or re.fullmatch(_NUM + r"[-,，.、]" + _NUM, raw)
    if m:
        return ("at", int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"第?" + _NUM + r"行(?:末尾|最后)?", raw)
    if m:
        return ("at", int(m.group(1)), None)
    raise ValueError("无法识别位置。" + POSITION_HINT)


SIDES = {
    "before": "⬅️ 加在它前面", "after": "➡️ 加在它后面",
    "above": "⬆️ 上方新一行", "below": "⬇️ 下方新一行",
}


def anchor_where(layout: list[list], anchor, side: str) -> tuple:
    """Translate 'next to an existing button' into a row/column position."""
    for r, row in enumerate(layout, 1):
        if anchor in row:
            c = row.index(anchor) + 1
            if side == "before":
                return ("at", r, c)
            if side == "after":
                return ("at", r, c + 1)
            if side == "above":
                return ("after", r - 1)
            if side == "below":
                return ("after", r)
            raise ValueError("未知位置")
    raise ValueError("这个按钮已不存在，请重新选择")


def insert_rows(layout: list[list], new_rows: list[list], where: tuple) -> list[list]:
    """Insert ``new_rows`` (rows of items) into ``layout`` (rows of items) at ``where``."""
    if where and where[0] == "anchor":
        where = anchor_where(layout, where[1], where[2])
    layout = [list(row) for row in layout]
    new_rows = [list(row) for row in new_rows if row]
    kind = where[0]
    if kind == "bottom":
        result = layout + new_rows
    elif kind == "top":
        result = new_rows + layout
    elif kind == "after":
        n = int(where[1])
        if not 0 <= n <= len(layout):
            raise ValueError(f"当前只有 {len(layout)} 行按钮")
        result = layout[:n] + new_rows + layout[n:]
    elif kind == "at":
        n, col = int(where[1]), where[2]
        if n == len(layout) + 1:
            return layout + new_rows
        if not 1 <= n <= len(layout):
            raise ValueError(f"当前只有 {len(layout)} 行按钮（可填第{len(layout) + 1}行新起一行）")
        target = layout[n - 1]
        index = len(target) if col is None else int(col) - 1
        if not 0 <= index <= len(target):
            raise ValueError(f"第{n}行只有 {len(target)} 个按钮，可填第1-{len(target) + 1}个")
        first, rest = new_rows[0], new_rows[1:]
        layout[n - 1] = target[:index] + first + target[index:]
        result = layout[:n] + rest + layout[n:]
    else:
        raise ValueError("未知位置")
    for row in result:
        if len(row) > MAX_PER_ROW:
            raise ValueError(f"每行最多 {MAX_PER_ROW} 个按钮")
    if sum(len(row) for row in result) > MAX_BUTTONS:
        raise ValueError(f"每条消息最多 {MAX_BUTTONS} 个按钮")
    return result


def legacy_rows(buttons) -> list[list]:
    """Old layout: long = own row, two shorts share a row."""
    rows, short = [], []
    for button in buttons:
        if str(button["width"] or "long") == "short":
            short.append(button)
            if len(short) == 2:
                rows.append(short)
                short = []
            continue
        if short:
            rows.append(short)
            short = []
        rows.append([button])
    if short:
        rows.append(short)
    return rows


def layout_rows(buttons) -> list[list]:
    """Rows of button rows. Buttons with row_no>=0 use their explicit row; the rest
    (never positioned) follow with the legacy long/short pairing."""
    explicit: dict[int, list] = {}
    legacy = []
    for button in buttons:
        keys = button.keys() if hasattr(button, "keys") else ()
        row_no = int(button["row_no"]) if "row_no" in keys and button["row_no"] is not None else -1
        if row_no >= 0:
            explicit.setdefault(row_no, []).append(button)
        else:
            legacy.append(button)
    return [explicit[k] for k in sorted(explicit)] + legacy_rows(legacy)


def layout_text(rows) -> list[str]:
    if not rows:
        return ["  暂无"]
    return [
        f"  第{i}行：" + " ".join(f"[#{b['id']} {b['text']}]" for b in row)
        for i, row in enumerate(rows, 1)
    ]
