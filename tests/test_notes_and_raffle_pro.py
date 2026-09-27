from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType
from telegram.error import BadRequest

from tg_directory_bot.bot import (
    PENDING_NOTE_TIMEOUT_SECONDS,
    RAFFLE_RULES_HEADING,
    build_raffle_extras_from_pro,
    draw_raffle,
    next_postponed_ends_at,
    pin_raffle_message,
    point_redeem_reply,
    private_message,
    private_note_saved_text,
    private_notes_page_view,
    send_private_note_media,
    send_private_notes,
    raffle_pro_answers_from_row,
    raffle_text,
    spawn_daily_recur_raffles,
)
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.time_utils import BEIJING_TZ, utc_after_minutes_text


def _config(db_path: Path) -> Config:
    return Config(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        admin_ids=set(), super_admin_ids=set(), developer_ids={7},
        db_path=db_path, categories=("other",), blocked_keywords=(),
    )


def _message(message_id: int, text=None, caption=None, **extra):
    fields = dict(
        message_id=message_id, chat_id=7, text=text, caption=caption, forward_origin=None,
        animation=None, document=None, photo=None, video=None, audio=None,
        voice=None, sticker=None, video_note=None, media_group_id=None,
        reply_to_message=None,
    )
    fields.update(extra)
    msg = SimpleNamespace(**fields)
    msg.reply_text = AsyncMock(
        return_value=SimpleNamespace(chat_id=7, message_id=message_id + 1000)
    )
    return msg


class PrivateNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name) / "db.sqlite3")
        self.store = DirectoryStore(self.config.db_path)
        self.store.init()
        self.context = SimpleNamespace(
            user_data={}, args=[], bot=SimpleNamespace(),
            application=SimpleNamespace(
                bot_data={"store": self.store, "config": self.config}
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, message):
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
        )
        with patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)):
            asyncio.run(private_message(update, self.context))
        return message.reply_text

    def _last_reply(self, reply_mock) -> str:
        return str(reply_mock.await_args.args[0])

    # ---- storage ----
    def test_permanent_note_never_expires_and_is_not_trimmed(self):
        self.store.add_private_note("客户a", "永久", 7, permanent=True)
        for i in range(101):
            self.store.add_private_note("客户A", f"note {i}", 7)
        rows = self.store.private_notes("客户A", limit=99, offset=0)
        with self.store.connect() as conn:
            perm = conn.execute(
                "SELECT * FROM private_notes WHERE is_permanent=1"
            ).fetchall()
        self.assertEqual(len(perm), 1)
        self.assertEqual(perm[0]["expires_at"], DirectoryStore.PERMANENT_NOTE_EXPIRES_AT)
        self.assertEqual(self.store.count_private_notes("客户A"), 100)
        self.assertEqual(rows[0]["body"], "note 100")
        self.assertEqual(self.store.latest_private_note("客户A")["body"], "note 100")
        self.assertIsNone(self.store.latest_private_note("不存在"))

    def test_migration_adds_new_columns_on_existing_database(self):
        self.store.add_private_note("旧", "旧笔记", 7)
        rid = self.store.create_raffle(-1, 1, "奖品", 1, utc_after_minutes_text(60))
        with self.store.connect() as conn:
            conn.execute("ALTER TABLE private_notes DROP COLUMN is_permanent")
            conn.execute("ALTER TABLE raffles DROP COLUMN min_participants")
        store = DirectoryStore(self.config.db_path)
        store.init()
        store.init()  # idempotent
        with store.connect() as conn:
            note_cols = {r["name"] for r in conn.execute("PRAGMA table_info(private_notes)")}
            raffle_cols = {r["name"] for r in conn.execute("PRAGMA table_info(raffles)")}
        self.assertIn("is_permanent", note_cols)
        self.assertIn("min_participants", raffle_cols)
        self.assertEqual(int(store.get_raffle(rid)["min_participants"]), 0)
        self.assertEqual(store.latest_private_note("旧")["body"], "旧笔记")

    # ---- A1 confirmation text ----
    def test_saved_text_shows_previous_truncated_and_escaped(self):
        self.store.add_private_note("kw", "<b>" + "长" * 600, 7)
        previous = self.store.latest_private_note("kw")
        text = private_note_saved_text("kw", 2, previous)
        self.assertIn("上一条保存的信息：", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("<b>长", text)
        self.assertIn("…", text)
        self.assertIn("🕒 ", text)
        self.assertLess(len(text), 4096)
        self.assertIn("当前保留：2/99", text)

    def test_saved_text_media_previous_shows_attachment_and_caption(self):
        self.store.add_private_note(
            "kw", "图片说明", 7, file_id="F1", file_type="photo",
        )
        text = private_note_saved_text("kw", 2, self.store.latest_private_note("kw"))
        self.assertIn("图片说明", text)
        self.assertIn("📎 附件：图片", text)

    def test_saved_text_without_previous_unchanged(self):
        text = private_note_saved_text("kw", 1, None)
        self.assertEqual(text, "已保存私密笔记：kw\n当前保留：1/99\n保存时间：9个月")

    def test_save_with_1_shows_previous_note(self):
        reply = self._send(_message(1, text="1 客户 第一条"))
        self.assertNotIn("上一条保存的信息", self._last_reply(reply))
        reply = self._send(_message(2, text="1 客户 第二条"))
        body = self._last_reply(reply)
        self.assertIn("上一条保存的信息：", body)
        self.assertIn("第一条", body)
        self.assertEqual(reply.await_args.kwargs.get("parse_mode"), "HTML")

    # ---- A2 ----
    def test_2_keyword_content_saves_permanently(self):
        self.store.add_private_note("客户", "旧内容", 7)
        reply = self._send(_message(1, text="2 客户 新内容"))
        body = self._last_reply(reply)
        self.assertIn("已永久保存私密笔记：客户", body)
        self.assertIn("永久", body)
        self.assertIn("上一条保存的信息：", body)
        self.assertIn("旧内容", body)
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM private_notes WHERE body='新内容'"
            ).fetchone()
        self.assertEqual(int(row["is_permanent"]), 1)
        self.assertNotIn("pending_private_note", self.context.user_data)

    def test_2_keyword_pending_collects_and_saves(self):
        self.store.add_private_note("资料", "更早的", 7)
        reply = self._send(_message(1, text="2 资料"))
        self.assertIn("开始收集", self._last_reply(reply))
        self.assertIn("pending_private_note", self.context.user_data)
        # forwarded text from a user must be collected, not treated as user lookup
        forwarded = _message(
            2, text="转发的文字",
            forward_origin=SimpleNamespace(sender_user=SimpleNamespace(id=99)),
        )
        reply = self._send(forwarded)
        self.assertIn("已收集第 1 条", self._last_reply(reply))
        photo = _message(
            3, caption="图片说明",
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="PHOTO")],
        )
        reply = self._send(photo)
        self.assertIn("已收集第 2 条", self._last_reply(reply))
        self.assertEqual(self.store.count_private_notes("资料"), 1)
        reply = self._send(_message(4, text="保存", reply_to_message=photo))
        body = self._last_reply(reply)
        self.assertIn("已永久保存私密笔记：资料", body)
        self.assertIn("本次保存：2 条", body)
        self.assertIn("上一条保存的信息：", body)
        self.assertIn("更早的", body)
        self.assertNotIn("pending_private_note", self.context.user_data)
        rows = self.store.private_notes("资料", limit=10)
        self.assertEqual(len(rows), 3)
        media = [r for r in rows if r["file_id"]]
        self.assertEqual(media[0]["file_id"], "PHOTO")
        self.assertEqual(media[0]["file_type"], "photo")
        self.assertEqual(media[0]["body"], "图片说明")
        self.assertTrue(all(int(r["is_permanent"]) for r in rows if r["body"] != "更早的"))

    def test_pending_save_without_items_and_cancel(self):
        self._send(_message(1, text="2 空"))
        reply = self._send(_message(2, text="保存"))
        self.assertIn("还没有收集到任何消息", self._last_reply(reply))
        self.assertIn("pending_private_note", self.context.user_data)
        self._send(_message(3, text="一些内容"))
        reply = self._send(_message(4, text="取消"))
        self.assertIn("已取消保存", self._last_reply(reply))
        self.assertNotIn("pending_private_note", self.context.user_data)
        self.assertEqual(self.store.count_private_notes("空"), 0)

    def test_pending_times_out(self):
        self._send(_message(1, text="2 超时"))
        self.context.user_data["pending_private_note"]["touched"] = (
            time.time() - PENDING_NOTE_TIMEOUT_SECONDS - 5
        )
        with patch(
            "tg_directory_bot.bot.handle_plain_text", new_callable=AsyncMock
        ):
            reply = self._send(_message(2, text="保存"))
        first = str(reply.await_args_list[0].args[0])
        self.assertIn("已超时", first)
        self.assertNotIn("pending_private_note", self.context.user_data)
        self.assertEqual(self.store.count_private_notes("超时"), 0)

    def test_pending_mode_yields_to_other_menu_modes(self):
        self._send(_message(1, text="2 资料"))
        self.context.user_data["menu_mode"] = "renamehist_query"
        with patch(
            "tg_directory_bot.bot.process_menu_input",
            new=AsyncMock(return_value=True),
        ) as menu:
            self._send(_message(2, text="@someone"))
        menu.assert_awaited()
        self.assertEqual(self.context.user_data["pending_private_note"]["items"], [])

    def test_note_panel_mode_still_accepts_1_save(self):
        self.context.user_data["menu_mode"] = "private_note_query"
        reply = self._send(_message(1, text="1 面板 内容"))
        self.assertIn("已保存私密笔记：面板", self._last_reply(reply))
        self.assertEqual(self.store.count_private_notes("面板"), 1)


def _media_bot():
    bot = AsyncMock()
    counter = {"n": 5000}

    def make(*_args, **_kwargs):
        counter["n"] += 1
        return SimpleNamespace(chat_id=7, message_id=counter["n"])

    for name in (
        "send_message", "send_photo", "send_video", "send_document", "send_audio",
        "send_voice", "send_animation", "send_sticker", "send_video_note",
    ):
        setattr(bot, name, AsyncMock(side_effect=make))
    bot.send_media_group = AsyncMock(
        side_effect=lambda chat_id, media, **kw: tuple(make() for _ in media)
    )
    return bot


class PrivateNoteMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name) / "db.sqlite3")
        self.store = DirectoryStore(self.config.db_path)
        self.store.init()
        self.bot = _media_bot()
        self.context = SimpleNamespace(
            user_data={}, args=[], bot=self.bot,
            application=SimpleNamespace(
                bot_data={"store": self.store, "config": self.config}
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, body, file_id="", file_type="", file_name=""):
        self.store.add_private_note(
            "资料", body, 7, file_id=file_id, file_type=file_type, file_name=file_name,
        )
        with self.store.connect() as conn:  # distinct, ordered created_at
            conn.execute(
                "UPDATE private_notes SET created_at=DATETIME('now', ? || ' seconds') WHERE id=(SELECT MAX(id) FROM private_notes)",
                (str(len(self.store.private_notes("资料", limit=99))),),
            )

    def _query(self):
        message = _message(1, text="资料")
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
        )
        asyncio.run(send_private_notes(update, self.context, "资料"))
        return message

    def test_query_sends_original_media_after_list(self):
        self._add("合同", "DOC", "document", "合同.pdf")
        self._add("图1", "P1", "photo")
        self._add("图2", "P2", "photo")
        self._add("视频说明", "V1", "video")
        self._add("纯文字")
        message = self._query()
        page_text = str(message.reply_text.await_args.args[0])
        self.assertIn("纯文字", page_text)
        self.assertIn("📎 附件：视频（见下方）", page_text)
        self.assertIn("📎 附件：文件（合同.pdf）（见下方）", page_text)
        # video + 2 photos are consecutive -> one media group, in list order
        self.bot.send_media_group.assert_awaited_once()
        media = self.bot.send_media_group.await_args.args[1]
        self.assertEqual([m.media for m in media], ["V1", "P2", "P1"])
        self.assertTrue(media[0].caption.startswith("#2 · "))
        self.assertIn("视频说明", media[0].caption)
        # document sent as the original file
        self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.bot.send_document.await_args.args[1], "DOC")
        self.assertIn("合同", self.bot.send_document.await_args.kwargs["caption"])

    def test_single_video_and_invalid_file_fallback(self):
        self.bot.send_video = AsyncMock(side_effect=BadRequest("wrong file identifier"))
        self._add("坏视频", "BAD", "video")
        self._query()
        texts = [str(c.args[1]) for c in self.bot.send_message.await_args_list]
        self.assertTrue(any("视频发送失败" in t and "坏视频" in t for t in texts))

    def test_media_group_failure_falls_back_to_single_sends(self):
        self.bot.send_media_group = AsyncMock(side_effect=BadRequest("bad group"))
        self._add("a", "P1", "photo")
        self._add("b", "V1", "video")
        self._query()
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_video.assert_awaited_once()

    def test_long_caption_overflows_to_text_and_sticker_has_no_caption(self):
        long_body = "<长>" + "字" * 1500
        self._add(long_body, "D1", "document")
        rows = self.store.private_notes("资料", limit=1)
        asyncio.run(send_private_note_media(self.context, 7, [("#1 · t", rows[0])]))
        caption = self.bot.send_document.await_args.kwargs["caption"]
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("见下一条消息", caption)
        overflow = str(self.bot.send_message.await_args.args[1])
        self.assertIn("&lt;长&gt;", overflow)
        self.assertEqual(len(overflow), len("&lt;长&gt;") + 1500)
        self.store.add_private_note("贴", "贴纸说明", 7, file_id="S1", file_type="sticker", permanent=True)
        sticker_row = self.store.private_notes("贴", limit=1)[0]
        asyncio.run(send_private_note_media(self.context, 7, [("#1 · t", sticker_row)]))
        self.bot.send_sticker.assert_awaited_once_with(7, "S1")
        self.assertIn("贴纸说明", str(self.bot.send_message.await_args.args[1]))

    def test_page_view_media_items_for_second_page(self):
        for i in range(12):
            if i == 0:
                self._add("最早的视频", "V0", "video")
            else:
                self._add(f"文字{i}")
        view = private_notes_page_view(self.store, "资料", 1)
        text, markup, media_items, page, rows, total = view
        self.assertEqual(page, 1)
        self.assertEqual(total, 12)
        self.assertEqual(len(media_items), 1)
        self.assertTrue(media_items[0][0].startswith("#12 · "))
        self.assertIn("（见下方）", text)
        first = private_notes_page_view(self.store, "资料", 0)
        self.assertEqual(first[2], [])

    def test_save_confirmation_sends_previous_media(self):
        self.store.add_private_note("客户", "上次的视频", 7, file_id="VPREV", file_type="video")
        message = _message(1, text="2 客户 新文字")
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
        )
        with patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)):
            asyncio.run(private_message(update, self.context))
        confirm = str(message.reply_text.await_args.args[0])
        self.assertIn("上一条保存的信息：", confirm)
        self.assertIn("📎 附件：视频（见下方）", confirm)
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.await_args.args[1], "VPREV")
        self.assertIn("上一条保存的信息", self.bot.send_video.await_args.kwargs["caption"])
        self.assertIn("上次的视频", self.bot.send_video.await_args.kwargs["caption"])


class RafflePostponeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.tmp.name) / "db.sqlite3")
        self.store.init()
        self.bot = AsyncMock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(full_name="管理员", username="adm"),
                status="member",
            )
        )
        self.context = SimpleNamespace(
            bot=self.bot, user_data={},
            application=SimpleNamespace(bot_data={"store": self.store}),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _past(self, minutes=1) -> str:
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def test_extras_parse_min_participants(self):
        base = ["标题", "1. 规则一", "2099-09-15 21:00", "立即", "无", "点按钮", "1 | 奖品", "否"]
        _, _, _, extras = build_raffle_extras_from_pro(base)
        self.assertEqual(extras["min_participants"], 0)
        _, _, _, extras = build_raffle_extras_from_pro(base + ["20"])
        self.assertEqual(extras["min_participants"], 20)
        self.assertEqual(json.loads(extras["template_json"])["min_participants"], 20)
        with self.assertRaises(ValueError):
            build_raffle_extras_from_pro(base + ["abc"])

    def test_raffle_text_rules_heading_once_and_min_line(self):
        rules = ["1. 不许作弊", "2. 每人一次", "3. 最终解释权归群主"]
        rid = self.store.create_raffle(
            -1, 1, "奖品", 1, utc_after_minutes_text(60), title="福利",
            rules_json=json.dumps(rules, ensure_ascii=False), min_participants=5,
        )
        body = raffle_text(self.store.get_raffle(rid))
        self.assertEqual(body.count("规则"), 1)
        self.assertTrue(body.startswith(RAFFLE_RULES_HEADING + "\n1. 不许作弊\n2. 每人一次"))
        self.assertIn("├最少参与: 5 人", body)
        self.assertEqual(raffle_pro_answers_from_row(self.store.get_raffle(rid))[8], "5")
        # user text already starts with 规则 -> no extra heading
        rid2 = self.store.create_raffle(
            -1, 1, "奖品", 1, utc_after_minutes_text(60),
            rules_json=json.dumps(["规则：", "1. A"], ensure_ascii=False),
        )
        body2 = raffle_text(self.store.get_raffle(rid2))
        self.assertNotIn(RAFFLE_RULES_HEADING, body2)
        self.assertEqual(body2.count("规则"), 1)
        # no rules -> no heading, no min line
        rid3 = self.store.create_raffle(-1, 1, "奖品", 1, utc_after_minutes_text(60))
        body3 = raffle_text(self.store.get_raffle(rid3))
        self.assertNotIn("规则", body3)
        self.assertNotIn("最少参与", body3)

    def test_next_postponed_keeps_beijing_clock_and_catches_up(self):
        ends = datetime(2026, 9, 28, 21, 0, tzinfo=BEIJING_TZ).astimezone(timezone.utc)
        now = ends + timedelta(minutes=1)
        new = next_postponed_ends_at(ends.strftime("%Y-%m-%d %H:%M:%S"), now=now)
        new_bj = datetime.strptime(new, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).astimezone(BEIJING_TZ)
        self.assertEqual((new_bj.month, new_bj.day, new_bj.hour, new_bj.minute), (9, 29, 21, 0))
        late = next_postponed_ends_at(
            ends.strftime("%Y-%m-%d %H:%M:%S"), now=ends + timedelta(days=2, hours=1),
        )
        late_bj = datetime.strptime(late, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).astimezone(BEIJING_TZ)
        self.assertEqual((late_bj.day, late_bj.hour), (1, 21))

    def test_draw_postpones_when_not_enough_participants(self):
        ends_at = self._past()
        rid = self.store.create_raffle(
            -100, 1, "奖品", 1, ends_at, title="福利", min_participants=3,
        )
        self.store.set_raffle_message(rid, 42)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO raffle_entries (raffle_id, user_id, username, display_name) VALUES (?,?,?,?)",
                (rid, 11, "a", "A"),
            )
        asyncio.run(draw_raffle(self.context, rid))
        raffle = self.store.get_raffle(rid)
        self.assertEqual(raffle["status"], "active")
        self.assertEqual(int(raffle["entries"]), 1)
        old = datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S")
        new = datetime.strptime(raffle["ends_at"], "%Y-%m-%d %H:%M:%S")
        self.assertEqual(new - old, timedelta(days=1))
        notice = str(self.bot.send_message.await_args.args[1])
        self.assertIn("人数不足（当前 1/3）", notice)
        self.assertIn("顺延到", notice)
        self.bot.edit_message_text.assert_awaited()  # announcement refreshed
        self.bot.pin_chat_message.assert_awaited_with(-100, 555, disable_notification=True)
        self.assertEqual(self.store.due_raffles(), [])

    def test_draw_happens_when_enough_and_result_is_pinned(self):
        rid = self.store.create_raffle(
            -100, 1, "奖品", 1, self._past(), min_participants=2,
        )
        for uid in (11, 12):
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO raffle_entries (raffle_id, user_id, username, display_name) VALUES (?,?,?,?)",
                    (rid, uid, "u", "U"),
                )
        asyncio.run(draw_raffle(self.context, rid))
        self.assertEqual(self.store.get_raffle(rid)["status"], "drawn")
        self.assertIn("恭喜以下中奖用户", str(self.bot.send_message.await_args.args[1]))
        self.bot.pin_chat_message.assert_awaited_with(-100, 555, disable_notification=True)

    def test_zero_min_participants_draws_as_before(self):
        rid = self.store.create_raffle(-100, 1, "奖品", 1, self._past())
        asyncio.run(draw_raffle(self.context, rid))
        self.assertEqual(self.store.get_raffle(rid)["status"], "drawn")
        self.assertIn("没有用户报名", str(self.bot.send_message.await_args.args[1]))

    def test_pin_failure_is_silent(self):
        self.bot.pin_chat_message = AsyncMock(side_effect=BadRequest("not enough rights"))
        rid = self.store.create_raffle(-100, 1, "奖品", 1, self._past())
        asyncio.run(draw_raffle(self.context, rid))
        self.assertEqual(self.store.get_raffle(rid)["status"], "drawn")
        self.assertFalse(asyncio.run(pin_raffle_message(self.context, -100, 5, rid)))
        # private chats are never pinned
        self.bot.pin_chat_message.reset_mock()
        self.assertFalse(asyncio.run(pin_raffle_message(self.context, 7, 5, rid)))
        self.bot.pin_chat_message.assert_not_awaited()

    def test_recur_postponed_instance_is_not_spawned_until_drawn(self):
        template = {
            "title": "每日", "rules": ["1. 规则"], "conditions": {},
            "how_to_join": "", "prize": "奖品", "winner_count": 1,
            "recur_daily": 1, "stats_start_mode": "immediate",
            "draw_clock": "21:00", "min_participants": 2,
        }
        rid = self.store.create_raffle(
            -100, 1, "奖品", 1, self._past(), title="每日", recur_daily=1,
            rules_json=json.dumps(["1. 规则"], ensure_ascii=False),
            template_json=json.dumps(template, ensure_ascii=False),
            min_participants=2,
        )
        asyncio.run(draw_raffle(self.context, rid))
        asyncio.run(spawn_daily_recur_raffles(self.context))
        self.assertEqual(self.store.get_raffle(rid)["status"], "active")
        self.assertEqual(len(self.store.list_raffles(-100, limit=10)), 1)
        # later enough people join and it draws -> next day's instance spawned + announced
        with self.store.connect() as conn:
            conn.execute("UPDATE raffles SET ends_at=? WHERE id=?", (self._past(), rid))
            for uid in (11, 12):
                conn.execute(
                    "INSERT INTO raffle_entries (raffle_id, user_id, username, display_name) VALUES (?,?,?,?)",
                    (rid, uid, "u", "U"),
                )
        self.bot.send_message.reset_mock()
        self.bot.pin_chat_message.reset_mock()
        asyncio.run(draw_raffle(self.context, rid))
        asyncio.run(spawn_daily_recur_raffles(self.context))
        rows = self.store.list_raffles(-100, limit=10)
        self.assertEqual(len(rows), 2)
        new = [r for r in rows if int(r["id"]) != rid][0]
        new_row = self.store.get_raffle(int(new["id"]))
        self.assertEqual(int(new_row["min_participants"]), 2)
        self.assertEqual(int(new_row["message_id"]), 555)
        announce = str(self.bot.send_message.await_args_list[-1].args[1])
        self.assertIn(RAFFLE_RULES_HEADING, announce)
        self.assertIn("├最少参与: 2 人", announce)
        self.assertGreaterEqual(self.bot.pin_chat_message.await_count, 2)

    def test_redeem_message_is_pinned_in_group(self):
        chat_id = -100
        sent = SimpleNamespace(message_id=77)
        message = SimpleNamespace(reply_text=AsyncMock(return_value=sent))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
            effective_user=SimpleNamespace(id=5, username="u", full_name="U"),
            effective_message=message,
        )
        with patch.object(
            self.store, "redeem_point_gift", return_value=(9, "礼品", "10")
        ):
            asyncio.run(point_redeem_reply(update, self.context, 1))
        self.assertIn("兑换成功", str(message.reply_text.await_args.args[0]))
        self.bot.pin_chat_message.assert_awaited_with(chat_id, 77, disable_notification=True)


if __name__ == "__main__":
    unittest.main()
