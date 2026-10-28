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

WINDOWS = (10, 30, 50, 100, 300, 500, 1000)
AI_SELF_WINDOW = 10   # how many of the AI's own latest (already drawn) answers are listed back to it
AI_HABIT_WINDOW = 30  # how many are used for the 跟龙/断龙 habit counts
FOLLOW_BREAK_WINDOWS = (1000, 500, 300, 100)  # recent windows for the 跟龙/断龙 and 连跳 continue/break numbers
HISTORY_CAP = 50000
TIMEOUT_GRACE = 5.0  # hard stop on top of the HTTP timeout
SYSTEM_PROMPT = (
    "你是单双参考助手。下面是程序已经算好的单双统计结果（全部来自开奖之前的数据），请综合判断下一期开 单 还是 双。\n"
    "判断要求：\n"
    "1. 每一期都要把两种可能摆在一起公平比较：「龙继续（跟龙）」和「龙断开（断龙）」。"
    "数据里并排给出：当前连龙长度下，历史上龙继续和龙断开各多少次、各占多少百分比，"
    "以及最近 1000/500/300/100 期的同类数字（用来看近期倾向）；当前连跳（单双交替）也给了继续和断开的同类数字。\n"
    "2. 默认不偏向断龙，也默认不偏向跟龙；数据和你的分析支持哪一边，就答哪一边。"
    "龙很短（连1到3期）时，跟龙和断龙基本各占一半，两种答案都是正当的；龙很长、接近或超过历史最长时再认真权衡。\n"
    "3. 再结合各窗口的单双占比、最长龙、龙长度分布和连跳一起看。\n"
    "4. 每一期都独立判断，不要因为上一期你答过某一边就重复它，不要出于习惯总答同一边，也不要总是逆着龙答。"
    "数据里会给出你自己最近的回答里跟龙和断龙各多少次、已连续答同一边几次；如果明显偏向一边，要自我纠正，重新评估。\n"
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
    longest_s, longest_d = _longest_side(values, SINGLE), _longest_side(values, DOUBLE)
    return (
        f"{label}：单{singles}期({singles * 100 / n:.0f}%) 双{doubles}期({doubles * 100 / n:.0f}%)；"
        f"当前连龙：连{values[-1]}{streak}期；最长龙{max(longest_s, longest_d)}期"
        f"（最长单连{longest_s}期，最长双连{longest_d}期）；"
        f"最长连跳(单双交替){_alternating(values)}期；当前连跳：最新{_tail_alternating(values)}期单双交替"
    )


def _longest_side(values: list[str], side: str) -> int:
    best = run = 0
    for value in values:
        run = run + 1 if value == side else 0
        best = max(best, run)
    return best


def _runs(values: list[str]) -> list[tuple[str, int]]:
    """[(side, length)] oldest -> newest; the last run is the (still running) current streak."""
    runs: list[tuple[str, int]] = []
    for value in values:
        if runs and runs[-1][0] == value:
            runs[-1] = (value, runs[-1][1] + 1)
        else:
            runs.append((value, 1))
    return runs


def _tail_alternating(values: list[str]) -> int:
    n = 1 if values else 0
    while n < len(values) and values[-1 - n] != values[-n]:
        n += 1
    return n


def _run_and_alt_lengths(values: list[str]) -> tuple[list[int], list[int]]:
    """Per position: length of the same-side run ending there, and of the alternating run ending there."""
    run: list[int] = []
    alt: list[int] = []
    for index, value in enumerate(values):
        if index and values[index - 1] == value:
            run.append(run[-1] + 1)
            alt.append(1)
        else:
            run.append(1)
            alt.append(alt[-1] + 1 if index else 1)
    return run, alt


def continue_break_counts(values: list[str], kind: str, length: int, window: int | None = None) -> tuple[int, int]:
    """(继续, 断开) counts over history using only positions whose next period is known.

    kind "run": a same-side run that was exactly ``length`` long at position i continued if the next period is
    the same side (跟龙) and broke otherwise (断龙).  kind "alt": an alternating run exactly ``length`` long at i
    continued if the next period differs, and broke if it is the same side.  ``window`` limits the positions to
    the latest ``window`` periods (the newest period itself has no next, so it never counts)."""
    run, alt = _run_and_alt_lengths(values)
    marks = run if kind == "run" else alt
    n = len(values)
    start = 0 if window is None else max(0, n - window)
    cont = brk = 0
    for i in range(start, n - 1):
        if marks[i] != length:
            continue
        same = values[i + 1] == values[i]
        if (same and kind == "run") or (not same and kind == "alt"):
            cont += 1
        else:
            brk += 1
    return cont, brk


def _cb_line(label: str, cont: int, brk: int, cont_name: str, brk_name: str) -> str:
    total = cont + brk
    if not total:
        return f"  {label}：无记录"
    return (
        f"  {label}：{cont_name}{cont}次({cont * 100 / total:.0f}%) ／ {brk_name}{brk}次({brk * 100 / total:.0f}%)，共{total}次"
    )


def _follow_break_lines(results: list[str], length: int, alt_len: int) -> list[str]:
    side = results[-1]
    other = DOUBLE if side == SINGLE else SINGLE
    n = len(results)
    windows = [("全部历史", None)] + [(f"最近{w}期", w) for w in FOLLOW_BREAK_WINDOWS if n > w]
    lines = [
        f"【跟龙 vs 断龙】当前连{side}{length}期。答「{side}」＝跟龙（龙继续），答「{other}」＝断龙（龙断开）。"
        f"历史上龙恰好长到{length}期时，下一期："
    ]
    for label, window in windows:
        c, b = continue_break_counts(results, "run", length, window)
        lines.append(_cb_line(label, c, b, "跟龙(龙继续)", "断龙(龙断开)"))
    lines.append(
        f"【连跳继续 vs 连跳断开】当前连跳已{alt_len}期（最新{alt_len}期单双交替）。答「{other}」＝连跳继续（答案与断龙相同），"
        f"答「{side}」＝连跳断开（答案与跟龙相同）。历史上连跳恰好长到{alt_len}期时，下一期："
    )
    for label, window in windows:
        c, b = continue_break_counts(results, "alt", alt_len, window)
        lines.append(_cb_line(label, c, b, "连跳继续", "连跳断开"))
    return lines


def _streak_lines(results: list[str]) -> list[str]:
    """Current long streak against the history of streaks (only past data)."""
    runs = _runs(results)
    if not runs:
        return []
    side, length = runs[-1]
    lens = [n for _, n in runs]
    counts: dict[int, int] = {}
    for n in lens:
        counts[n] = counts.get(n, 0) + 1
    shown = [f"{k}期×{counts[k]}" for k in sorted(counts) if k <= 8]
    longer = sum(v for k, v in counts.items() if k > 8)
    if longer:
        shown.append(f"9期及以上×{longer}")
    longest = max(lens)
    alt_len = _tail_alternating(results)
    lines = [
        f"当前连龙：连{side}{length}期（历史最长龙{longest}期；当前龙长度是历史最长的{length * 100 / longest:.0f}%）；"
        f"当前连跳（单双交替）已{alt_len}期",
        "历史龙长度分布（全部历史，每条龙算一次）：" + " ".join(shown),
    ]
    lines.extend(_follow_break_lines(results, length, alt_len))
    return lines


def _habit_text(rows: list[tuple], label: str) -> str:
    """跟龙 / 断龙 counts of the AI's own answers relative to the previous result (its streak then known)."""
    follow = brk = long_follow = long_break = 0
    for row in rows:
        pick, prev, prev_streak = row[0], row[2], int(row[3])
        if prev not in (SINGLE, DOUBLE):
            continue
        if pick == prev:
            follow += 1
            long_follow += 1 if prev_streak >= 3 else 0
        else:
            brk += 1
            long_break += 1 if prev_streak >= 3 else 0
    total = follow + brk
    if not total:
        return ""
    text = f"{label}：跟龙{follow}次({follow * 100 / total:.0f}%) 断龙{brk}次({brk * 100 / total:.0f}%)"
    if long_follow + long_break:
        text += f"；其中当时已连龙3期及以上：跟龙{long_follow}次 断龙{long_break}次"
    return text


def _self_lines(ai_history) -> list[str]:
    """The AI's own latest answers (oldest -> newest), already drawn: [(pick, result[, prev_result, prev_streak])]."""
    all_rows = [tuple(x) for x in (ai_history or []) if x[0] in (SINGLE, DOUBLE)][-AI_HABIT_WINDOW:]
    rows = all_rows[-AI_SELF_WINDOW:]
    if not rows:
        return []
    picks = [r[0] for r in rows]
    run = 1
    while run < len(picks) and picks[-1 - run] == picks[-1]:
        run += 1
    hits = sum(1 for r in rows if r[0] == r[1])
    lines = [
        f"你自己最近{len(rows)}次的回答（旧→新）：{''.join(picks)}，对应实际开奖：{''.join(str(r[1]) for r in rows)}，"
        f"命中{hits}/{len(rows)}；你已连续{run}次答{picks[-1]}"
    ]
    detailed = [r for r in all_rows if len(r) >= 4]
    habit = [
        t for t in (
            _habit_text(detailed[-10:], f"你最近{min(10, len(detailed))}次相对当时最新一期的倾向"),
            _habit_text(detailed, f"你最近{len(detailed)}次相对当时最新一期的倾向") if len(detailed) > 10 else "",
        ) if t
    ]
    lines.extend(habit)
    return lines


def build_stats_text(results: list[str], ai_history=None) -> str:
    """Compact statistics text for the prompt (oldest -> newest ``results``).

    ``ai_history`` = the AI's own earlier answers with their (already known) results; nothing from the
    period being predicted, so the future never leaks in."""
    results = list(results[-HISTORY_CAP:])
    lines = [_window_line(f"最近{size}期", results[-size:]) for size in WINDOWS if len(results) >= size]
    if len(results) < WINDOWS[0]:
        lines.append(_window_line("最近", results))
    lines.append(_window_line(f"全部历史（{len(results)}期）", results))
    lines.extend(_streak_lines(results))
    lines.extend(_self_lines(ai_history))
    lines.append("最近10期（旧→新）：" + "".join(results[-10:]))
    return "\n".join(lines)


def build_messages(results: list[str], ai_history=None) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_stats_text(results, ai_history) + "\n下一期开单还是双？只回答一个字。"},
    ]


def _payload(model: str, messages: list[dict]) -> dict:
    body = {"model": model, "messages": messages}
    if model.casefold().startswith(("gpt-5", "o1", "o3", "o4")):
        body["max_completion_tokens"] = 512  # reasoning models need room before the answer
    else:
        body["max_tokens"] = 5
        body["temperature"] = 0.7
    return body


async def request_pick(settings, results: list[str], ai_history=None, *, transport=None) -> str | None:
    """One chat/completions call; returns 单/双 or None. Never logs headers/keys."""
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    timeout = float(settings.ai_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"},
            json=_payload(settings.ai_model, build_messages(results, ai_history)),
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
            self.task = asyncio.get_running_loop().create_task(
                self._run(next_block, results, self._own_history(rows))
            )
            return True
        except Exception:
            logger.exception("TRON AI scheduling failed")
            return False

    def _own_history(self, rows) -> list[tuple[str, str, str, int]]:
        """The AI's own answers for periods that are already drawn (oldest -> newest), contiguous at the
        newest end, as (pick, result, previous_result, previous_streak_length).  Only stored, write-once,
        pre-draw predictions; the period being asked is never included, and the previous result / streak are
        what was already known before that period was drawn."""
        try:
            blocks = [int(row["block_number"]) for row in rows]
            results = [str(row["result"]) for row in rows]
            if not blocks:
                return []
            index_of = {b: i for i, b in enumerate(blocks)}
            low, high = blocks[max(0, len(blocks) - AI_HABIT_WINDOW)], blocks[-1]
            picks = self.store.tron_ai_predictions(low, high, self.settings.ai_model)
            out: list[tuple[str, str, str, int]] = []
            block = high
            while block >= low and block in picks and block in index_of and len(out) < AI_HABIT_WINDOW:
                i = index_of[block]
                prev, streak = "", 0
                if i >= 1 and blocks[i - 1] == block - DRAW_INTERVAL:
                    prev, streak = results[i - 1], 1
                    while streak < i and results[i - 1 - streak] == prev:
                        streak += 1
                out.append((picks[block], results[i], prev, streak))
                block -= DRAW_INTERVAL
            return list(reversed(out))
        except Exception:
            logger.warning("TRON AI own-history lookup failed")
            return []

    async def _run(self, next_block: int, results: list[str], ai_history=None) -> None:
        self.stats["calls"] += 1
        try:
            pick = await asyncio.wait_for(
                request_pick(self.settings, results, ai_history, transport=self.transport),
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
