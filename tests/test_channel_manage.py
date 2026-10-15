"""📡 频道管理 + 超级管理员与开发者功能一致（私人笔记除外）。"""

import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram import Update

from tg_directory_bot import bot as handlers
from tg_directory_bot import channel_manage
from tg_directory_bot.config import Config

from tests.test_group_wizard_save import BOT_ID, FakeRequest

SUPER = 7
DEV = 8
OTHER = 9
CH_A, CH_B, CH_C = -1001, -1002, -1003


def member_json(user_id, status):
    user = {"id": user_id, "is_bot": user_id == BOT_ID, "first_name": "U"}
    if status == "administrator":
        return {
            "status": "administrator", "user": user, "can_be_edited": False, "is_anonymous": False,
            "can_manage_chat": True, "can_delete_messages": True, "can_manage_video_chats": True,
            "can_restrict_members": True, "can_promote_members": False, "can_change_info": True,
            "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False,
            "can_delete_stories": False, "can_post_messages": True, "can_edit_messages": True,
        }
    if status == "creator":
        return {"status": "creator", "user": user, "is_anonymous": False}
    return {"status": "left", "user": user}


class ChannelRequest(FakeRequest):
    def __init__(self):
        super().__init__()
        self.members = {}
        self.no_rights = set()
        self.retry_once = set()
        self.copy_fail = set()

    def error(self, code, text, **extra):
        body = {"ok": False, "error_code": code, "description": text}
        body.update(extra)
        return code, json.dumps(body).encode()

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if api == "getChatMember":
            self.calls.append((api, params))
            status = self.members.get((int(params["chat_id"]), int(params["user_id"])), "left")
            return 200, json.dumps({"ok": True, "result": member_json(int(params["user_id"]), status)}).encode()
        if api == "getChat":
            self.calls.append((api, params))
            return 200, json.dumps({"ok": True, "result": {"id": CH_C, "type": "channel", "title": "频道C", "username": "chc", "accent_color_id": 0, "max_reaction_count": 11}}).encode()
        if api == "getFile":
            self.calls.append((api, params))
            return 200, json.dumps({"ok": True, "result": {"file_id": "f", "file_unique_id": "u", "file_path": "photos/a.jpg"}}).encode()
        if api in {"sendMessage", "sendPhoto"} and int(params.get("chat_id", 0)) < 0 and int(params["chat_id"]) in {CH_A, CH_B, CH_C}:
            self.calls.append((api, params))
            chat = int(params["chat_id"])
            if (api, chat) in self.retry_once:
                self.retry_once.discard((api, chat))
                return self.error(429, "Too Many Requests: retry after 3", parameters={"retry_after": 3})
            if chat in self.no_rights:
                return self.error(400, "Bad Request: not enough rights to send")
            return 200, json.dumps({"ok": True, "result": {
                "message_id": next(self.ids), "date": int(time.time()),
                "chat": {"id": chat, "type": "channel", "title": "x"}, "text": "x"}}).encode()
        if api in {"setChatTitle", "setChatDescription", "setChatPhoto", "copyMessage", "forwardMessage"}:
            self.calls.append((api, params))
            chat = int(params["chat_id"])
            if (api, chat) in self.retry_once:
                self.retry_once.discard((api, chat))
                return self.error(429, "Too Many Requests: retry after 3", parameters={"retry_after": 3})
            if chat in self.no_rights:
                return self.error(400, "Bad Request: not enough rights to change chat title")
            if api == "copyMessage" and chat in self.copy_fail:
                return self.error(400, "Bad Request: message to copy not found")
            if api == "copyMessage":
                result = {"message_id": next(self.ids)}
            elif api == "forwardMessage":
                result = {"message_id": next(self.ids), "date": int(time.time()),
                          "chat": {"id": chat, "type": "channel", "title": "x"}, "text": "fwd"}
            else:
                result = True
            return 200, json.dumps({"ok": True, "result": result}).encode()
        if api.endswith(".jpg"):
            return 200, b"\xff\xd8jpegdata"
        return await super().do_request(url, method, request_data, **kwargs)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config(
            bot_token="1:test", admin_ids=set(), super_admin_ids={SUPER}, developer_ids={DEV},
            db_path=Path(self.temp.name) / "bot.sqlite3", categories=("tools",),
            blocked_keywords=(), tron_block_scan=False,
        )
        self.app = handlers.build_application(config)
        self.request = ChannelRequest()
        object.__setattr__(self.app.bot, "_request", (self.request, self.request))
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.store = self.app.bot_data["store"]
        self.ids = itertools.count(1)
        self.mids = itertools.count(5000)
        self.sleep = AsyncMock()
        patcher = patch.object(channel_manage, "_sleep", self.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def user(self, uid=SUPER):
        return {"id": uid, "is_bot": False, "first_name": "U", "username": f"u{uid}"}

    async def process(self, data):
        data["update_id"] = next(self.ids)
        await self.app.process_update(Update.de_json(data, self.app.bot))

    async def send(self, uid=SUPER, **content):
        msg = {"message_id": next(self.mids), "date": int(time.time()),
               "chat": {"id": uid, "type": "private", "first_name": "U"}, "from": self.user(uid)}
        msg.update(content)
        await self.process({"message": msg})

    async def tap(self, data, uid=SUPER, message_id=777):
        await self.process({"callback_query": {
            "id": str(next(self.ids)), "from": self.user(uid), "chat_instance": "ci", "data": data,
            "message": {"message_id": message_id, "date": int(time.time()),
                        "chat": {"id": uid, "type": "private", "first_name": "U"},
                        "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}, "text": "panel"},
        }})

    def texts(self):
        return [str(p.get("text", "")) for a, p in self.request.calls if a in {"sendMessage", "editMessageText"}]

    def report(self):
        return next(t for t in reversed(self.texts()) if "成功" in t)

    def last_keyboard(self):
        for api, p in reversed(self.request.calls):
            if api in {"sendMessage", "editMessageText"} and p.get("reply_markup"):
                m = p["reply_markup"]
                m = json.loads(m) if isinstance(m, str) else m
                return [b for row in m["inline_keyboard"] for b in row]
        return []

    def button(self, label):
        for b in self.last_keyboard():
            if b["text"] == label:
                return b["callback_data"]
        self.fail(f"{label} not in {[b['text'] for b in self.last_keyboard()]}")

    async def wizard_save(self):
        await self.tap(self.button("确认保存"), message_id=self.panel_id())

    def panel_id(self):
        for api, p in reversed(self.request.calls):
            if api in {"sendMessage", "editMessageText"} and "wizard:" in json.dumps(p.get("reply_markup") or ""):
                return int(p.get("message_id") or 777)
        return 777

    def bot_added(self, chat_id, title, status="administrator", username=""):
        return {"my_chat_member": {
            "chat": {"id": chat_id, "type": "channel", "title": title, "username": username} if username
            else {"id": chat_id, "type": "channel", "title": title},
            "from": self.user(SUPER), "date": int(time.time()),
            "old_chat_member": member_json(BOT_ID, "left"),
            "new_chat_member": member_json(BOT_ID, status),
        }}

    async def setup_channels(self):
        await self.process(self.bot_added(CH_A, "频道A"))
        await self.process(self.bot_added(CH_B, "频道B"))
        for ch in (CH_A, CH_B):
            self.request.members[(ch, BOT_ID)] = "administrator"
            self.request.members[(ch, SUPER)] = "creator"


class TrackingTest(Base):
    async def test_my_chat_member_and_channel_post_tracking(self):
        await self.process(self.bot_added(CH_A, "频道A", username="cha"))
        row = self.store.managed_channel(CH_A)
        self.assertEqual((row["title"], row["username"], row["status"]), ("频道A", "cha", "active"))
        self.assertTrue(json.loads(row["rights_json"])["can_change_info"])
        await self.process({"channel_post": {"message_id": 1, "date": int(time.time()),
                                             "chat": {"id": CH_B, "type": "channel", "title": "频道B"}, "text": "hi"}})
        self.assertEqual(self.store.managed_channel(CH_B)["status"], "active")
        await self.process(self.bot_added(CH_A, "频道A", status="left"))
        self.assertEqual(self.store.managed_channel(CH_A)["status"], "left")
        self.assertEqual([int(r["chat_id"]) for r in self.store.managed_channels()], [CH_B])

    async def test_permission_filter_and_add_by_username(self):
        await self.setup_channels()
        self.request.members[(CH_B, SUPER)] = "left"  # 用户不是 B 的管理员
        await self.tap("chm:refresh")
        self.assertIn("你和机器人都是管理员的频道：1 个", self.texts()[-1])
        await self.tap("chm:list")
        labels = [b["text"] for b in self.last_keyboard()]
        self.assertIn("频道A", labels)
        self.assertNotIn("频道B", labels)
        # 普通用户也能打开，但只看到自己是管理员的频道（此时 0 个）
        await self.tap("chm:refresh", uid=OTHER)
        self.assertIn("你和机器人都是管理员的频道：0 个", self.texts()[-1])
        # 添加更早加入的频道：@用户名
        self.request.members[(CH_C, BOT_ID)] = "administrator"
        self.request.members[(CH_C, SUPER)] = "administrator"
        await self.tap("chm:set:add:a0")
        await self.send(text="@chc")
        await self.wizard_save()
        self.assertTrue(any("已添加频道：频道C" in t for t in self.texts()))
        self.assertEqual(self.store.managed_channel(CH_C)["status"], "active")


class GroupsAndBulkTest(Base):
    async def test_groups_crud_and_bulk_ops(self):
        await self.setup_channels()
        await self.tap("chm:menu")
        await self.tap("chm:groups")
        await self.tap(self.button("➕ 新建分组"))
        await self.send(text="主频道")
        await self.wizard_save()
        gid = int(self.store.chm_groups()[0]["id"])
        await self.tap(f"chm:gtog:{gid}:{CH_A}")
        await self.tap(f"chm:gtog:{gid}:{CH_B}")
        self.assertEqual(sorted(self.store.chm_group_channel_ids(gid)), sorted([CH_A, CH_B]))
        # 同一频道可在多个分组
        g2 = self.store.add_chm_group("备用", SUPER)
        self.store.toggle_chm_group_channel(g2, CH_A)
        self.assertIn(CH_A, self.store.chm_group_channel_ids(g2))
        # 重命名
        await self.tap(f"chm:set:grouprename:g{gid}")
        await self.send(text="核心频道")
        await self.wizard_save()
        self.assertEqual(self.store.chm_group(gid)["name"], "核心频道")
        # 批量改名称（模板 {原名}），B 缺权限
        self.request.no_rights.add(CH_B)
        self.request.calls.clear()
        await self.tap(f"chm:set:title:g{gid}")
        await self.send(text="{原名}｜官方")
        await self.wizard_save()
        titles = {int(p["chat_id"]): p["title"] for a, p in self.request.calls if a == "setChatTitle"}
        self.assertEqual(titles, {CH_A: "频道A｜官方", CH_B: "频道B｜官方"})
        report = self.report()
        self.assertIn("✅ 成功 1 个，❌ 失败 1 个", report)
        self.assertIn("❌ 频道B：机器人缺少相应管理权限", report)
        self.assertEqual(self.store.managed_channel(CH_A)["title"], "频道A｜官方")
        # 批量改简介（带 RetryAfter）
        self.request.no_rights.clear()
        self.request.retry_once.add(("setChatDescription", CH_A))
        await self.tap(f"chm:set:desc:g{gid}")
        await self.send(text="新简介")
        await self.wizard_save()
        descs = [int(p["chat_id"]) for a, p in self.request.calls if a == "setChatDescription"]
        self.assertEqual(descs.count(CH_A), 2)
        self.assertIn("✅ 成功 2 个，❌ 失败 0 个", self.report())
        self.assertTrue(any(c.args[0] >= 3 for c in self.sleep.call_args_list))
        # 批量改头像
        await self.tap(f"chm:set:photo:c{CH_A}")
        await self.send(photo=[{"file_id": "p1", "file_unique_id": "u1", "width": 10, "height": 10}])
        await self.wizard_save()
        self.assertEqual([int(p["chat_id"]) for a, p in self.request.calls if a == "setChatPhoto"], [CH_A])
        self.assertIn("✅ 成功 1 个", self.report())
        # 删除分组
        await self.tap(f"chm:gdel:{gid}")
        self.assertIsNone(self.store.chm_group(gid))

    async def test_copy_and_forward_fan_out(self):
        await self.setup_channels()
        gid = self.store.add_chm_group("全部", SUPER)
        for ch in (CH_A, CH_B):
            self.store.toggle_chm_group_channel(gid, ch)
        self.request.retry_once.add(("copyMessage", CH_B))
        await self.tap(f"chm:set:send:g{gid}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        markup = {"inline_keyboard": [[{"text": "官网", "url": "https://a.com", "style": "success",
                                        "icon_custom_emoji_id": "777"}]]}
        entities = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "5368324170671202286"},
                    {"type": "bold", "offset": 3, "length": 4}]
        self.request.calls.clear()
        await self.send(text="👍 广告内容😀", entities=entities, reply_markup=markup)
        await self.wizard_save()
        # 真实路径：频道里用 copyMessage 原样复制（会员表情只能这样保留），不重建文字
        copies = [p for a, p in self.request.calls if a == "copyMessage" and int(p["chat_id"]) < 0]
        self.assertEqual(sorted(int(p["chat_id"]) for p in copies), sorted([CH_A, CH_B, CH_B]))
        self.assertFalse([1 for a, p in self.request.calls if a == "sendMessage" and int(p["chat_id"]) < 0])
        self.assertTrue(all(int(p["from_chat_id"]) == SUPER for p in copies))
        kb = copies[-1]["reply_markup"]
        kb = json.loads(kb) if isinstance(kb, str) else kb
        self.assertEqual(kb["inline_keyboard"][0][0]["url"], "https://a.com")
        self.assertEqual(kb["inline_keyboard"][0][0].get("icon_custom_emoji_id"), "777")
        self.assertIn("✅ 成功 2 个，❌ 失败 0 个", self.report())
        # 频道帖不自动撤回
        self.assertFalse([k for k in self.app.bot._delete_by_message if k[0] < 0])
        # 无按钮：copy 不带 reply_markup（不触发重发，保留会员表情）
        self.request.calls.clear()
        await self.tap(f"chm:set:send:c{CH_A}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        await self.send(photo=[{"file_id": "p9", "file_unique_id": "u9", "width": 10, "height": 10}],
                        caption="⭐ 图说", caption_entities=[{"type": "custom_emoji", "offset": 0, "length": 1,
                                                             "custom_emoji_id": "42"}])
        await self.wizard_save()
        copy = next(p for a, p in self.request.calls if a == "copyMessage" and int(p["chat_id"]) == CH_A)
        self.assertNotIn("reply_markup", copy)
        self.assertNotIn("caption", copy)
        # copy 失败时退回重建：text + entities（含会员表情）原样
        self.request.copy_fail.add(CH_A)
        self.request.calls.clear()
        await self.tap(f"chm:set:send:c{CH_A}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        await self.send(text="👍 你好🔥", entities=entities[:1])
        await self.wizard_save()
        sent = next(p for a, p in self.request.calls if a == "sendMessage" and int(p["chat_id"]) == CH_A)
        self.assertEqual(sent["text"], "👍 你好🔥")
        ents = sent["entities"]
        ents = json.loads(ents) if isinstance(ents, str) else ents
        ents = [e if isinstance(e, dict) else e.to_dict() for e in ents]
        self.assertEqual(ents[0]["custom_emoji_id"], "5368324170671202286")
        self.assertNotIn("parse_mode", sent)
        self.assertFalse([k for k in self.app.bot._delete_by_message if k[0] < 0])
        self.request.copy_fail.clear()
        # 转发自频道的帖子：copy 频道原帖（原样保留一切）
        self.request.calls.clear()
        await self.tap(f"chm:set:send:c{CH_A}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        await self.send(text="频道帖", forward_origin={
            "type": "channel", "date": int(time.time()), "message_id": 55,
            "chat": {"id": -100999, "type": "channel", "title": "源"}})
        await self.wizard_save()
        copies = [p for a, p in self.request.calls if a == "copyMessage" and int(p["chat_id"]) == CH_A]
        self.assertEqual(len(copies), 1)
        self.assertEqual((int(copies[0]["from_chat_id"]), int(copies[0]["message_id"])), (-100999, 55))
        self.assertNotIn("reply_markup", copies[0])
        self.assertNotIn("parse_mode", copies[0])
        self.assertNotIn("caption", copies[0])
        self.assertIn("机器人需要是源频道管理员", self.report())
        # 源频道管理员 + 已设中转频道：仍优先 copy 源帖，不走中转
        self.request.members[(-100999, BOT_ID)] = "administrator"
        self.store.set_chm_staging_channel(SUPER, CH_B)
        self.request.calls.clear()
        await self.tap(f"chm:set:send:c{CH_A}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        await self.send(text="频道帖", forward_origin={
            "type": "channel", "date": int(time.time()), "message_id": 56,
            "chat": {"id": -100999, "type": "channel", "title": "源"}})
        await self.wizard_save()
        self.assertFalse([1 for a, p in self.request.calls if a == "forwardMessage"])
        copies = [p for a, p in self.request.calls if a == "copyMessage"]
        self.assertEqual([(int(p["chat_id"]), int(p["from_chat_id"]), int(p["message_id"])) for p in copies], [(CH_A, -100999, 56)])
        self.assertNotIn("源频道", self.report())
        # 转发（显示来源），B 被移除
        self.request.no_rights.add(CH_B)
        await self.tap(f"chm:set:send:g{gid}")
        await self.tap(self.button("转发"), message_id=self.panel_id())
        await self.send(photo=[{"file_id": "p1", "file_unique_id": "u1", "width": 10, "height": 10}], caption="图")
        await self.wizard_save()
        fwd = [int(p["chat_id"]) for a, p in self.request.calls if a == "forwardMessage"]
        self.assertEqual(sorted(fwd), sorted([CH_A, CH_B]))
        self.assertIn("✅ 成功 1 个，❌ 失败 1 个", self.report())


class SuperAdminParityTest(Base):
    def callbacks(self, markup):
        return {b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data}

    def ctx(self):
        from types import SimpleNamespace
        return SimpleNamespace(application=self.app, bot=self.app.bot, user_data={})

    async def test_super_admin_sees_all_developer_features_except_notes(self):
        ctx = self.ctx()
        dev_main = self.callbacks(handlers.main_keyboard_for(ctx, DEV))
        sup_main = self.callbacks(handlers.main_keyboard_for(ctx, SUPER))
        self.assertIn("admin:notes", dev_main)
        self.assertEqual(sup_main, dev_main - {"admin:notes"})
        self.assertIn("cat:clone", sup_main)
        self.assertIn("chm:menu", sup_main)
        # 🛡 管理员菜单
        menus = {}
        for uid in (DEV, SUPER):
            self.request.calls.clear()
            await self.tap("nav:admin", uid=uid)
            menus[uid] = {b["callback_data"] for b in self.last_keyboard() if b.get("callback_data")}
        self.assertIn("admin:notes", menus[DEV])
        self.assertEqual(menus[SUPER], menus[DEV] - {"admin:notes"})
        for data in ("admin:clones", "clonetree:0", "admin:usage:0", "admin:tronmonitors:0", "admin:admins"):
            self.assertIn(data, menus[SUPER])
        # 私人笔记仍仅开发者
        self.assertFalse(handlers.has_notes_access(ctx, SUPER))
        self.assertTrue(handlers.has_developer_access(ctx, SUPER))
        await self.tap("admin:notes", uid=SUPER)
        self.assertNotIn("🗒 私人笔记", "\n".join(self.texts()[-1:]))
        # 普通用户也能看到 📡 频道管理
        self.assertIn("chm:menu", self.callbacks(handlers.main_keyboard_for(ctx, OTHER)))

    async def test_child_bot_hides_developer_features(self):
        ctx = self.ctx()
        import dataclasses
        self.app.bot_data["config"] = dataclasses.replace(self.app.bot_data["config"], is_clone=True)
        sup_main = self.callbacks(handlers.main_keyboard_for(ctx, SUPER))
        self.assertNotIn("cat:clone", sup_main)
        self.assertNotIn("admin:notes", sup_main)
        self.assertIn("chm:menu", sup_main)


class OpenToAllTest(Base):
    async def test_ordinary_user_isolated_channels_and_groups(self):
        await self.setup_channels()
        self.request.members[(CH_B, OTHER)] = "administrator"
        await self.tap("chm:refresh", uid=OTHER)
        self.assertIn("你和机器人都是管理员的频道：1 个", self.texts()[-1])
        await self.tap("chm:list", uid=OTHER)
        labels = [b["text"] for b in self.last_keyboard()]
        self.assertIn("频道B", labels)
        self.assertNotIn("频道A", labels)
        # 分组按用户隔离
        g_super = self.store.add_chm_group("超管分组", SUPER)
        g_other = self.store.add_chm_group("我的分组", OTHER)
        await self.tap("chm:groups", uid=OTHER)
        labels = [b["text"] for b in self.last_keyboard()]
        self.assertTrue(any("我的分组" in t for t in labels))
        self.assertFalse(any("超管分组" in t for t in labels))
        await self.tap(f"chm:g:{g_super}", uid=OTHER)
        await self.tap(f"chm:gdel:{g_super}", uid=OTHER)
        self.assertIsNotNone(self.store.chm_group(g_super))
        # 不能操作自己不是管理员的频道 A
        await self.tap(f"chm:gtog:{g_other}:{CH_A}", uid=OTHER)
        self.assertEqual(self.store.chm_group_channel_ids(g_other), [])
        # 执行前重新校验：被撤销管理员后不能再改名
        self.request.members[(CH_B, OTHER)] = "left"
        self.request.calls.clear()
        await self.tap(f"chm:set:title:c{CH_B}", uid=OTHER)
        self.assertFalse([1 for a, _ in self.request.calls if a == "setChatTitle"])


class SuperAdminManageTest(Base):
    async def test_developer_adds_and_removes_super_admin(self):
        await self.tap("admin:admins", uid=DEV)
        data = self.button("➕ 添加超级管理员")
        await self.tap(data, uid=DEV)
        await self.send(uid=DEV, text="4242")
        await self.tap(self.button("确认保存"), uid=DEV, message_id=self.panel_id())
        self.assertTrue(self.store.is_super_admin(4242))
        # 转发消息添加
        await self.tap("admin:manage:addsuper", uid=DEV)
        await self.send(uid=DEV, text="hi", forward_origin={
            "type": "user", "date": int(time.time()),
            "sender_user": {"id": 5151, "is_bot": False, "first_name": "F"}})
        await self.tap(self.button("确认保存"), uid=DEV, message_id=self.panel_id())
        self.assertTrue(self.store.is_super_admin(5151))
        await self.tap("admin:manage:delsuper", uid=DEV)
        await self.send(uid=DEV, text="4242")
        await self.tap(self.button("确认保存"), uid=DEV, message_id=self.panel_id())
        self.assertFalse(self.store.is_super_admin(4242))
        # 超级管理员看不到也不能使用
        await self.tap("admin:admins", uid=SUPER)
        self.assertNotIn("admin:manage:addsuper", [b.get("callback_data") for b in self.last_keyboard()])
        await self.tap("admin:manage:addsuper", uid=SUPER)
        self.assertFalse(self.store.is_super_admin(4242))


class StagingRelayTest(Base):
    async def test_copy_via_staging_channel(self):
        await self.setup_channels()
        STG = CH_C
        self.request.members[(STG, BOT_ID)] = "administrator"
        self.request.members[(STG, SUPER)] = "creator"
        await self.process(self.bot_added(STG, "中转"))
        # 未设置时：直接复制并提示
        await self.tap(f"chm:set:send:c{CH_A}")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        await self.send(text="👍 hi")
        await self.wizard_save()
        self.assertIn("会员表情和按钮图标会丢失", self.report())
        # 菜单 → ⚙️ 设置中转频道 → 点选
        await self.tap("chm:menu")
        await self.tap(self.button("⚙️ 设置中转频道"))
        await self.tap(self.button("中转"))
        self.assertEqual(self.store.chm_staging_channel(SUPER), STG)
        self.assertEqual(self.store.chm_staging_channel(OTHER), 0)
        self.request.calls.clear()
        await self.tap(f"chm:set:send:a0")
        await self.tap(self.button("复制"), message_id=self.panel_id())
        entities = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "99"}]
        await self.send(text="👍 会员表情", entities=entities)
        with self.assertLogs(level="INFO") as logs:
            await self.wizard_save()
        joined = "\n".join(logs.output)
        self.assertIn("chm-send source:", joined)
        self.assertIn("chm-send call: forwardMessage", joined)
        self.assertIn("chm-send call: copyMessage", joined)
        fwd = [p for a, p in self.request.calls if a == "forwardMessage"]
        self.assertEqual(len(fwd), 1)
        self.assertEqual((int(fwd[0]["chat_id"]), int(fwd[0]["from_chat_id"])), (STG, SUPER))
        copies = [p for a, p in self.request.calls if a == "copyMessage"]
        self.assertEqual(sorted(int(p["chat_id"]) for p in copies), sorted([CH_A, CH_B, STG]))
        self.assertTrue(all(int(p["from_chat_id"]) == STG for p in copies))
        self.assertTrue(all("reply_markup" not in p for p in copies))
        self.assertFalse([1 for a, p in self.request.calls if a == "sendMessage" and int(p["chat_id"]) < 0])
        dels = [p for a, p in self.request.calls if a == "deleteMessage"]
        self.assertEqual([int(p["chat_id"]) for p in dels], [STG])
        self.assertNotIn("⚠️", self.report())
