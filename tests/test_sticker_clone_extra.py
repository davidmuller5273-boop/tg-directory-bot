"""贴纸复制：自定义表情、超时/名称健壮性、按序号删除。"""
import asyncio
import unittest
from types import SimpleNamespace

from telegram.error import BadRequest, TimedOut
from telegram.ext import ApplicationHandlerStop

from tg_directory_bot import sticker_clone as sc
from tests.test_sticker_clone import (
    FakeStickerBot, make_context, make_update, run, sticker, sticker_set,
)


class SlowCreateBot(FakeStickerBot):
    """create_new_sticker_set times out on the client but succeeds on the server
    (set becomes visible after ``visible_after`` polls)."""

    def __init__(self, *a, visible_after=2, fail_kind="appear", **kw):
        super().__init__(*a, **kw)
        self.visible_after = visible_after
        self.polls = 0
        self.fail_kind = fail_kind
        self.hidden = set()
        self.timeouts_seen = []

    async def get_sticker_set(self, name):
        if name in self.hidden:
            self.polls += 1
            if self.polls < self.visible_after:
                raise BadRequest("Stickerset_invalid")
            self.hidden.discard(name)
        return await super().get_sticker_set(name)

    async def create_new_sticker_set(self, user_id, name, title, stickers,
                                     sticker_type=None, needs_repainting=None, **kw):
        self.timeouts_seen.append(kw)
        if not self.create_calls:
            await super().create_new_sticker_set(user_id, name, title, stickers,
                                                 sticker_type, needs_repainting)
            if self.fail_kind == "appear":
                self.hidden.add(name)
            else:  # never visible until retried with the same name
                self.hidden.add(name)
                self.visible_after = 10 ** 6
            raise TimedOut()
        return await super().create_new_sticker_set(user_id, name, title, stickers,
                                                    sticker_type, needs_repainting)


class RobustNameTest(unittest.TestCase):
    def test_long_timeouts_passed(self):
        bot = FakeStickerBot({"s": sticker_set(52, name="s")})
        calls = []
        original = bot.create_new_sticker_set

        async def spy(*a, **kw):
            calls.append(kw)
            return await original(*a, **kw)
        bot.create_new_sticker_set = spy
        run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        self.assertGreaterEqual(calls[0]["read_timeout"], 60)

    def test_timeout_then_set_appears_is_success(self):
        bot = SlowCreateBot({"s": sticker_set(50, name="s")}, visible_after=3)
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot"))
        self.assertEqual(len(bot.created), 1)          # 没有重复创建
        self.assertEqual(result.added, 50)
        self.assertEqual(len(bot.create_calls), 1)

    def test_timeout_then_occupied_same_name_is_ours(self):
        bot = SlowCreateBot({"s": sticker_set(5, name="s")}, fail_kind="late")
        names = iter(["first_by_bot", "second_by_bot"])

        async def scenario():
            result = await sc.clone_sticker_set(bot, 1, "s", "T", "bot",
                                                name_factory=lambda: next(names))
            return result
        # 第二次用同名重试 → occupied；之后查询到集合（取消隐藏）即视为成功
        original = bot.get_sticker_set

        async def get_set(name):
            if len(bot.create_calls) >= 2:
                bot.hidden.discard(name)
            return await original(name)
        bot.get_sticker_set = get_set
        result = run(scenario())
        self.assertEqual(result.name, "first_by_bot")
        self.assertEqual([c["name"] for c in bot.create_calls], ["first_by_bot", "first_by_bot"])
        self.assertEqual(len(bot.created), 1)

    def test_invalid_name_refreshes_username_with_get_me(self):
        bot = FakeStickerBot({"s": sticker_set(2, name="s")})
        original = bot.create_new_sticker_set

        async def create(user_id, name, title, stickers, sticker_type=None,
                         needs_repainting=None, **kw):
            if not name.lower().endswith("_by_realbot"):
                bot.create_calls.append({"name": name})
                raise BadRequest("Invalid sticker set name is specified")
            return await original(user_id, name, title, stickers, sticker_type, needs_repainting)

        async def get_me():
            return SimpleNamespace(username="RealBot")
        bot.create_new_sticker_set = create
        bot.get_me = get_me
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "wrongbot"))
        self.assertTrue(result.name.lower().endswith("_by_realbot"))

    def test_missing_username_uses_get_me(self):
        bot = FakeStickerBot({"s": sticker_set(2, name="s")})

        async def get_me():
            return SimpleNamespace(username="sssxxxjqbot")
        bot.get_me = get_me
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", ""))
        self.assertTrue(result.name.endswith("_by_sssxxxjqbot"))

    def test_non_ascii_source_name_sanitized(self):
        name = sc.build_set_name("表情_包", "bot", "abcdef12")
        self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
        self.assertNotIn("__", name)

    def test_name_occupied_many_times_reports_failure(self):
        bot = FakeStickerBot({"s": sticker_set(2, name="s")})
        bot.taken = {f"n{i}_by_bot" for i in range(20)}
        names = iter(sorted(bot.taken))
        with self.assertRaises(sc.StickerCloneError):
            run(sc.clone_sticker_set(bot, 1, "s", "T", "bot", name_factory=lambda: next(names)))


class CustomEmojiTest(unittest.TestCase):
    def test_custom_emoji_formats_and_emoji(self):
        stickers = [sticker(0), sticker(1, video=True), sticker(2, animated=True, emoji=None)]
        source = SimpleNamespace(name="Emo", title="E", sticker_type="custom_emoji", stickers=stickers)
        bot = FakeStickerBot({"Emo": source})
        result = run(sc.clone_sticker_set(bot, 1, "Emo", "T", "bot"))
        call = bot.create_calls[0]
        self.assertEqual([s.format for s in call["stickers"]], ["static", "video", "animated"])
        self.assertEqual(call["stickers"][2].emoji_list, ("🙂",))
        self.assertIs(call["needs_repainting"], False)
        self.assertEqual(result.link, f"https://t.me/addemoji/{result.name}")

    def test_custom_emoji_limit_200(self):
        source = sticker_set(210, name="Emo", sticker_type="custom_emoji")
        bot = FakeStickerBot({"Emo": source})
        result = run(sc.clone_sticker_set(bot, 1, "Emo", "T", "bot"))
        self.assertEqual((result.added, result.skipped_over_limit), (200, 10))


class DeletePositionsTest(unittest.TestCase):
    def test_split_and_plan(self):
        self.assertEqual(sc.split_link_and_positions("https://t.me/addstickers/abc 3|5|12"),
                         ("https://t.me/addstickers/abc", "3|5|12"))
        self.assertEqual(sc.split_link_and_positions("https://t.me/addstickers/abc"),
                         ("https://t.me/addstickers/abc", ""))
        deleted, notes = sc.plan_deletions(10, "3|5|12|5|0")
        self.assertEqual(deleted, [3, 5])
        self.assertIn("已忽略超出范围的序号：12、0（共 10 张）", notes)
        self.assertIn("已忽略重复的序号：5", notes)
        self.assertEqual(sc.plan_deletions(10, ""), ([], []))
        self.assertTrue(sc.is_sticker_link("https://t.me/addstickers/abc 1|2"))

    def test_clone_skips_positions(self):
        bot = FakeStickerBot({"s": sticker_set(6, name="s")})
        result = run(sc.clone_sticker_set(bot, 1, "s", "T", "bot", skip=[1, 3, 99]))
        names = [s.sticker for s in bot.created[result.name]["stickers"]]
        self.assertEqual(names, ["file1", "file3", "file4", "file5"])
        self.assertEqual(result.deleted, 2)
        self.assertIn("已按要求删除 2 张", sc.result_text(result))

    def test_conversation_with_positions(self):
        bot = FakeStickerBot({"abc": sticker_set(10, name="abc", title="原包")})

        async def scenario():
            context, tasks = make_context(bot)
            update, msg = make_update("/jx")
            await sc.begin(update, context)
            update, msg = make_update("https://t.me/addstickers/abc 3|5|12")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            text = msg.replies[0].text
            self.assertIn("将删除第 3、5 张，剩余 8 张", text)
            self.assertIn("已忽略超出范围的序号：12", text)
            update, msg = make_update("新标题")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertIn("共 8 张", msg.replies[0].text)
            await asyncio.gather(*tasks)
        run(scenario())
        created = next(iter(bot.created.values()))["stickers"]
        self.assertEqual(len(created), 8)
        self.assertNotIn("file2", [s.sticker for s in created])
        self.assertNotIn("file4", [s.sticker for s in created])

    def test_all_deleted_keeps_prompt(self):
        bot = FakeStickerBot({"abc": sticker_set(2, name="abc")})

        async def scenario():
            context, _ = make_context(bot)
            await sc.begin(make_update("/jx")[0], context)
            update, msg = make_update("https://t.me/addstickers/abc 1|2")
            with self.assertRaises(ApplicationHandlerStop):
                await sc.handle_input(update, context)
            self.assertIn("没有剩余贴图", msg.replies[0].text)
            self.assertEqual(context.user_data[sc.STATE_KEY]["step"], "link")
        run(scenario())

    def test_help_mentions_delete(self):
        self.assertIn("3|5|12", sc.LINK_PROMPT)


if __name__ == "__main__":
    unittest.main()
