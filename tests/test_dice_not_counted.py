import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType

from tg_directory_bot.bot import is_dice_command, track_group_activity
from tg_directory_bot.storage import DirectoryStore

CHAT, USER = -70707, 321


class DiceNotCountedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)  # 骰子默认开启
        self.user = SimpleNamespace(id=USER, username="m", full_name="Member", is_bot=False)
        self.chat = SimpleNamespace(id=CHAT, title="群", username="", type=ChatType.SUPERGROUP)

    def tearDown(self):
        self.temp.cleanup()

    def send(self, text):
        message = SimpleNamespace(
            text=text, caption=None, new_chat_members=[], left_chat_member=None,
            entities=[], caption_entities=[], chat_id=CHAT, message_id=1,
            reply_text=AsyncMock(), delete=AsyncMock(),
        )
        update = SimpleNamespace(effective_chat=self.chat, effective_message=message,
                                 effective_user=self.user)
        context = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()), user_data={},
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        asyncio.run(track_group_activity(update, context))
        return message

    def counts(self):
        with self.store.connect() as conn:
            events = conn.execute(
                "SELECT COUNT(*) FROM group_message_events WHERE chat_id=? AND user_id=?",
                (CHAT, USER),
            ).fetchone()[0]
            group = conn.execute(
                "SELECT message_count FROM groups WHERE chat_id=?", (CHAT,)
            ).fetchone()
            daily = conn.execute(
                "SELECT COALESCE(SUM(messages),0) FROM group_daily_stats WHERE chat_id=?",
                (CHAT,),
            ).fetchone()[0]
        return (self.store.user_today_messages(CHAT, USER), events,
                int(group[0]) if group else 0, int(daily))

    def test_dice_commands_are_not_counted(self):
        # 含会被拒绝的下注（如超出余额的 大99999）
        for text in ("大3", "小 5", "单：10", "双-2", "大1.5", "大99999"):
            self.send(text)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.send("大家好")
        self.send("大0")      # 解析器不认（金额无效）→ 普通消息
        self.send("大3元")    # 不是纯口令 → 普通消息
        self.assertEqual(self.counts(), (3, 3, 3, 3))

    def test_counted_when_dice_not_active(self):
        self.store.set_dice_enabled(CHAT, False, 1)
        self.send("大3")
        self.assertEqual(self.counts()[0], 1)
        other = -80808
        self.assertFalse(is_dice_command(self.store.points_config(other),
                                         SimpleNamespace(text="大3")))

    def test_dice_command_does_not_trigger_activity_reward(self):
        self.store.set_activity_points(CHAT, 1, 1, 5, 5, 1)
        message = self.send("大3")
        message.reply_text.assert_not_awaited()
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 0)
        message = self.send("你好")
        message.reply_text.assert_awaited_once()
        self.assertIn("今日已发言 1 条", message.reply_text.await_args.args[0])

    def test_dice_threshold_uses_count_without_dice_commands(self):
        self.store.set_dice_activity_rule(CHAT, "min", 2, 1)
        for _ in range(5):
            self.send("大1")
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 0)
        self.send("聊天一")
        self.send("聊天二")
        self.assertEqual(self.store.user_today_messages(CHAT, USER), 2)


if __name__ == "__main__":
    unittest.main()
