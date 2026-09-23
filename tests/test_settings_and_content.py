import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, MessageOriginChannel, MessageOriginUser, PhotoSize, User
from telegram.error import BadRequest, TelegramError

from tg_directory_bot import bot as handlers, settings_wizard as wizard
from tg_directory_bot.auto_delete import AutoDeleteBot
from tg_directory_bot.rich_content import button_content, buttons_markup, capture_buttons, capture_buttons_resolving, capture_content, content_entities, forward_channel_source, send_content
from tg_directory_bot.storage import DirectoryStore


class StorageRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.store = DirectoryStore(self.path)
        self.store.init()
        self.link = self.store.save_invite_link(-100, 7, "https://t.me/+test", username="owner")

    def test_negative_inviter_receives_decimal_reward_exactly_once(self):
        self.store.adjust_points(-100, 7, -100, "debt", 1, allow_negative=True)
        for expected in (True, False):
            self.assertEqual(self.store.record_invite_join(-100, 8, 7, self.link, Decimal("1.25"), credit_points=True), expected)
        self.assertEqual(Decimal(str(self.store.point_account(-100, 7)["balance"])), Decimal("-98.75"))
        self.assertEqual(self.store.count_point_ledger(-100, 7), 2)
        self.assertIsNone(self.store.point_account(-100, 8))

    def test_join_rolls_back_when_credit_fails(self):
        with patch.object(self.store, "_adjust_points_conn", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.store.record_invite_join(-100, 8, 7, self.link, 2, credit_points=True)
        self.assertEqual(self.store.invite_stats(-100)["invites"], 0)
        self.assertTrue(self.store.record_invite_join(-100, 8, 7, self.link, 2, credit_points=True))
        self.assertEqual(self.store.point_account(-100, 7)["balance"], 2)

    def test_reward_can_cross_zero_and_zero_reward_has_no_ledger(self):
        self.store.adjust_points(-100, 7, -1, "debt", 1, allow_negative=True)
        self.store.record_invite_join(-100, 8, 7, self.link, 3, credit_points=True)
        self.store.record_invite_join(-100, 9, 7, self.link, 0, credit_points=True)
        self.assertEqual(self.store.point_account(-100, 7)["balance"], 2)
        self.assertEqual(self.store.count_point_ledger(-100, 7), 2)

    def test_spending_still_rejects_insufficient_points(self):
        self.store.adjust_points(-100, 7, -100, "debt", 1, allow_negative=True)
        with self.assertRaises(ValueError):
            self.store.adjust_points(-100, 7, -1, "purchase", 0)
        self.assertEqual(self.store.point_account(-100, 7)["balance"], -100)

    def test_schema_upgrade_retains_existing_content_and_is_repeatable(self):
        self.store.set_group_ad(-100, "prefix", "old", 7)
        self.store.update_quick_post(-100, 7, text="post")
        with self.store.connect() as conn:
            for table in ("group_ads", "quick_posts"):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN entities_json")
        self.store.init()
        self.store.init()
        self.assertEqual(self.store.group_ad(-100, "prefix")["text"], "old")
        self.assertEqual(self.store.quick_post(-100)["entities_json"], "[]")


class ConversationTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_callback_opens_question_instead_of_pipe_prompt(self):
        await handlers.handle_callback(self.query_update("points:set:checkin"), self.context)
        self.assertIn("settings_draft", self.context.user_data)
        self.assertIn("最少奖励积分", self.bot.edit_message_text.call_args.args[0])
        self.assertNotIn("|", self.bot.edit_message_text.call_args.args[0])

    async def test_general_guard_does_not_count_usage_but_search_does(self):
        update = self.update(self.message("骰子大10"))
        self.assertTrue(await handlers.guard(update, self.context))
        self.assertEqual(self.store.bot_usage_users()[1], 0)
        handlers.record_directory_search(update, self.store, "测试", 0, "private")
        rows, total = self.store.bot_usage_users()
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["user_id"], self.user.id)

    async def test_back_next_edit_and_save_once(self):
        draft = await self.begin("points_giftadd")
        await self.answer("2.50")
        await self.answer("VIP | 礼品")
        await self.answer("10")
        self.assertEqual(self.store.point_gifts(-100), [])
        await self.click("back")
        await self.answer("20")
        update = await self.click("save")
        self.assertNotIn("settings_draft", self.context.user_data)
        gifts = self.store.point_gifts(-100)
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0]["name"], "VIP | 礼品")
        self.assertEqual(gifts[0]["stock"], 20)
        await handlers.handle_callback(update, self.context)
        self.assertEqual(len(self.store.point_gifts(-100)), 1)

    async def test_next_requires_value_and_reuses_answer_after_back(self):
        draft = await self.begin("points_checkin")
        await self.click("next")
        self.assertEqual(draft.step, 0)
        await self.answer("1")
        await self.click("back")
        await self.click("next")
        self.assertEqual(draft.step, 1)
        self.assertEqual(draft.answers[0], "1")

    async def test_cancel_does_not_mutate(self):
        await self.begin("invite_points")
        await self.answer("5")
        await self.click("cancel")
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)
        self.assertNotIn("settings_draft", self.context.user_data)

    async def test_invalid_and_nonfinite_number_stays_on_question(self):
        for value in ("NaN", "Infinity", "6", "-1", "hello"):
            draft = await self.begin("points_drawrate")
            await self.answer(value)
            self.assertEqual(draft.step, 0)
        await self.answer("1.5")
        self.assertEqual(draft.step, 1)

    async def test_three_wrong_replies_cancel_without_saving(self):
        await self.begin("invite_points")
        for _ in range(3):
            await self.answer("unrelated conversation")
        self.assertNotIn("settings_draft", self.context.user_data)
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)

    async def test_expired_state_cannot_save(self):
        draft = await self.begin("invite_points")
        await self.answer("9")
        draft.touched = time.monotonic() - 181
        await self.click("save")
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)

    async def test_foreign_click_and_wrong_chat_cannot_save(self):
        draft = await self.begin("invite_points")
        await self.answer("9")
        update = self.query_update(f"wizard:{draft.nonce}:{draft.revision}:save")
        update.effective_user = User(8, "Other", False)
        await handlers.handle_callback(update, self.context)
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)
        update.effective_user = self.user
        update.callback_query.message = Message(50, datetime.now(timezone.utc), Chat(-999, "supergroup"))
        await handlers.handle_callback(update, self.context)
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)

    async def test_revoked_permission_or_group_change_blocks_commit(self):
        await self.begin("invite_points")
        await self.answer("5")
        self.context.application.bot_data["config"].developer_ids.clear()
        await self.click("save")
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)
        self.context.application.bot_data["config"].developer_ids.add(7)
        self.context.user_data["selected_group_id"] = -200
        await self.click("save")
        self.assertEqual(self.store.invite_config(-200)["points_per_invite"], 0)

    async def test_cross_field_error_retains_draft(self):
        original = dict(self.store.points_config(-100))
        await self.begin("points_checkin")
        for value in ("5", "1", "0"):
            await self.answer(value)
        for _ in range(3):
            await self.click("save")
            self.assertIn("settings_draft", self.context.user_data)
        self.assertEqual(dict(self.store.points_config(-100)), original)

    async def test_permission_toggle_requires_confirmation(self):
        self.store.set_points_enabled(-100, True, 7)
        await handlers.handle_callback(self.query_update("points:disable"), self.context)
        self.assertTrue(self.store.points_config(-100)["is_enabled"])
        await self.click("choice0")
        await self.click("save")
        self.assertFalse(self.store.points_config(-100)["is_enabled"])

    async def test_timed_ad_preserves_forwarded_caption_entities(self):
        draft = await self.begin("group_ad_interval")
        await self.answer("10")
        raw = "  😀 Special | offer  "
        entities = [MessageEntity("custom_emoji", 2, 2, custom_emoji_id="123456789"), MessageEntity("bold", 5, 7)]
        await self.answer(None, caption=raw, caption_entities=entities, photo=[PhotoSize("file", "unique", 100, 100)], forward_origin=MessageOriginUser(datetime.now(timezone.utc), User(99, "Source", False)))
        await self.click("save")
        row = self.store.group_ad(-100, "interval")
        self.assertEqual(row["text"], raw)
        self.assertEqual(row["interval_seconds"], 600)
        self.assertEqual(row["file_id"], "file")
        await handlers.send_stored_media(self.bot, -100, row)
        kwargs = self.bot.send_photo.call_args.kwargs
        self.assertEqual(kwargs["caption"], raw)
        self.assertEqual(kwargs["caption_entities"], entities)
        self.assertIsNone(kwargs["parse_mode"])
        self.assertIsNone(kwargs.get("reply_markup"))

    async def test_send_stored_media_attaches_saved_url_buttons(self):
        buttons_json = json.dumps([[{"text": "去看看", "url": "https://example.com/ad"}]])
        self.store.set_group_ad(
            -100, "interval", "定时广告文案", 7,
            interval_seconds=600,
            buttons_json=buttons_json,
        )
        row = self.store.group_ad(-100, "interval")
        await handlers.send_stored_media(self.bot, -100, row)
        kwargs = self.bot.send_message.call_args.kwargs
        markup = kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "去看看")
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com/ad")

        with self.store.connect() as conn:
            conn.execute(
                "UPDATE group_ads SET next_run_at=DATETIME('now', '-1 minute') WHERE id=?",
                (int(row["id"]),),
            )
        self.bot.send_message.reset_mock()
        await handlers.send_due_group_ads(self.context)
        due_kwargs = self.bot.send_message.call_args.kwargs
        self.assertEqual(
            due_kwargs["reply_markup"].inline_keyboard[0][0].url,
            "https://example.com/ad",
        )

    async def test_quick_post_text_keeps_emoji_and_literal_pipe(self):
        await self.begin("quickpost_text")
        raw = " 😀|hello "
        entities = [MessageEntity("custom_emoji", 1, 2, custom_emoji_id="42")]
        await self.answer(raw, entities=entities)
        await self.click("save")
        row = self.store.quick_post(-100)
        self.assertEqual(row["text"], raw)
        await handlers.send_quick_post(self.bot, -100, row)
        self.assertEqual(self.bot.send_message.call_args.args[1], raw)
        self.assertEqual(self.bot.send_message.call_args.kwargs["entities"], entities)

    async def test_quick_post_supports_multiple_colored_buttons(self):
        row = self.store.quick_post(-100)
        self.store.add_quick_post_button(
            int(row["id"]), "继续添加", "https://example.com/one", "success", "short", "emoji-42"
        )
        self.store.add_quick_post_button(
            int(row["id"]), "第二个", "https://example.com/two", "danger", "short"
        )
        markup = handlers.quick_post_markup(self.store, row)
        buttons = [button for line in markup.inline_keyboard for button in line]
        self.assertEqual(len(buttons), 2)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(buttons[0].api_kwargs["style"], "success")
        self.assertEqual(buttons[0].api_kwargs["icon_custom_emoji_id"], "emoji-42")
        self.assertEqual(buttons[1].api_kwargs["style"], "danger")

    async def test_button_custom_icon_removes_only_its_fallback(self):
        entity = MessageEntity("custom_emoji", 0, 2, custom_emoji_id="42")
        self.assertEqual(button_content(self.message("😀 联系🎉", entities=[entity])), (" 联系🎉", "42"))
        self.assertEqual(button_content(self.message("🎉😀 内容")), ("🎉😀 内容", ""))
        self.assertEqual(button_content(self.message("😀", entities=[entity])), (" ", "42"))
        with self.assertRaisesRegex(ValueError, "一个"):
            button_content(self.message("😀😀", entities=[entity, MessageEntity("custom_emoji", 2, 2, custom_emoji_id="43")]))
        with self.assertRaisesRegex(ValueError, "最前面"):
            button_content(self.message("字😀", entities=[MessageEntity("custom_emoji", 1, 2, custom_emoji_id="42")]))

    async def test_quick_post_media_replaces_old_caption_and_entities(self):
        self.store.update_quick_post(-100, 7, text="old", entities_json='[{"type":"bold","offset":0,"length":3}]')
        await self.begin("quickpost_media")
        await self.answer(None, photo=[PhotoSize("new", "unique", 1, 1)])
        await self.click("save")
        row = self.store.quick_post(-100)
        self.assertEqual(row["text"], "")
        self.assertEqual(row["entities_json"], "[]")

    async def test_album_is_not_silently_truncated(self):
        draft = await self.begin("quickpost_media")
        await self.answer(None, photo=[PhotoSize("new", "unique", 1, 1)], media_group_id="album")
        self.assertEqual(draft.step, 0)
        self.assertIn("相册", self.bot.edit_message_text.call_args.args[0])

    async def test_inline_share_keeps_custom_emoji(self):
        raw, entity = "😀hello", MessageEntity("custom_emoji", 0, 2, custom_emoji_id="42")
        self.store.update_quick_post(-100, 7, text=raw, entities_json=json.dumps([entity.to_dict()]))
        row = self.store.quick_post(-100)
        query = SimpleNamespace(query=row["share_code"], from_user=self.user, answer=AsyncMock())
        await handlers.quick_post_inline(SimpleNamespace(inline_query=query), self.context)
        result = query.answer.call_args.args[0][0]
        self.assertEqual(result.input_message_content.message_text, raw)
        self.assertEqual(result.input_message_content.entities, (entity,))


    async def test_ad_buttons_merge_keeps_original_markup(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        bot = AutoDeleteBot("123:token")
        original = InlineKeyboardMarkup([[InlineKeyboardButton("原按钮", url="https://a.example")]])
        ad_markup = InlineKeyboardMarkup([[InlineKeyboardButton("广告", url="https://b.example")]])

        async def markup_decorator(_bot, _chat_id, position):
            return ad_markup if position == "prefix" else None

        object.__setattr__(bot, "markup_decorator", markup_decorator)
        kwargs = await bot._merge_ad_markup(
            (), {"chat_id": -100, "reply_markup": original},
        )
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[0][0].text, "广告")
        self.assertEqual(rows[1][0].text, "原按钮")

    async def test_capture_buttons_roundtrip(self):
        message = SimpleNamespace(
            text="广告",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("去看看", url="https://example.com")],
            ]),
        )
        raw = capture_buttons(message)
        markup = buttons_markup(raw)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com")

    async def test_capture_buttons_preserves_style_and_icon(self):
        """Ad buttons must keep Bot API style + icon_custom_emoji_id through JSON."""
        colored = InlineKeyboardButton.de_json(
            {
                "text": "🎉去看看",
                "url": "https://example.com/ad",
                "style": "success",
                "icon_custom_emoji_id": "emoji-42",
            },
            None,
        )
        danger = InlineKeyboardButton.de_json(
            {
                "text": "危险",
                "callback_data": "cb1",
                "style": "danger",
            },
            None,
        )
        message = SimpleNamespace(
            text="广告",
            reply_markup=InlineKeyboardMarkup([[colored], [danger]]),
        )
        raw = capture_buttons(message)
        data = json.loads(raw)
        self.assertEqual(data[0][0]["style"], "success")
        self.assertEqual(data[0][0]["icon_custom_emoji_id"], "emoji-42")
        self.assertEqual(data[0][0]["text"], "🎉去看看")
        self.assertEqual(data[1][0]["style"], "danger")
        self.assertNotIn("icon_custom_emoji_id", data[1][0])

        markup = buttons_markup(raw)
        first = markup.inline_keyboard[0][0]
        second = markup.inline_keyboard[1][0]
        self.assertEqual(first.url, "https://example.com/ad")
        self.assertEqual(first.api_kwargs["style"], "success")
        self.assertEqual(first.api_kwargs["icon_custom_emoji_id"], "emoji-42")
        self.assertEqual(first.to_dict()["style"], "success")
        self.assertEqual(first.to_dict()["icon_custom_emoji_id"], "emoji-42")
        self.assertEqual(second.callback_data, "cb1")
        self.assertEqual(second.api_kwargs["style"], "danger")
        self.assertNotIn("icon_custom_emoji_id", second.to_dict())

    async def test_channel_forward_ad_recovers_buttons_via_copy(self):
        """Hypothesis: Bot API omits reply_markup on forwards; copy_message recovers it."""
        channel = Chat(-100555, "channel", title="Ads")
        origin = MessageOriginChannel(datetime.now(timezone.utc), channel, 77)
        recovered = Message(
            901,
            datetime.now(timezone.utc),
            self.chat,
            from_user=self.user,
            text="频道广告文案",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("立即查看", url="https://example.com/ad")],
            ]),
        )
        recovered.set_bot(self.bot)
        self.bot.copy_message = AsyncMock(return_value=recovered)
        self.bot.delete_message = AsyncMock()

        await self.begin("group_ad_interval")
        await self.answer("10")
        # Forwarded message has no reply_markup (the real Telegram bug).
        await self.answer("频道广告文案", forward_origin=origin, reply_markup=None)
        await self.click("save")

        row = self.store.group_ad(-100, "interval")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["source_chat_id"]), -100555)
        self.assertEqual(int(row["source_message_id"]), 77)
        buttons = json.loads(row["buttons_json"] or "[]")
        self.assertTrue(buttons, "buttons_json should be non-empty after copy recover")
        self.assertEqual(buttons[0][0]["url"], "https://example.com/ad")
        self.bot.copy_message.assert_awaited()
        # Temporary scratch copy must be deleted.
        self.bot.delete_message.assert_awaited()

    async def test_send_stored_media_prefers_copy_then_falls_back(self):
        buttons_json = json.dumps([[{"text": "去看看", "url": "https://example.com/ad"}]])
        self.store.set_group_ad(
            -100, "interval", "定时广告文案", 7,
            interval_seconds=600,
            buttons_json=buttons_json,
            source_chat_id=-100555,
            source_message_id=77,
        )
        row = self.store.group_ad(-100, "interval")
        self.bot.copy_message = AsyncMock(return_value=self.message("copied", message_id=950))
        await handlers.send_stored_media(self.bot, -100, row)
        self.bot.copy_message.assert_awaited_once_with(
            chat_id=-100, from_chat_id=-100555, message_id=77,
        )
        self.bot.send_message.assert_not_called()

        self.bot.copy_message = AsyncMock(side_effect=TelegramError("forbidden"))
        self.bot.send_message.reset_mock()
        await handlers.send_stored_media(self.bot, -100, row)
        self.bot.send_message.assert_awaited()
        markup = self.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com/ad")

    async def test_forward_channel_source_only_accepts_channel_origin(self):
        channel = Chat(-100555, "channel")
        origin = MessageOriginChannel(datetime.now(timezone.utc), channel, 77)
        self.assertEqual(
            forward_channel_source(SimpleNamespace(forward_origin=origin)),
            (-100555, 77),
        )
        user_origin = MessageOriginUser(datetime.now(timezone.utc), User(99, "Source", False))
        self.assertIsNone(
            forward_channel_source(SimpleNamespace(forward_origin=user_origin))
        )

    async def test_rich_prefix_text_is_merged_not_sent_separately(self):
        self.store.set_group_ad(
            -100, "prefix", "hello", 7,
            entities_json='[{"type":"bold","offset":0,"length":5}]',
        )
        # Text (even with entities) merges into the bot reply; no separate send.
        self.assertEqual(await handlers.group_ad_text(self.store, -100, "prefix"), "hello")
        bot = AutoDeleteBot("123:token")
        object.__setattr__(
            bot, "message_decorator",
            AsyncMock(side_effect=["hello", ""]),
        )
        args, kwargs = await bot._merge_ad_text(
            (-100, "原消息"), {"parse_mode": "HTML"}, "text", 1, 4096,
        )
        self.assertEqual(args[1], "hello" + chr(10) + chr(10) + "原消息")

    async def test_original_entity_offsets_shift_by_utf16_for_plain_prefix(self):
        bot = AutoDeleteBot("123:token")
        decorator = AsyncMock(side_effect=["😀", ""])
        object.__setattr__(bot, "message_decorator", decorator)
        entity = MessageEntity("custom_emoji", 0, 2, custom_emoji_id="42")
        _, kwargs = await bot._merge_ad_text((), {"chat_id": -100, "text": "😀hello", "entities": [entity]}, "text", 1, 4096)
        self.assertEqual(kwargs["entities"][0].offset, 4)
        self.assertEqual(entity.offset, 0)

    async def test_telegram_failure_does_not_offer_repeat_write(self):
        draft = await self.begin("points_adjust")
        for value in ("7", "2", "reward"):
            await self.answer(value)
        update = self.query_update(f"wizard:{draft.nonce}:{draft.revision}:save")
        with patch.object(handlers, "commit_settings_draft", AsyncMock(side_effect=BadRequest("network fail"))):
            await handlers.handle_callback(update, self.context)
        self.assertNotIn("settings_draft", self.context.user_data)

    async def test_other_chat_message_cannot_write_private_draft(self):
        await self.begin("invite_points")
        update = self.update(self.message("30"))
        update.effective_chat = Chat(-222, "supergroup")
        await handlers.group_menu_input(update, self.context)
        self.assertEqual(self.store.invite_config(-222)["points_per_invite"], 0)
        self.assertEqual(self.store.invite_config(-100)["points_per_invite"], 0)
        self.assertEqual(self.context.user_data["settings_draft"].step, 0)

    async def test_old_step_button_rejected(self):
        draft = await self.begin("points_checkin")
        update = self.query_update(f"wizard:{draft.nonce}:{draft.revision}:next")
        await self.answer("1")
        await handlers.handle_callback(update, self.context)
        self.assertEqual(draft.step, 1)
        update.callback_query.answer.assert_awaited()

    async def test_new_configuration_navigation_discards_old_draft(self):
        await self.begin("points_checkin")
        await self.answer("1")
        await handlers.handle_callback(self.query_update("invite:set:points"), self.context)
        self.assertEqual(self.context.user_data["settings_draft"].mode, "invite_points")
        self.assertEqual(self.context.user_data["settings_draft"].answers, [None])

    async def test_private_keyword_token_is_hidden_in_review(self):
        await self.begin("clone_token")
        await self.answer("123456:private-token")
        review = self.bot.edit_message_text.call_args.args[0]
        self.assertNotIn("private-token", review)
        self.assertIn("隐藏", review)

    async def test_oversized_caption_does_not_replace_valid_content(self):
        self.store.update_quick_post(-100, 7, text="previous")
        await self.begin("quickpost_media")
        await self.answer(None, caption="a" * 1025, photo=[PhotoSize("file", "unique", 1, 1)])
        await self.click("save")
        self.assertEqual(self.store.quick_post(-100)["text"], "previous")
        self.assertEqual(self.store.quick_post(-100)["file_id"], "")
        self.assertIn("settings_draft", self.context.user_data)

    async def test_monitor_invalid_threshold_does_not_partially_change_state(self):
        self.context.user_data.update(tron_monitor_low="1", tron_monitor_high="10")
        await self.begin("tron_monitor_low")
        await self.answer("20")
        await self.click("save")
        self.assertEqual(self.context.user_data["tron_monitor_low"], "1")

    async def test_inline_photo_carries_caption_entities(self):
        entity = MessageEntity("custom_emoji", 0, 2, custom_emoji_id="42")
        self.store.update_quick_post(-100, 7, text="😀hello", file_type="photo", file_id="cached-photo", entities_json=json.dumps([entity.to_dict()]))
        query = SimpleNamespace(query=self.store.quick_post(-100)["share_code"], from_user=self.user, answer=AsyncMock())
        await handlers.quick_post_inline(SimpleNamespace(inline_query=query), self.context)
        result = query.answer.call_args.args[0][0]
        self.assertEqual(result.caption_entities, (entity,))
        self.assertEqual(result.photo_file_id, "cached-photo")

    async def test_media_senders_preserve_original_ids_and_entities(self):
        entity = MessageEntity("bold", 0, 5)
        for media in ("photo", "video", "animation", "audio", "document", "voice"):
            row = {"text": "hello", "file_id": "original", "file_type": media, "entities_json": json.dumps([entity.to_dict()])}
            await send_content(self.bot, -100, row)
            method = getattr(self.bot, "send_" + media)
            self.assertEqual(method.call_args.args[1], "original")
            self.assertEqual(method.call_args.kwargs["caption_entities"], [entity])
        for media in ("sticker", "video_note"):
            await send_content(self.bot, -100, {"text": "", "file_id": "original", "file_type": media})
            self.assertEqual(getattr(self.bot, "send_" + media).call_args.args[1], "original")

    async def test_invitation_handler_negative_join_leave_and_rejoin(self):
        link = "https://t.me/+own"
        self.store.save_invite_link(-100, 7, link, username="tester", display_name="Tester")
        self.store.set_points_enabled(-100, True, 7)
        self.store.update_invite_config(-100, 7, points_per_invite=Decimal("1.25"))
        self.store.adjust_points(-100, 7, -100, "debt", 1, allow_negative=True)
        member = User(8, "New", False)
        async def event(old, new):
            change = SimpleNamespace(
                chat=Chat(-100, "supergroup"), from_user=member,
                old_chat_member=SimpleNamespace(status=old),
                new_chat_member=SimpleNamespace(status=new, user=member),
                invite_link=SimpleNamespace(invite_link=link),
            )
            await handlers.track_personal_invite(SimpleNamespace(chat_member=change), self.context)
        await event("left", "member")
        await event("left", "member")
        self.assertEqual(self.store.point_account(-100, 7)["balance"], -98.75)
        await event("member", "left")
        await event("member", "left")
        self.assertEqual(self.store.point_account(-100, 7)["balance"], -100)
        await event("left", "member")
        self.assertEqual(self.store.point_account(-100, 7)["balance"], -100)
        self.assertEqual(self.store.count_point_ledger(-100, 7), 3)

    async def test_low_balance_cannot_draw_after_invitation_reward(self):
        self.store.set_points_enabled(-100, True, 7)
        self.store.set_point_draw_config(-100, True, 1, 1, 7)
        gift = self.store.add_point_gift(-100, "Gift", 5, 10, 7)
        self.store.adjust_points(-100, 7, -100, "debt", 1, allow_negative=True)
        link = self.store.save_invite_link(-100, 7, "https://t.me/+own")
        self.store.record_invite_join(-100, 8, 7, link, 2, credit_points=True)
        with self.assertRaisesRegex(ValueError, "积分余额不足"):
            self.store.draw_point_gift(-100, 7, gift, "tester", "Tester")
        self.assertEqual(self.store.point_account(-100, 7)["balance"], -98)

    async def test_delete_raffle_does_not_fall_through_to_creation(self):
        raffle_id = self.store.create_raffle(-100, 7, "Gift", 1, "2027-01-01 00:00:00")
        await self.begin("raffle_delete")
        await self.answer(f"#{raffle_id}")
        with patch.object(handlers, "create_raffle_from_input", AsyncMock()) as create:
            await self.click("save")
            create.assert_not_awaited()
        self.assertIsNone(self.store.get_raffle(raffle_id))

    async def test_activity_wizard_passes_all_answers_to_creation(self):
        for style, minimum, expected in (("随机", "3", "activity_random"), ("排名", "0", "activity_rank")):
            await self.begin("raffle_active_start")
            for value in ("0", style, "10", "2", minimum, "Gift"):
                await self.answer(value)
            with patch.object(handlers, "create_raffle_from_input", AsyncMock()) as create:
                await self.click("save")
                self.assertEqual(create.call_args.args[5], expected)
                self.assertEqual(create.call_args.args[7], int(minimum))

    async def test_poll_wizard_has_no_early_publish(self):
        self.bot.send_poll = AsyncMock(return_value=self.message("poll", message_id=777))
        await self.begin("group_poll_question")
        await self.answer("Question?")
        await self.answer("Yes\nNo")
        self.bot.send_poll.assert_not_awaited()
        await self.click("save")
        self.bot.send_poll.assert_awaited_once()
        self.assertEqual(self.bot.send_poll.call_args.args[:3], (-100, "Question?", ["Yes", "No"]))

    async def test_wizard_next_skips_when_prefilled_and_blocks_when_empty(self):
        self.context.user_data["menu_mode"] = "raffle_pro"
        self.context.user_data["wizard_prefills"] = [
            "春日福利", "无", "2099-09-15 21:00", "立即", "无",
            "点按钮参与", "1 | 测试奖", "否",
        ]
        self.context.user_data["edit_raffle_id"] = 42
        self.context.user_data["wizard_action_title"] = "修改抽奖 #42"
        await wizard.begin(self.query_update("test"), self.context, -100)
        draft = self.context.user_data["settings_draft"]
        self.assertEqual(draft.edit_raffle_id, 42)
        self.assertEqual(draft.answers[0], "春日福利")
        text, markup = draft.view()
        self.assertIn("当前值：春日福利", text)
        self.assertIn("点下一步可保留当前值", text)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("保留并下一步", labels)
        await self.click("next")
        self.assertEqual(draft.step, 1)
        empty = await self.begin("points_checkin")
        await self.click("next")
        self.assertEqual(empty.step, 0)


if __name__ == "__main__":
    unittest.main()
