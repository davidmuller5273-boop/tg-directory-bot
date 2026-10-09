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
    point_draw_gift,
    point_draw_settings_view,
    point_draw_view,
    points_status_text,
    track_group_activity,
)
from tg_directory_bot.storage import DirectoryStore

CHAT, USER = -90909, 55


class PointDrawActivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.store.set_point_draw_config(CHAT, True, 10, 1.0, 1)
        self.store.adjust_points(CHAT, USER, 100, "seed", 1, "u", "U")
        self.gift = self.store.add_point_gift(CHAT, "礼品", 1000, -1, 1)
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

    def speak(self, text="大家好啊"):
        message = SimpleNamespace(
            text=text, caption=None, new_chat_members=[], left_chat_member=None,
            entities=[], caption_entities=[], chat_id=CHAT, message_id=1,
            reply_text=AsyncMock(), delete=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, title="群", username="", type=ChatType.SUPERGROUP),
            effective_message=message,
            effective_user=SimpleNamespace(id=USER, username="u", full_name="U", is_bot=False),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), user_data={},
                                  application=SimpleNamespace(bot_data={"store": self.store}))
        asyncio.run(track_group_activity(update, context))

    def test_storage_gate_and_validation(self):
        self.assertEqual(self.store.points_config(CHAT)["draw_min_activity"], 0)
        self.store.draw_point_gift(CHAT, USER, self.gift, "u", "U")  # 默认不限
        self.store.set_point_draw_min_activity(CHAT, 2, 1)
        before = self.balance()
        self.speak()
        with self.assertRaises(ValueError) as raised:
            self.store.draw_point_gift(CHAT, USER, self.gift, "u", "U")
        self.assertEqual(str(raised.exception), "今日活跃不足：需要当日有效发言 2 条才能参与积分抽奖（1 分钟内最多算 2 条，少于 3 个字不算），你今天已有效发言 1 条")
        self.assertEqual(self.balance(), before)
        self.speak("大3")   # 骰子口令不算发言
        with self.assertRaises(ValueError):
            self.store.draw_point_gift(CHAT, USER, self.gift, "u", "U")
        self.speak()
        result = self.store.draw_point_gift(CHAT, USER, self.gift, "u", "U")
        self.assertEqual(Decimal(str(result["points_spent"])), 10)
        for bad in (-1, 100001, "2", True):
            with self.assertRaises(ValueError):
                self.store.set_point_draw_min_activity(CHAT, bad, 1)

    def test_migration_on_old_database(self):
        path = Path(self.temp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE group_points_config (chat_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO group_points_config (chat_id) VALUES (-5)")
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        self.assertEqual(store.points_config(-5)["draw_min_activity"], 0)

    def click(self, chat_id, override=None):
        query = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
            from_user=SimpleNamespace(id=USER, username="u", full_name="U"),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(
            user_data={}, bot=SimpleNamespace(send_message=AsyncMock()),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.has_group_permission", return_value=False):
            asyncio.run(point_draw_gift(update, context, self.gift, override))
        return query

    def test_group_and_private_entry_both_use_group_count(self):
        self.store.set_point_draw_min_activity(CHAT, 1, 1)
        query = self.click(CHAT)                     # 群内按钮
        query.answer.assert_awaited_once_with(
            "今日活跃不足：需要当日有效发言 1 条才能参与积分抽奖（1 分钟内最多算 2 条，少于 3 个字不算），你今天已有效发言 0 条", show_alert=True,
        )
        query = self.click(USER, override=CHAT)      # 私聊群组管理里选中的群
        self.assertIn("才能参与积分抽奖", query.answer.await_args.args[0])
        self.assertEqual(self.balance(), 100)
        self.speak()
        query = self.click(USER, override=CHAT)
        self.assertNotIn("今日活跃不足", query.answer.await_args.args[0])
        self.assertEqual(self.balance(), 90)

    def test_views_and_settings(self):
        text, markup = point_draw_settings_view(self.store, CHAT)
        self.assertIn("最低当日活跃：不限", text)
        self.assertIn("points:set:drawmsgmin",
                      [b.callback_data for row in markup.inline_keyboard for b in row])
        self.assertNotIn("参与条件", point_draw_view(self.store, CHAT)[0])
        self.store.set_point_draw_min_activity(CHAT, 6, 1)
        self.assertIn("最低当日活跃：今日有效发言满 6 条才能参与",
                      point_draw_settings_view(self.store, CHAT)[0])
        self.assertIn("参与条件：今日有效发言满 6 条（1 分钟内最多算 2 条，少于 3 个字不算）", point_draw_view(self.store, CHAT)[0])
        self.assertIn("积分抽奖：开启，每次 10 积分，今日有效发言满 6 条可参与",
                      points_status_text(self.store, CHAT))
        self.assertIn("points_drawmsgmin", settings_wizard.FLOWS)

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
            user_data={"menu_mode": "points_drawmsgmin"}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.is_chat_admin", AsyncMock(return_value=True)), \
                patch("tg_directory_bot.bot.has_group_permission", return_value=True):
            asyncio.run(commit_group_menu_input(update, context))
        self.assertEqual(self.store.points_config(CHAT)["draw_min_activity"], 12)
        reply = message.reply_text.await_args_list[0].args[0]
        self.assertIn("积分抽奖最低当日活跃已设为 12 条有效发言", reply)
        self.assertIn("⚙️ 积分抽奖设置", reply)


if __name__ == "__main__":
    unittest.main()
