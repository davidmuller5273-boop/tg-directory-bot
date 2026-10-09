"""邀请发言/助推奖励、活跃阶梯奖励、负积分仍可获得奖励、奖励消息不自动删除。"""
import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatMemberStatus, ChatType

from tg_directory_bot import bot as botmod
from tg_directory_bot.auto_delete import is_persistent_message
from tg_directory_bot.storage import DirectoryStore

CHAT, INVITER, MEMBER = -60606, 11, 22


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        self.store.set_points_enabled(CHAT, True, 1)
        self.now = 1_700_000_000.0
        clock = patch("tg_directory_bot.storage.activity_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        self.temp.cleanup()

    def balance(self, user):
        account = self.store.point_account(CHAT, user)
        return Decimal(str(account["balance"])) if account else Decimal("0")

    def context(self):
        return SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()), user_data={},
            application=SimpleNamespace(bot_data={"store": self.store}),
        )

    def send(self, text="大家好呀", user_id=MEMBER, premium=False, step=61, context=None):
        self.now += step
        message = SimpleNamespace(
            text=text, caption=None, new_chat_members=[], left_chat_member=None,
            entities=[], caption_entities=[], chat_id=CHAT, message_id=1,
            reply_text=AsyncMock(), delete=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, title="群", username="", type=ChatType.SUPERGROUP),
            effective_message=message,
            effective_user=SimpleNamespace(id=user_id, username="m", full_name="Member",
                                           is_bot=False, is_premium=premium),
        )
        context = context or self.context()
        asyncio.run(botmod.track_group_activity(update, context))
        return message, context


class InviteRewardTest(_Base):
    def setUp(self):
        super().setUp()
        link_id = self.store.save_invite_link(CHAT, INVITER, "https://t.me/+abc", "x", "inv", "Inviter")
        self.link_id = link_id
        self.store.update_invite_rewards(
            CHAT, 1, premium_msg_threshold=3, premium_msg_points=50,
            premium_boost_points=30, normal_msg_threshold=2, normal_msg_points=10,
        )

    def join(self, premium):
        self.store.record_invite_join(CHAT, MEMBER, INVITER, self.link_id, 0, "m", "Member")
        self.store.mark_invitee_premium(CHAT, MEMBER, premium)

    def test_defaults_are_off(self):
        config = self.store.invite_config(-1)
        for key in ("premium_msg_threshold", "premium_msg_points", "premium_boost_points",
                    "normal_msg_threshold", "normal_msg_points"):
            self.assertEqual(float(config[key] or 0), 0)

    def test_normal_invitee_message_reward_and_clawback(self):
        self.join(False)
        self.send()
        self.assertEqual(self.balance(INVITER), 0)
        _msg, ctx = self.send()
        self.assertEqual(self.balance(INVITER), 10)
        text = ctx.bot.send_message.await_args.args[1]
        self.assertIn("邀请奖励", text)
        self.send()
        self.send()
        self.assertEqual(self.balance(INVITER), 10)   # 只发一次
        departed = self.store.record_invite_leave(CHAT, MEMBER)
        self.store.revoke_invite_extra_rewards(CHAT, MEMBER, INVITER, departed["msg_reward"])
        self.assertEqual(self.balance(INVITER), 0)

    def test_short_or_fast_messages_do_not_count_for_invite(self):
        self.join(False)
        self.send("好")
        self.send("ok")
        self.assertEqual(self.balance(INVITER), 0)
        self.send(step=1)
        self.send(step=1)
        self.send(step=1)   # 第 3 条在 1 分钟内不算，但前 2 条已满足门槛
        self.assertEqual(self.balance(INVITER), 10)

    def test_premium_detected_from_message(self):
        self.join(False)
        for _ in range(3):
            self.send(premium=True)
        self.assertEqual(self.balance(INVITER), 50)

    def test_boost_reward_and_removal(self):
        self.join(True)
        boost = SimpleNamespace(boost_id="b1", source=SimpleNamespace(
            user=SimpleNamespace(id=MEMBER, full_name="Member", username="m")))
        update = SimpleNamespace(chat_boost=SimpleNamespace(chat=SimpleNamespace(id=CHAT), boost=boost))
        ctx = self.context()
        asyncio.run(botmod.track_chat_boost(update, ctx))
        asyncio.run(botmod.track_chat_boost(update, ctx))   # 同一助推不重复发
        self.assertEqual(self.balance(INVITER), 30)
        self.assertIn("助推奖励", ctx.bot.send_message.await_args.args[1])
        removed = SimpleNamespace(removed_chat_boost=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), boost_id="b1"))
        asyncio.run(botmod.track_removed_chat_boost(removed, ctx))
        self.assertEqual(self.balance(INVITER), 0)

    def test_leave_claws_back_boost_and_message_rewards(self):
        self.join(True)
        self.store.award_invite_boost(CHAT, "b1", MEMBER)
        self.store.award_invite_boost(CHAT, "b2", MEMBER)
        for _ in range(3):
            self.send()
        self.assertEqual(self.balance(INVITER), 110)
        member = SimpleNamespace(id=MEMBER, username="m", full_name="Member", is_premium=True)
        actor = SimpleNamespace(id=MEMBER, username="m", full_name="Member")
        update = SimpleNamespace(chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), from_user=actor, invite_link=None,
            old_chat_member=SimpleNamespace(status=ChatMemberStatus.MEMBER, user=member),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.BANNED, user=member),
        ))
        asyncio.run(botmod.track_personal_invite(update, self.context()))
        self.assertEqual(self.balance(INVITER), 0)

    def test_non_invited_boost_ignored(self):
        self.assertIsNone(self.store.award_invite_boost(CHAT, "x", 999))


class ActivityTierTest(_Base):
    def test_tiers_once_per_day(self):
        self.store.set_activity_tier(CHAT, 2, 5, 1)
        self.store.set_activity_tier(CHAT, 4, 20, 1)
        replies = []
        for _ in range(6):
            message, _ctx = self.send()
            replies.extend(c.args[0] for c in message.reply_text.await_args_list)
        self.assertEqual(self.balance(MEMBER), 25)
        self.assertEqual(len([r for r in replies if "阶梯奖励" in r]), 2)
        self.store.set_activity_tier(CHAT, 4, 30, 1)   # 修改同一档
        self.assertEqual([(int(r["messages"]), float(r["points"])) for r in self.store.activity_tiers(CHAT)],
                         [(2, 5.0), (4, 30.0)])
        tier_id = self.store.activity_tiers(CHAT)[0]["id"]
        self.assertTrue(self.store.delete_activity_tier(CHAT, tier_id))
        self.assertEqual(len(self.store.activity_tiers(CHAT)), 1)

    def test_tiers_coexist_with_random_reward(self):
        self.store.set_activity_points(CHAT, 2, 2, 3, 3, 1)
        self.store.set_activity_tier(CHAT, 2, 5, 1)
        self.send()
        self.send()
        self.assertEqual(self.balance(MEMBER), 8)

    def test_negative_balance_still_earns(self):
        self.store.adjust_points(CHAT, MEMBER, -50, "罚", 1, allow_negative=True)
        self.store.set_activity_tier(CHAT, 1, 5, 1)
        self.send()
        self.assertEqual(self.balance(MEMBER), -45)
        self.store.adjust_points(CHAT, MEMBER, 3, "管理员加分", 1)
        self.assertEqual(self.balance(MEMBER), -42)
        with self.assertRaises(ValueError):
            self.store.adjust_points(CHAT, MEMBER, -1, "扣分", 1)

    def test_award_messages_are_persistent(self):
        self.store.set_activity_points(CHAT, 1, 1, 3, 3, 1)
        self.store.set_activity_tier(CHAT, 1, 5, 1)
        flags = []

        async def reply(*_a, **_k):
            flags.append(is_persistent_message())

        self.now += 61
        message = SimpleNamespace(
            text="大家好呀", caption=None, new_chat_members=[], left_chat_member=None,
            entities=[], caption_entities=[], chat_id=CHAT, message_id=1,
            reply_text=reply, delete=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT, title="群", username="", type=ChatType.SUPERGROUP),
            effective_message=message,
            effective_user=SimpleNamespace(id=MEMBER, username="m", full_name="M", is_bot=False),
        )
        asyncio.run(botmod.track_group_activity(update, self.context()))
        self.assertEqual(flags, [True, True])


if __name__ == "__main__":
    unittest.main()
