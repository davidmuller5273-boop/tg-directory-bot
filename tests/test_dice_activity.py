import asyncio
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from tg_directory_bot import settings_wizard
from tg_directory_bot.bot import (
    commit_group_menu_input,
    dice_settings_view,
    menu_mode_group_permission,
    point_dice_bet_reply,
    points_status_text,
)
from tg_directory_bot.storage import DirectoryStore

CHAT, USER = -60606, 77


class DiceActivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.store.adjust_points(CHAT, USER, 500, "seed", 1, "alice", "Alice")
        # 有效发言 1 分钟去重：测试里每次发言相隔 61 秒，保证每条都计入
        self._now = 1_700_000_000.0

        def _tick():
            self._now += 61
            return self._now

        clock = patch("tg_directory_bot.storage.activity_now", side_effect=_tick)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        self.temp.cleanup()

    def speak(self, count, user_id=USER):
        for _ in range(count):
            self.store.record_group_activity(
                CHAT, "群", "", "supergroup", user_id, "alice", "Alice", messages=1,
            )

    def bet(self, amount=10, schedule_open=True):
        dice_msg = SimpleNamespace(dice=SimpleNamespace(value=6))
        message = SimpleNamespace(reply_dice=AsyncMock(return_value=dice_msg),
                                  reply_text=AsyncMock(), message_id=9)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=USER, username="alice", full_name="Alice"),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={},
                                  application=SimpleNamespace(bot_data={"store": self.store}))
        with patch("tg_directory_bot.bot.dice_schedule_is_open", return_value=schedule_open):
            asyncio.run(point_dice_bet_reply(update, context, "大", amount))
        return message

    def balance(self):
        return Decimal(str(self.store.point_account(CHAT, USER)["balance"]))

    # ---- storage --------------------------------------------------------

    def test_today_messages_source_and_defaults(self):
        config = self.store.points_config(CHAT)
        self.assertEqual((config["dice_min_activity"], config["dice_free_activity"]), (0, 0))
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 0)
        self.speak(3)
        self.speak(5, user_id=88)
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 3)
        with self.store.connect() as conn:  # 昨天的发言不计入今天
            conn.execute(
                """UPDATE group_activity_users SET day=DATE('now','+8 hours','-1 day')
                   WHERE user_id=88"""
            )
        self.assertEqual(self.store.user_today_messages(CHAT, 88), 0)

    def test_rule_validation(self):
        self.store.set_dice_activity_rule(CHAT, "min", 5, 1)
        with self.assertRaises(ValueError) as raised:
            self.store.set_dice_activity_rule(CHAT, "free", 3, 1)
        self.assertIn("不能低于最低当日活跃条数", str(raised.exception))
        self.store.set_dice_activity_rule(CHAT, "free", 5, 1)
        with self.assertRaises(ValueError):
            self.store.set_dice_activity_rule(CHAT, "min", 6, 1)
        self.store.set_dice_activity_rule(CHAT, "min", 0, 1)
        self.store.set_dice_activity_rule(CHAT, "free", 0, 1)
        for bad in (-1, 100001, "3", 1.5, True):
            with self.assertRaises(ValueError):
                self.store.set_dice_activity_rule(CHAT, "min", bad, 1)

    def test_migration_on_old_database(self):
        path = Path(self.temp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE group_points_config (chat_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO group_points_config (chat_id) VALUES (-5)")
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        config = store.points_config(-5)
        self.assertEqual((config["dice_min_activity"], config["dice_free_activity"]), (0, 0))

    # ---- betting --------------------------------------------------------

    def test_min_activity_blocks_until_reached(self):
        self.store.set_dice_activity_rule(CHAT, "min", 3, 1)
        self.speak(2)
        message = self.bet()
        message.reply_dice.assert_not_awaited()
        self.assertEqual(message.reply_text.await_args.args[0],
                         "今日活跃不足：需要当日有效发言 3 条才能玩骰子（1 分钟内最多算 2 条，少于 3 个字不算），你今天已有效发言 2 条")
        self.assertEqual(self.balance(), 500)
        self.speak(1)
        message = self.bet()
        message.reply_dice.assert_awaited_once()
        self.assertEqual(self.balance(), 510)

    def test_default_zero_keeps_old_behaviour(self):
        message = self.bet()
        message.reply_dice.assert_awaited_once()
        self.store.set_dice_schedule(CHAT, True, "09:00", "10:00", 1)
        message = self.bet(schedule_open=False)
        message.reply_dice.assert_not_awaited()
        self.assertEqual(message.reply_text.await_args.args[0],
                         "骰子当前未开放，每日开放时间：09:00-10:00")

    def test_free_activity_bypasses_closed_schedule(self):
        self.store.set_dice_schedule(CHAT, True, "09:00", "10:00", 1)
        self.store.set_dice_activity_rule(CHAT, "free", 4, 1)
        self.speak(3)
        message = self.bet(schedule_open=False)
        message.reply_dice.assert_not_awaited()
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "骰子当前未开放，每日开放时间：09:00-10:00\n今日有效发言满 4 条可不受时间限制（1 分钟内最多算 2 条，少于 3 个字不算，当前 3 条）",
        )
        self.assertEqual(self.balance(), 500)
        self.speak(1)
        message = self.bet(schedule_open=False)
        message.reply_dice.assert_awaited_once()
        self.assertEqual(self.balance(), 510)
        # 开放时间内，未达标成员照常可玩
        self.store.set_dice_activity_rule(CHAT, "free", 100, 1)
        self.assertEqual(self.bet(schedule_open=True).reply_dice.await_count, 1)

    def test_bypass_still_respects_toggle_min_bet_cap_and_threshold(self):
        self.store.set_dice_schedule(CHAT, True, "09:00", "10:00", 1)
        self.store.set_dice_activity_rule(CHAT, "min", 2, 1)
        self.store.set_dice_activity_rule(CHAT, "free", 2, 1)
        self.store.set_dice_min_bet(CHAT, 5, 1)
        self.store.set_dice_max_bet(CHAT, 50, 1)
        self.speak(2)
        cases = [
            (1, "每次至少需要 5 积分"),
            (60, "单注最高 50 积分"),
        ]
        for amount, expected in cases:
            message = self.bet(amount, schedule_open=False)
            message.reply_dice.assert_not_awaited()
            self.assertEqual(message.reply_text.await_args.args[0], expected)
        self.store.set_dice_enabled(CHAT, False, 1)
        message = self.bet(10, schedule_open=False)
        message.reply_dice.assert_not_awaited()
        self.assertEqual(message.reply_text.await_args.args[0], "本群骰子游戏尚未开启")
        self.assertEqual(self.balance(), 500)

    # ---- settings UI ----------------------------------------------------

    def test_settings_page_and_points_center(self):
        text, markup = dice_settings_view(self.store.points_config(CHAT))
        self.assertIn("最低当日活跃：不限", text)
        self.assertIn("免定时活跃：关闭", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("points:set:dicemsgmin", data)
        self.assertIn("points:set:dicemsgfree", data)
        self.store.set_dice_activity_rule(CHAT, "min", 5, 1)
        self.store.set_dice_activity_rule(CHAT, "free", 20, 1)
        text, _ = dice_settings_view(self.store.points_config(CHAT))
        self.assertIn("最低当日活跃：今日有效发言满 5 条才能玩", text)
        self.assertIn("免定时活跃：今日有效发言满 20 条不受定时限制", text)
        status = points_status_text(self.store, CHAT)
        self.assertIn("骰子最低当日活跃：今日有效发言满 5 条才能玩", status)
        self.assertIn("骰子免定时活跃：今日有效发言满 20 条不受定时限制", status)
        for mode in ("points_dicemsgmin", "points_dicemsgfree"):
            self.assertIn(mode, settings_wizard.FLOWS)
            self.assertEqual(menu_mode_group_permission(mode), "diceodds")

    def commit(self, mode, text):
        class NoneMessage(SimpleNamespace):
            def __getattr__(self, name):
                return None

        message = NoneMessage(
            text=text, message_id=3, chat_id=CHAT,
            reply_text=AsyncMock(return_value=SimpleNamespace(chat_id=CHAT, message_id=4)),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=1, username="a", full_name="A"),
            effective_message=message,
        )
        context = SimpleNamespace(
            user_data={"menu_mode": mode}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.is_chat_admin", AsyncMock(return_value=True)), \
                patch("tg_directory_bot.bot.has_group_permission", return_value=True):
            asyncio.run(commit_group_menu_input(update, context))
        return " ".join(str(c.args[0]) for c in message.reply_text.await_args_list)

    def test_commit_inputs(self):
        reply = self.commit("points_dicemsgmin", "8")
        self.assertIn("骰子最低当日活跃已设为 8 条有效发言（1 分钟内最多算 2 条，少于 3 个字不算）", reply)
        reply = self.commit("points_dicemsgfree", "5")
        self.assertIn("不能低于最低当日活跃条数", reply)
        self.assertEqual(self.store.points_config(CHAT)["dice_free_activity"], 0)
        reply = self.commit("points_dicemsgfree", "30")
        self.assertIn("今日有效发言满 30 条的成员将不受骰子定时限制（1 分钟内最多算 2 条，少于 3 个字不算）", reply)
        config = self.store.points_config(CHAT)
        self.assertEqual((config["dice_min_activity"], config["dice_free_activity"]), (8, 30))


if __name__ == "__main__":
    unittest.main()
