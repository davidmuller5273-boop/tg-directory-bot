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


def is_draw_block(number: int) -> bool:
    return int(number) > 0 and int(number) % DRAW_INTERVAL == 0


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


class TronDrawRecorder:
    """Records draw blocks; back-fills the ones missed while the bot was down."""

    def __init__(
        self, chain, store, *, confirmations: int = 20, initial_backfill: int = 500,
        max_backfill: int = 20000, per_step: int = 3,
    ):
        self.chain = chain
        self.store = store
        self.confirmations = max(1, int(confirmations))
        self.initial_backfill = max(1, int(initial_backfill))
        self.max_backfill = max(1, int(max_backfill))
        self.per_step = max(1, int(per_step))
        self._pending: list[int] = []
        self._planned_for = 0
        self._retry_at = 0.0
        self.stats = {"recorded": 0, "requests": 0, "errors": 0}

    def latest_draw(self, head: int) -> int:
        target = int(head) - self.confirmations
        return target - target % DRAW_INTERVAL if target > 0 else 0

    def plan(self, head: int) -> list[int]:
        """Missing draw blocks (newest first) in the window we promise to cover."""
        latest = self.latest_draw(head)
        if latest <= 0:
            return []
        floor = self.store.tron_draw_floor()
        if floor <= 0 or floor % DRAW_INTERVAL:
            floor = max(DRAW_INTERVAL, latest - DRAW_INTERVAL * (self.initial_backfill - 1))
            self.store.set_tron_draw_floor(floor)
        floor = max(floor, latest - DRAW_INTERVAL * (self.max_backfill - 1), DRAW_INTERVAL)
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
        if not self._pending or time.monotonic() < self._retry_at:
            return 0
        recorded = 0
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
                self._retry_at = time.monotonic() + 15
                break
            if self.store.record_tron_draw(number, block_hash, timestamp_ms):
                recorded += 1
                self.stats["recorded"] += 1
            self._pending.pop(0)
        return recorded
