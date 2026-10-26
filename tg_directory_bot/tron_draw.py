"""TRON block-hash 单/双 draw statistics (reuses the existing block scanner).

Rules: every ``DRAW_INTERVAL`` (20) blocks the block whose number is a
multiple of 20 (ends in 00/20/40/60/80) is a draw block. Its block hash is
read, letters are ignored and the LAST DIGIT character of the hash decides:
1/3/5/7/9 -> 单, 0/2/4/6/8 -> 双.

History lives permanently in the main SQLite database (table ``tron_draws``,
never pruned). The recorder is driven by the scanner job (``scan_tron_blocks``)
and shares its head tracking; missed draw blocks are back-filled by block
number with a tiny header-only request, a few per second at most.
"""
from __future__ import annotations

import logging
import re
import time

from .chain import ChainQueryError

logger = logging.getLogger(__name__)

DRAW_INTERVAL = 20
SINGLE = "单"
DOUBLE = "双"
MAX_ROWS = 6
_DIGIT = re.compile(r"[0-9]")


VERIFY_BLOCKS = 20  # ~1 min: a TRON block this deep is solidified (no more forks)


def is_draw_block(number: int, offset: int = 0) -> bool:
    """height % 20 == offset (offset 0 = the 00/20/40/60/80 rule)."""
    return int(number) > 0 and int(number) % DRAW_INTERVAL == int(offset) % DRAW_INTERVAL


def last_digit(block_hash: str) -> int | None:
    """Last digit character of the hash (letters ignored); None if no digit."""
    digits = _DIGIT.findall(str(block_hash or ""))
    return int(digits[-1]) if digits else None


def draw_result(block_hash: str) -> tuple[int, str] | None:
    digit = last_digit(block_hash)
    if digit is None:
        return None
    return digit, SINGLE if digit % 2 else DOUBLE


def bead_road(results: list[str], max_rows: int = MAX_ROWS) -> tuple[list[dict], int]:
    """Bead-road layout, oldest result first.

    Equal consecutive results stack downward; a change starts a new column
    (right of every column used so far); after ``max_rows`` the streak turns
    right along the bottom row (tail). Returns (cells, column count) where a
    cell is ``{"c": col, "r": row, "v": "单"|"双", "i": index}``.
    """
    cells: list[dict] = []
    col = row = max_col = 0
    previous = None
    for index, value in enumerate(results):
        if previous is None:
            col = row = 0
        elif value == previous:
            if row + 1 < max_rows:
                row += 1
            else:
                col += 1
        else:
            col = max_col + 1
            row = 0
        max_col = max(max_col, col)
        cells.append({"c": col, "r": row, "v": value, "i": index})
        previous = value
    return cells, (max_col + 1 if cells else 0)


def parse_limit(raw, default: int = 100, maximum: int = 5000) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


# --- 参考方向 (fixed algorithms, pure history statistics, no look-ahead) ---

DEFAULT_STREAK_N = 3
DEFAULT_RECENT_M = 20
DEFAULT_MINORITY_M = 10
MODE_FOLLOW = "follow"  # 跟龙
MODE_CUT = "cut"  # 斩龙
ALGORITHMS = ("trend", "dragon", "ratio", "minority")  # 顺势 / 跟龙·斩龙 / 近期占比 / 近期少数方
REFERENCE_NOTE = "开奖为哈希末位，单双各约50%，统计仅供参考"


def _opposite(value: str) -> str:
    return DOUBLE if value == SINGLE else SINGLE


def streak_at(results: list[str], end: int | None = None) -> int:
    """Length of the streak that ends at ``results[end - 1]`` (0 if empty)."""
    end = len(results) if end is None else end
    if end <= 0:
        return 0
    last = results[end - 1]
    n = 1
    while end - 1 - n >= 0 and results[end - 1 - n] == last:
        n += 1
    return n


def reference_pick(
    algorithm: str, results: list[str], end: int | None = None, *,
    streak_n: int = DEFAULT_STREAK_N, recent_m: int = DEFAULT_RECENT_M,
    mode: str = MODE_FOLLOW, minority_m: int = DEFAULT_MINORITY_M,
) -> str | None:
    """Reference direction for the period after ``results[:end]``.

    Only ``results[:end]`` is read, so a backtest at index ``end`` can never
    see the result it is judged against.
    """
    end = len(results) if end is None else end
    if end <= 0:
        return None
    last = results[end - 1]
    if algorithm == "trend":  # 顺势
        return last
    if algorithm == "dragon":  # 跟龙/斩龙
        if streak_at(results, end) >= streak_n and mode == MODE_CUT:
            return _opposite(last)
        return last
    if algorithm == "ratio":  # 近期占比
        window = results[max(0, end - recent_m):end]
        singles = sum(1 for value in window if value == SINGLE)
        doubles = len(window) - singles
        if singles == doubles:
            return last
        return SINGLE if singles > doubles else DOUBLE
    if algorithm == "minority":  # 近期少数方
        window = results[max(0, end - minority_m):end]
        singles = sum(1 for value in window if value == SINGLE)
        doubles = len(window) - singles
        if singles == doubles:
            return last
        return SINGLE if singles < doubles else DOUBLE
    raise ValueError(f"unknown algorithm: {algorithm}")


def backtest(
    algorithm: str, results: list[str], start: int = 1, **params,
) -> dict:
    """Simulate on every index >= ``start`` using only earlier results."""
    hits = total = 0
    for index in range(max(1, start), len(results)):
        pick = reference_pick(algorithm, results, index, **params)
        total += 1
        hits += pick == results[index]
    return {"hits": hits, "total": total, "rate": round(hits * 100 / total, 1) if total else None}


def longest_streak(results: list[str]) -> int:
    best = run = 0
    previous = None
    for value in results:
        run = run + 1 if value == previous else 1
        best = max(best, run)
        previous = value
    return best


def omissions(results: list[str]) -> dict:
    """Periods since 单 / 双 last appeared (0 = the latest period)."""
    out = {}
    for key, label in (("single", SINGLE), ("double", DOUBLE)):
        gap = 0
        for value in reversed(results):
            if value == label:
                break
            gap += 1
        out[key] = gap
    return out


def lookback_needed(streak_n: int, recent_m: int, minority_m: int = DEFAULT_MINORITY_M) -> int:
    return max(1, int(streak_n), int(recent_m), int(minority_m))


def reference_panel(
    results: list[str], limit: int, *, streak_n: int = DEFAULT_STREAK_N,
    recent_m: int = DEFAULT_RECENT_M, mode: str = MODE_FOLLOW,
    minority_m: int = DEFAULT_MINORITY_M,
) -> dict:
    """``results``: oldest first, ideally ``limit`` + lookback_needed() entries.

    Stats are for the last ``limit`` results; every backtested period in that
    window is predicted from all results before it (extra lookback included).
    """
    window_start = max(0, len(results) - int(limit))
    window = results[window_start:]
    params = {
        "streak_n": streak_n, "recent_m": recent_m, "mode": mode, "minority_m": minority_m,
    }
    singles = sum(1 for value in window if value == SINGLE)
    total = len(window)
    algorithms = {}
    for name in ALGORITHMS:
        result = backtest(name, results, window_start, **params)
        result["next"] = reference_pick(name, results, **params)
        algorithms[name] = result
    return {
        "note": REFERENCE_NOTE,
        "params": params,
        "current_streak": streak_at(window),
        "current_value": window[-1] if window else None,
        "longest_streak": longest_streak(window),
        "ratio": {
            "single": round(singles * 100 / total, 1) if total else None,
            "double": round((total - singles) * 100 / total, 1) if total else None,
        },
        "omission": omissions(window),
        "algorithms": algorithms,
    }


def parse_reference_params(
    streak_n, recent_m, mode, minority_m="",
) -> tuple[int, int, str, int]:
    return (
        parse_limit(streak_n, DEFAULT_STREAK_N, 50) if str(streak_n).strip() else DEFAULT_STREAK_N,
        parse_limit(recent_m, DEFAULT_RECENT_M, 200) if str(recent_m).strip() else DEFAULT_RECENT_M,
        MODE_CUT if str(mode).strip().casefold() == MODE_CUT else MODE_FOLLOW,
        parse_limit(minority_m, DEFAULT_MINORITY_M, 200)
        if str(minority_m).strip() else DEFAULT_MINORITY_M,
    )


class TronDrawRecorder:
    """Records draw blocks; back-fills the ones missed while the bot was down."""

    def __init__(
        self, chain, store, *, confirmations: int = 0, initial_backfill: int = 500,
        max_backfill: int = 20000, per_step: int = 3, block_offset: int = 0,
    ):
        self.chain = chain
        self.store = store
        self.confirmations = max(0, int(confirmations))
        self.offset = int(block_offset) % DRAW_INTERVAL
        self.initial_backfill = max(1, int(initial_backfill))
        self.max_backfill = max(1, int(max_backfill))
        self.per_step = max(1, int(per_step))
        self._pending: list[int] = []
        self._planned_for = 0
        self._retry_at = 0.0
        self.stats = {"recorded": 0, "requests": 0, "errors": 0}

    def latest_draw(self, head: int) -> int:
        target = int(head) - self.confirmations
        if target <= 0:
            return 0
        latest = target - (target - self.offset) % DRAW_INTERVAL
        return latest if latest > 0 else 0

    def plan(self, head: int) -> list[int]:
        """Missing draw blocks (newest first) in the window we promise to cover."""
        latest = self.latest_draw(head)
        if latest <= 0:
            return []
        floor = self.store.tron_draw_floor()
        if floor <= 0 or floor % DRAW_INTERVAL != self.offset:
            floor = max(
                DRAW_INTERVAL + self.offset, latest - DRAW_INTERVAL * (self.initial_backfill - 1),
            )
            self.store.set_tron_draw_floor(floor)
        floor = max(
            floor, latest - DRAW_INTERVAL * (self.max_backfill - 1), DRAW_INTERVAL + self.offset,
        )
        have = self.store.tron_draw_existing(floor, latest)
        return [n for n in range(latest, floor - 1, -DRAW_INTERVAL) if n not in have]

    async def step(self, head: int) -> int:
        """Record up to ``per_step`` missing draws; returns how many were stored."""
        if not head:
            return 0
        latest = self.latest_draw(head)
        if latest != self._planned_for:
            self._pending = self.plan(head)
            self._planned_for = latest
        recorded = await self._verify(head)
        if not self._pending or time.monotonic() < self._retry_at:
            return recorded
        for _ in range(self.per_step):
            if not self._pending:
                break
            number = self._pending[0]
            try:
                block_hash, timestamp_ms = await self.chain.tron_block_header(number)
                self.stats["requests"] += 1
            except ChainQueryError as exc:
                self.stats["errors"] += 1
                logger.info("TRON draw block %s not recorded yet: %s", number, exc)
                # a just-produced block may not be visible on every node yet:
                # retry quickly; the 3 s block time makes 2 s cheap and fast
                self._retry_at = time.monotonic() + 2
                break
            if self.store.record_tron_draw(
                number, block_hash, timestamp_ms,
                verified=number <= int(head) - VERIFY_BLOCKS, offset=self.offset,
            ):
                recorded += 1
                self.stats["recorded"] += 1
            self._pending.pop(0)
        return recorded

    async def _verify(self, head: int) -> int:
        """Re-check hashes recorded from young blocks once they are solidified.

        Draws are recorded as soon as the block exists (fast); on a rare fork
        the stored hash/result is corrected here. Returns the corrections made.
        """
        corrected = 0
        for row in self.store.tron_draw_unverified(int(head) - VERIFY_BLOCKS, 2):
            number = int(row["block_number"])
            try:
                block_hash, timestamp_ms = await self.chain.tron_block_header(number)
                self.stats["requests"] += 1
            except ChainQueryError:
                break
            if block_hash == str(row["block_hash"]):
                self.store.tron_draw_mark_verified(number)
            elif self.store.tron_draw_correct(number, block_hash, timestamp_ms):
                corrected += 1
                self.stats["corrected"] = self.stats.get("corrected", 0) + 1
                logger.warning(
                    "TRON draw block %s hash changed after fork: %s -> %s",
                    number, row["block_hash"], block_hash,
                )
            else:
                self.store.tron_draw_mark_verified(number)
        return corrected
