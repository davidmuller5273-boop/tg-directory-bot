import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from telegram.ext import ApplicationHandlerStop

from tg_directory_bot import sticker_clone as sc
from tg_directory_bot.bot import HELP_TEXT, cancel, jx_command


def sticker(i, *, video=False, animated=False, emoji="😀", repaint=False):
    return SimpleNamespace(
        file_id=f"file{i}", emoji=emoji, is_video=video, is_animated=animated,
        mask_position=None, needs_repainting=repaint,
    )


def sticker_set(n, *, name="bagebage88", sticker_type="regular", title="霸哥 @bage5"):
    stickers = [sticker(i, video=i % 3 == 1, animated=i % 3 == 2) for i in range(n)]
    return SimpleNamespace(name=name, title=title, sticker_type=sticker_type, stickers=stickers)


async def no_sleep(*_a, **_k):
    return None


class FakeStickerBot:
    """Minimal in-memory Bot API for sticker sets."""

    def __init__(self, sources, *, bad_files=(), taken=(), create_errors=None,
                 add_errors=None):
        self.sources = dict(sources)
        self.created = {}
        self.bad_files = set(bad_files)
        self.taken = set(taken)
        self.create_errors = list(create_errors or [])
        self.add_errors = dict(add_errors or {})
        self.create_calls = []
        self.add_calls = []
        self.username = "sssxxxjqbot"

    async def get_sticker_set(self, name):
        if name in self.created:
            data = self.created[name]
            return SimpleNamespace(name=name, title=data["title"],
                                   sticker_type=data["type"], stickers=list(data["stickers"]))
        if name in self.sources:
            return self.sources[name]
        raise BadRequest("Stickerset_invalid")

    async def create_new_sticker_set(self, user_id, name, title, stickers,
                                     sticker_type=None, needs_repainting=None):
        self.create_calls.append(dict(user_id=user_id, name=name, title=title,
                                      stickers=list(stickers), sticker_type=sticker_type,
                                      needs_repainting=needs_repainting))
        if self.create_errors:
            raise self.create_errors.pop(0)
        if name in self.taken or name in self.created:
            raise BadRequest("Sticker set name is already occupied")
        if len(stickers) > 50:
            raise BadRequest("too many initial stickers")
        if any(s.sticker in self.bad_files for s in stickers):
            raise BadRequest("Wrong file identifier/http url specified")
        self.created[name] = {"owner": user_id, "title": title, "type": sticker_type,
                              "stickers": list(stickers)}
        return True

    async def add_sticker_to_set(self, user_id, name, sticker):
        self.add_calls.append((user_id, name, sticker.sticker))
        queued = self.add_errors.get(sticker.sticker)
        if queued:
            error = queued.pop(0)
            if not queued:
                self.add_errors.pop(sticker.sticker)
            if error == "timeout-added":
                self.created[name]["stickers"].append(sticker)
                raise TimedOut()
            raise error
        if sticker.sticker in self.bad_files:
            raise BadRequest("Wrong file identifier/http url specified")
        self.created[name]["stickers"].append(sticker)
        return True


def run(coro):
    with patch("tg_directory_bot.sticker_clone.asyncio.sleep", no_sleep):
        return asyncio.run(coro)


class HelperTest(unittest.TestCase):
    def test_parse_links_and_names(self):
        cases = {
            "https://t.me/addstickers/bagebage88": "bagebage88",
            "t.me/addstickers/bagebage88": "bagebage88",
            "http://telegram.me/addstickers/Abc_1/": "Abc_1",
            "https://t.me/addemoji/MyEmoji?x=1": "MyEmoji",
            "tg://addstickers?set=Foo": "Foo",
            "bagebage88": "bagebage88",
        }
        for text, expected in cases.items():
            self.assertEqual(sc.parse_sticker_set_name(text), expected, text)
        for bad in ("", "https://t.me/joinchat/abc", "https://example.com/addstickers/x",
                    "1abc", "has space", "a" * 65):
            self.assertIsNone(sc.parse_sticker_set_name(bad), bad)

    def test_build_set_name_rules(self):
        name = sc.build_set_name("bagebage88", "sssxxxjqbot", "5A2F61AB")
        self.assertEqual(name, "bagebage88_5a2f61ab_by_sssxxxjqbot")
        long_name = sc.build_set_name("x" * 64, "a_very_long_bot_username_bot", "deadbeef")
        self.assertLessEqual(len(long_name), 64)
        self.assertTrue(long_name.endswith("_deadbeef_by_a_very_long_bot_username_bot"))
        odd = sc.build_set_name("__9a__b__", "bot", "00000000")
        self.assertRegex(odd, r"^[A-Za-z][A-Za-z0-9_]*$")
        self.assertNotIn("__", odd)
        self.assertNotEqual(sc.build_set_name("a", "b"), sc.build_set_name("a", "b"))

    def test_input_sticker_formats(self):
        self.assertEqual(sc.sticker_format(sticker(0)), "static")
        self.assertEqual(sc.sticker_format(sticker(0, video=True)), "video")
        self.assertEqual(sc.sticker_format(sticker(0, animated=True)), "animated")
        item = sc.input_sticker_from(sticker(5, emoji=None), "regular")
        self.assertEqual((item.sticker, item.format, tuple(item.emoji_list)),
                         ("file5", "static", ("🙂",)))

    def test_title_length_counts_utf16(self):
        self.assertEqual(sc.title_length("V系列"), 3)
        self.assertEqual(sc.title_length("😀"), 2)

    def test_help_and_menu(self):
        self.assertIn("• /jx：复制贴纸包并改标题", HELP_TEXT)
        self.assertLess(HELP_TEXT.index("🧰 其他"), HELP_TEXT.index("/jx"))
        source = (sc.__file__.replace("sticker_clone.py", "bot.py"))
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('BotCommand("jx", "复制贴纸包并改标题")', text)
        self.assertIn('CommandHandler("jx", jx_command)', text)


class CloneJobTest(unittest.TestCase):
    def test_clone_80_stickers_create_50_then_add_30(self):
        bot = FakeStickerBot({"bagebage88": sticker_set(80)})
        progress = []

        async def on_progress(done, total):
            progress.append((done, total))

        result = run(sc.clone_sticker_set(
            bot, 42, "bagebage88", "V系列哈希彩票找 @jiuye", "sssxxxjqbot",
            progress=on_progress,
        ))
        self.assertRegex(result.name, r"^bagebage88_[0-9a-f]{8}_by_sssxxxjqbot$")
        self.assertEqual(result.link, f"https://t.me/addstickers/{result.name}")
        self.assertEqual((result.added, result.failed, result.skipped_over_limit), (80, 0, 0))
        create = bot.create_calls[0]
        self.assertEqual((create["user_id"], create["title"], create["sticker_type"]),
                         (42, "V系列哈希彩票找 @jiuye", "regular"))
        self.assertEqual(len(create["stickers"]), 50)
        self.assertEqual(len(bot.add_calls), 30)
        created = bot.created[result.name]["stickers"]
        self.assertEqual([s.sticker for s in created], [f"file{i}" for i in range(80)])
        self.assertEqual([s.format for s in created[:3]], ["static", "video", "animated"])
        self.assertEqual(progress[-1], (80, 80))

    def test_custom_emoji_pack_keeps_type_and_repainting(self):
        source = sticker_set(3, name="MyEmoji", sticker_type="custom_emoji")
        source.stickers[1].needs_repainting = True
        bot = FakeStickerBot({"MyEmoji": source})
        result = run(sc.clone_sticker_set(bot, 1, "MyEmoji", "T", "bot"))
        self.assertTrue(result.link.startswith("https://t.me/addemoji/"))
        self.assertEqual(bot.create_calls[0]["sticker_type"], "custom_emoji")
        self.assertIs(bot.create_calls[0]["needs_repainting"], True)

    def test_over_limit_is_truncated_and_reported(self):
        bot = FakeStickerBot({"big": sticker_set(130, name="big")})
        result = run(sc.clone_sticker_set(bot, 1, "big", "T", "bot"))
        self.assertEqual((result.added, result.skipped_over_limit, result.source_count),
                         (120, 10, 130))
        self.assertIn("10 张未复制", sc.result_text(result))

    def test_name_taken_is_regenerated(self):
        names = iter(["taken_by_bot", "taken2_by_bot", "free_by_bot"])
        bot = FakeStickerBot({"s": sticker_set(2, name="s")},
                             taken={"taken_by_bot", "taken2_by_bot"})
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot",
                                          name_factory=lambda: next(names)))
        self.assertEqual(result.name, "free_by_bot")
        self.assertEqual(len(bot.create_calls), 3)

    def test_bad_sticker_skipped_and_counted(self):
        bot = FakeStickerBot({"s": sticker_set(60, name="s")},
                             bad_files={"file3", "file55"})
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        self.assertEqual((result.added, result.failed), (58, 2))
        self.assertIn("2 张复制失败已跳过", sc.result_text(result))
        names = [s.sticker for s in bot.created[result.name]["stickers"]]
        self.assertNotIn("file3", names)
        self.assertNotIn("file55", names)

    def test_first_stickers_bad_falls_back_to_next_valid(self):
        bot = FakeStickerBot({"s": sticker_set(5, name="s")},
                             bad_files={"file0", "file1"})
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        self.assertEqual((result.added, result.failed), (3, 2))
        self.assertEqual(bot.created[result.name]["stickers"][0].sticker, "file2")

    def test_retry_after_is_waited_out(self):
        bot = FakeStickerBot({"s": sticker_set(52, name="s")},
                             add_errors={"file51": [RetryAfter(3), RetryAfter(2)]})
        waits = []

        async def record(seconds):
            waits.append(seconds)

        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot", sleep=record))
        self.assertEqual((result.added, result.failed), (52, 0))
        self.assertIn(4, waits)
        self.assertIn(3, waits)

    def test_create_retry_after_then_success(self):
        bot = FakeStickerBot({"s": sticker_set(2, name="s")}, create_errors=[RetryAfter(1)])
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        self.assertEqual(result.added, 2)

    def test_timeout_on_add_does_not_duplicate(self):
        bot = FakeStickerBot({"s": sticker_set(52, name="s")},
                             add_errors={"file50": ["timeout-added"]})
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        names = [s.sticker for s in bot.created[result.name]["stickers"]]
        self.assertEqual(names.count("file50"), 1)
        self.assertEqual((result.added, result.failed, len(names)), (52, 0, 52))

    def test_user_not_started_bot(self):
        for error in (BadRequest("PEER_ID_INVALID"), Forbidden("bot can't initiate conversation")):
            bot = FakeStickerBot({"s": sticker_set(2, name="s")}, create_errors=[error])
            with self.assertRaises(sc.StickerCloneError) as ctx:
                run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
            self.assertIn("/start", str(ctx.exception))

    def test_source_not_found(self):
        with self.assertRaises(sc.StickerCloneError):
            run(sc.clone_sticker_set(FakeStickerBot({}), 1, "nope", "T", "bot"))


class FakeMessage:
    _ids = iter(range(1000, 100000))

    def __init__(self, text="", chat_id=42):
        self.text = text
        self.message_id = next(self._ids)
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        reply = FakeMessage(text, self.chat_id)
        reply.kwargs = kwargs
        reply.edits = []

        async def edit_text(new_text, **_kw):
            reply.edits.append(new_text)
        reply.edit_text = edit_text
        self.replies.append(reply)
        return reply


def make_context(bot, user_data=None, args=None):
    tasks = []
    app = SimpleNamespace(bot_data={"bot_username": "sssxxxjqbot"})

    def create_task(coro):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task
    app.create_task = create_task
    return SimpleNamespace(bot=bot, application=app, user_data=user_data if user_data is not None else {},
                           args=args or []), tasks


def make_update(text, *, user_id=42, chat_type=ChatType.PRIVATE, chat_id=None):
    message = FakeMessage(text, chat_id or user_id)
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id or user_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    ), message


class FlowTest(unittest.TestCase):
    def test_full_conversation_like_example(self):
        bot = FakeStickerBot({"bagebage88": sticker_set(80, title="所有彩票平台主管 霸哥 @bage5")})

        async def scenario():
            context, tasks = make_context(bot)
            update, msg = make_update("/jx")
            await sc.begin(update, context)
            self.assertEqual(msg.replies[0].text, sc.LINK_PROMPT)

            update, msg = make_update("https://t.me/addstickers/bagebage88")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertEqual(
                msg.replies[0].text,
                "已解析贴纸包：所有彩票平台主管 霸哥 @bage5\n共 80 张贴图\n\n"
                "请发送需要更改的联系方式（将作为新贴纸包标题）：",
            )

            update, msg = make_update("V系列哈希彩票找 @jiuye")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            progress = msg.replies[0]
            self.assertEqual(progress.text, "开始封装贴纸包（共 80 张），请稍候…")
            self.assertNotIn(sc.STATE_KEY, context.user_data)
            self.assertIn(42, context.application.bot_data["sticker_clone_active"])
            await asyncio.gather(*tasks)
            self.assertNotIn(42, context.application.bot_data["sticker_clone_active"])
            return progress

        progress = run(scenario())
        final = progress.edits[-1]
        self.assertRegex(
            final, r"^贴纸包封装完成：\nhttps://t\.me/addstickers/bagebage88_[0-9a-f]{8}_by_sssxxxjqbot$"
        )
        name = final.rsplit("/", 1)[-1]
        self.assertEqual(bot.created[name]["owner"], 42)
        self.assertEqual(bot.created[name]["title"], "V系列哈希彩票找 @jiuye")

    def test_invalid_link_and_missing_set_keep_prompt(self):
        bot = FakeStickerBot({})

        async def scenario():
            context, _ = make_context(bot, {sc.STATE_KEY: {
                "step": "link", "chat_id": 42, "expires": time.time() + 60}})
            update, msg = make_update("hello world")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertIn("链接格式不正确", msg.replies[0].text)
            update, msg = make_update("https://t.me/addstickers/missing")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertIn("未找到该贴纸包", msg.replies[0].text)
            self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "link")
        run(scenario())

    def test_title_validation(self):
        async def scenario():
            context, tasks = make_context(FakeStickerBot({"s": sticker_set(1, name="s")}), {
                sc.STATE_KEY: {"step": "title", "chat_id": 42, "source": "s", "count": 1,
                               "expires": time.time() + 60}})
            update, msg = make_update("字" * 65)
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertIn("1-64", msg.replies[0].text)
            self.assertEqual(tasks, [])
            self.assertIn(sc.STATE_KEY, context.user_data)
        run(scenario())

    def test_one_job_per_user(self):
        async def scenario():
            context, tasks = make_context(FakeStickerBot({"s": sticker_set(1, name="s")}))
            context.application.bot_data["sticker_clone_active"] = {42}
            update, msg = make_update("/jx")
            await sc.begin(update, context)
            self.assertIn("正在封装中", msg.replies[0].text)
            self.assertNotIn(sc.STATE_KEY, context.user_data)
        run(scenario())

    def test_global_semaphore_limits_concurrency(self):
        running = {"now": 0, "peak": 0}

        class SlowBot(FakeStickerBot):
            async def create_new_sticker_set(self, *args, **kwargs):
                running["now"] += 1
                running["peak"] = max(running["peak"], running["now"])
                await asyncio.sleep(0.01)
                running["now"] -= 1
                return await super().create_new_sticker_set(*args, **kwargs)

        bot = SlowBot({"s": sticker_set(1, name="s")})

        async def scenario():
            bot_data = {}
            jobs = []
            for user in range(5):
                progress = await FakeMessage().reply_text("x")
                jobs.append(sc.run_job(bot, bot_data, user, "s", "T", "bot", progress))
            results = await asyncio.gather(*jobs)
            self.assertTrue(all(results))

        asyncio.run(scenario())
        self.assertLessEqual(running["peak"], sc.MAX_CONCURRENT_JOBS)

    def test_group_redirects_to_private(self):
        async def scenario():
            context, _ = make_context(FakeStickerBot({}))
            update, msg = make_update("/jx", chat_type=ChatType.SUPERGROUP, chat_id=-100)
            await sc.begin(update, context)
            reply = msg.replies[0]
            self.assertIn("私聊", reply.text)
            url = reply.kwargs["reply_markup"].inline_keyboard[0][0].url
            self.assertEqual(url, "https://t.me/sssxxxjqbot?start=jx")
            self.assertNotIn(sc.STATE_KEY, context.user_data)
        run(scenario())

    def test_commands_and_other_chats_pass_through(self):
        async def scenario():
            state = {"step": "link", "chat_id": 42, "expires": time.time() + 60}
            context, _ = make_context(FakeStickerBot({}), {sc.STATE_KEY: state})
            update, msg = make_update("/cancel")
            await sc.handle_input(update, context)  # no stop
            update, msg = make_update("hi", chat_type=ChatType.GROUP, chat_id=-5)
            await sc.handle_input(update, context)
            self.assertEqual(msg.replies, [])
            context.user_data[sc.STATE_KEY]["expires"] = time.time() - 1
            update, msg = make_update("https://t.me/addstickers/x")
            await sc.handle_input(update, context)
            self.assertNotIn(sc.STATE_KEY, context.user_data)
        run(scenario())

    def test_cancel_clears_prompt_state(self):
        async def scenario():
            context, _ = make_context(FakeStickerBot({}), {sc.STATE_KEY: {"step": "title"}})
            context.application.bot_data.update({"config": SimpleNamespace(
                admin_ids=set(), super_admin_ids=set(), developer_ids=set())})
            update, msg = make_update("/cancel")
            with patch("tg_directory_bot.bot.main_keyboard_for", return_value=None):
                await cancel(update, context)
            self.assertNotIn(sc.STATE_KEY, context.user_data)
            self.assertEqual(msg.replies[0].text, "已取消。")
        run(scenario())

    def test_jx_command_with_link_argument(self):
        bot = FakeStickerBot({"bagebage88": sticker_set(3)})

        async def scenario():
            context, _ = make_context(bot, args=["https://t.me/addstickers/bagebage88"])
            update, msg = make_update("/jx https://t.me/addstickers/bagebage88")
            with patch("tg_directory_bot.bot.guard", AsyncMock(return_value=True)):
                await jx_command(update, context)
            self.assertIn("共 3 张贴图", msg.replies[0].text)
            self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "title")
        run(scenario())

    def test_job_failure_message(self):
        bot = FakeStickerBot({"s": sticker_set(2, name="s")},
                             create_errors=[BadRequest("PEER_ID_INVALID")])

        async def scenario():
            bot_data = {}
            progress = await FakeMessage().reply_text("x")
            result = await sc.run_job(bot, bot_data, 7, "s", "T", "bot", progress)
            self.assertIsNone(result)
            self.assertIn("贴纸包封装失败", progress.edits[-1])
            self.assertIn("/start", progress.edits[-1])
            self.assertEqual(bot_data["sticker_clone_active"], set())
        run(scenario())


if __name__ == "__main__":
    unittest.main()
