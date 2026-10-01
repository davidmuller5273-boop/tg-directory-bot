import asyncio
import io
import shutil
import subprocess
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

from tg_directory_bot import settings_wizard
from tg_directory_bot import sticker_clone as sc
from tg_directory_bot import sticker_preview as sp
from tg_directory_bot.bot import (
    HELP_TEXT,
    commit_group_menu_input,
    dice_settings_view,
    handle_callback,
    main_keyboard,
    menu_mode_group_permission,
    point_dice_bet_reply,
)
from tg_directory_bot.storage import DirectoryStore


def image_bytes(color, size=(512, 512), fmt="WEBP", mode="RGBA"):
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class FakeFile:
    def __init__(self, data):
        self.data = data
        self.file_size = len(data or b"")

    async def download_as_bytearray(self):
        if self.data is None:
            raise BadRequest("file is too big")
        return bytearray(self.data)


def sticker(i, *, video=False, animated=False):
    return SimpleNamespace(
        file_id=f"file{i}", emoji="😀", is_video=video, is_animated=animated,
        mask_position=None, needs_repainting=False,
        thumbnail=SimpleNamespace(file_id=f"thumb{i}"),
    )


async def no_sleep(*_a, **_k):
    return None


class FakeBot:
    """Sticker sets + files + channel admin checks + sends."""

    def __init__(self, sources=None, files=None):
        self.id = 999
        self.username = "sssxxxjqbot"
        self.sources = dict(sources or {})
        self.files = dict(files or {})
        self.created = {}
        self.sent = []
        self.chats = {}
        self.members = {}
        self.fail_channel_post = None

    async def get_file(self, file_id):
        if file_id not in self.files:
            raise BadRequest("wrong file id")
        return FakeFile(self.files[file_id])

    async def get_sticker_set(self, name):
        if name in self.created:
            return self.created[name]
        if name in self.sources:
            return self.sources[name]
        raise BadRequest("Stickerset_invalid")

    async def create_new_sticker_set(self, user_id, name, title, stickers,
                                     sticker_type=None, needs_repainting=None):
        source = next(iter(self.sources.values()))
        by_id = {s.file_id: s for s in source.stickers}
        self.created[name] = SimpleNamespace(
            name=name, title=title, sticker_type=sticker_type, owner=user_id,
            stickers=[by_id[item.sticker] for item in stickers],
        )
        return True

    async def add_sticker_to_set(self, user_id, name, sticker):
        source = next(iter(self.sources.values()))
        by_id = {s.file_id: s for s in source.stickers}
        self.created[name].stickers.append(by_id[sticker.sticker])
        return True

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        if chat_id < 0 and self.fail_channel_post:
            raise self.fail_channel_post
        self.sent.append(("photo", chat_id, photo, caption, reply_markup))
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.sent))

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id < 0 and self.fail_channel_post:
            raise self.fail_channel_post
        self.sent.append(("text", chat_id, None, text, reply_markup))
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.sent))

    async def get_chat(self, reference):
        if reference not in self.chats:
            raise BadRequest("Chat not found")
        return self.chats[reference]

    async def get_chat_member(self, chat_id, user_id):
        key = (chat_id, user_id)
        if key not in self.members:
            raise BadRequest("member list is inaccessible")
        return self.members[key]


def channel_bot(**overrides):
    bot = FakeBot()
    chat = SimpleNamespace(id=-1001234567890, type="channel", title="表情频道", username="biaoqing")
    bot.chats.update({"@biaoqing": chat, -1001234567890: chat})
    bot.members[(chat.id, 42)] = SimpleNamespace(status=overrides.get("user_status", "creator"))
    bot.members[(chat.id, 999)] = SimpleNamespace(
        status=overrides.get("bot_status", "administrator"),
        can_post_messages=overrides.get("can_post", True),
    )
    return bot, chat


class Msg:
    _ids = iter(range(5000, 100000))

    def __init__(self, text="", chat_id=42, forward_origin=None):
        self.text = text
        self.chat_id = chat_id
        self.message_id = next(self._ids)
        self.forward_origin = forward_origin
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        reply = Msg(text, self.chat_id)
        reply.kwargs = kwargs
        self.replies.append(reply)
        return reply

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


def ctx(bot, store, user_data=None):
    tasks = []
    app = SimpleNamespace(bot_data={"bot_username": "sssxxxjqbot", "store": store})

    def create_task(coro):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task
    app.create_task = create_task
    return SimpleNamespace(bot=bot, application=app,
                           user_data=user_data if user_data is not None else {}, args=[]), tasks


def text_update(text, *, forward_origin=None, user_id=42):
    message = Msg(text, user_id, forward_origin)
    return SimpleNamespace(
        effective_message=message, callback_query=None,
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        effective_user=SimpleNamespace(id=user_id),
    ), message


def callback_update(data, *, user_id=42, chat_type=ChatType.PRIVATE):
    message = Msg("panel", user_id)
    query = SimpleNamespace(data=data, message=message, answers=[], edited=[])

    async def answer(text=None, show_alert=False):
        query.answers.append((text, show_alert))

    async def edit_message_text(text, reply_markup=None, **kwargs):
        query.edited.append((text, reply_markup))
    query.answer = answer
    query.edit_message_text = edit_message_text
    return SimpleNamespace(
        callback_query=query, effective_message=message,
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    ), query


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def expect_stop(coro):
    try:
        await coro
    except ApplicationHandlerStop:
        return True
    return False


class PreviewTest(unittest.TestCase):
    def test_grid_5x6_dark_background(self):
        images = [Image.new("RGBA", (512, 512), (255, 0, 0, 255)) for _ in range(35)]
        png = sp.build_grid(images)
        grid = Image.open(io.BytesIO(png))
        cell, pad = sp.CELL_SIZE, sp.CELL_PADDING
        self.assertEqual(grid.size, (5 * cell + 6 * pad, 6 * cell + 7 * pad))
        self.assertEqual(grid.getpixel((2, 2))[:3], sp.BACKGROUND)
        self.assertEqual(grid.getpixel((pad + cell // 2, pad + cell // 2))[:3], (255, 0, 0))

    def test_grid_small_set_and_transparency(self):
        images = [Image.new("RGBA", (100, 50), (0, 0, 0, 0)) for _ in range(3)]
        grid = Image.open(io.BytesIO(sp.build_grid(images)))
        self.assertEqual(grid.size[0], 3 * sp.CELL_SIZE + 4 * sp.CELL_PADDING)
        self.assertEqual(grid.getpixel((sp.CELL_PADDING + 80, sp.CELL_PADDING + 80))[:3],
                         sp.BACKGROUND)
        with self.assertRaises(ValueError):
            sp.build_grid([])

    def test_preview_uses_files_thumbnails_and_skips_failures(self):
        stickers = [
            sticker(0),                       # static: own webp
            sticker(1, animated=True),        # tgs: thumbnail
            sticker(2, video=True),           # video: thumbnail when no ffmpeg
            sticker(3),                       # broken data -> thumbnail fallback
            sticker(4),                       # nothing downloadable -> skipped
        ]
        files = {
            "file0": image_bytes((255, 0, 0, 255)),
            "thumb1": image_bytes((0, 255, 0, 255), (128, 128)),
            "thumb2": image_bytes((0, 0, 255, 255), (128, 128), "JPEG", "RGB"),
            "file3": b"not an image", "thumb3": image_bytes((9, 9, 9, 255)),
            "file1": b"tgs-gzip-data",
        }
        bot = FakeBot(files=files)
        with patch("tg_directory_bot.sticker_preview.shutil.which", return_value=None):
            png = asyncio.run(sp.build_sticker_preview(bot, stickers))
        grid = Image.open(io.BytesIO(png))
        self.assertEqual(grid.size, (4 * sp.CELL_SIZE + 5 * sp.CELL_PADDING,
                                     sp.CELL_SIZE + 2 * sp.CELL_PADDING))
        self.assertIsNone(asyncio.run(sp.build_sticker_preview(FakeBot(), stickers)))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_video_first_frame_with_ffmpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "v.webm"
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                "color=c=yellow:s=64x64:d=0.2", "-c:v", "libvpx-vp9", str(target),
            ], check=True, timeout=60)
            frame = sp.ffmpeg_first_frame(target.read_bytes())
        self.assertIsNotNone(frame)
        self.assertEqual(frame.size, (64, 64))

    def test_caption_tags(self):
        self.assertEqual(
            sc.post_caption("V系列哈希彩票找 @jiuye", "regular", True),
            "👉 V系列哈希彩票找 @jiuye 👈\n\n#动态 #贴纸 #表情包 #斗图\n\n⬇️点击下方按钮添加表情⬇️",
        )
        self.assertIn("#静态 #贴纸", sc.post_caption("t", "regular", False))
        self.assertIn("#表情 #表情包", sc.post_caption("t", "custom_emoji", False))


class MenuTest(unittest.TestCase):
    def test_main_menu_button_for_everyone(self):
        for markup in (main_keyboard(False, False), main_keyboard(True, True, False)):
            match = [b for b in buttons(markup) if b.text == "表情包复制更改标题"]
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0].callback_data, "stk:menu")

    def test_help_mentions_feature(self):
        self.assertIn("• /jx：复制贴纸包并改标题", HELP_TEXT)
        self.assertIn("表情包复制更改标题", HELP_TEXT)
        doc = Path(__file__).resolve().parent.parent / "使用帮助.md"
        self.assertIn(HELP_TEXT, doc.read_text(encoding="utf-8"))

    def test_menu_callback_routes_through_bot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            context, _ = ctx(FakeBot(), store)
            update, query = callback_update("stk:menu")
            with patch("tg_directory_bot.bot.guard", AsyncMock(return_value=True)):
                asyncio.run(handle_callback(update, context))
            text, markup = query.edited[-1]
            self.assertIn("表情包复制更改标题", text)
            labels = [b.text for b in buttons(markup)]
            self.assertIn("默认模式", labels)
            self.assertIn("固定模式", labels)

    def test_default_mode_button_starts_jx_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            context, _ = ctx(FakeBot(), store)
            update, query = callback_update("stk:default")
            asyncio.run(sc.handle_callback(update, context))
            self.assertEqual(query.edited[-1][0], sc.LINK_PROMPT)
            self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "link")

    def test_group_click_is_rejected(self):
        context, _ = ctx(FakeBot(), None)
        update, query = callback_update("stk:fixed", chat_type=ChatType.SUPERGROUP)
        asyncio.run(sc.handle_callback(update, context))
        self.assertTrue(query.answers[-1][1])


class ChannelValidationTest(unittest.TestCase):
    def check(self, bot, message):
        return asyncio.run(sc.resolve_channel(bot, 42, message))

    def test_accepts_username_id_and_forward(self):
        bot, chat = channel_bot()
        self.assertIs(self.check(bot, Msg("@biaoqing")), chat)
        self.assertIs(self.check(bot, Msg("https://t.me/biaoqing")), chat)
        self.assertIs(self.check(bot, Msg("-1001234567890")), chat)
        origin = SimpleNamespace(type="channel", chat=SimpleNamespace(id=-1001234567890))
        self.assertIs(self.check(bot, Msg("", forward_origin=origin)), chat)

    def test_rejections(self):
        cases = [
            (channel_bot(user_status="member")[0], "你不是该频道的管理员"),
            (channel_bot(bot_status="member")[0], "机器人不是该频道的管理员"),
            (channel_bot(can_post=False)[0], "发布消息"),
        ]
        for bot, expected in cases:
            with self.assertRaises(sc.StickerCloneError) as raised:
                self.check(bot, Msg("@biaoqing"))
            self.assertIn(expected, str(raised.exception))
        bot, _ = channel_bot()
        with self.assertRaises(sc.StickerCloneError) as raised:
            self.check(bot, Msg("@nosuchchannel"))
        self.assertIn("找不到该频道", str(raised.exception))
        with self.assertRaises(sc.StickerCloneError):
            self.check(bot, Msg("随便写的"))
        bot.chats["@groupchat"] = SimpleNamespace(id=-100555, type="supergroup")
        with self.assertRaises(sc.StickerCloneError) as raised:
            self.check(bot, Msg("@groupchat"))
        self.assertIn("这不是频道", str(raised.exception))


def source_set(n=12, animated=False):
    stickers = [sticker(i, animated=animated) for i in range(n)]
    return SimpleNamespace(name="bagebage88", title="霸哥 @bage5",
                           sticker_type="regular", stickers=stickers)


class FixedModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def run_async(self, coro):
        with patch("tg_directory_bot.sticker_clone.asyncio.sleep", no_sleep):
            return asyncio.run(coro)

    def test_storage_profile_crud(self):
        self.assertIsNone(self.store.sticker_profile(42))
        self.store.set_sticker_fixed_title(42, "  联系 @me  ")
        self.store.set_sticker_channel(42, -100123, "频道", "chan")
        row = self.store.sticker_profile(42)
        self.assertEqual((row["fixed_title"], row["channel_id"]), ("联系 @me", -100123))
        self.store.set_sticker_fixed_title(42, "新标题")
        self.assertEqual(self.store.sticker_profile(42)["channel_id"], -100123)
        self.store.clear_sticker_channel(42)
        self.assertEqual(self.store.sticker_profile(42)["channel_id"], 0)
        self.assertEqual(self.store.sticker_profile(42)["fixed_title"], "新标题")
        with self.assertRaises(ValueError):
            self.store.set_sticker_fixed_title(42, "字" * 65)
        self.store.clear_sticker_profile(42)
        self.assertIsNone(self.store.sticker_profile(42))

    def test_first_setup_title_channel_then_auto_build_and_post(self):
        bot, chat = channel_bot()
        files = {f"file{i}": image_bytes((i * 20, 100, 200, 255)) for i in range(12)}
        bot.files.update(files)
        bot.sources["bagebage88"] = source_set(12)
        context, tasks = ctx(bot, self.store)

        async def scenario():
            update, query = callback_update("stk:fixed")
            await sc.handle_callback(update, context)
            self.assertIn("请先发送固定更改标题", query.edited[-1][0])

            update, msg = text_update("V系列哈希彩票找 @jiuye")
            self.assertTrue(await expect_stop(sc.handle_input(update, context)))
            self.assertIn("已保存固定更改标题", msg.replies[0].text)
            self.assertEqual(msg.replies[0].kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                             "stk:skip")

            update, msg = text_update("@biaoqing")
            self.assertTrue(await expect_stop(sc.handle_input(update, context)))
            self.assertIn("已设置固定发送频道：表情频道（@biaoqing）", msg.replies[0].text)
            self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "fixed_link")

            update, msg = text_update("https://t.me/addstickers/bagebage88")
            self.assertTrue(await expect_stop(sc.handle_input(update, context)))
            progress = msg.replies[0]
            self.assertIn("固定标题：V系列哈希彩票找 @jiuye", progress.text)
            self.assertIn("完成后发送到频道", progress.text)
            await asyncio.gather(*tasks)
            return progress

        progress = self.run_async(scenario())
        self.assertTrue(progress.edits[-1].startswith("贴纸包封装完成：\nhttps://t.me/addstickers/"))
        name = progress.edits[-1].rsplit("/", 1)[-1]
        self.assertEqual(bot.created[name].title, "V系列哈希彩票找 @jiuye")
        self.assertEqual(bot.created[name].owner, 42)
        posts = {item[1]: item for item in bot.sent}
        self.assertEqual(set(posts), {42, chat.id})
        kind, _, photo, caption, markup = posts[chat.id]
        self.assertEqual(kind, "photo")
        self.assertEqual(caption, sc.post_caption("V系列哈希彩票找 @jiuye", "regular", False))
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, "✨ 免费添加贴纸 ✨")
        self.assertEqual(button.url, f"https://t.me/addstickers/{name}")
        grid = Image.open(io.BytesIO(photo))
        self.assertEqual(grid.size, (5 * sp.CELL_SIZE + 6 * sp.CELL_PADDING,
                                     3 * sp.CELL_SIZE + 4 * sp.CELL_PADDING))
        self.assertEqual(posts[42][3], caption)
        # 固定模式保持，可连续发送
        self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "fixed_link")

    def test_skip_channel_and_no_title_prompt(self):
        bot = FakeBot(sources={"bagebage88": source_set(3, animated=True)})
        context, tasks = ctx(bot, self.store)
        self.store.set_sticker_fixed_title(42, "固定标题")

        async def scenario():
            update, query = callback_update("stk:set:channel")
            await sc.handle_callback(update, context)
            update, query = callback_update("stk:skip")
            await sc.handle_callback(update, context)
            self.assertIn("固定模式设置", query.edited[-1][0])
            update, query = callback_update("stk:fixed")
            await sc.handle_callback(update, context)
            self.assertIn("已进入固定模式", query.edited[-1][0])
            self.assertIn("未设置（只发给你）", query.edited[-1][0])
            update, msg = text_update("https://t.me/addstickers/bagebage88")
            await expect_stop(sc.handle_input(update, context))
            self.assertNotIn("请发送需要更改的联系方式", msg.replies[0].text)
            await asyncio.gather(*tasks)

        self.run_async(scenario())
        self.assertEqual([item[1] for item in bot.sent], [42])
        self.assertIn("#动态", bot.sent[0][3])
        self.assertEqual(bot.sent[0][0], "text")  # 没有可用图片时退回文字+按钮

    def test_non_link_text_passes_through_in_fixed_mode(self):
        self.store.set_sticker_fixed_title(42, "固定标题")
        context, _ = ctx(FakeBot(), self.store, {sc.STATE_KEY: {
            "step": "fixed_link", "chat_id": 42, "expires": time.time() + 60}})
        update, msg = text_update("XX地址")
        stopped = asyncio.run(expect_stop(sc.handle_input(update, context)))
        self.assertFalse(stopped)
        self.assertEqual(msg.replies, [])
        update, msg = text_update("bagebage88")  # bare name is not intercepted either
        self.assertFalse(asyncio.run(expect_stop(sc.handle_input(update, context))))

    def test_channel_post_failure_is_reported(self):
        bot, chat = channel_bot()
        bot.sources["bagebage88"] = source_set(2)
        bot.fail_channel_post = Forbidden("bot is not a member of the channel chat")
        self.store.set_sticker_fixed_title(42, "固定标题")
        self.store.set_sticker_channel(42, chat.id, "表情频道", "biaoqing")
        context, tasks = ctx(bot, self.store, {sc.STATE_KEY: {
            "step": "fixed_link", "chat_id": 42, "expires": time.time() + 60}})

        async def scenario():
            update, msg = text_update("https://t.me/addstickers/bagebage88")
            await expect_stop(sc.handle_input(update, context))
            results = await asyncio.gather(*tasks)
            return msg.replies[0], results

        progress, results = self.run_async(scenario())
        self.assertIsNotNone(results[0])
        self.assertIn("贴纸包封装完成", progress.edits[-1])
        notices = [item[3] for item in bot.sent if item[1] == 42 and "失败" in item[3]]
        self.assertEqual(len(notices), 1)
        self.assertIn("发送到频道 表情频道（@biaoqing） 失败", notices[0])

    def test_invalid_channel_keeps_prompt(self):
        bot, _ = channel_bot(user_status="member")
        context, _ = ctx(bot, self.store, {sc.STATE_KEY: {
            "step": "fixed_channel", "chat_id": 42, "expires": time.time() + 60}})
        update, msg = text_update("@biaoqing")
        asyncio.run(expect_stop(sc.handle_input(update, context)))
        self.assertIn("你不是该频道的管理员", msg.replies[0].text)
        self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "fixed_channel")
        self.assertIsNone(self.store.sticker_profile(42))

    def test_settings_view_modify_and_clear(self):
        self.store.set_sticker_fixed_title(42, "老标题")
        self.store.set_sticker_channel(42, -100777, "频道A", "")
        context, _ = ctx(FakeBot(), self.store)

        async def scenario():
            update, query = callback_update("stk:settings")
            await sc.handle_callback(update, context)
            text, markup = query.edited[-1]
            self.assertIn("固定更改标题：老标题", text)
            self.assertIn("固定发送频道：频道A", text)
            data = [b.callback_data for b in buttons(markup)]
            for expected in ("stk:set:title", "stk:set:channel", "stk:clear:channel",
                             "stk:clear:all", "stk:fixed", "stk:menu"):
                self.assertIn(expected, data)
            update, _ = callback_update("stk:set:title")
            await sc.handle_callback(update, context)
            update, msg = text_update("新标题")
            await expect_stop(sc.handle_input(update, context))
            self.assertIn("固定更改标题：新标题", msg.replies[0].text)
            self.assertNotIn(sc.STATE_KEY, context.user_data)
            update, query = callback_update("stk:clear:channel")
            await sc.handle_callback(update, context)
            self.assertIn("固定发送频道：未设置", query.edited[-1][0])
            update, query = callback_update("stk:clear:all")
            await sc.handle_callback(update, context)
            self.assertIn("固定更改标题：未设置", query.edited[-1][0])

        asyncio.run(scenario())
        self.assertIsNone(self.store.sticker_profile(42))


class DiceMaxBetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_storage_default_set_and_consistency(self):
        chat_id = -9001
        self.assertEqual(Decimal(str(self.store.points_config(chat_id)["dice_max_bet"])), 0)
        self.store.set_dice_max_bet(chat_id, "50", 1)
        self.assertEqual(Decimal(str(self.store.points_config(chat_id)["dice_max_bet"])), 50)
        with self.assertRaises(ValueError):
            self.store.set_dice_max_bet(chat_id, "0.5", 1)  # below min bet 1
        with self.assertRaises(ValueError):
            self.store.set_dice_min_bet(chat_id, 60, 1)     # above cap 50
        with self.assertRaises(ValueError):
            self.store.set_dice_max_bet(chat_id, -1, 1)
        self.store.set_dice_max_bet(chat_id, 0, 1)          # 0 = unlimited
        self.store.set_dice_min_bet(chat_id, 60, 1)

    def test_migration_adds_column_to_old_db(self):
        path = Path(self.temp.name) / "old.sqlite3"
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE group_points_config (chat_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO group_points_config (chat_id) VALUES (-5)")
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        self.assertEqual(Decimal(str(store.points_config(-5)["dice_max_bet"])), 0)

    def bet(self, chat_id, amount):
        dice_msg = SimpleNamespace(dice=SimpleNamespace(value=6))
        message = SimpleNamespace(reply_dice=AsyncMock(return_value=dice_msg),
                                  reply_text=AsyncMock(), message_id=9)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=77, username="alice", full_name="Alice"),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={},
                                  application=SimpleNamespace(bot_data={"store": self.store}))
        asyncio.run(point_dice_bet_reply(update, context, "大", amount))
        return message

    def test_bet_above_cap_rejected_without_deduction(self):
        chat_id = -4343
        self.store.set_points_enabled(chat_id, True, 1)
        self.store.adjust_points(chat_id, 77, 500, "seed", 1, "alice", "Alice")
        message = self.bet(chat_id, 300)                  # unlimited by default
        message.reply_dice.assert_awaited_once()
        self.store.set_dice_max_bet(chat_id, 100, 1)
        before = self.store.point_account(chat_id, 77)["balance"]
        message = self.bet(chat_id, 101)
        message.reply_dice.assert_not_awaited()
        self.assertEqual(message.reply_text.await_args.args[0], "单注最高 100 积分")
        self.assertEqual(self.store.point_account(chat_id, 77)["balance"], before)
        message = self.bet(chat_id, 100)
        message.reply_dice.assert_awaited_once()

    def test_settings_view_and_wizard(self):
        config = self.store.points_config(-1)
        text, markup = dice_settings_view(config)
        self.assertIn("单注上限：不限", text)
        self.assertIn("points:set:dicemax", [b.callback_data for b in buttons(markup)])
        self.store.set_dice_max_bet(-1, 20, 1)
        self.assertIn("单注上限：20 积分", dice_settings_view(self.store.points_config(-1))[0])
        self.assertIn("points_dicemax", settings_wizard.FLOWS)
        self.assertEqual(menu_mode_group_permission("points_dicemax"), "diceodds")

    def test_commit_input_sets_cap(self):
        chat_id = -7070
        self.store.set_points_enabled(chat_id, True, 1)
        class NoneMessage(SimpleNamespace):
            def __getattr__(self, name):
                return None

        message = NoneMessage(text="80", message_id=3, chat_id=-7070, reply_text=AsyncMock(
            return_value=SimpleNamespace(chat_id=-7070, message_id=4)))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=1, username="a", full_name="A"),
            effective_message=message,
        )
        context = SimpleNamespace(
            user_data={"menu_mode": "points_dicemax"}, bot=SimpleNamespace(),
            application=SimpleNamespace(bot_data={"store": self.store}),
        )
        with patch("tg_directory_bot.bot.is_chat_admin", AsyncMock(return_value=True)), \
                patch("tg_directory_bot.bot.has_group_permission", return_value=True):
            asyncio.run(commit_group_menu_input(update, context))
        self.assertEqual(Decimal(str(self.store.points_config(chat_id)["dice_max_bet"])), 80)
        self.assertIn("单注上限已设为 80 积分", message.reply_text.await_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
