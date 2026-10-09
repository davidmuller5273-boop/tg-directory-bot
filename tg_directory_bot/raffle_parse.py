"""抽奖自动识别：把粘贴/转发来的抽奖公告解析成「样板通用抽奖」的 9 项答案。

支持本机器人自己的抽奖公告格式（📜 规则 / 标题 / ├活动类型 … / [如何参与？]），
也容忍松散文字：开奖时间、定时开奖、关注频道、最低发言、助推、最少参与、奖品。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .time_utils import BEIJING_TZ, beijing_now

TREE_CHARS = "├└│┃┣┗|-•·*>　 \t"
RULES_HEADING_RE = re.compile(r"^[📜📋📌📝*#【\[「<《\s]*规则\s*[】\]」>》]?\s*[:：]?\s*(.*)$")
HOW_TO_RE = re.compile(r"^[\[【(（]?\s*如何参与\s*[?？]?\s*[\]】)）]?\s*[:：]?\s*(.*)$")

FIELD_ALIASES = {
    "type": ("活动类型", "抽奖类型", "类型"),
    "draw": ("定时开奖", "开奖时间", "开奖"),
    "keyword": ("参与关键词", "关键词", "口令"),
    "channel": ("关注频道", "频道"),
    "messages": ("最低发言", "发言要求", "发言"),
    "boosts": ("最低助推", "助推"),
    "min_participants": ("最少参与", "最低参与", "最少人数"),
    "entries": ("已参与", "参与人数"),
    "prizes": ("奖品列表", "奖品", "奖励"),
}
_FIELD_RE = re.compile(
    r"^(" + "|".join(sorted({a for v in FIELD_ALIASES.values() for a in v}, key=len, reverse=True))
    + r")\s*[:：]?\s*(.*)$"
)
_ALIAS_TO_KEY = {alias: key for key, aliases in FIELD_ALIASES.items() for alias in aliases}
HEADER_ID_RE = re.compile(r"^(?:[🎁🎉]\s*)?(?:通用)?抽奖\s*#\s*\d+$")
MARKERS = ("活动类型", "定时开奖", "开奖时间", "奖品列表", "如何参与", "最低发言", "关注频道",
           "最少参与", "最低助推", "奖品")


@dataclass
class ParsedRaffle:
    title: str = ""
    rules: list[str] = field(default_factory=list)
    raffle_type: str = "通用抽奖"
    draw_at: datetime | None = None       # Beijing aware datetime (already in future)
    draw_adjusted: bool = False           # past time moved forward
    keyword: str = ""
    channel: str = ""
    messages: int = 0
    boosts: int = 0
    min_participants: int = 0
    prizes: list[tuple[int, str]] = field(default_factory=list)
    how_to: str = ""
    daily_hint: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def winner_count(self) -> int:
        return sum(q for q, _ in self.prizes)

    def draw_text(self) -> str:
        return self.draw_at.strftime("%Y-%m-%d %H:%M:%S") if self.draw_at else ""

    def missing(self) -> list[str]:
        out = []
        if not self.draw_at:
            out.append("开奖时间")
        if not self.prizes:
            out.append("奖品")
        return out


def looks_like_raffle(text: str) -> bool:
    raw = text or ""
    hits = sum(1 for marker in MARKERS if marker in raw)
    return hits >= 2 and ("奖" in raw)


def _strip_tree(line: str) -> str:
    return line.strip().lstrip(TREE_CHARS).strip()


def _int(value: str) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else 0


_PRIZE_LABEL_X_QTY = re.compile(r"^(.+?)\s+[xX×]\s*(\d+)\s*(?:份|个|名|人)?$")
_PRIZE_LABEL_QTY = re.compile(r"^(.+?)\s*[xX×*]\s*(\d+)\s*(?:份|个|名|人)?$")
_PRIZE_QTY_LABEL = re.compile(r"^(\d+)\s*(?:份|个|名|人)?\s*[xX×*]\s*(.+)$")
_PRIZE_LABEL_COUNT = re.compile(r"^(.+?)\s+(\d+)\s*(?:份|个|名|人)$")


def parse_prize(text: str) -> tuple[int, str] | None:
    raw = _strip_tree(text)
    raw = re.sub(r"^\d+\s*[.、)）]\s*", "", raw) if re.match(r"^\d+\s*[.、)）]\s*\D", raw) else raw
    if not raw:
        return None
    # 「5*88RMB x 5」：末尾的 “ x 数量” 优先（机器人公告格式），奖品名里可以带 *
    for regex, order in ((_PRIZE_LABEL_X_QTY, "lq"), (_PRIZE_QTY_LABEL, "ql"),
                         (_PRIZE_LABEL_QTY, "lq"), (_PRIZE_LABEL_COUNT, "lq")):
        match = regex.match(raw)
        if match:
            a, b = match.group(1).strip(), match.group(2).strip()
            qty, label = (int(a), b) if order == "ql" else (int(b), a)
            if qty > 0 and label:
                return qty, label
    return 1, raw


def parse_draw_time(value: str, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """Return (Beijing datetime in the future, adjusted?)."""
    now = now or beijing_now()
    raw = (value or "").replace("北京时间", "").replace("+0800", "").replace("UTC+8", "")
    raw = raw.replace("：", ":").replace("/", "-").replace(".", "-").strip()
    raw = re.sub(r"\s+", " ", raw)
    full = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    md = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    cn = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:点时](\d{2})?", raw)
    clock = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?", raw) or re.search(r"(\d{1,2})点(?:(\d{1,2})分?)?", raw)
    target = None
    try:
        if full:
            y, mo, d, h, mi, s = full.groups()
            target = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=BEIJING_TZ)
        elif md:
            mo, d, h, mi, s = md.groups()
            target = datetime(now.year, int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=BEIJING_TZ)
        elif cn:
            mo, d, h, mi = cn.groups()
            target = datetime(now.year, int(mo), int(d), int(h), int(mi or 0), tzinfo=BEIJING_TZ)
        elif clock:
            groups = clock.groups()
            h, mi = int(groups[0]), int(groups[1] or 0)
            s = int(groups[2] or 0) if len(groups) > 2 and groups[2] else 0
            target = now.replace(hour=h, minute=mi, second=s, microsecond=0)
    except ValueError:
        return None, False
    if target is None:
        return None, False
    if target > now:
        return target, False
    # 已过去：顺延到下一次同一时刻（今天还没到就今天，否则明天）
    candidate = now.replace(hour=target.hour, minute=target.minute, second=target.second, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate, True


def parse(text: str, now: datetime | None = None) -> ParsedRaffle:
    result = ParsedRaffle()
    lines = [line.rstrip() for line in (text or "").replace("\r", "").split("\n")]
    index = 0
    # 跳过开头空行和「抽奖 #37」这类编号行
    while index < len(lines) and (
        not lines[index].strip() or HEADER_ID_RE.match(lines[index].strip())
    ):
        index += 1
    # 📜 规则：…（直到空行）
    if index < len(lines):
        match = RULES_HEADING_RE.match(lines[index].strip())
        if match:
            first = match.group(1).strip()
            if first:
                result.rules.append(first)
            index += 1
            while index < len(lines) and lines[index].strip():
                result.rules.append(lines[index].strip())
                index += 1
    in_prizes = False
    how_to_lines: list[str] = []
    in_how_to = False
    for line in lines[index:]:
        stripped = line.strip()
        if in_how_to:
            if stripped:
                how_to_lines.append(stripped)
            continue
        if not stripped:
            in_prizes = False
            continue
        how = HOW_TO_RE.match(_strip_tree(stripped))
        if how:
            in_how_to = True
            in_prizes = False
            if how.group(1).strip():
                how_to_lines.append(how.group(1).strip())
            continue
        body = _strip_tree(stripped)
        field_match = _FIELD_RE.match(body)
        if field_match:
            key = _ALIAS_TO_KEY[field_match.group(1)]
            value = field_match.group(2).strip()
            in_prizes = False
            if key == "type":
                result.raffle_type = value or result.raffle_type
            elif key == "draw":
                result.draw_at, result.draw_adjusted = parse_draw_time(value, now)
                if "每日" in value or "每天" in value:
                    result.daily_hint = True
            elif key == "keyword":
                result.keyword = value
            elif key == "channel":
                found = re.search(r"(@[A-Za-z0-9_]{4,}|https?://t\.me/[A-Za-z0-9_]{4,}|-100\d{5,})", value)
                if found:
                    channel = found.group(1)
                    if channel.startswith("http"):
                        channel = "@" + channel.rsplit("/", 1)[-1]
                    result.channel = channel
            elif key == "messages":
                result.messages = _int(value)
            elif key == "boosts":
                result.boosts = _int(value)
            elif key == "min_participants":
                result.min_participants = _int(value)
            elif key == "entries":
                pass  # 已参与人数：忽略
            elif key == "prizes":
                in_prizes = True
                if value:
                    prize = parse_prize(value)
                    if prize:
                        result.prizes.append(prize)
            continue
        if in_prizes:
            prize = parse_prize(stripped)
            if prize:
                result.prizes.append(prize)
            continue
        if not result.title:
            result.title = body or stripped
        else:
            result.warnings.append(f"未识别：{stripped[:40]}")
    result.how_to = "\n".join(how_to_lines).strip()
    joined = "\n".join([result.title, *result.rules, result.how_to])
    if any(word in joined for word in ("每日", "每天", "当天发言")):
        result.daily_hint = True
    if result.raffle_type and "通用" not in result.raffle_type:
        result.warnings.append(f"活动类型「{result.raffle_type}」将按通用抽奖创建")
    return result


def to_answers(parsed: ParsedRaffle, recur: bool | None = None) -> list[str]:
    """9 answers for the raffle_pro wizard / build_raffle_extras_from_pro."""
    if recur is None:
        recur = parsed.daily_hint
    conditions = []
    if parsed.channel:
        conditions.append(f"频道 {parsed.channel}")
    if parsed.messages:
        conditions.append(f"发言 {parsed.messages}")
    if parsed.keyword:
        conditions.append(f"关键词 {parsed.keyword}")
    if parsed.boosts:
        conditions.append(f"助推 {parsed.boosts}")
    if len(parsed.prizes) > 1:
        prize_text = "\n".join(f"{q}*{label}" for q, label in parsed.prizes)
    elif parsed.prizes:
        q, label = parsed.prizes[0]
        prize_text = f"{q} | {label}"
    else:
        prize_text = ""
    return [
        parsed.title or "通用抽奖",
        "\n".join(parsed.rules) if parsed.rules else "无",
        parsed.draw_at.strftime("%Y-%m-%d %H:%M:%S") if parsed.draw_at else "",
        "立即",
        "\n".join(conditions) if conditions else "无",
        parsed.how_to or "点击下方按钮参与抽奖。",
        prize_text,
        "是" if recur else "否",
        str(parsed.min_participants),
    ]


def summary_text(parsed: ParsedRaffle, recur: bool, group_label: str = "") -> str:
    lines = ["🧾 已识别抽奖信息，请确认：", ""]
    if group_label:
        lines.append(f"发布到：{group_label}")
    lines.append(f"标题：{parsed.title or '通用抽奖'}")
    if parsed.rules:
        lines.append("规则：")
        lines.extend(f"  {rule}" for rule in parsed.rules)
    lines.append(f"类型：通用抽奖")
    if parsed.draw_at:
        note = "（原时间已过，已顺延）" if parsed.draw_adjusted else ""
        lines.append(f"开奖时间：{parsed.draw_text()}{note}")
    else:
        lines.append("开奖时间：未识别")
    lines.append(f"每日重复：{'是' if recur else '否'}" + ("（检测到“每日”，可点按钮切换）" if parsed.daily_hint else ""))
    if parsed.channel:
        lines.append(f"关注频道：{parsed.channel}")
    if parsed.messages:
        lines.append(f"最低发言：{parsed.messages} 条")
    if parsed.boosts:
        lines.append(f"最低助推：{parsed.boosts}")
    if parsed.keyword:
        lines.append(f"参与关键词：{parsed.keyword}")
    if parsed.min_participants:
        lines.append(f"最少参与：{parsed.min_participants} 人")
    if parsed.prizes:
        lines.append(f"奖品（共 {parsed.winner_count} 名）：")
        lines.extend(f"  {label} x {q}" for q, label in parsed.prizes)
    else:
        lines.append("奖品：未识别")
    if parsed.how_to:
        lines.append(f"如何参与：{parsed.how_to}")
    missing = parsed.missing()
    if missing:
        lines.extend(["", f"⚠️ 缺少：{'、'.join(missing)}，请点「✏️ 修改」补充。"])
    for warning in parsed.warnings[:3]:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)
