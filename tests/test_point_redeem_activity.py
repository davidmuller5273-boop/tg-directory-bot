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
    point_gifts_text,
    point_redeem_reply,
    points_menu_keyboard,
    points_status_text,
    track_group_activity,
)
from tg_directory_bot.storage import DirectoryStore

CHAT, USER = -80808, 66
REFUSE = "今日活跃不足：需要当日有效发言 {n} 条才能兑换（1 分钟内最多算 2 条，少于 3 个字不算），你今天已有效发言 {x} 条"


class PointRedeemActivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.store.adjust_points(CHAT, USER, 100, "seed", 1, "u", "U")
        self.gift = self.store.add_point_gift(CHAT, "礼品", 10, -1, 1)
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

    def balance(self):
        return Decimal(str(self.store.point_account(CHAT, USER)["balance"]))

    def redemptions(self):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM point_redemptions WHERE chat_id=? AND user_id=?",
                (CHAT, USER),
            ).fetchone()["n"]

    def speak(self, text="大家好啊", chat_id=CHAT):
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

    def redeem(self):
        return self.store.redeem_point_gift(CHAT, USER, self.gift, "u", "U")

    def test_threshold_zero_allows(self):
        self.assertEqual(self.store.points_config(CHAT)["redeem_min_activity"], 0)
        _, name, balance = self.redeem()          # 默认不限，未发言也可兑换
        self.assertEqual(name, "礼品")
        self.assertEqual(Decimal(str(balance)), 90)
        self.assertEqual(self.redemptions(), 1)

    def test_below_threshold_refuses_without_deduction(self):
        self.store.set_point_redeem_min_activity(CHAT, 2, 1)
        with self.assertRaises(ValueError) as raised:
            self.redeem()
        self.assertEqual(str(raised.exception), REFUSE.format(n=2, x=0))
        self.speak()
        self.speak("大3")                          # 骰子口令不算发言
        self.speak(chat_id=CHAT - 1)               # 其他群的发言不算
        with self.assertRaises(ValueError) as raised:
            self.redeem()
        self.assertEqual(str(raised.exception), REFUSE.format(n=2, x=1))
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.redemptions(), 0)

    def test_at_and_above_threshold_allows(self):
        self.store.set_point_redeem_min_activity(CHAT, 2, 1)
        self.speak()
        self.speak()
        self.redeem()                              # 恰好满足
        self.assertEqual(self.balance(), 90)
        self.speak()
        self.redeem()                              # 超过门槛
        self.assertEqual(self.balance(), 80)
        self.assertEqual(self.redemptions(), 2)

    def test_validation_and_migration(self):
        for bad in (-1, 100001, "2", True):
            with self.assertRaises(ValueError):
                self.store.set_point_redeem_min_activity(CHAT, bad, 1)
        path = Path(self.temp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE group_points_config (chat_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO group_points_config (chat_id) VALUES (-5)")
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        self.assertEqual(store.points_config(-5)["redeem_min_activity"], 0)

    def reply_redeem(self, chat_id, override=None):
        message = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=9)))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=chat_id, type=ChatType.SUPERGROUP if chat_id < 0 else ChatType.PRIVATE,
            ),
            effective_user=SimpleNamespace(id=USER, username="u", full_name="U"),
            effective_message=message,
        )
        context = SimpleNamespace(
            user_data={}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.pin_raffle_message", AsyncMock()):
            asyncio.run(point_redeem_reply(update, context, self.gift, override))
        return message.reply_text.await_args.args[0]

    def test_group_and_private_entry_both_use_group_count(self):
        self.store.set_point_redeem_min_activity(CHAT, 1, 1)
        self.assertEqual(self.reply_redeem(CHAT), REFUSE.format(n=1, x=0))
        self.assertEqual(self.reply_redeem(USER, override=CHAT), REFUSE.format(n=1, x=0))
        self.assertEqual(self.balance(), 100)
        self.speak()
        self.assertIn("兑换成功", self.reply_redeem(USER, override=CHAT))
        self.assertEqual(self.balance(), 90)

    def test_views_and_settings(self):
        callbacks = [
            b.callback_data
            for row in points_menu_keyboard(True, True).inline_keyboard for b in row
        ]
        self.assertIn("points:set:redeemmsgmin", callbacks)
        self.assertIn("积分兑换最低当日活跃：不限", points_status_text(self.store, CHAT))
        self.assertNotIn("兑换条件", point_gifts_text(self.store, CHAT))
        self.store.set_point_redeem_min_activity(CHAT, 6, 1)
        self.assertIn("积分兑换最低当日活跃：今日有效发言满 6 条才能兑换",
                      points_status_text(self.store, CHAT))
        self.assertIn("兑换条件：今日有效发言满 6 条（1 分钟内最多算 2 条，少于 3 个字不算）", point_gifts_text(self.store, CHAT))
        self.assertIn("points_redeemmsgmin", settings_wizard.FLOWS)

    def test_commit_input(self):
        class NoneMessage(SimpleNamespace):
            def __getattr__(self, name):
                return None

        message = NoneMessage(
            text="12", message_id=3, chat_id=CHAT,
            reply_text=AsyncMock(return_value=SimpleNamespace(chat_id=CHAT, message_id=4)),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=1, username="a", full_name="A"),
            effective_message=message,
        )
        context = SimpleNamespace(
            user_data={"menu_mode": "points_redeemmsgmin"}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.is_chat_admin", AsyncMock(return_value=True)), \
                patch("tg_directory_bot.bot.has_group_permission", return_value=True):
            asyncio.run(commit_group_menu_input(update, context))
        self.assertEqual(self.store.points_config(CHAT)["redeem_min_activity"], 12)
        reply = message.reply_text.await_args_list[0].args[0]
        self.assertIn("积分兑换最低当日活跃已设为 12 条有效发言", reply)
        self.assertIn("积分兑换最低当日活跃：今日有效发言满 12 条才能兑换", reply)


if __name__ == "__main__":
    unittest.main()
