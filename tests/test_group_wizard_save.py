"""Regression: settings wizard saves must work inside groups.

User report: 「这个机器人积分抽奖设置单次的积分保存不了啊」.
Outside private chats PTB auto-quotes replies (reply_parameters), and
Bot.send_message rejects ``allow_sending_without_reply`` together with
``reply_parameters``. MessageInput.reply_text used to pass that flag directly,
so every wizard commit in a group raised *after* the value was written and the
panel showed 「保存未完成：`allow_sending_without_reply` and `reply_parameters`
are mutually exclusive」.

These tests drive the real Application/handlers with a real ExtBot whose HTTP
layer is faked, so PTB's own argument validation runs (mocked bots hid this).
"""

import asyncio
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Chat, ReplyParameters, Update
from telegram.request import BaseRequest

from tg_directory_bot import bot as handlers
from tg_directory_bot import settings_wizard
from tg_directory_bot.config import Config

BOT_ID = 999000
GROUP = -100123
ADMIN = 7


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self.ids = itertools.count(1000)

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        api = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((api, params))
        if api == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": "testbot"}
        elif api in {"sendMessage", "editMessageText"}:
            chat_id = int(params["chat_id"])
            result = {
                "message_id": int(params.get("message_id") or next(self.ids)),
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "title": "G"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


class GroupWizardSaveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config(
            bot_token="1:test", admin_ids=set(), super_admin_ids=set(),
            db_path=Path(self.temp.name) / "bot.sqlite3", categories=("tools",),
            blocked_keywords=(), tron_block_scan=False,
        )
        self.app = handlers.build_application(config)
        self.request = FakeRequest()
        object.__setattr__(self.app.bot, "_request", (self.request, self.request))
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.store = self.app.bot_data["store"]
        # 普通群管理员（非超管），只分配了积分和骰子权限
        self.store.set_group_admin_permissions(GROUP, ADMIN, {"points", "diceodds"}, 1)
        self.store.set_points_enabled(GROUP, True, 1)
        self.store.set_point_draw_config(GROUP, True, 5, 1.0, 1)
        self.update_ids = itertools.count(1)
        self.message_ids = itertools.count(5000)
        self.panel_id = 555
        self.private = False

    # -- helpers -----------------------------------------------------------
    def chat(self):
        if self.private:
            return {"id": ADMIN, "type": "private", "first_name": "Admin"}
        return {"id": GROUP, "type": "supergroup", "title": "G"}

    def user(self):
        return {"id": ADMIN, "is_bot": False, "first_name": "Admin", "username": "admin"}

    async def send(self, text):
        data = {"update_id": next(self.update_ids), "message": {
            "message_id": next(self.message_ids), "date": int(time.time()),
            "chat": self.chat(), "from": self.user(), "text": text,
        }}
        await self.app.process_update(Update.de_json(data, self.app.bot))

    async def tap(self, callback_data, message_id=None):
        data = {"update_id": next(self.update_ids), "callback_query": {
            "id": str(next(self.update_ids)), "from": self.user(), "chat_instance": "ci",
            "data": callback_data,
            "message": {
                "message_id": message_id or self.panel_id, "date": int(time.time()),
                "chat": self.chat(), "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"},
                "text": "panel",
            },
        }}
        await self.app.process_update(Update.de_json(data, self.app.bot))

    def texts(self):
        return [str(p.get("text", "")) for api, p in self.request.calls
                if api in {"sendMessage", "editMessageText"}]

    def button(self, label):
        for api, params in reversed(self.request.calls):
            markup = params.get("reply_markup")
            if api not in {"sendMessage", "editMessageText"} or not markup:
                continue
            markup = json.loads(markup) if isinstance(markup, str) else markup
            for row in markup["inline_keyboard"]:
                for button in row:
                    if button.get("text") == label:
                        return button["callback_data"], int(params.get("message_id") or 0)
        self.fail(f"button {label!r} not found")

    async def save(self):
        data, message_id = self.button("确认保存")
        self.request.calls.clear()
        await self.tap(data, message_id)

    def user_data(self):
        return self.app.user_data[ADMIN]

    def assert_saved_cleanly(self, expected_confirmation):
        texts = self.texts()
        joined = "\n".join(texts)
        self.assertNotIn("mutually exclusive", joined)
        self.assertNotIn("保存未完成", joined)
        self.assertNotIn("未保存", joined)
        self.assertTrue(any(expected_confirmation in text for text in texts), texts)
        self.assertNotIn("settings_draft", self.user_data())
        self.assertNotIn("menu_mode", self.user_data())

    async def open_draw_settings(self):
        await self.tap("group:points")
        await self.tap("points:set:drawconfig")

    # -- 积分抽奖设置：每次（单次）消耗积分 ------------------------------
    async def test_group_admin_saves_per_draw_cost(self):
        await self.open_draw_settings()
        await self.tap("points:set:drawmincost")
        await self.send("20")
        await self.save()
        self.assertEqual(self.store.points_config(GROUP)["draw_cost"], 20)
        self.assert_saved_cleanly("积分抽奖最低消耗已设为 20 积分。")
        reply = next(p for api, p in self.request.calls if api == "sendMessage")
        self.assertIn("每次最低消耗：20 积分", reply["text"])
        self.assertNotIn("allow_sending_without_reply", reply)
        self.assertEqual(reply["reply_parameters"]["allow_sending_without_reply"], True)
        self.assertIn("积分抽奖：开启，每次 20 积分", handlers.points_status_text(self.store, GROUP))

    async def test_private_admin_saves_per_draw_cost(self):
        self.private = True
        self.app.user_data[ADMIN]["selected_group_id"] = GROUP
        await self.open_draw_settings()
        await self.tap("points:set:drawmincost")
        await self.send("12.5")
        await self.save()
        self.assertEqual(self.store.points_config(GROUP)["draw_cost"], 12.5)
        self.assert_saved_cleanly("积分抽奖最低消耗已设为 12.5 积分。")

    async def test_group_member_spend_for_this_draw(self):
        await self.tap("points:draw")
        await self.tap("points:drawcost")
        await self.send("20")
        await self.save()
        self.assertEqual(self.user_data()[f"point_draw_spend:{GROUP}"], 20.0)
        self.assert_saved_cleanly("本次抽奖消耗已设为 20 积分。")

    async def test_group_validation_error_is_shown_not_ptb_error(self):
        await self.tap("points:draw")
        await self.tap("points:drawcost")
        await self.send("2")  # 低于本群最低 5
        await self.save()
        texts = self.texts()
        self.assertTrue(any("本群每次抽奖最少消耗 5 积分" in text for text in texts), texts)
        self.assertNotIn("mutually exclusive", "\n".join(texts))
        self.assertIn("settings_draft", self.user_data())

    # -- 同一菜单与积分中心的其他设置 ------------------------------------
    async def test_group_other_draw_and_points_settings(self):
        cases = [
            ("points:set:drawmsgmin", ["3"], "积分抽奖最低当日活跃已设为 3 条",
             lambda c: c["draw_min_activity"] == 3),
            ("points:set:drawrate", ["2"], "积分抽奖中奖倍率已设为 2。",
             lambda c: c["draw_rate_multiplier"] == 2.0),
            ("points:set:redeemmsgmin", ["4"], "积分兑换最低当日活跃已设为 4 条",
             lambda c: c["redeem_min_activity"] == 4),
            ("points:set:activity", ["10", "30", "2", "8"], "每日活跃积分设置成功",
             lambda c: (c["activity_messages_min"], c["activity_messages_max"],
                        c["activity_points_min"], c["activity_points_max"]) == (10, 30, 2, 8)),
            ("points:set:checkin", ["3", "6", "1"], "签到积分设置成功。",
             lambda c: (c["checkin_min"], c["checkin_max"], c["streak_bonus"]) == (3, 6, 1)),
            ("points:set:dicemin", ["2"], "骰子每次最低参与积分已设为 2。",
             lambda c: c["dice_min_bet"] == 2),
            ("points:set:dicemsgmin", ["5"], "骰子最低当日活跃已设为 5 条",
             lambda c: c["dice_min_activity"] == 5),
        ]
        for callback, answers, confirmation, check in cases:
            with self.subTest(callback=callback):
                await self.tap("group:points")
                await self.tap(callback)
                for answer in answers:
                    await self.send(answer)
                await self.save()
                self.assertTrue(check(self.store.points_config(GROUP)))
                self.assert_saved_cleanly(confirmation)

    async def test_group_draw_toggle_via_confirmation(self):
        await self.open_draw_settings()
        await self.tap("points:set:drawtoggle")
        data, message_id = self.button("确认")
        await self.tap(data, message_id)
        await self.save()
        config = self.store.points_config(GROUP)
        self.assertFalse(config["draw_enabled"])
        self.assertEqual(config["draw_cost"], 5)
        self.assertNotIn("mutually exclusive", "\n".join(self.texts()))


class MessageInputReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_reply_folds_flag_into_reply_parameters(self):
        original = SimpleNamespace(
            chat=Chat(GROUP, "supergroup"), message_id=42, reply_text=AsyncMock(),
        )
        await settings_wizard.MessageInput(original, "1", ["1"]).reply_text("ok")
        kwargs = original.reply_text.await_args.kwargs
        self.assertNotIn("allow_sending_without_reply", kwargs)
        self.assertEqual(
            kwargs["reply_parameters"],
            ReplyParameters(message_id=42, allow_sending_without_reply=True),
        )

    async def test_private_reply_does_not_quote(self):
        original = SimpleNamespace(
            chat=Chat(ADMIN, "private"), message_id=42, reply_text=AsyncMock(),
        )
        await settings_wizard.MessageInput(original, "1", ["1"]).reply_text("ok")
        kwargs = original.reply_text.await_args.kwargs
        self.assertNotIn("allow_sending_without_reply", kwargs)
        self.assertNotIn("reply_parameters", kwargs)
        self.assertIs(kwargs["do_quote"], False)

    async def test_explicit_reply_arguments_are_respected(self):
        original = SimpleNamespace(
            chat=Chat(GROUP, "supergroup"), message_id=42, reply_text=AsyncMock(),
        )
        params = ReplyParameters(message_id=7)
        await settings_wizard.MessageInput(original, "1", ["1"]).reply_text(
            "ok", reply_parameters=params, allow_sending_without_reply=True,
        )
        kwargs = original.reply_text.await_args.kwargs
        self.assertIs(kwargs["reply_parameters"], params)
        self.assertNotIn("allow_sending_without_reply", kwargs)


if __name__ == "__main__":
    unittest.main()
