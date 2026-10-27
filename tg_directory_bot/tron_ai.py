"""AI reference prediction for the 单/双 page (OpenAI-compatible chat/completions relay).

The program computes the statistics itself and sends only those results as
compact text; the model answers a single character 单 or 双 by "feel". The
key is read from the server environment only: it is never logged, stored in
the database, or returned by any API.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

from .tron_draw import DOUBLE, DRAW_INTERVAL, SINGLE

logger = logging.getLogger(__name__)

WINDOWS = (10, 30, 50, 100, 300, 1000)
HISTORY_CAP = 50000
TIMEOUT_GRACE = 5.0  # hard stop on top of the HTTP timeout
SYSTEM_PROMPT = (
    "你是单双参考助手。下面是程序已经算好的单双统计结果，请凭你的直觉判断下一期开 单 还是 双。"
    "只回答一个字：单 或 双。不要理由，不要任何其它文字。"
)


def parse_pick(text) -> str | None:
    """First 单/双 in the reply, otherwise None (the reply is discarded)."""
    for char in str(text or ""):
        if char in (SINGLE, DOUBLE):
            return char
    return None


def _alternating(values: list[str]) -> int:
    best = run = 0
    for index, value in enumerate(values):
        run = run + 1 if index and values[index - 1] != value else 1
        best = max(best, run)
    return best


def _window_line(label: str, values: list[str]) -> str:
    n = len(values)
    singles = sum(1 for value in values if value == SINGLE)
    doubles = n - singles
    if not n:
        return f"{label}：无数据"
    streak = 1
    while streak < n and values[n - 1 - streak] == values[n - 1]:
        streak += 1
    return (
        f"{label}：单{singles}期({singles * 100 / n:.0f}%) 双{doubles}期({doubles * 100 / n:.0f}%)；"
        f"当前连{values[-1]}{streak}期；最长单连{_longest_side(values, SINGLE)}期，"
        f"最长双连{_longest_side(values, DOUBLE)}期；最长连跳(单双交替){_alternating(values)}期"
    )


def _longest_side(values: list[str], side: str) -> int:
    best = run = 0
    for value in values:
        run = run + 1 if value == side else 0
        best = max(best, run)
    return best


def build_stats_text(results: list[str]) -> str:
    """Compact statistics text for the prompt (oldest -> newest ``results``)."""
    results = list(results[-HISTORY_CAP:])
    lines = [_window_line(f"最近{size}期", results[-size:]) for size in WINDOWS if len(results) >= size]
    if len(results) < WINDOWS[0]:
        lines.append(_window_line("最近", results))
    lines.append(_window_line(f"全部历史（{len(results)}期）", results))
    lines.append("最近10期（旧→新）：" + "".join(results[-10:]))
    return "\n".join(lines)


def build_messages(results: list[str]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_stats_text(results) + "\n下一期开单还是双？只回答一个字。"},
    ]


def _payload(model: str, messages: list[dict]) -> dict:
    body = {"model": model, "messages": messages}
    if model.casefold().startswith(("gpt-5", "o1", "o3", "o4")):
        body["max_completion_tokens"] = 512  # reasoning models need room before the answer
    else:
        body["max_tokens"] = 5
        body["temperature"] = 0.7
    return body


async def request_pick(settings, results: list[str], *, transport=None) -> str | None:
    """One chat/completions call; returns 单/双 or None. Never logs headers/keys."""
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    timeout = float(settings.ai_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"},
            json=_payload(settings.ai_model, build_messages(results)),
        )
    response.raise_for_status()
    data = response.json()
    return parse_pick(data["choices"][0]["message"]["content"])


class AiPredictor:
    """Asks the model once per upcoming period, in the background, write-once."""

    def __init__(self, settings, store, *, transport=None, clock=time.monotonic):
        self.settings = settings
        self.store = store
        self.transport = transport
        self.clock = clock
        self.calls: deque[float] = deque()
        self.attempted_for = 0
        self.task: asyncio.Task | None = None
        self.stats = {"calls": 0, "stored": 0, "failed": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.tron_ai_enabled)

    def _allow_call(self) -> bool:
        now = self.clock()
        while self.calls and now - self.calls[0] > 3600:
            self.calls.popleft()
        if len(self.calls) >= int(self.settings.tron_ai_max_per_hour):
            return False
        self.calls.append(now)
        return True

    def schedule(self) -> bool:
        """Start the background request for the next draw (never raises, never blocks)."""
        try:
            if not self.enabled:
                return False
            newest = self.store.tron_draw_recent(1)
            if not newest:
                return False
            next_block = int(newest[0]["block_number"]) + DRAW_INTERVAL
            if next_block == self.attempted_for:  # one attempt per period, no retry storm
                return False
            if self.task is not None and not self.task.done():
                return False
            self.attempted_for = next_block
            if next_block in self.store.tron_ai_predictions(next_block, next_block):
                return False
            if not self._allow_call():
                logger.warning("TRON AI hourly call cap reached; skipping period %s", next_block)
                return False
            rows = self.store.tron_draw_recent(HISTORY_CAP)
            results = [str(row["result"]) for row in rows]
            self.task = asyncio.get_running_loop().create_task(self._run(next_block, results))
            return True
        except Exception:
            logger.exception("TRON AI scheduling failed")
            return False

    async def _run(self, next_block: int, results: list[str]) -> None:
        self.stats["calls"] += 1
        try:
            pick = await asyncio.wait_for(
                request_pick(self.settings, results, transport=self.transport),
                timeout=float(self.settings.ai_timeout_seconds) + TIMEOUT_GRACE,
            )
        except Exception as exc:
            self.stats["failed"] += 1
            status = getattr(getattr(exc, "response", None), "status_code", "")
            logger.warning("TRON AI prediction failed for %s: %s %s", next_block, type(exc).__name__, status)
            return
        if pick is None:
            self.stats["failed"] += 1
            logger.warning("TRON AI reply for %s had no 单/双; discarded", next_block)
            return
        try:
            if self.store.record_tron_ai_prediction(next_block, pick, self.settings.ai_model):
                self.stats["stored"] += 1
        except Exception:
            logger.exception("TRON AI prediction store failed")
