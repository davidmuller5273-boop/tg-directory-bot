"""最低当日活跃按“有效发言”计：同一成员在同一群、同一天，
距上一条计入的发言满 60 秒才再计 1 条；骰子口令不算。"""
import asyncio
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from tg_directory_bot.bot import point_dice_bet_reply, track_group_activity
from tg_directory_bot.storage import DirectoryStore

CHAT, USER = -50505, 44


class ActiveMessageDedupeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.store.adjust_points(CHAT, USER, 500, "seed", 1, "u", "U")
        self.now = 1_700_000_000.0
        clock = patch("tg_directory_bot.storage.activity_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        self.temp.cleanup()

    def send(self, text="聊天", at=None, chat_id=CHAT):
        if at is not None:
            self.now = 1_700_000_000.0 + at
        message = SimpleNamespace(
            text=text, caption=None, new_chat_members=[], left_chat_member=None,
            entities=[], caption_entities=[], chat_id=chat_id, message_id=1,
            reply_text=AsyncMock(), delete=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, title="群", username="", type=ChatType.SUPERGROUP),
            effective_message=message,
            effective_user=SimpleNamespace(id=USER, username="u", full_name="U", is_bot=False),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), user_data={},
                                  application=SimpleNamespace(bot_data={"store": self.store}))
        asyncio.run(track_group_activity(update, context))
        return message

    def active(self, chat_id=CHAT):
        return self.store.user_today_active_messages(chat_id, USER)

    def balance(self):
        return Decimal(str(self.store.point_account(CHAT, USER)["balance"]))

    # ---- 计数规则 --------------------------------------------------------

    def test_messages_within_a_minute_count_once(self):
        for second in (0, 5, 20, 59):
            self.send(at=second)
        self.assertEqual(self.active(), 1)
        # 群统计/原始发言数不受影响
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 4)
        with self.store.connect() as conn:
            daily = conn.execute(
                "SELECT messages FROM group_daily_stats WHERE chat_id=?", (CHAT,)
            ).fetchone()[0]
        self.assertEqual(daily, 4)

    def test_message_at_or_after_sixty_seconds_counts(self):
        self.send(at=0)
        self.send(at=59.9)
        self.assertEqual(self.active(), 1)
        self.send(at=60)          # 恰好 60 秒
        self.assertEqual(self.active(), 2)
        self.send(at=200)         # 超过 60 秒
        self.assertEqual(self.active(), 3)

    def test_window_starts_from_last_counted_message(self):
        self.send(at=0)           # 计入
        self.send(at=50)          # 忽略
        self.send(at=100)         # 距上条消息仅 50 秒，但距上条计入 100 秒 → 计入
        self.assertEqual(self.active(), 2)
        self.send(at=130)         # 距计入的 100 仅 30 秒 → 忽略
        self.send(at=159)         # 59 秒 → 忽略
        self.assertEqual(self.active(), 2)
        self.send(at=160)         # 60 秒 → 计入
        self.assertEqual(self.active(), 3)

    def test_per_group_isolation(self):
        self.send(at=0)
        self.send(at=1, chat_id=CHAT - 1)   # 其他群独立计数
        self.assertEqual(self.active(), 1)
        self.assertEqual(self.active(CHAT - 1), 1)

    def test_day_rollover_resets(self):
        self.send(at=0)
        self.send(at=100)
        self.assertEqual(self.active(), 2)
        with self.store.connect() as conn:   # 模拟这些发言都发生在昨天
            conn.execute(
                "UPDATE group_activity_users SET day=DATE('now','+8 hours','-1 day')"
            )
        self.assertEqual(self.active(), 0)
        self.send(at=110)          # 新的一天第一条立即计入（不受昨天 60 秒窗口影响）
        self.assertEqual(self.active(), 1)
        self.send(at=120)
        self.assertEqual(self.active(), 1)

    def test_dice_commands_excluded_and_do_not_consume_window(self):
        self.send("大3", at=0)
        self.send("小5", at=70)
        self.assertEqual(self.active(), 0)
        self.send("你好", at=80)   # 骰子口令不占用窗口，这条立即计入
        self.assertEqual(self.active(), 1)
        self.send("大3", at=200)
        self.assertEqual(self.active(), 1)

    def test_migration_keeps_today_counts(self):
        path = Path(self.temp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE group_activity_users (
               chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
               messages INTEGER NOT NULL DEFAULT 0,
               last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY(chat_id, user_id, day))"""
        )
        conn.execute(
            "INSERT INTO group_activity_users (chat_id, user_id, day, messages) "
            "VALUES (?, ?, DATE('now','+8 hours'), 5)", (CHAT, USER),
        )
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        self.assertEqual(store.user_today_active_messages(CHAT, USER), 5)
        store.init()   # 再次启动不会重复覆盖
        store.record_group_activity(CHAT, "群", "", "supergroup", USER, "u", "U", messages=1)
        store.record_group_activity(CHAT, "群", "", "supergroup", USER, "u", "U", messages=1)
        self.assertEqual(store.user_today_active_messages(CHAT, USER), 6)
        self.assertEqual(store.user_today_messages(CHAT, USER), 7)

    def test_activity_reward_still_uses_raw_message_count(self):
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        replies = [self.send(at=second).reply_text for second in (0, 1, 2)]
        self.assertEqual(self.active(), 1)
        self.assertTrue(any(r.await_count for r in replies))   # 3 条原始发言即达标

    # ---- 三处门槛都使用有效发言 --------------------------------------------

    def test_dice_threshold_uses_active_count(self):
        self.store.set_dice_activity_rule(CHAT, "min", 2, 1)
        for second in (0, 10, 20):
            self.send(at=second)

        def bet():
            message = SimpleNamespace(
                reply_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=6))),
                reply_text=AsyncMock(), message_id=9,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(id=USER, username="u", full_name="U"),
                effective_message=message,
            )
            context = SimpleNamespace(user_data={},
                                      application=SimpleNamespace(bot_data={"store": self.store}))
            asyncio.run(point_dice_bet_reply(update, context, "大", 10))
            return message

        message = bet()
        message.reply_dice.assert_not_awaited()
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "今日活跃不足：需要当日有效发言 2 条才能玩骰子（1 分钟内多条只算 1 条），你今天已有效发言 1 条",
        )
        self.assertEqual(self.balance(), 500)
        self.send(at=60)
        bet().reply_dice.assert_awaited_once()

    def test_dice_free_activity_uses_active_count(self):
        self.store.set_dice_schedule(CHAT, True, "09:00", "10:00", 1)
        self.store.set_dice_activity_rule(CHAT, "free", 2, 1)
        for second in (0, 30):
            self.send(at=second)
        message = SimpleNamespace(reply_dice=AsyncMock(), reply_text=AsyncMock(), message_id=9)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=USER, username="u", full_name="U"),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={},
                                  application=SimpleNamespace(bot_data={"store": self.store}))
        with patch("tg_directory_bot.bot.dice_schedule_is_open", return_value=False):
            asyncio.run(point_dice_bet_reply(update, context, "大", 10))
        message.reply_dice.assert_not_awaited()
        self.assertIn(
            "今日有效发言满 2 条可不受时间限制（1 分钟内多条只算 1 条，当前 1 条）",
            message.reply_text.await_args.args[0],
        )

    def test_raffle_threshold_uses_active_count(self):
        self.store.set_point_draw_config(CHAT, True, 10, 1.0, 1)
        gift = self.store.add_point_gift(CHAT, "礼品", 1000, -1, 1)
        self.store.set_point_draw_min_activity(CHAT, 2, 1)
        for second in (0, 15, 45):
            self.send(at=second)
        with self.assertRaises(ValueError) as raised:
            self.store.draw_point_gift(CHAT, USER, gift, "u", "U")
        self.assertEqual(
            str(raised.exception),
            "今日活跃不足：需要当日有效发言 2 条才能参与积分抽奖（1 分钟内多条只算 1 条），你今天已有效发言 1 条",
        )
        self.assertEqual(self.balance(), 500)
        self.send(at=61)
        self.store.draw_point_gift(CHAT, USER, gift, "u", "U")
        self.assertEqual(self.balance(), 490)

    def test_redeem_threshold_uses_active_count(self):
        gift = self.store.add_point_gift(CHAT, "礼品", 10, -1, 1)
        self.store.set_point_redeem_min_activity(CHAT, 2, 1)
        for second in (0, 15, 45):
            self.send(at=second)
        with self.assertRaises(ValueError) as raised:
            self.store.redeem_point_gift(CHAT, USER, gift, "u", "U")
        self.assertEqual(
            str(raised.exception),
            "今日活跃不足：需要当日有效发言 2 条才能兑换（1 分钟内多条只算 1 条），你今天已有效发言 1 条",
        )
        self.assertEqual(self.balance(), 500)
        self.send(at=75)
        self.store.redeem_point_gift(CHAT, USER, gift, "u", "U")
        self.assertEqual(self.balance(), 490)


if __name__ == "__main__":
    unittest.main()
