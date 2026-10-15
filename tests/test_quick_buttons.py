"""快捷发布按钮：批量输入、按已有按钮前后/上下定位、移动/修改/删除、预览、发布。"""

import json
import tempfile
import unittest
from pathlib import Path

from telegram import MessageEntity

from tg_directory_bot import bot as handlers
from tg_directory_bot import quick_buttons as qb
from tg_directory_bot.storage import DirectoryStore

from tests import test_group_wizard_save as gw


class ParseTest(unittest.TestCase):
    def test_rows_pipes_and_colors(self):
        rows = qb.parse_buttons("官网 - https://a.com | 客服 - https://t.me/x\n频道 - https://t.me/y - 绿色")
        self.assertEqual([[b["text"] for b in r] for r in rows], [["官网", "客服"], ["频道"]])
        self.assertEqual(rows[1][0]["color"], "success")
        self.assertEqual(rows[0][1]["url"], "https://t.me/x")

    def test_custom_emoji_icon(self):
        text = "官网 - https://a.com | 😀客服 - https://b.com"
        offset = len("官网 - https://a.com | ".encode("utf-16-le")) // 2
        rows = qb.parse_buttons(text, [MessageEntity("custom_emoji", offset, 2, custom_emoji_id="42")])
        self.assertEqual((rows[0][1]["text"], rows[0][1]["custom_emoji_id"]), ("客服", "42"))
        self.assertEqual(rows[0][0]["custom_emoji_id"], "")

    def test_errors(self):
        for bad in ("", "没有链接", "a - https://x - 紫色", " | ".join(f"b{i} - https://x" for i in range(9))):
            with self.assertRaises(ValueError):
                qb.parse_buttons(bad)

    def test_anchor_insert(self):
        layout = [[1, 2], [3]]
        new = [["n"]]
        self.assertEqual(qb.insert_rows(layout, new, ("anchor", 2, "before")), [[1, "n", 2], [3]])
        self.assertEqual(qb.insert_rows(layout, new, ("anchor", 2, "after")), [[1, 2, "n"], [3]])
        self.assertEqual(qb.insert_rows(layout, new, ("anchor", 3, "above")), [[1, 2], ["n"], [3]])
        self.assertEqual(qb.insert_rows(layout, new, ("anchor", 1, "below")), [[1, 2], ["n"], [3]])
        self.assertEqual(qb.insert_rows(layout, [["a", "b"], ["c"]], ("anchor", 1, "after")), [[1, "a", "b", 2], ["c"], [3]])
        with self.assertRaises(ValueError):
            qb.insert_rows(layout, new, ("anchor", 9, "after"))


class StoreLayoutTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DirectoryStore(Path(temp.name) / "x.db")
        self.store.init()
        self.post = int(self.store.quick_post(-100)["id"])

    def names(self):
        return [[b["text"] for b in r] for r in self.store.quick_post_button_rows(self.post)]

    def ids(self):
        return {b["text"]: int(b["id"]) for b in self.store.quick_post_buttons(self.post)}

    def test_legacy_layout_kept_then_insert_and_move(self):
        self.store.add_quick_post_button(self.post, "L", "https://l", "default", "long")
        self.store.add_quick_post_button(self.post, "S1", "https://s1", "default", "short")
        self.store.add_quick_post_button(self.post, "S2", "https://s2", "default", "short")
        self.assertEqual(self.names(), [["L"], ["S1", "S2"]])
        ids = self.ids()
        self.store.insert_quick_post_buttons(self.post, [[{"text": "N", "url": "https://n"}]], ("anchor", ids["S2"], "before"))
        self.assertEqual(self.names(), [["L"], ["S1", "N", "S2"]])
        self.store.insert_quick_post_buttons(self.post, [[{"text": "T", "url": "https://t"}]], ("anchor", ids["L"], "above"))
        self.assertEqual(self.names(), [["T"], ["L"], ["S1", "N", "S2"]])
        self.assertTrue(self.store.move_quick_post_button(self.post, ids["L"], ("anchor", ids["S1"], "after")))
        self.assertEqual(self.names(), [["T"], ["S1", "L", "N", "S2"]])
        with self.assertRaises(ValueError):
            self.store.move_quick_post_button(self.post, ids["L"], ("anchor", ids["L"], "after"))
        self.store.delete_quick_post_button(self.post, self.ids()["N"])
        self.store.add_quick_post_button(self.post, "Z", "https://z")
        self.assertEqual(self.names(), [["T"], ["S1", "L", "S2"], ["Z"]])
        # restart keeps explicit rows
        again = DirectoryStore(self.store.db_path)
        again.init()
        self.assertEqual([[b["text"] for b in r] for r in again.quick_post_button_rows(self.post)], self.names())

    def test_markup_matches_layout(self):
        rows = qb.parse_buttons("A - https://a - 红色 | B - https://b\nC - https://c")
        self.store.insert_quick_post_buttons(self.post, rows)
        markup = handlers.quick_post_markup(self.store, self.store.quick_post(-100))
        self.assertEqual([[b.text for b in r] for r in markup.inline_keyboard], [["A", "B"], ["C"]])
        self.assertEqual(markup.inline_keyboard[0][0].api_kwargs["style"], "danger")


class QuickButtonWizardTest(gw.GroupWizardSaveTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.store.set_group_admin_permissions(gw.GROUP, gw.ADMIN, {"quickpost"}, 1)
        self.store.update_quick_post(gw.GROUP, 1, text="帖子正文")
        self.post = int(self.store.quick_post(gw.GROUP)["id"])

    def names(self):
        return [[b["text"] for b in r] for r in self.store.quick_post_button_rows(self.post)]

    def keyboards(self):
        out = []
        for api, p in self.request.calls:
            markup = p.get("reply_markup")
            if markup:
                markup = json.loads(markup) if isinstance(markup, str) else markup
                out.append((api, p, markup.get("inline_keyboard", [])))
        return out

    async def add(self, callback, text):
        await self.tap(callback)
        await self.send(text)
        await self.save()

    async def test_group_wizard_add_position_edit_delete_preview_publish(self):
        await self.tap("quickpost:menu")
        await self.tap("quickpost:view:buttons")
        # 没有按钮时「📍 指定位置」直接进入添加
        await self.tap("quickpost:picker")
        await self.send("官网 - https://a.com | 客服 - https://b.com")
        await self.save()
        self.assertEqual(self.names(), [["官网", "客服"]])
        ids = {b["text"]: int(b["id"]) for b in self.store.quick_post_buttons(self.post)}
        # 可点选的键盘预览
        self.request.calls.clear()
        await self.tap("quickpost:picker")
        picker = self.keyboards()[-1][2]
        self.assertEqual([[b["callback_data"] for b in r] for r in picker[:1]],
                         [[f"quickpost:pick:{ids['官网']}", f"quickpost:pick:{ids['客服']}"]])
        self.request.calls.clear()
        await self.tap(f"quickpost:pick:{ids['客服']}")
        labels = [b["text"] for r in self.keyboards()[-1][2] for b in r]
        for label in ("⬅️ 加在它前面", "➡️ 加在它后面", "⬆️ 上方新一行", "⬇️ 下方新一行", "✏️ 修改", "🗑 删除"):
            self.assertIn(label, labels)
        await self.add(f"quickpost:set:at:before:{ids['客服']}", "频道 - https://c.com")
        self.assert_saved_cleanly("✅ 按钮已添加")
        self.assertEqual(self.names(), [["官网", "频道", "客服"]])
        await self.add(f"quickpost:set:at:above:{ids['官网']}", "置顶 - https://top.com - 绿色")
        self.assertEqual(self.names(), [["置顶"], ["官网", "频道", "客服"]])
        # 修改 / 移动 / 删除
        await self.tap(f"quickpost:set:bedit:{ids['官网']}")
        await self.send("官方网站")
        await self.tap(self.button("下一步")[0], self.button("下一步")[1])
        await self.send("https://new.com")
        await self.tap(self.button("蓝色")[0], self.button("蓝色")[1])
        await self.save()
        edited = next(b for b in self.store.quick_post_buttons(self.post) if int(b["id"]) == ids["官网"])
        self.assertEqual((edited["text"], edited["url"], edited["color"]), ("官方网站", "https://new.com", "primary"))
        await self.tap(f"quickpost:bmove:{ids['客服']}")
        await self.tap(f"quickpost:pick:{ids['官网']}")
        await self.tap(f"quickpost:mv:below:{ids['官网']}")
        self.assertEqual(self.names(), [["置顶"], ["官方网站", "频道"], ["客服"]])
        await self.tap(f"quickpost:bdel:{ids['客服']}")
        self.assertEqual(self.names(), [["置顶"], ["官方网站", "频道"]])
        # 预览：带键盘发到当前对话，不算发布
        self.request.calls.clear()
        await self.tap("quickpost:preview")
        sent = [(p, kb) for api, p, kb in self.keyboards() if api == "sendMessage" and p.get("text") == "帖子正文"]
        self.assertEqual([[b["text"] for b in r] for r in sent[0][1]], [["置顶"], ["官方网站", "频道"]])
        # 发布
        self.request.calls.clear()
        await self.tap("quickpost:publish")
        sent = [(p, kb) for api, p, kb in self.keyboards() if api == "sendMessage" and p.get("text") == "帖子正文"]
        self.assertEqual(int(sent[0][0]["chat_id"]), gw.GROUP)
        self.assertEqual(sent[0][1][0][0]["url"], "https://top.com")
        self.assertEqual(sent[0][1][0][0].get("style"), "success")


    async def qa_add(self, callback, label, url, color, bad_url=None):
        await self.tap(callback)
        await self.send(label)
        texts_before = len(self.texts())
        if bad_url:
            await self.send(bad_url)
            self.assertTrue(any("链接无效" in t for t in self.texts()[texts_before:]), self.texts())
        await self.send(url)
        data, mid = self.button(color)
        self.request.calls.clear()
        await self.tap(data, mid)  # 选颜色即保存

    def continue_buttons(self):
        for api, p, kb in reversed(self.keyboards()):
            labels = {b["text"]: b.get("callback_data") for r in kb for b in r}
            if "✅ 完成" in labels:
                return p, labels
        self.fail("no continue panel")

    async def _qa_flow(self):
        await self.tap("quickpost:menu")
        await self.tap("quickpost:view:buttons")
        await self.qa_add("quickpost:set:button", "官网", "https://a.com", "蓝色", bad_url="a.com")
        self.assertEqual(self.names(), [["官网"]])
        first = self.store.quick_post_buttons(self.post)[0]
        self.assertEqual((first["url"], first["color"]), ("https://a.com", "primary"))
        panel, labels = self.continue_buttons()
        self.assertIn("✅ 按钮已添加", panel["text"])
        self.assertIn("第1行：[#", panel["text"])
        self.assertIn("➕ 同一行继续加", labels)
        self.assertIn("⬇️ 新一行继续加", labels)
        self.assertIn("官网", labels)  # 预览键盘
        await self.qa_add(labels["➕ 同一行继续加"], "客服", "tg://resolve?domain=x", "默认")
        self.assertEqual(self.names(), [["官网", "客服"]])
        _, labels = self.continue_buttons()
        await self.qa_add(labels["⬇️ 新一行继续加"], "频道", "http://c.com", "绿色")
        self.assertEqual(self.names(), [["官网", "客服"], ["频道"]])
        _, labels = self.continue_buttons()
        # 新一行之后再同一行继续
        await self.qa_add(labels["➕ 同一行继续加"], "群组", "https://d.com", "红色")
        self.assertEqual(self.names(), [["官网", "客服"], ["频道", "群组"]])
        _, labels = self.continue_buttons()
        await self.tap(labels["✅ 完成"])
        self.assertNotIn("menu_mode", self.user_data())
        # 📍 指定位置也是同样的问答
        ids = {b["text"]: int(b["id"]) for b in self.store.quick_post_buttons(self.post)}
        await self.qa_add(f"quickpost:set:at:before:{ids['官网']}", "置顶", "https://top.com", "默认")
        self.assertEqual(self.names(), [["置顶", "官网", "客服"], ["频道", "群组"]])

    async def test_qa_in_group(self):
        await self._qa_flow()

    async def test_qa_in_private(self):
        self.private = True
        self.app.user_data[gw.ADMIN]["selected_group_id"] = gw.GROUP
        await self._qa_flow()

    async def test_three_bad_links_cancel_without_saving(self):
        await self.tap("quickpost:set:button")
        await self.send("官网")
        for bad in ("ftp://x", "x", "www.x.com"):
            await self.send(bad)
        self.assertTrue(any("连续3次" in t for t in self.texts()))
        self.assertEqual(self.names(), [])


for _name in [n for n in dir(gw.GroupWizardSaveTest) if n.startswith("test_")]:
    setattr(QuickButtonWizardTest, _name, None)


if __name__ == "__main__":
    unittest.main()
