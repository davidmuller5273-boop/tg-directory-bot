"""⚡ 助推奖励：成员自己助推一次加积分，取消/到期扣回。"""
import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_directory_bot import bot as botmod
from tg_directory_bot.auto_delete import is_persistent_message
from tg_directory_bot.storage import DirectoryStore

from tests import test_group_wizard_save as gw

GROUP = gw.GROUP

CHAT, INVITER, MEMBER = -70707, 11, 22


def boost_update(boost_id, user_id=MEMBER):
    boost = SimpleNamespace(boost_id=boost_id, source=SimpleNamespace(
        user=SimpleNamespace(id=user_id, full_name="Member", username="m")))
    return SimpleNamespace(chat_boost=SimpleNamespace(chat=SimpleNamespace(id=CHAT), boost=boost))


def removed_update(boost_id):
    return SimpleNamespace(removed_chat_boost=SimpleNamespace(chat=SimpleNamespace(id=CHAT), boost_id=boost_id))


class SelfBoostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.persistent = []

        async def send(*args, **kwargs):
            self.persistent.append(is_persistent_message())
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        self.ctx = SimpleNamespace(bot=self.bot, user_data={},
                                   application=SimpleNamespace(bot_data={"store": self.store}))

    def balance(self, user=MEMBER):
        account = self.store.point_account(CHAT, user)
        return Decimal(str(account["balance"])) if account else Decimal("0")

    def texts(self):
        return [call.args[1] for call in self.bot.send_message.await_args_list]

    def test_default_off(self):
        self.assertEqual(self.store.self_boost_points(CHAT), 0)
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))
        self.assertEqual(self.balance(), 0)
        self.bot.send_message.assert_not_awaited()
        self.assertIn("助推奖励：关闭", botmod.points_status_text(self.store, CHAT))

    def test_reward_dedupe_and_clawback(self):
        self.store.set_self_boost_points(CHAT, Decimal("8.5"), 1)
        self.assertIn("助推奖励：每助推一次 +8.5 积分，取消助推扣回", botmod.points_status_text(self.store, CHAT))
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))  # 同一 boost_id 重复更新
        self.assertEqual(self.balance(), Decimal("8.5"))
        asyncio.run(botmod.track_chat_boost(boost_update("b2"), self.ctx))  # 第二次助推
        self.assertEqual(self.balance(), Decimal("17"))
        texts = self.texts()
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("⚡ 感谢助推："))
        self.assertIn("+8.5 积分", texts[0])
        self.assertTrue(all(self.persistent))  # 不自动删除
        asyncio.run(botmod.track_removed_chat_boost(removed_update("b1"), self.ctx))
        asyncio.run(botmod.track_removed_chat_boost(removed_update("b1"), self.ctx))  # 重复移除只扣一次
        asyncio.run(botmod.track_removed_chat_boost(removed_update("unknown"), self.ctx))  # 未奖励过的不扣
        self.assertEqual(self.balance(), Decimal("8.5"))
        notice = self.texts()[-1]
        self.assertTrue(notice.startswith("⚡ 助推已取消："))
        self.assertIn("-8.5 积分", notice)
        self.assertEqual(len(self.texts()), 3)

    def test_clawback_uses_rewarded_amount_and_can_go_negative(self):
        self.store.set_self_boost_points(CHAT, 10, 1)
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))
        self.store.adjust_points(CHAT, MEMBER, -10, "spend", 1)
        self.store.set_self_boost_points(CHAT, 3, 1)  # 改设置后仍扣回当时奖励的 10
        asyncio.run(botmod.track_removed_chat_boost(removed_update("b1"), self.ctx))
        self.assertEqual(self.balance(), Decimal("-10"))
        # 负积分时继续获得奖励
        asyncio.run(botmod.track_chat_boost(boost_update("b3"), self.ctx))
        self.assertEqual(self.balance(), Decimal("-7"))

    def test_points_disabled_no_reward(self):
        self.store.set_self_boost_points(CHAT, 5, 1)
        self.store.set_points_enabled(CHAT, False, 1)
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))
        self.assertEqual(self.balance(), 0)

    def test_coexists_with_inviter_reward(self):
        self.store.set_self_boost_points(CHAT, 5, 1)
        link = self.store.save_invite_link(CHAT, INVITER, "https://t.me/+x", username="inv")
        with self.store.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO group_invite_config (chat_id) VALUES (?)", (CHAT,))
            conn.execute("UPDATE group_invite_config SET premium_boost_points=30 WHERE chat_id=?", (CHAT,))
        self.store.record_invite_join(CHAT, MEMBER, INVITER, link, 0)
        asyncio.run(botmod.track_chat_boost(boost_update("b1"), self.ctx))
        self.assertEqual(self.balance(MEMBER), 5)
        self.assertEqual(self.balance(INVITER), 30)
        asyncio.run(botmod.track_removed_chat_boost(removed_update("b1"), self.ctx))
        self.assertEqual(self.balance(MEMBER), 0)
        self.assertEqual(self.balance(INVITER), 0)

    def test_buttons(self):
        markup = botmod.points_menu_keyboard(True, True)
        self.assertIn(("⚡ 助推奖励", "points:set:selfboost"),
                      [(b.text, b.callback_data) for r in markup.inline_keyboard for b in r])
        rows = botmod.category_group_rows("points", {"points"})
        self.assertIn("points:set:selfboost", [b.callback_data for r in rows for b in r])
        self.assertIn("⚡ 助推奖励", botmod.HELP_TEXT)


class SelfBoostWizardTest(gw.GroupWizardSaveTest):
    async def test_group_admin_saves_self_boost(self):
        await self.tap("group:points")
        await self.tap("points:set:selfboost")
        await self.send("6.5")
        await self.save()
        self.assertEqual(self.store.self_boost_points(GROUP), Decimal("6.5"))
        self.assert_saved_cleanly("助推奖励已设为每助推一次 +6.5 积分（取消助推会扣回）。")

    async def test_private_admin_turns_off(self):
        self.private = True
        self.app.user_data[7]["selected_group_id"] = GROUP
        self.store.set_self_boost_points(GROUP, 3, 1)
        await self.tap("group:points")
        await self.tap("points:set:selfboost")
        await self.send("0")
        await self.save()
        self.assertEqual(self.store.self_boost_points(GROUP), 0)
        self.assert_saved_cleanly("助推奖励已关闭。")


# 只运行本文件新增的用例，不重复运行父类里的用例
for _name in [n for n in dir(gw.GroupWizardSaveTest) if n.startswith("test_")]:
    if _name not in SelfBoostWizardTest.__dict__:
        setattr(SelfBoostWizardTest, _name, None)
