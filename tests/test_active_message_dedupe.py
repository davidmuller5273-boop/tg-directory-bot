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

    # ---- 活跃奖励也按有效发言 --------------------------------------------

    def reward_replies(self, seconds):
        texts = []
        for second in seconds:
            reply = self.send(at=second).reply_text
            texts.extend(str(c.args[0]) for c in reply.await_args_list)
        return texts

    def test_activity_reward_quick_messages_count_once(self):
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        self.assertEqual(self.reward_replies((0, 10, 20)), [])   # 3 条只算 1 条
        self.assertEqual(self.active(), 1)
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 3)  # 原始计数照旧
        self.assertEqual(self.balance(), 500)

    def test_activity_reward_messages_61s_apart_count(self):
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        self.assertEqual(self.reward_replies((0, 61)), [])
        texts = self.reward_replies((122,))
        self.assertEqual(len(texts), 1)
        self.assertIn("🔥 今日已有效发言 3 条（1 分钟内多条只算 1 条），随机奖励 +5 积分", texts[0])
        self.assertEqual(self.balance(), 505)
        # 发奖后重新累计：快速连发不算，满 3 条有效发言再奖
        self.assertEqual(self.reward_replies((130, 140, 183, 244)), [])
        self.assertEqual(len(self.reward_replies((305,))), 1)
        self.assertEqual(self.balance(), 510)

    def legacy_row(self, raw, active, baseline, target=3):
        """模拟升级前已结算过的今日数据：基线是原始发言数（effective_basis=0）。"""
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO group_activity_users
                   (chat_id, user_id, day, messages, active_messages, last_counted_ts)
                   VALUES (?, ?, DATE('now','+8 hours'), ?, ?, 0)""",
                (CHAT, USER, raw, active),
            )
            conn.execute(
                """INSERT INTO point_activity_rewards
                   (chat_id, user_id, day, message_target, points, message_baseline,
                    reward_count, effective_basis)
                   VALUES (?, ?, DATE('now','+8 hours'), ?, 5, ?, 1, 0)""",
                (CHAT, USER, target, baseline),
            )

    def reward_row(self):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT * FROM point_activity_rewards WHERE chat_id=? AND user_id=?",
                (CHAT, USER),
            ).fetchone()

    def test_no_double_award_at_transition_just_paid(self):
        # 今天按原始口径在第 10 条时刚发过奖；升级迁移把有效计数设为 10。
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        self.legacy_row(raw=10, active=10, baseline=10)
        self.assertEqual(self.reward_replies((0,)), [])     # 新口径第 1 条进度
        row = self.reward_row()
        self.assertEqual((row["effective_basis"], row["message_baseline"]), (1, 10))
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.reward_replies((61,)), [])
        self.assertEqual(len(self.reward_replies((122,))), 1)   # 满 3 条新有效发言才发
        self.assertEqual(self.balance(), 505)
        self.assertEqual(self.reward_row()["points"], 10)       # 已发奖励保留并累加

    def test_no_double_award_when_effective_lags_raw(self):
        # 升级后旧进程已按新规则累计一段时间：有效 6 < 原始 12，原始基线 12。
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        self.legacy_row(raw=12, active=6, baseline=12)
        self.assertEqual(self.reward_replies((0,)), [])
        row = self.reward_row()
        # 进度 = min(原始进度 1, 有效 7) = 1 → 基线换算为 6，不会卡住也不会补发
        self.assertEqual((row["effective_basis"], row["message_baseline"]), (1, 6))
        self.assertEqual(self.reward_replies((61,)), [])
        self.assertEqual(len(self.reward_replies((122,))), 1)
        self.assertEqual(self.balance(), 505)

    def test_transition_keeps_partial_progress_without_overpaying(self):
        # 原始口径上次发奖后又发了 2 条（基线 8，原始 10），目标 3。
        self.store.set_activity_points(CHAT, 3, 3, 5, 5, 1)
        self.legacy_row(raw=10, active=10, baseline=8)
        texts = self.reward_replies((0,))     # 原始进度 3 → 达标发奖一次（原规则下本来也该发）
        self.assertEqual(len(texts), 1)
        self.assertEqual(self.balance(), 505)
        self.assertEqual(self.reward_replies((10, 20, 30)), [])   # 不重复

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
