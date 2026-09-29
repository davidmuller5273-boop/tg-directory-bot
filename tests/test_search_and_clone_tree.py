import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import ChatType
from telegram.error import TelegramError

from tg_directory_bot import bot as botmod
from tg_directory_bot.bot import (
    HELP_TEXT,
    admin_menu_keyboard,
    clone_tree_flat,
    deliver_entry,
    entry_reviewer_ids,
    group_keyword_reply,
    has_developer_access,
    has_review_access,
    is_developer_user,
    main_keyboard_for,
    parse_rich_submission_command,
    save_rich_submission,
    slice_submission_content,
)
from tg_directory_bot.clones import CloneManager, token_bot_id
from tg_directory_bot.config import Config, load_config
from tg_directory_bot.storage import DirectoryStore, Entry, entry_keyword_key
from tg_directory_bot.validation import Submission

MOTHER_TOKEN = "111111:" + "M" * 35


def make_config(db_path, **kwargs):
    base = dict(
        bot_token=MOTHER_TOKEN, admin_ids=set(), super_admin_ids=set(),
        db_path=Path(db_path), categories=("other",), blocked_keywords=(),
    )
    base.update(kwargs)
    return Config(**base)


def make_message(text=None, entities=None, caption=None, caption_entities=None, **extra):
    fields = dict(
        text=text, entities=entities or [], caption=caption,
        caption_entities=caption_entities or [], animation=None, document=None,
        photo=None, video=None, audio=None, voice=None, sticker=None,
        video_note=None, forward_origin=None, reply_markup=None,
        reply_to_message=None, message_id=10, chat_id=5,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


def entry(**kwargs):
    base = dict(
        id=1, url="tgcontent://1/1", title="v8地址", category="other", description="",
        status="approved", user_id=1, username="", reason="", created_at="",
        updated_at="", reports_count=0,
    )
    base.update(kwargs)
    return Entry(**base)


class KeywordKeyTest(unittest.TestCase):
    def test_normalization_rules(self):
        self.assertEqual(entry_keyword_key("ＶＸ８ 地址"), "vx8")
        self.assertEqual(entry_keyword_key(" V8  "), "v8")
        self.assertEqual(entry_keyword_key("v8地址"), entry_keyword_key("V8"))
        self.assertEqual(entry_keyword_key("地址"), "地址")  # bare suffix stays
        self.assertEqual(entry_keyword_key("v\u200b8"), "v8")


class SearchStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def add(self, keyword, content, status="pending", n=[0]):
        n[0] += 1
        return self.store.add_rich_submission(
            keyword, f"tgcontent://1/{n[0]}", content, 7, "u", status=status,
        )

    def test_exact_match_with_and_without_suffix_only(self):
        self.add("v8地址", "内容A", "approved")
        self.add("v8pro", "内容B", "approved")
        self.assertEqual(self.store.find_keyword_entry("v8").content_text, "内容A")
        self.assertEqual(self.store.find_keyword_entry("V8 地址").content_text, "内容A")
        self.assertEqual(self.store.find_keyword_entry("ｖ８").content_text, "内容A")
        self.assertIsNone(self.store.find_keyword_entry("我想要v8地址"))
        self.assertIsNone(self.store.find_keyword_entry("v"))
        self.assertIsNone(self.store.find_keyword_entry("今天去哪里 v8 地址 呢"))
        # configured trigger suffix other than 地址
        self.assertEqual(
            self.store.find_keyword_entry("v8网址", ("网址",)).content_text, "内容A"
        )

    def test_approving_duplicate_keyword_deletes_older_entries(self):
        old = self.add("v8地址", "旧内容", "approved")
        self.store.add_report(old, 99, "bad")
        older_pending = self.add("V8", "更早待审")
        new = self.add("v8", "新内容")
        newer_pending = self.add("v8 地址", "更新的待审")
        other = self.add("v9", "别的", "approved")
        self.assertTrue(self.store.update_status(new, "approved"))
        self.assertIsNone(self.store.get(old))
        self.assertIsNone(self.store.get(older_pending))
        self.assertIsNotNone(self.store.get(newer_pending))
        self.assertIsNotNone(self.store.get(other))
        self.assertEqual(self.store.find_keyword_entry("v8地址").content_text, "新内容")
        # pending submissions never replace the live entry before review
        self.assertEqual(self.store.get(new).status, "approved")

    def test_approved_insert_also_dedupes(self):
        self.add("abc", "1", "approved")
        newest = self.add("ABC地址", "2", "approved")
        rows = self.store.list_entries(status=None, limit=50)
        self.assertEqual([row.id for row in rows], [newest])

    def test_broad_search_lists_one_per_keyword_with_limit(self):
        for index in range(15):
            self.add(f"钱包{index}", "x", "approved")
        self.add("交易所", "y", "approved")
        results = self.store.search_keyword_titles("钱包", limit=10)
        self.assertEqual(len(results), 10)
        self.assertTrue(all("钱包" in item.title for item in results))
        self.assertEqual(self.store.search_keyword_titles("%"), [])

    def test_migration_backfills_keys_without_deleting(self):
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                "INSERT INTO entries (url,title,status,user_id,keyword_key) VALUES ('https://a','旧词','approved',1,'')"
            )
            conn.execute(
                "INSERT INTO entries (url,title,status,user_id,keyword_key) VALUES ('https://b','旧词地址','approved',1,'')"
            )
        self.store.init()
        self.assertEqual(self.store.count_entries(status="approved"), 2)
        found = self.store.find_keyword_entry("旧词")
        self.assertEqual(found.url, "https://b")  # newest wins on reply

    def test_rich_fields_round_trip(self):
        entry_id = self.store.add_rich_submission(
            "kw", "tgcontent://1/99", " 粗体", 1, "u",
            entities_json='[{"type":"bold","offset":1,"length":2}]',
            buttons_json='[[{"text":"去","url":"https://t.me/x","style":"success"}]]',
            copy_chat_id=-100, copy_message_id=5,
        )
        item = self.store.get(entry_id)
        self.assertEqual(item.content_text, " 粗体")  # not stripped: entity offsets stay valid
        self.assertEqual(json.loads(item.entities_json)[0]["offset"], 1)
        self.assertEqual((item.copy_chat_id, item.copy_message_id), (-100, 5))
        self.assertIn("success", item.buttons_json)
        self.assertEqual(item.keyword_key, "kw")

    def test_edit_title_updates_key_and_content_edit_drops_formatting(self):
        entry_id = self.store.add_rich_submission(
            "a1", "tgcontent://1/77", "文字", 1, "u", status="approved",
            entities_json='[{"type":"bold","offset":0,"length":2}]',
            copy_chat_id=1, copy_message_id=2,
        )
        self.store.update_rich_entry(entry_id, "b2", "新文字", "other", "")
        item = self.store.get(entry_id)
        self.assertEqual(item.keyword_key, "b2")
        self.assertEqual((item.entities_json, item.copy_message_id), ("[]", 0))

    def test_storage_usage_counts_database_files(self):
        self.assertGreater(self.store.storage_usage_bytes(), 0)


class SubmissionCaptureTest(unittest.TestCase):
    def test_parse_allows_reply_form(self):
        self.assertEqual(parse_rich_submission_command("v8 搜录"), ("v8", ""))
        self.assertEqual(parse_rich_submission_command("v8 搜录 a b"), ("v8", "a b"))
        self.assertIsNone(parse_rich_submission_command("v8搜录"))
        self.assertIsNone(parse_rich_submission_command("v8 搜录图片"))

    def test_slice_keeps_entities_with_utf16_offsets(self):
        text = "😀v8 搜录 看👉粗体 链接"
        # "😀" is 2 UTF-16 units; "😀v8 搜录 " = 2+2+1+2+1 = 8 units
        bold = MessageEntity("bold", 8 + 3, 2)          # 粗体 (after 看👉)
        link = MessageEntity("text_link", 8 + 6, 2, url="https://t.me/x")
        prefix = MessageEntity("italic", 0, 4)          # inside the command part only
        message = make_message(text=text, entities=[prefix, bold, link])
        content, raw = slice_submission_content(message)
        self.assertEqual(content, "看👉粗体 链接")
        entities = json.loads(raw)
        self.assertEqual([(e["type"], e["offset"], e["length"]) for e in entities], [
            ("bold", 3, 2), ("text_link", 6, 2),
        ])
        self.assertEqual(entities[1]["url"], "https://t.me/x")


def submission_context(store, config, bot=None):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"config": config, "store": store}),
        bot=bot or SimpleNamespace(send_message=AsyncMock(), copy_message=AsyncMock()),
        user_data={},
    )


class SaveSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name) / "db.sqlite3", super_admin_ids={500})
        self.store = DirectoryStore(self.config.db_path)
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def run_save(self, message, keyword, content, chat_type=ChatType.PRIVATE, config=None):
        message.reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42, username="u", full_name="U"),
            effective_chat=SimpleNamespace(id=42, type=chat_type),
        )
        context = submission_context(self.store, config or self.config)
        with patch("tg_directory_bot.bot.notify_admins", new=AsyncMock()) as notify:
            asyncio.run(save_rich_submission(update, context, keyword, content))
        return message, notify

    def test_inline_form_keeps_content_formatting(self):
        message = make_message(
            text="v8 搜录 粗体", entities=[MessageEntity("bold", 6, 2)], message_id=31,
        )
        _, notify = self.run_save(message, "v8", "粗体")
        item = self.store.list_entries(status="pending")[0]
        self.assertEqual(item.content_text, "粗体")
        self.assertEqual(json.loads(item.entities_json)[0]["offset"], 0)
        self.assertEqual(item.copy_message_id, 0)
        notify.assert_awaited_once()

    def test_reply_form_stores_replied_message_verbatim(self):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "打开", url="https://t.me/x", api_kwargs={"style": "primary", "icon_custom_emoji_id": "555"},
        )]])
        target = make_message(
            text="原消息 加粗", entities=[MessageEntity("bold", 4, 2)],
            reply_markup=markup, message_id=77,
        )
        message = make_message(text="v8 搜录", reply_to_message=target, message_id=78)
        self.run_save(message, "v8", "")
        item = self.store.list_entries(status="pending")[0]
        self.assertEqual(item.content_text, "原消息 加粗")
        self.assertEqual(json.loads(item.entities_json)[0]["offset"], 4)
        self.assertEqual((item.copy_chat_id, item.copy_message_id), (42, 77))
        buttons = json.loads(item.buttons_json)
        self.assertEqual(buttons[0][0]["style"], "primary")
        self.assertEqual(buttons[0][0]["icon_custom_emoji_id"], "555")
        self.assertEqual(item.source_message_id, 78)

    def test_command_without_content_or_reply_explains_usage(self):
        message = make_message(text="v8 搜录")
        message, notify = self.run_save(message, "v8", "")
        self.assertIn("回复要收录的消息", message.reply_text.await_args.args[0])
        notify.assert_not_awaited()

    def test_over_quota_blocks_new_submission(self):
        config = make_config(self.config.db_path, is_clone=True, storage_quota_bytes=10)
        message = make_message(text="v8 搜录 内容")
        message, notify = self.run_save(message, "v8", "内容", config=config)
        reply = message.reply_text.await_args.args[0]
        self.assertIn("存储空间已满", reply)
        self.assertIn("存储用量", reply)
        self.assertEqual(self.store.list_entries(status=None), [])
        notify.assert_not_awaited()


class DeliverEntryTest(unittest.TestCase):
    def test_copy_first_with_buttons(self):
        message = SimpleNamespace(
            reply_copy=AsyncMock(return_value=SimpleNamespace(message_id=9)),
            reply_text=AsyncMock(), chat_id=-100,
        )
        item = entry(
            content_text="x", copy_chat_id=-200, copy_message_id=3,
            buttons_json='[[{"text":"go","url":"https://t.me/a","style":"danger"}]]',
        )
        asyncio.run(deliver_entry(item, message=message))
        message.reply_copy.assert_awaited_once()
        self.assertEqual(message.reply_copy.await_args.args, (-200, 3))
        markup = message.reply_copy.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].api_kwargs["style"], "danger")
        message.reply_text.assert_not_awaited()

    def test_fallback_rebuild_uses_entities_and_no_wrapper(self):
        message = SimpleNamespace(
            reply_copy=AsyncMock(side_effect=TelegramError("gone")),
            reply_text=AsyncMock(), chat_id=5,
        )
        item = entry(
            content_text="粗体内容", copy_chat_id=1, copy_message_id=2,
            entities_json='[{"type":"custom_emoji","offset":0,"length":2,"custom_emoji_id":"99"}]',
        )
        asyncio.run(deliver_entry(item, message=message))
        args, kwargs = message.reply_text.await_args
        self.assertEqual(args[0], "粗体内容")
        self.assertNotIn("关键词", args[0])
        self.assertEqual(kwargs["entities"][0].custom_emoji_id, "99")
        self.assertIsNone(kwargs["parse_mode"])

    def test_media_caption_entities_and_legacy_url(self):
        message = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock(), chat_id=5)
        item = entry(
            content_text="说明", media_file_id="PHOTO", media_type="photo",
            entities_json='[{"type":"bold","offset":0,"length":2}]',
        )
        asyncio.run(deliver_entry(item, message=message))
        kwargs = message.reply_photo.await_args.kwargs
        self.assertEqual(message.reply_photo.await_args.args[0], "PHOTO")
        self.assertEqual(kwargs["caption"], "说明")
        self.assertEqual(kwargs["caption_entities"][0].type, "bold")
        asyncio.run(deliver_entry(entry(url="https://example.com/"), message=message))
        self.assertEqual(message.reply_text.await_args.args[0], "https://example.com/")

    def test_send_to_chat_with_bot(self):
        bot = SimpleNamespace(send_message=AsyncMock(), copy_message=AsyncMock())
        asyncio.run(deliver_entry(entry(content_text="hi"), bot=bot, chat_id=77))
        self.assertEqual(bot.send_message.await_args.args[:2], (77, "hi"))


class GroupTriggerTest(unittest.TestCase):
    def run_group(self, store, text):
        message = SimpleNamespace(
            text=text, reply_to_message=None, forward_origin=None,
            reply_text=AsyncMock(), reply_copy=AsyncMock(), message_id=3, chat_id=-500,
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-500, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=1, username="m", full_name="M"),
            effective_message=message,
        )
        context = SimpleNamespace(
            user_data={}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": store}),
        )
        with patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)), \
                patch("tg_directory_bot.bot.schedule_group_trigger_cleanup"):
            asyncio.run(group_keyword_reply(update, context))
        return message

    def test_precise_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.set_setting("group_keyword_enabled", "1")
            store.add_rich_submission("v8地址", "tgcontent://1/1", "V8内容", 1, "u", status="approved")
            store.add_rich_submission("v8pro", "tgcontent://1/2", "PRO内容", 1, "u", status="approved")
            message = self.run_group(store, "v8")
            message.reply_text.assert_awaited_once()
            self.assertEqual(message.reply_text.await_args.args[0], "V8内容")
            # long sentence merely ending with 地址: silent
            message = self.run_group(store, "请问一下大家有没有人知道那个最新的钱包下载地址")
            message.reply_text.assert_not_awaited()
            # short unknown XX地址: single not-found reply
            message = self.run_group(store, "v7地址")
            message.reply_text.assert_awaited_once()
            self.assertIn("没有收录", message.reply_text.await_args.args[0])
            # substring of a keyword never triggers
            message = self.run_group(store, "v8p")
            message.reply_text.assert_not_awaited()


class RoleTest(unittest.TestCase):
    def context_for(self, config, store):
        return SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "store": store}))

    def test_developer_features_only_on_mother_but_full_rights_everywhere(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "db.sqlite3"
            store = DirectoryStore(db)
            store.init()
            child = make_config(db, is_clone=True, admin_ids={2}, super_admin_ids={2}, developer_ids={1})
            store.ensure_config_admins(child.admin_ids, child.super_admin_ids, child.developer_ids)
            ctx = self.context_for(child, store)
            self.assertFalse(has_developer_access(ctx, 1))
            self.assertTrue(is_developer_user(ctx, 1))
            self.assertTrue(botmod.has_super_admin_access(ctx, 1))
            self.assertTrue(has_review_access(ctx, 2))
            self.assertFalse(has_review_access(ctx, 3))
            self.assertEqual(entry_reviewer_ids(ctx), [2])
            mother = make_config(db, developer_ids={1}, super_admin_ids={2})
            ctx = self.context_for(mother, store)
            self.assertTrue(has_developer_access(ctx, 1))
            self.assertEqual(entry_reviewer_ids(ctx), [1, 2])

    def test_admin_menu_hides_developer_rows_for_child_super_admin(self):
        child_rows = [b.callback_data for row in admin_menu_keyboard(True, False).inline_keyboard for b in row]
        self.assertIn("admin:pending", child_rows)
        for hidden in ("admin:notes", "admin:clones", "clonetree:0", "admin:usage:0", "admin:tronmonitors:0"):
            self.assertNotIn(hidden, child_rows)
        mother_rows = [b.callback_data for row in admin_menu_keyboard(True, True).inline_keyboard for b in row]
        for shown in ("admin:pending", "admin:notes", "admin:clones", "clonetree:0"):
            self.assertIn(shown, mother_rows)

    def test_clone_button_follows_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "db.sqlite3"
            store = DirectoryStore(db)
            store.init()
            ctx = self.context_for(make_config(db, is_clone=True), store)
            labels = [b.text for row in main_keyboard_for(ctx, 9).inline_keyboard for b in row]
            self.assertNotIn("🤖 克隆机器人", labels)
            ctx.application.bot_data["clone_manager"] = object()
            labels = [b.text for row in main_keyboard_for(ctx, 9).inline_keyboard for b in row]
            self.assertIn("🤖 克隆机器人", labels)

    def test_help_text(self):
        self.assertNotIn("开发者", HELP_TEXT)
        self.assertLessEqual(len(HELP_TEXT), 4096)
        for heading in ("🔎 搜索收录", "🤖 克隆机器人", "🎁 全部抽奖", "👥 群组管理"):
            self.assertEqual(HELP_TEXT.count(heading), 1)
        doc = Path(__file__).resolve().parent.parent / "使用帮助.md"
        self.assertIn(HELP_TEXT, doc.read_text(encoding="utf-8"))


class FakeBot:
    def __init__(self, bot_id, username):
        self.me = SimpleNamespace(id=bot_id, username=username)

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_me(self):
        return self.me


class CloneHierarchyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mother_db = Path(self.temp.name) / "mother.sqlite3"
        self.mother_store = DirectoryStore(self.mother_db)
        self.mother_store.init()
        self.mother_config = make_config(self.mother_db, developer_ids={1})
        self.mother = CloneManager(self.mother_config, self.mother_store)
        self.mother.clones_dir = Path(self.temp.name) / "clones"

    def tearDown(self):
        self.temp.cleanup()

    def request(self, manager, owner, bot_id, username):
        with patch("tg_directory_bot.clones.Bot", FakeBot(bot_id, username)):
            return asyncio.run(manager.request(owner, f"{bot_id}:" + "t" * 35, owner_name=f"owner{owner}"))

    def child_manager(self, clone_id):
        env = self.mother.child_env(clone_id, 2, "x")
        config = make_config(
            Path(env["DB_PATH"]), bot_token="222222:" + "c" * 35, is_clone=True,
            clone_id=int(env["CLONE_ID"]), mother_db_path=env["MOTHER_DB_PATH"],
            mother_bot_id=int(env["MOTHER_BOT_ID"]), clone_cipher_key=env["CLONE_CIPHER_KEY"],
        )
        return CloneManager(config, DirectoryStore(Path(env["MOTHER_DB_PATH"])), manage_processes=False)

    def test_child_env(self):
        env = self.mother.child_env(7, 55, "tok")
        self.assertEqual(env["ADMIN_IDS"], "55")
        self.assertEqual(env["DEVELOPER_IDS"], "1")
        self.assertEqual(env["CLONE_ID"], "7")
        self.assertEqual(env["IS_CLONE"], "1")
        self.assertEqual(int(env["STORAGE_QUOTA_BYTES"]), 2 * 1024 ** 3)
        self.assertEqual(env["MOTHER_BOT_ID"], "111111")
        self.assertTrue(env["DB_PATH"].endswith("clone-7.sqlite3"))
        self.assertEqual(env["MOTHER_DB_PATH"], str(self.mother_db.resolve()))
        self.assertEqual(token_bot_id(MOTHER_TOKEN), 111111)

    def test_load_config_reads_clone_env(self):
        env = self.mother.child_env(3, 9, MOTHER_TOKEN)
        with patch.dict(os.environ, env, clear=False):
            config = load_config(env_file=None)
        self.assertTrue(config.is_clone)
        self.assertEqual(config.clone_id, 3)
        self.assertEqual(config.developer_ids, {1})
        self.assertEqual(config.super_admin_ids, {9})
        self.assertEqual(config.storage_quota_bytes, 2 * 1024 ** 3)
        self.assertEqual(config.mother_bot_id, 111111)

    def test_child_requests_go_to_mother_registry_and_approval_only_on_mother(self):
        first, _ = self.request(self.mother, 2, 222222, "child_bot")
        self.assertTrue(self.mother_store.review_bot_clone(first, True, 1))
        child = self.child_manager(first)
        grand, name = self.request(child, 3, 333333, "grand_bot")
        row = self.mother_store.bot_clone(grand)
        self.assertEqual((row["parent_clone_id"], row["notified"], row["result_notified"]), (first, 0, 0))
        self.assertEqual(row["owner_name"], "owner3")
        # token sealed with the mother's key -> mother can decrypt it
        self.assertEqual(self.mother.decrypt(row["token_cipher"]), "333333:" + "t" * 35)
        with self.assertRaises(ValueError):
            asyncio.run(child.approve(grand, 1))
        with self.assertRaises(ValueError):
            child.reject(grand, 1)
        # mother picks it up exactly once
        self.assertEqual([int(r["id"]) for r in self.mother.sync_requests()], [grand])
        self.assertEqual(self.mother.sync_requests(), [])
        self.mother_store.review_bot_clone(grand, False, 1)
        results = child.store.bot_clone_results_for_parent(first)
        self.assertEqual([int(r["id"]) for r in results], [grand])
        child.store.mark_bot_clone_result_notified(grand)
        self.assertEqual(child.store.bot_clone_results_for_parent(first), [])

    def test_cannot_register_self_mother_or_ancestor(self):
        first, _ = self.request(self.mother, 2, 222222, "child_bot")
        child = self.child_manager(first)
        with self.assertRaises(ValueError):
            self.request(child, 3, 111111, "mother")  # mother bot
        with self.assertRaises(ValueError):
            self.request(child, 3, 222222, "self")  # itself / ancestor
        with self.assertRaises(ValueError):
            self.request(self.mother, 3, 111111, "mother")

    def test_delete_tree_cascades_and_archives(self):
        a, _ = self.request(self.mother, 2, 222222, "a_bot")
        b = self.mother_store.save_bot_clone_request(3, 333333, "b_bot", "c", parent_clone_id=a)
        c = self.mother_store.save_bot_clone_request(4, 444444, "c_bot", "c", parent_clone_id=b)
        other = self.mother_store.save_bot_clone_request(5, 555555, "d_bot", "c")
        self.mother.clones_dir.mkdir(parents=True)
        self.mother.clone_db_path(b).write_bytes(b"data")
        running = MagicMock()
        running.poll.return_value = None
        self.mother.processes[c] = running
        self.assertEqual(
            [int(r["id"]) for r in self.mother_store.bot_clone_descendants(a)], [b, c]
        )
        removed = self.mother.delete_tree(a)
        self.assertEqual(sorted(removed), sorted([a, b, c]))
        running.terminate.assert_called_once()
        self.assertIsNone(self.mother_store.bot_clone(c))
        self.assertIsNotNone(self.mother_store.bot_clone(other))
        self.assertFalse(self.mother.clone_db_path(b).exists())
        archived = list((self.mother.clones_dir / "deleted").iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), b"data")

    def test_tree_flat_depths(self):
        a = self.mother_store.save_bot_clone_request(2, 20, "a", "c")
        b = self.mother_store.save_bot_clone_request(3, 30, "b", "c", parent_clone_id=a)
        c = self.mother_store.save_bot_clone_request(4, 40, "c", "c")
        d = self.mother_store.save_bot_clone_request(5, 50, "d", "c", parent_clone_id=b)
        flat = [(int(row["id"]), depth) for row, depth in clone_tree_flat(self.mother_store.list_bot_clones())]
        self.assertEqual(flat, [(a, 1), (b, 2), (d, 3), (c, 1)])

    def callback_context(self):
        return SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "config": self.mother_config, "store": self.mother_store,
                "clone_manager": self.mother, "bot_username": "mother_bot",
            }),
            bot=SimpleNamespace(send_message=AsyncMock()), user_data={},
        )

    def click(self, data, user_id=1):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id), answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        asyncio.run(botmod.handle_clone_tree_callback(update, self.callback_context(), data))
        return query

    def test_tree_view_and_confirmed_cascade_delete(self):
        a = self.mother_store.save_bot_clone_request(2, 20, "a_bot", "c", owner_name="Alice")
        b = self.mother_store.save_bot_clone_request(3, 30, "b_bot", "c", parent_clone_id=a)
        query = self.click("clonetree:0")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("子机器人共 2 个", text)
        self.assertIn("@a_bot", text)
        self.assertIn("超管：Alice（2）", text)
        self.assertIn(f"上级：#{a} @a_bot", text)
        self.assertIn("第2层", text)
        query = self.click(f"clonedel:ask:{a}:0")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("下级子机器人：1 个", text)
        self.assertIsNotNone(self.mother_store.bot_clone(a))  # ask does not delete
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("共 2 个", markup.inline_keyboard[0][0].text)
        self.click(f"clonedel:do:{a}:0")
        self.assertIsNone(self.mother_store.bot_clone(a))
        self.assertIsNone(self.mother_store.bot_clone(b))

    def test_tree_requires_mother_developer(self):
        self.mother_store.save_bot_clone_request(2, 20, "a_bot", "c")
        query = self.click("clonetree:0", user_id=2)
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))

    def test_mother_job_pushes_child_requests_to_developers(self):
        a = self.mother_store.save_bot_clone_request(2, 20, "a_bot", "c")
        g = self.mother_store.save_bot_clone_request(
            3, 30, "g_bot", "c", parent_clone_id=a, notified=False, owner_name="Gina",
        )
        context = self.callback_context()
        asyncio.run(botmod.sync_clone_requests(context))
        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.await_args
        self.assertEqual(args[0], 1)
        self.assertIn(f"来自 #{a} @a_bot", args[1])
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].callback_data, f"clone:approve:{g}"
        )
        asyncio.run(botmod.sync_clone_requests(context))
        context.bot.send_message.assert_awaited_once()

    def test_old_rows_keep_mother_scope_after_migration(self):
        with sqlite3.connect(self.mother_db) as conn:
            conn.execute(
                "INSERT INTO bot_clones (owner_id,bot_id,token_cipher,status) VALUES (1,999,'x','approved')"
            )
        self.mother_store.init()
        row = self.mother_store.bot_clone_by_bot_id(999)
        self.assertEqual((row["parent_clone_id"], row["notified"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
