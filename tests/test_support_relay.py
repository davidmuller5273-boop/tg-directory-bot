"""双向客服：管理员直接「回复」转发来的消息即可回复用户（文字/媒体），用户也可回复继续对话。"""

import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path

from telegram import Update

from tg_directory_bot import bot as handlers
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore

from tests.test_group_wizard_save import BOT_ID, FakeRequest

ADMIN = 900
USER = 4242


class RelayRequest(FakeRequest):
    def __init__(self):
        super().__init__()
        self.blocked = set()

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if api == "copyMessage":
            self.calls.append((api, params))
            if int(params["chat_id"]) in self.blocked:
                return 403, json.dumps({"ok": False, "error_code": 403,
                                        "description": "Forbidden: bot was blocked by the user"}).encode()
            return 200, json.dumps({"ok": True, "result": {"message_id": next(self.ids)}}).encode()
        if api == "sendMessage" and int(params["chat_id"]) in self.blocked:
            self.calls.append((api, params))
            return 403, json.dumps({"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was blocked by the user"}).encode()
        return await super().do_request(url, method, request_data, **kwargs)


class SupportRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "bot.sqlite3"
        await self.start_app()
        self.update_ids = itertools.count(1)
        self.message_ids = itertools.count(5000)

    async def start_app(self):
        config = Config(
            bot_token="1:test", admin_ids=set(), super_admin_ids={ADMIN},
            db_path=self.db, categories=("tools",), blocked_keywords=(), tron_block_scan=False,
        )
        self.app = handlers.build_application(config)
        self.request = RelayRequest()
        object.__setattr__(self.app.bot, "_request", (self.request, self.request))
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.store = self.app.bot_data["store"]

    async def send(self, who, reply_to=None, **content):
        mid = next(self.message_ids)
        msg = {
            "message_id": mid, "date": int(time.time()),
            "chat": {"id": who, "type": "private", "first_name": "U"},
            "from": {"id": who, "is_bot": False, "first_name": "U", "username": f"u{who}"},
        }
        msg.update(content)
        text = content.get("text", "")
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to:
            msg["reply_to_message"] = {
                "message_id": reply_to, "date": int(time.time()),
                "chat": {"id": who, "type": "private", "first_name": "U"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}, "text": "x",
            }
        await self.app.process_update(Update.de_json({"update_id": next(self.update_ids), "message": msg}, self.app.bot))
        return mid

    def calls(self, api, chat=None):
        return [p for a, p in self.request.calls
                if a == api and (chat is None or int(p["chat_id"]) == chat)]

    def reply_id(self, params):
        rp = params.get("reply_parameters")
        if rp is None:
            return None
        rp = json.loads(rp) if isinstance(rp, str) else rp
        return int(rp["message_id"]) if isinstance(rp, dict) else int(rp.message_id)

    def header_for_admin(self):
        heads = [p for p in self.calls("sendMessage", ADMIN) if "客服消息" in str(p.get("text")) or "用户回复" in str(p.get("text"))]
        self.assertTrue(heads)
        return heads[-1]

    async def user_contacts(self, text="你好，在吗"):
        await self.send(USER, text="/contact")
        mid = await self.send(USER, text=text)
        head = self.header_for_admin()
        self.assertIn(text, head["text"])
        self.assertIn("直接回复本消息", head["text"])
        # header id: find the sendMessage result id — map lookup by scanning store
        return mid

    def admin_header_id(self):
        rows = self.store.connect().execute(
            "SELECT message_id FROM support_relay_map WHERE chat_id=? AND kind='admin' ORDER BY rowid DESC", (ADMIN,)
        ).fetchall()
        self.assertTrue(rows)
        return int(rows[0][0])

    async def test_admin_text_reply_by_quoting(self):
        user_mid = await self.user_contacts()
        header = self.admin_header_id()
        relay = self.store.support_relay(ADMIN, header)
        self.assertEqual(int(relay["user_id"]), USER)
        await self.send(ADMIN, reply_to=header, text="收到，马上处理")
        copies = self.calls("copyMessage", USER)
        self.assertEqual(len(copies), 1)
        self.assertEqual(int(copies[0]["from_chat_id"]), ADMIN)
        self.assertEqual(self.reply_id(copies[0]), user_mid)
        self.assertTrue(any(p.get("text") == "✅ 已回复" for p in self.calls("sendMessage", ADMIN)))

    async def test_media_relay_both_ways(self):
        await self.send(USER, text="/contact")
        photo = [{"file_id": "p1", "file_unique_id": "u1", "width": 10, "height": 10}]
        await self.send(USER, photo=photo, caption="看图")
        to_admin = self.calls("copyMessage", ADMIN)
        self.assertEqual(len(to_admin), 1)
        self.assertIn("看图", self.header_for_admin()["text"])
        # admin replies to the copied photo itself with a sticker
        copy_rows = self.store.connect().execute(
            "SELECT message_id FROM support_relay_map WHERE chat_id=?", (ADMIN,)).fetchall()
        self.assertEqual(len(copy_rows), 2)  # header + copy
        sticker = {"file_id": "s1", "file_unique_id": "su", "width": 1, "height": 1,
                   "is_animated": False, "is_video": False, "type": "regular"}
        target = max(int(r[0]) for r in copy_rows)
        await self.send(ADMIN, reply_to=target, sticker=sticker)
        self.assertEqual(len(self.calls("copyMessage", USER)), 1)

    async def test_user_reply_routes_back_with_quote_and_persists(self):
        await self.user_contacts()
        header = self.admin_header_id()
        admin_mid = await self.send(ADMIN, reply_to=header, text="请提供订单号")
        delivered = self.store.connect().execute(
            "SELECT message_id FROM support_relay_map WHERE chat_id=? AND kind='user'", (USER,)).fetchone()
        self.assertIsNotNone(delivered)
        # restart: new store / app on the same DB still knows the mapping
        self.assertIsNotNone(DirectoryStore(self.db).support_relay(USER, int(delivered[0])))
        await self.app.shutdown()
        await self.start_app()
        self.update_ids = itertools.count(100)
        await self.send(USER, reply_to=int(delivered[0]), text="订单 123")
        head = self.header_for_admin()
        self.assertIn("订单 123", head["text"])
        self.assertEqual(self.reply_id(head), admin_mid)

    async def test_blocked_user_error(self):
        await self.user_contacts()
        header = self.admin_header_id()
        self.request.blocked.add(USER)
        await self.send(ADMIN, reply_to=header, text="hi")
        texts = [str(p.get("text")) for p in self.calls("sendMessage", ADMIN)]
        self.assertTrue(any("用户已屏蔽机器人" in t for t in texts))
        self.assertFalse(any(t == "✅ 已回复" for t in texts))

    async def test_reply_command_still_works_and_is_mapped(self):
        await self.user_contacts()
        await self.send(ADMIN, text=f"/reply {USER} 命令回复")
        self.assertTrue(any("命令回复" in str(p.get("text")) for p in self.calls("sendMessage", USER)))
        row = self.store.connect().execute(
            "SELECT 1 FROM support_relay_map WHERE chat_id=? AND kind='user'", (USER,)).fetchone()
        self.assertIsNotNone(row)

    async def test_unmapped_reply_is_not_relayed(self):
        await self.send(ADMIN, reply_to=123456, text="随便")
        self.assertEqual(self.calls("copyMessage"), [])


if __name__ == "__main__":
    unittest.main()


class SupportRelayExpiryTest(SupportRelayTest):
    """47 小时到期撤回（Telegram 仅允许删除 48 小时内消息）：持久队列（重启不丢），删除成功后清理映射。"""

    def shift(self, seconds):
        # 假时钟：把队列里的到期时间整体提前，相当于时间过去了 seconds 秒
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE scheduled_message_deletions SET delete_at=DATETIME(delete_at, ?)",
                (f"-{int(seconds)} seconds",),
            )

    async def run_deletions(self):
        from types import SimpleNamespace
        await handlers.process_scheduled_deletions(SimpleNamespace(application=self.app, bot=self.app.bot))
        return {(int(p["chat_id"]), int(p["message_id"])) for a, p in self.request.calls if a == "deleteMessage"}

    async def test_relay_messages_deleted_after_47_hours(self):
        self.assertEqual(self.app.bot_data["config"].support_relay_delete_seconds, 169200)
        user_mid = await self.user_contacts()
        header = self.admin_header_id()
        admin_mid = await self.send(ADMIN, reply_to=header, text="好的")
        delivered = int(self.store.connect().execute(
            "SELECT message_id FROM support_relay_map WHERE chat_id=? AND kind='user'", (USER,)).fetchone()[0])
        expected = {(ADMIN, header), (USER, user_mid), (USER, delivered), (ADMIN, admin_mid)}
        queued = {(int(r[0]), int(r[1])) for r in self.store.connect().execute(
            "SELECT chat_id, message_id FROM scheduled_message_deletions WHERE delete_at > DATETIME('now','+46 hours')")}
        self.assertTrue(expected <= queued)
        # restart: queue is in the DB
        await self.app.shutdown()
        await self.start_app()
        self.shift(46 * 3600)
        self.assertFalse(expected & await self.run_deletions())
        self.assertIsNotNone(self.store.support_relay(ADMIN, header))  # 46 小时仍可回复
        self.shift(3600 + 60)
        self.assertTrue(expected <= await self.run_deletions())
        self.assertIsNone(self.store.support_relay(ADMIN, header))
        self.assertIsNone(self.store.support_relay(USER, delivered))

    async def test_disabled_when_zero(self):
        self.store.support_relay_delete_seconds = 0
        await self.user_contacts()
        header = self.admin_header_id()
        row = self.store.connect().execute(
            "SELECT 1 FROM scheduled_message_deletions WHERE chat_id=? AND message_id=?", (ADMIN, header)).fetchone()
        self.assertIsNone(row)


for _name in [n for n in dir(SupportRelayTest) if n.startswith("test_")]:
    setattr(SupportRelayExpiryTest, _name, None)
