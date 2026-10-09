import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tg_directory_bot import raffle_fair as rf
from tg_directory_bot.storage import DirectoryStore


class SplitTest(unittest.TestCase):
    def test_split_thirds(self):
        rng = random.Random(1)
        self.assertEqual(rf.split_slots(6, rng), {"random": 2, "balanced": 2, "newcomer": 2})
        for total in range(0, 12):
            slots = rf.split_slots(total, random.Random(total))
            self.assertEqual(sum(slots.values()), total)
            self.assertLessEqual(max(slots.values()) - min(slots.values()), 1)

    def test_single_slot_picks_random_part(self):
        parts = Counter()
        for seed in range(300):
            slots = rf.split_slots(1, random.Random(seed))
            parts[next(p for p, n in slots.items() if n)] += 1
        self.assertEqual(set(parts), set(rf.PARTS))


class PickTest(unittest.TestCase):
    def test_no_duplicates_and_size(self):
        ids = list(range(1, 21)) + [5, 5]
        for seed in range(50):
            winners = rf.pick_winner_ids(ids, 7, rng=random.Random(seed))
            self.assertEqual(len(winners), 7)
            self.assertEqual(len(set(winners)), 7)
            self.assertTrue(set(winners) <= set(range(1, 21)))

    def test_more_slots_than_people(self):
        self.assertEqual(sorted(rf.pick_winner_ids([1, 2], 5, rng=random.Random(0))), [1, 2])
        self.assertEqual(rf.pick_winner_ids([], 3, rng=random.Random(0)), [])

    def test_deterministic_with_seed(self):
        a = rf.pick_winner_ids(range(100), 9, rng=random.Random(42))
        b = rf.pick_winner_ids(range(100), 9, rng=random.Random(42))
        self.assertEqual(a, b)

    def test_newcomer_slot_goes_to_newcomer_or_never_won(self):
        # 所有人都赢过，只有 99 是新人 → 3 个名额中新人部分必中 99
        ids = list(range(1, 31)) + [99]
        history = {uid: (1, 3) for uid in range(1, 31)}
        for seed in range(30):
            winners = rf.pick_winner_ids(ids, 3, history=history, newcomers=[99],
                                         rng=random.Random(seed))
            self.assertIn(99, winners)

    def test_newcomer_pool_empty_falls_back(self):
        ids = list(range(1, 11))
        history = {uid: (0, 1) for uid in ids}
        winners = rf.pick_winner_ids(ids, 6, history=history, rng=random.Random(3))
        self.assertEqual(len(set(winners)), 6)

    def test_balanced_prefers_fewer_recent_wins(self):
        ids = list(range(1, 41))
        # 1-20 最近赢了很多次，21-40 一次都没赢（但总数>0，非新人）
        history = {uid: ((9, 9) if uid <= 20 else (0, 5)) for uid in ids}
        low = 0
        for seed in range(400):
            winners = rf.pick_winner_ids(ids, 3, history=history, rng=random.Random(seed))
            low += sum(1 for uid in winners if uid > 20)
        # 纯随机期望 50%；有平衡部分后明显更高
        self.assertGreater(low / (400 * 3), 0.6)


class StoreIntegrationTest(unittest.TestCase):
    def test_history_and_newcomers_from_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = DirectoryStore(Path(temp.name) / "db.sqlite3")
        store.init()
        chat = -777
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO raffles (id, chat_id, creator_id, prize, ends_at) VALUES (1, ?, 1, 'p', CURRENT_TIMESTAMP)",
                (chat,),
            )
        store.complete_raffle(1, [10, 11])
        history = store.raffle_win_history(chat, [10, 11, 12])
        self.assertEqual(history, {10: (1, 1), 11: (1, 1), 12: (0, 0)})
        store.delete_raffle(chat, 1)   # 删除抽奖后历史仍保留
        self.assertEqual(store.raffle_win_history(chat, [10])[10], (1, 1))
        store.record_group_operation(chat, "join", 12, "u", "U", "test")
        self.assertEqual(store.recent_joiners(chat, [10, 12]), {12})
        rows = [{"user_id": uid} for uid in (10, 11, 12)]
        winners = rf.pick_winners(rows, 2, store=store, chat_id=chat, rng=random.Random(5))
        self.assertEqual(len({w["user_id"] for w in winners}), 2)


if __name__ == "__main__":
    unittest.main()
