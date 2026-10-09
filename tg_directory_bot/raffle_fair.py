"""Winner selection for random raffles.

Winner slots are split into three roughly equal parts:
  * random    – uniform over everyone;
  * balanced  – weighted towards people who won less in the last 30 days;
  * newcomer  – people who joined within 7 days or never won here.
A part that cannot be filled (empty pool) falls back to the remaining
participants. Nobody can win twice. With a single slot the part is chosen at
random. Internal only: never described in user-facing text.
"""
from __future__ import annotations

import random as _random
import secrets
from typing import Any, Callable, Iterable, Sequence

PARTS = ("random", "balanced", "newcomer")


def split_slots(total: int, rng: Any) -> dict[str, int]:
    slots = {part: 0 for part in PARTS}
    if total <= 0:
        return slots
    base, extra = divmod(total, 3)
    for part in PARTS:
        slots[part] = base
    for part in rng.sample(list(PARTS), extra):
        slots[part] += 1
    return slots


def _weighted_pick(pool: list[int], weight: Callable[[int], float], rng: Any) -> int:
    weights = [max(1e-9, float(weight(uid))) for uid in pool]
    point = rng.random() * sum(weights)
    for uid, w in zip(pool, weights):
        point -= w
        if point < 0:
            return uid
    return pool[-1]


def pick_winner_ids(
    user_ids: Sequence[int], total: int, *,
    history: dict[int, tuple[int, int]] | None = None,
    newcomers: Iterable[int] = (),
    rng: Any = None,
) -> list[int]:
    """Return ``total`` distinct user ids (in random order)."""
    rng = rng or secrets.SystemRandom()
    ids = list(dict.fromkeys(int(uid) for uid in user_ids))
    total = max(0, min(int(total), len(ids)))
    if total == 0:
        return []
    history = history or {}
    newcomer_set = {int(uid) for uid in newcomers}
    never_won = {uid for uid in ids if history.get(uid, (0, 0))[1] == 0}
    fresh = newcomer_set | never_won
    slots = split_slots(total, rng)
    remaining = list(ids)
    chosen: list[int] = []

    def take(uid: int) -> None:
        chosen.append(uid)
        remaining.remove(uid)

    def recent(uid: int) -> int:
        return int(history.get(uid, (0, 0))[0])

    shortfall = 0
    # 最受限的池子先抽，避免被其它部分抢光
    for _ in range(slots["newcomer"]):
        pool = [uid for uid in remaining if uid in fresh]
        if not pool:
            shortfall += 1
            continue
        take(rng.choice(pool))
    for _ in range(slots["balanced"] + shortfall):
        if not remaining:
            break
        take(_weighted_pick(remaining, lambda uid: 1.0 / (1 + recent(uid)), rng))
    for _ in range(slots["random"]):
        if not remaining:
            break
        take(rng.choice(remaining))
    while len(chosen) < total and remaining:
        take(rng.choice(remaining))
    rng.shuffle(chosen)
    return chosen


def pick_winners(entries: Sequence[Any], total: int, *, store: Any = None,
                 chat_id: int = 0, rng: Any = None) -> list[Any]:
    """Entries are rows with ``user_id``; uses the store for history/newcomers."""
    by_id: dict[int, Any] = {}
    for row in entries:
        by_id.setdefault(int(row["user_id"]), row)
    ids = list(by_id)
    history: dict[int, tuple[int, int]] = {}
    newcomers: set[int] = set()
    if store is not None and ids:
        try:
            history = store.raffle_win_history(chat_id, ids, 30)
            newcomers = store.recent_joiners(chat_id, ids, 7)
        except Exception:  # noqa: BLE001 - selection must never fail
            history, newcomers = {}, set()
    winner_ids = pick_winner_ids(ids, total, history=history, newcomers=newcomers, rng=rng)
    return [by_id[uid] for uid in winner_ids]


def seeded(seed: int) -> _random.Random:
    return _random.Random(seed)
