"""邀请奖励、活跃阶梯的设置向导。"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, Message, User

from tg_directory_bot import bot as handlers, settings_wizard as wizard
from tg_directory_bot.storage import DirectoryStore


class NewWizardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DirectoryStore(Path(self.temp.name) / "bot.sqlite3")
        self.store.init()
        self.user = User(7, "Tester", False, username="tester")
        self.chat = Chat(7, "private")
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock(return_value=Message(900, datetime.now(timezone.utc), self.chat, text="sent"))
        self.bot.edit_message_text = AsyncMock()
        for name in ("send_photo", "send_video", "send_animation", "send_audio", "send_voice", "send_document", "send_sticker", "send_video_note", "unpin_chat_message", "delete_message"):
            setattr(self.bot, name, AsyncMock())
        config = SimpleNamespace(developer_ids={7}, super_admin_ids=set(), admin_ids=set(), is_clone=False)
        self.context = SimpleNamespace(
            bot=self.bot, user_data={"selected_group_id": -100},
            application=SimpleNamespace(bot_data={"store": self.store, "config": config}),
        )
        self.panel = self.message("panel", message_id=50)

    def message(self, text=None, message_id=100, **kwargs):
        msg = Message(message_id, datetime.now(timezone.utc), self.chat, from_user=self.user, text=text, **kwargs)
        msg.set_bot(self.bot)
        return msg

    def update(self, message=None, query=None):
        return SimpleNamespace(
            effective_user=self.user, effective_chat=self.chat,
            effective_message=message or self.panel, message=message,
            callback_query=query,
        )

    def query_update(self, data):
        query = SimpleNamespace(data=data, message=self.panel, from_user=self.user, answer=AsyncMock(), edit_message_text=AsyncMock())
        return self.update(query=query)

    async def begin(self, mode):
        self.context.user_data["menu_mode"] = mode
        await wizard.begin(self.query_update("test"), self.context, -100)
        return self.context.user_data["settings_draft"]

    async def answer(self, text=None, **kwargs):
        await handlers.group_menu_input(self.update(self.message(text, **kwargs)), self.context)

    async def click(self, action, draft=None):
        draft = draft or self.context.user_data["settings_draft"]
        update = self.query_update(f"wizard:{draft.nonce}:{draft.revision}:{action}")
        await handlers.handle_callback(update, self.context)
        return update

    async def test_invite_premium_and_normal(self):
        await self.begin("invite_premium")
        await self.answer("20")
        await self.answer("50")
        await self.answer("30")
        await self.click("save")
        config = self.store.invite_config(-100)
        self.assertEqual((int(config["premium_msg_threshold"]), float(config["premium_msg_points"]),
                          float(config["premium_boost_points"])), (20, 50.0, 30.0))
        await self.begin("invite_normal")
        await self.answer("10")
        await self.answer("5")
        await self.click("save")
        config = self.store.invite_config(-100)
        self.assertEqual((int(config["normal_msg_threshold"]), float(config["normal_msg_points"])), (10, 5.0))

    async def test_activity_tiers_add_and_delete(self):
        await self.begin("points_tieradd")
        await self.answer("10")
        await self.answer("5")
        await self.click("save")
        await self.begin("points_tieradd")
        await self.answer("30")
        await self.answer("20")
        await self.click("save")
        tiers = self.store.activity_tiers(-100)
        self.assertEqual([(int(t["messages"]), float(t["points"])) for t in tiers], [(10, 5.0), (30, 20.0)])
        first_id = tiers[0]["id"]
        await self.begin("points_tierdel")
        await self.answer(f"#{first_id}")
        await self.click("save")
        self.assertEqual([int(t["messages"]) for t in self.store.activity_tiers(-100)], [30])
        text, markup = handlers.activity_settings_view(self.store, -100)
        self.assertIn("30", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        for cb in ("points:set:activity", "points:set:tieradd", "points:set:tierdel", "group:points"):
            self.assertIn(cb, data)


if __name__ == "__main__":
    unittest.main()
