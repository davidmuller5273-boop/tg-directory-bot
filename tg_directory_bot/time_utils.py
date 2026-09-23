from __future__ import annotations

import re

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def configure_beijing_timezone() -> None:
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_now_text() -> str:
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


def utc_after_minutes_text(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def beijing_datetime_to_utc_text(value: str, *, allow_past: bool = False) -> str:
    """Parse Beijing wall time into UTC text ``YYYY-MM-DD HH:MM:SS``.

    By default the result must be strictly later than now (开奖/定时).
    Pass ``allow_past=True`` for stats/activity start times that may be earlier.
    """
    raw = " ".join(value.strip().split())
    # Normalize common separators / fullwidth punctuation users type on phones.
    raw = (
        raw.replace("/", "-")
        .replace(".", "-")
        .replace("：", ":")
        .replace("／", "-")
    )
    # Zero-pad 2026-9-7 9:05 -> 2026-09-07 09:05
    match = re.fullmatch(
        r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        raw,
    )
    if match:
        year, month, day, hour, minute, second = match.groups()
        parts = []
        if year:
            parts.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
        else:
            parts.append(f"{int(month):02d}-{int(day):02d}")
        if hour is not None:
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if second is not None:
                clock += f":{int(second):02d}"
            parts.append(clock)
        raw = " ".join(parts)
    elif re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", raw):
        hour, minute, *rest = raw.split(":")
        raw = f"{int(hour):02d}:{int(minute):02d}" + (f":{int(rest[0]):02d}" if rest else "")
    parsed = None
    kind = ""
    patterns = (
        ("%Y-%m-%d %H:%M:%S", "absolute"),
        ("%Y-%m-%d %H:%M", "absolute"),
        ("%Y-%m-%d", "date"),
        ("%m-%d %H:%M:%S", "md"),
        ("%m-%d %H:%M", "md"),
        ("%H:%M:%S", "clock"),
        ("%H:%M", "clock"),
    )
    for pattern, pattern_kind in patterns:
        try:
            candidate = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        now = beijing_now()
        kind = pattern_kind
        if pattern_kind == "absolute":
            parsed = candidate.replace(tzinfo=BEIJING_TZ)
        elif pattern_kind == "date":
            parsed = candidate.replace(
                hour=0, minute=0, second=0, tzinfo=BEIJING_TZ
            )
        elif pattern_kind == "md":
            parsed = candidate.replace(year=now.year, tzinfo=BEIJING_TZ)
        else:
            parsed = candidate.replace(
                year=now.year, month=now.month, day=now.day, tzinfo=BEIJING_TZ
            )
        break
    if parsed is None:
        raise ValueError("时间格式应为：2026-08-27 21:30、08-27 21:30 或 21:30")
    now = beijing_now()
    if parsed <= now and not allow_past:
        if kind == "clock":
            parsed += timedelta(days=1)
        elif kind == "md":
            parsed = parsed.replace(year=now.year + 1)
        elif kind == "date" and parsed.date() == now.date():
            # "今天" as a start date — treat as now (caller usually wants allow_past)
            parsed = now + timedelta(seconds=1)
        else:
            raise ValueError("开奖时间必须晚于当前时间")
        if parsed <= now:
            raise ValueError("开奖时间必须晚于当前时间")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def format_beijing_time(value: object, suffix: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    rendered = parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return f"{rendered} 北京时间" if suffix else rendered


def format_beijing_timestamp_ms(value: int | None) -> str:
    if not value:
        return "链上未提供"
    rendered = datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(BEIJING_TZ)
    return rendered.strftime("%Y-%m-%d %H:%M:%S")


def format_beijing_timestamp(value: int | None) -> str:
    if not value:
        return "时间未提供"
    rendered = datetime.fromtimestamp(value, timezone.utc).astimezone(BEIJING_TZ)
    return rendered.strftime("%Y-%m-%d %H:%M:%S")
