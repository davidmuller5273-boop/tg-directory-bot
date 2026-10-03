from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal

from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.lottery import LotteryResult
from tg_directory_bot.validation import Submission


class StorageTest(unittest.TestCase):
    def test_channel_broadcast_groups_messages_targets_and_schedule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            group_id = store.add_channel_group("业务频道", 1)
            first = store.save_broadcast_channel(-10001, "一号频道", "first", 1)
            second = store.save_broadcast_channel(-10002, "二号频道", "second", 1)
            self.assertTrue(store.assign_broadcast_channel(first, group_id))
            self.assertEqual(len(store.channel_targets("all")), 2)
            self.assertEqual(
                [row["id"] for row in store.channel_targets("group", group_id)], [first]
            )
            message_id = store.save_channel_message(
                "活动", "🎉 原样内容", "", "", "", "[]", 1,
            )
            store.save_channel_message(
                "活动修改", "🎉 修改内容", "", "", "", "[]", 1, message_id,
            )
            self.assertEqual(store.channel_message(message_id)["text"], "🎉 修改内容")
            schedule_id = store.schedule_channel_message(
                message_id, "channel", second, "2000-01-01 00:00:00", 1,
            )
            self.assertEqual(store.due_channel_schedules()[0]["id"], schedule_id)
            store.finish_channel_schedule(schedule_id, 1, 0)
            self.assertEqual(store.channel_schedules(), [])

    def test_tron_monitor_limit_stats_and_id_first_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.touch_user(77, "owner77", "Owner", "")
            store.touch_user(88, "", "No Username", "")
            for index in range(5):
                store.upsert_tron_monitor(
                    77, f"Taddress{index}", "both", seen_tx_ids=[]
                )
            for index in range(2):
                store.upsert_tron_monitor(
                    88, f"Tsecond{index}", "usdt", seen_tx_ids=[]
                )
            # 监控地址数量不再限制
            self.assertTrue(store.can_add_tron_monitor(77, "Tsixth"))
            self.assertTrue(store.can_add_tron_monitor(77, "Taddress0"))
            self.assertEqual(
                store.tron_monitor_stats(), {"addresses": 7, "users": 2}
            )
            rows, total = store.tron_monitor_users()
            self.assertEqual(total, 2)
            self.assertEqual(
                [(row["owner_id"], row["address_count"]) for row in rows],
                [(77, 5), (88, 2)],
            )
            self.assertEqual(
                store.tron_monitor_owner_by_query("@owner77")["owner_id"], 77
            )
            self.assertEqual(
                store.tron_monitor_owner_by_query("88")["display_name"],
                "No Username",
            )

    def test_invite_owner_query_returns_and_aggregates_all_recent_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100900, 77
            first = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+first",
                username="owner77", display_name="Owner",
            )
            store.record_invite_join(chat_id, 101, owner_id, first)
            second = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+second",
                username="owner77", display_name="Owner",
            )
            store.record_invite_join(chat_id, 102, owner_id, second)
            store.record_invite_leave(chat_id, 101)
            links = store.invite_links_by_owner_query(chat_id, "@owner77")
            self.assertEqual([row["id"] for row in links], [second, first])
            link_ids = [int(row["id"]) for row in links]
            self.assertEqual(
                store.invite_links_stats(chat_id, link_ids),
                {"invites": 2, "exits": 1, "remaining": 1},
            )
            self.assertEqual(
                store.count_invite_links_members(chat_id, link_ids, "joined"), 2
            )

    def test_deleted_gift_and_raffle_reuse_smallest_available_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            first_button = store.add_custom_button("menu", "按钮一", 1)
            second_button = store.add_custom_button("menu", "按钮二", 1)
            self.assertEqual((first_button, second_button), (1, 2))
            self.assertTrue(store.delete_custom_button(first_button))
            self.assertEqual(store.add_custom_button("menu", "按钮三", 1), 1)

            first = store.add_point_gift(-100, "礼品一", 10, -1, 1)
            second = store.add_point_gift(-100, "礼品二", 20, -1, 1)
            self.assertEqual((first, second), (1, 2))
            self.assertTrue(store.disable_point_gift(-100, first))
            self.assertEqual(store.add_point_gift(-100, "礼品三", 30, -1, 1), 1)

            first_raffle = store.create_raffle(
                -100, 1, "奖品一", 1, "2099-01-01 00:00:00"
            )
            second_raffle = store.create_raffle(
                -100, 1, "奖品二", 1, "2099-01-01 00:00:00"
            )
            self.assertEqual((first_raffle, second_raffle), (1, 2))
            store.join_raffle(first_raffle, 8, "member", "成员")
            self.assertTrue(store.delete_raffle(-100, first_raffle))
            self.assertIsNone(store.get_raffle(first_raffle))
            self.assertEqual(store.raffle_entries(first_raffle), [])
            self.assertEqual(
                store.create_raffle(-100, 1, "奖品三", 1, "2099-01-01 00:00:00"),
                1,
            )
            self.assertEqual(len(store.active_raffles(-100)), 2)

    def test_bot_usage_keeps_one_row_counts_sorts_and_expires_after_a_year(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for _ in range(3):
                store.record_bot_usage(10, "alice", "Alice")
            store.record_bot_usage(20, "bob", "Bob")
            store.record_bot_usage(30, "carol", "Carol")
            with store.connect() as conn:
                conn.execute(
                    "UPDATE bot_usage_users SET last_used_at=DATETIME('now','-366 days') "
                    "WHERE user_id=20"
                )
                conn.execute(
                    "UPDATE bot_usage_users SET use_count=99, "
                    "last_used_at=DATETIME('now','-1 day') WHERE user_id=10"
                )
            rows, total = store.bot_usage_users()
            self.assertEqual(total, 2)
            self.assertEqual([row["user_id"] for row in rows], [30, 10])
            self.assertEqual(rows[1]["use_count"], 99)

    def test_group_admin_permissions_actor_and_seven_day_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, admin_id, actor_id = -188, 88, 99

            self.assertEqual(store.group_admin_permissions(chat_id, admin_id), set())
            store.set_group_admin_permissions(
                chat_id, admin_id, {"points", "raffles"}, actor_id
            )
            self.assertEqual(
                store.group_admin_permissions(chat_id, admin_id),
                {"points", "raffles"},
            )
            self.assertEqual(
                store.list_group_admin_permissions(chat_id)[0]["assigned_by"],
                actor_id,
            )
            self.assertTrue(store.reset_group_admin_permissions(chat_id, admin_id))
            self.assertEqual(store.group_admin_permissions(chat_id, admin_id), set())

            store.touch_user(actor_id, "operator", "操作人")
            store.adjust_points(
                chat_id, admin_id, 10, "人工加分", actor_id, "member", "成员"
            )
            ledger = store.point_ledger_rows(chat_id, admin_id)[0]
            self.assertEqual(ledger["actor_username"], "operator")
            self.assertEqual(ledger["actor_first_name"], "操作人")

            store.record_group_operation(
                chat_id, "setting", admin_id, "member", "成员", "修改设置",
                actor_id, "operator", "操作人",
            )
            with store.connect() as conn:
                conn.execute(
                    "UPDATE group_operations SET created_at=DATETIME('now','-8 days')"
                )
                conn.execute(
                    "UPDATE point_ledger SET created_at=DATETIME('now','-7 months')"
                )
            self.assertEqual(store.group_operations(chat_id), [])
            self.assertEqual(store.point_ledger_rows(chat_id, admin_id), [])

    def test_super_admin_only_lists_admins_they_added(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_bot_admin(1, "developer", 0)
            store.add_bot_admin(2, "super", 1)
            store.add_bot_admin(20, "admin", 2)
            store.add_bot_admin(30, "admin", 3)
            visible = store.list_bot_admins(viewer_id=2, include_all=False)
            self.assertEqual({row["user_id"] for row in visible}, {2, 20})
            self.assertEqual(
                {row["user_id"] for row in store.list_bot_admins()},
                {1, 2, 20, 30},
            )

    def test_role_buttons_operations_and_raffle_history(self):
        from tg_directory_bot.bot import (
            group_menu_keyboard, group_recent_operations_view, invite_menu_view,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_bot_admin(20, "admin", 10)
            store.ensure_config_admins({11}, {12}, {10})
            self.assertTrue(store.is_developer(10))
            self.assertTrue(store.is_super_admin(11))
            self.assertTrue(store.is_super_admin(12))
            self.assertEqual(store.bot_admin_role(20), "super")
            store.add_bot_admin(30, "admin", 11)
            self.assertTrue(store.set_bot_admin_permissions(30, {"support", "stats"}))
            self.assertEqual(store.bot_admin_permissions(30), {"support", "stats"})

            button_id = store.add_custom_button(
                "support", "双向联系霸哥", 11, "霸哥", "jiuye", 99
            )
            button = store.custom_button_by_label("双向联系霸哥")
            self.assertEqual(button["target_user_id"], 99)
            self.assertTrue(store.delete_custom_button(button_id))

            clone_id = store.save_bot_clone_request(88, 999, "clone_bot", "cipher")
            self.assertEqual(store.bot_clone(clone_id)["status"], "pending")
            self.assertFalse(store.enabled_bot_clones())
            self.assertTrue(store.review_bot_clone(clone_id, True, 10))
            self.assertEqual(store.bot_clone(clone_id)["reviewed_by"], 10)
            self.assertEqual(store.enabled_bot_clones()[0]["id"], clone_id)

            store.record_group_operation(
                -100, "ban", 88, "member", "成员", "手动封禁",
                11, "admin", "管理者",
            )
            operation = store.group_operations(-100)[0]
            self.assertEqual(operation["actor_user_id"], 11)
            self.assertEqual(operation["actor_name"], "管理者")
            store.record_group_operation(
                -100, "setting", 11, "admin", "管理者", "修改设置",
                11, "admin", "管理者",
            )
            store.record_group_operation(
                -100, "points", 11, "admin", "管理者", "查询积分",
                11, "admin", "管理者",
            )
            store.record_group_operation(
                -100, "raffle", 11, "admin", "管理者", "参加抽奖",
                11, "admin", "管理者",
            )
            menu = group_menu_keyboard(permissions={"recent"})
            recent_callbacks = {
                button.callback_data
                for row in menu.inline_keyboard for button in row
                if button.callback_data and button.callback_data.startswith("group:recent:")
            }
            self.assertEqual(
                recent_callbacks,
                {"group:recent:bot:0", "group:recent:group:0"},
            )
            group_text, _ = group_recent_operations_view(store, -100, 0, "group")
            bot_text, _ = group_recent_operations_view(store, -100, 0, "bot")
            self.assertIn("手动封禁", group_text)
            self.assertNotIn("修改设置", group_text)
            self.assertIn("修改设置", bot_text)
            self.assertNotIn("手动封禁", bot_text)
            self.assertNotIn("查询积分", bot_text)
            self.assertNotIn("参加抽奖", bot_text)
            for index in range(1, 12):
                store.save_invite_link(
                    -100, 1000 + index, f"https://t.me/+owner{index}",
                    username=f"owner{index}", display_name=f"Owner {index}",
                )
            invite_text, invite_keyboard = invite_menu_view(store, -100)
            invite_buttons = [
                button.text for row in invite_keyboard.inline_keyboard for button in row
            ]
            self.assertEqual(invite_text.count("• "), 10)
            self.assertTrue(any(
                button.callback_data == "invite:owners:1"
                for row in invite_keyboard.inline_keyboard for button in row
            ))
            self.assertNotIn("🧹 清空统计", invite_buttons)

            ends_at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            raffle_id = store.create_raffle(-100, 11, "奖品A", 1, ends_at)
            store.join_raffle(raffle_id, 88, "member", "成员")
            store.complete_raffle(raffle_id, [88])
            history = store.raffle_history(-100)
            self.assertEqual(store.raffle_history_count(-100), 1)
            self.assertEqual(history[0]["winner_names"], "成员")
            winner_records = store.all_raffle_winners(-100)
            self.assertEqual(store.count_all_raffle_winners(-100), 1)
            self.assertEqual(
                (winner_records[0]["record_type"], winner_records[0]["user_id"]),
                ("群抽奖", 88),
            )
            self.assertEqual(int(winner_records[0]["winner_position"]), 1)
            self.assertTrue(winner_records[0]["created_at"])

    def test_group_speaker_columns_migrate_existing_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "db.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """CREATE TABLE group_activity_users (
                       chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
                       messages INTEGER NOT NULL DEFAULT 0,
                       last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       PRIMARY KEY(chat_id, user_id, day))"""
                )
            store = DirectoryStore(db_path)
            store.init()
            with store.connect() as conn:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(group_activity_users)")
                }
            self.assertIn("username", columns)
            self.assertIn("display_name", columns)

    def test_group_speaker_windows_pagination_and_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -2002
            store.record_group_activity(
                chat_id, "Paging Group", "paging", "supergroup",
                1, "user1", "User 1", messages=1,
            )
            beijing_today = datetime.now(timezone(timedelta(hours=8))).date()
            with store.connect() as conn:
                for user_id in range(2, 13):
                    conn.execute(
                        """INSERT INTO group_activity_users
                           (chat_id, user_id, day, username, display_name, messages)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            chat_id, user_id, beijing_today.isoformat(),
                            f"user{user_id}", f"User {user_id}", 20 - user_id,
                        ),
                    )
                for days_ago, messages in ((6, 3), (30, 4), (31, 5)):
                    conn.execute(
                        """INSERT INTO group_activity_users
                           (chat_id, user_id, day, username, display_name, messages)
                           VALUES (?, 1, ?, 'user1', 'User 1', ?)""",
                        (chat_id, (beijing_today - timedelta(days=days_ago)).isoformat(), messages),
                    )
                conn.execute(
                    """INSERT INTO group_daily_stats (chat_id, day, messages)
                       VALUES (?, ?, 5)""",
                    (chat_id, (beijing_today - timedelta(days=31)).isoformat()),
                )

            store.record_group_activity(
                chat_id, "Paging Group", "paging", "supergroup",
                1, "user1", "User 1", messages=1,
            )

            self.assertEqual(store.count_group_speakers(chat_id, 1), 12)
            self.assertEqual(store.count_group_speakers(chat_id, 31), 12)
            self.assertEqual(len(store.group_speaker_stats(chat_id, limit=10)), 10)
            self.assertEqual(
                len(store.group_speaker_stats(chat_id, limit=10, offset=10)), 2
            )
            user_one = next(
                row for row in store.group_speaker_stats(chat_id) if row["user_id"] == 1
            )
            self.assertEqual(user_one["today_messages"], 2)
            self.assertEqual(user_one["week_messages"], 5)
            self.assertEqual(user_one["month_messages"], 9)
            with store.connect() as conn:
                old_speakers = conn.execute(
                    "SELECT COUNT(*) FROM group_activity_users WHERE day < ?",
                    ((beijing_today - timedelta(days=30)).isoformat(),),
                ).fetchone()[0]
                old_daily = conn.execute(
                    "SELECT COUNT(*) FROM group_daily_stats WHERE day < ?",
                    ((beijing_today - timedelta(days=30)).isoformat(),),
                ).fetchone()[0]
            self.assertEqual(old_speakers, 0)
            self.assertEqual(old_daily, 0)

    def test_group_stats_page_hides_history_from_regular_members(self):
        from tg_directory_bot.bot import group_stats_page

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for user_id in range(1, 12):
                store.record_group_activity(
                    -3003, "Permissions Group", "permissions", "supergroup",
                    user_id, f"user{user_id}", f"User {user_id}", messages=user_id,
                )

            regular_text, regular_keyboard = group_stats_page(store, -3003, 0, 1, False)
            super_text, super_keyboard = group_stats_page(store, -3003, 0, 7, True)
            self.assertIn("成员今日发言排行", regular_text)
            self.assertNotIn("近7天", regular_text)
            self.assertNotIn("累计消息", regular_text)
            self.assertIn("成员近7天发言排行", super_text)
            self.assertIn("累计消息", super_text)
            self.assertIsNotNone(regular_keyboard)
            self.assertIsNotNone(super_keyboard)
            self.assertEqual(
                regular_keyboard.inline_keyboard[0][-1].callback_data, "groupstats:1:1"
            )
            self.assertEqual(
                [button.callback_data for button in super_keyboard.inline_keyboard[0]],
                ["groupstats:1:0", "groupstats:7:0", "groupstats:31:0"],
            )
            week_ranking = store.group_speaker_ranking(-3003, 7, limit=10)
            self.assertEqual(week_ranking[0]["user_id"], 11)
            self.assertEqual(week_ranking[0]["period_messages"], 11)

    def test_group_violations_and_search_statistics(self):
        from tg_directory_bot.bot import search_stats_page

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            keyword_id = store.add_moderation_keyword("违规词", 7)
            self.assertEqual(store.moderation_keyword_values(), ("违规词",))
            with self.assertRaises(ValueError):
                store.add_moderation_keyword("违规词", 8)
            self.assertTrue(store.remove_moderation_keyword(keyword_id))
            self.assertEqual(store.moderation_keyword_values(), ())
            for count in range(1, 6):
                self.assertEqual(
                    store.add_group_violation(
                        -4004, 88, "alice", "Alice", "发送链接"
                    ),
                    count,
                )
            store.record_search("888", 88, "alice", "Alice", -4004, "supergroup", "group_keyword", 1)
            store.record_search("888", 99, "bob", "Bob", 99, "private", "search_command", 0)
            store.record_search("999", 88, "alice", "Alice", 88, "private", "search_command", 2)
            self.assertEqual(store.moderation_keyword_values(), ())
            rankings = store.search_keyword_rankings()
            self.assertEqual(rankings[0]["query"], "888")
            self.assertEqual(rankings[0]["searches"], 2)
            self.assertEqual(rankings[0]["unique_users"], 2)
            self.assertEqual(len(rankings[0]["searchers"]), 2)
            self.assertEqual(store.count_search_events(), 3)
            self.assertEqual(store.list_search_events()[0]["query"], "999")
            keyword_text, keyword_keyboard = search_stats_page(store, "keywords", 0)
            event_text, event_keyboard = search_stats_page(store, "events", 0)
            self.assertIn("搜索关键词排名", keyword_text)
            self.assertIn("888", keyword_text)
            self.assertIn("Alice", keyword_text)
            self.assertIn("搜索明细", event_text)
            self.assertIn("999", event_text)
            self.assertIn("私聊", event_text)
            self.assertIn("群聊", event_text)
            self.assertEqual(
                keyword_keyboard.inline_keyboard[0][1].callback_data,
                "searchstats:events:0",
            )
            self.assertEqual(
                event_keyboard.inline_keyboard[0][0].callback_data,
                "searchstats:keywords:0",
            )

    def test_store_add_search_and_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            entry_id = store.add_submission(
                Submission(
                    url="https://example.com/",
                    title="Example",
                    category="docs",
                    description="Reference",
                ),
                user_id=1,
                username="alice",
                status="approved",
            )

            results = store.search("Example")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].id, entry_id)

            self.assertTrue(store.add_report(entry_id, user_id=2, reason="broken"))
            self.assertFalse(store.add_report(entry_id, user_id=2, reason="duplicate"))
            self.assertEqual(store.get(entry_id).reports_count, 1)

            store.touch_user(99, "bob", "Bob")
            store.add_support_message(99, "incoming", "hello")
            store.queue_support_reply(99, "world", "admin")
            self.assertEqual(len(store.support_thread(99)), 2)
            self.assertEqual(len(store.pending_outbox()), 1)

            broadcast_id = store.create_broadcast("Notice", "message")
            self.assertGreater(broadcast_id, 0)
            self.assertEqual(store.list_broadcasts()[0]["total"], 1)

            store.record_group_activity(
                -1001, "Test Group", "testgroup", "supergroup", 99, "bob", "Bob", messages=3
            )
            store.record_group_activity(
                -1001, "Test Group", "testgroup", "supergroup", 100, "alice", "Alice",
                messages=2, joins=1,
            )
            group = store.group_stats(-1001)
            self.assertEqual(group["today_messages"], 5)
            self.assertEqual(group["today_active"], 2)
            speakers = store.group_top_speakers(-1001)
            self.assertEqual(speakers[0]["display_name"], "Bob")
            self.assertEqual(speakers[0]["today_messages"], 3)
            self.assertEqual(speakers[0]["week_messages"], 3)
            self.assertEqual(speakers[0]["month_messages"], 3)
            self.assertEqual(speakers[1]["username"], "alice")

            ends_at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            raffle_id = store.create_raffle(-1001, 99, "Prize", 1, ends_at)
            self.assertEqual(store.join_raffle(raffle_id, 99, "bob", "Bob"), (True, 1))
            self.assertEqual(store.join_raffle(raffle_id, 99, "bob", "Bob"), (False, 1))
            self.assertTrue(store.complete_raffle(raffle_id, [99]))
            self.assertEqual(store.get_raffle(raffle_id)["status"], "drawn")
            self.assertEqual(store.raffle_winners(raffle_id)[0]["user_id"], 99)

            store.heartbeat("bot", "ok")
            self.assertEqual(store.runtime_status()[0]["service"], "bot")

            store.ensure_config_admins({101, 102}, {101})
            self.assertTrue(store.is_bot_admin(101))
            self.assertTrue(store.is_super_admin(101))
            self.assertTrue(store.is_bot_admin(102))
            self.assertFalse(store.is_super_admin(102))
            store.add_bot_admin(103, "admin", 101)
            self.assertIn(103, store.all_admin_ids())
            self.assertTrue(store.remove_bot_admin(103))

            for i in range(105):
                store.add_private_note("客户A", f"note {i}", 101)
            self.assertEqual(store.count_private_notes("客户A"), 99)
            notes = store.private_notes("客户A")
            self.assertEqual(len(notes), 10)  # default page size is 10
            self.assertEqual(notes[0]["body"], "note 104")

            contains_id = store.create_auto_reply("余额", "使用 /balance 查询", "contains")
            exact_id = store.create_auto_reply("人工客服", "发送 /contact 联系管理员", "exact")
            group_id = store.create_auto_reply("群帮助", "这是群自动回复", "exact", "group")
            matched = store.match_auto_reply("请问余额怎么查")
            self.assertEqual(matched["id"], contains_id)
            self.assertEqual(store.match_auto_reply("人工客服")["id"], exact_id)
            self.assertIsNone(store.match_auto_reply("我想找人工客服人员"))
            self.assertIsNone(store.match_auto_reply("群帮助"))
            self.assertEqual(store.match_auto_reply("群帮助", "group")["id"], group_id)
            self.assertEqual(store.list_auto_replies()[0]["hits"], 1)
            self.assertTrue(store.set_auto_reply_enabled(contains_id, False))
            self.assertIsNone(store.match_auto_reply("余额"))
            self.assertTrue(store.delete_auto_reply(contains_id))

            first_result = LotteryResult(
                "cwl", "ssq", "双色球", "2026001", "2026-01-01",
                ("01", "02", "03", "04", "05", "06"), ("16",),
            )
            next_result = LotteryResult(
                "cwl", "ssq", "双色球", "2026002", "2026-01-03",
                ("07", "08", "09", "10", "11", "12"), ("15",),
            )
            self.assertEqual(store.save_lottery_result(first_result), (True, False))
            self.assertEqual(store.save_lottery_result(first_result), (False, True))
            self.assertEqual(store.save_lottery_result(next_result), (True, True))
            store.add_lottery_subscription(-1001, "all", 99)
            store.add_lottery_subscription(-1001, "ssq", 99)
            self.assertEqual(store.lottery_subscriber_chat_ids("ssq", "cwl"), [-1001])
            self.assertEqual(len(store.lottery_subscriptions(-1001)), 2)
            self.assertEqual(store.remove_lottery_subscription(-1001, "ssq"), 1)

    def test_rich_entries_and_group_ads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            entry_id = store.add_rich_submission(
                "v8", "tgcontent://-100/20", "文字说明", 88, "alice",
                file_id="photo-file-id", file_type="photo",
            )
            entry = store.get(entry_id)
            self.assertEqual(entry.content_text, "文字说明")
            self.assertEqual(entry.media_type, "photo")
            self.assertEqual(
                store.list_entries(status="pending", query="文字说明")[0].id,
                entry_id,
            )
            store.update_status(entry_id, "approved")
            self.assertEqual(store.search_exact_title("V8")[0].id, entry_id)

            ad_id = store.set_group_ad(
                -100, "interval", "定时广告", 88, interval_seconds=60,
                file_id="photo-file-id", file_type="photo",
            )
            self.assertGreater(ad_id, 0)
            store.set_group_ad(-100, "prefix", "消息前广告", 88)
            self.assertEqual(store.group_ad(-100, "prefix")["text"], "消息前广告")
            with store.connect() as conn:
                conn.execute(
                    "UPDATE group_ads SET next_run_at='2000-01-01 00:00:00' WHERE id=?",
                    (ad_id,),
                )
            self.assertEqual(len(store.due_group_ads()), 1)
            self.assertEqual(len(store.due_group_ads()), 0)
            self.assertEqual(store.disable_group_ad(-100), 2)

    def test_group_points_checkin_activity_gifts_and_clear(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -7007, 77
            store.set_points_enabled(chat_id, True, 1)
            store.set_checkin_points(chat_id, 5, 5, 2, 1)

            self.assertEqual(
                store.checkin_points(chat_id, user_id, "alice", "Alice"),
                (5, 5, 1, 1),
            )
            with self.assertRaisesRegex(ValueError, "今天已经签到"):
                store.checkin_points(chat_id, user_id, "alice", "Alice")
            with store.connect() as conn:
                conn.execute(
                    "UPDATE point_checkins SET day=DATE(day,'-1 day') WHERE chat_id=?",
                    (chat_id,),
                )
            self.assertEqual(
                store.checkin_points(chat_id, user_id, "alice", "Alice"),
                (5, 10, 2, 1),
            )
            with store.connect() as conn:
                conn.execute(
                    "UPDATE point_checkins SET day=DATE(day,'-1 day') WHERE chat_id=?",
                    (chat_id,),
                )
            self.assertEqual(
                store.checkin_points(chat_id, user_id, "alice", "Alice"),
                (7, 17, 3, 1),
            )

            store.set_activity_points(chat_id, 2, 2, 3, 3, 1)
            # 活跃奖励按有效发言计（1 分钟内多条只算 1 条）：每条相隔 61 秒
            ticks = iter(range(1_700_000_000, 1_700_100_000, 61))
            clock = patch("tg_directory_bot.storage.activity_now", side_effect=lambda: next(ticks))
            clock.start()
            self.addCleanup(clock.stop)
            store.record_group_activity(
                chat_id, "Points", "points", "supergroup",
                user_id, "alice", "Alice", messages=1,
            )
            self.assertIsNone(
                store.award_activity_points(chat_id, user_id, "alice", "Alice")
            )
            store.record_group_activity(
                chat_id, "Points", "points", "supergroup",
                user_id, "alice", "Alice", messages=1,
            )
            self.assertEqual(
                store.award_activity_points(chat_id, user_id, "alice", "Alice"),
                (3, 20, 2, 2),
            )
            self.assertIsNone(
                store.award_activity_points(chat_id, user_id, "alice", "Alice")
            )
            for _ in range(2):
                store.record_group_activity(
                    chat_id, "Points", "points", "supergroup",
                    user_id, "alice", "Alice", messages=1,
                )
            self.assertEqual(
                store.award_activity_points(chat_id, user_id, "alice", "Alice"),
                (3, 23, 2, 4),
            )

            gift_id = store.add_point_gift(chat_id, "会员礼品", 10, 1, 1)
            redemption_id, gift_name, balance = store.redeem_point_gift(
                chat_id, user_id, gift_id, "alice", "Alice"
            )
            self.assertGreater(redemption_id, 0)
            self.assertEqual((gift_name, balance), ("会员礼品", 13))
            with self.assertRaisesRegex(ValueError, "已经兑完"):
                store.redeem_point_gift(chat_id, user_id, gift_id, "alice", "Alice")
            self.assertEqual(store.point_rankings(chat_id)[0]["display_name"], "Alice")
            self.assertEqual(store.adjust_points(chat_id, user_id, -4, "扣分", 1), 9)
            self.assertEqual(store.clear_points(chat_id, 1, user_id), 1)
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 0)

    def test_point_draw_is_atomic_and_uses_gift_cost_probability(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -8080, 88
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(chat_id, user_id, 100, "测试", 1, "alice", "Alice")
            gift_id = store.add_point_gift(chat_id, "积分礼品", 10, 1, 1)
            store.set_point_draw_config(chat_id, True, 10, 1.0, 1)
            result = store.draw_point_gift(chat_id, user_id, gift_id, "alice", "Alice")
            self.assertTrue(result["is_winner"])
            self.assertEqual(result["probability"], 1.0)
            self.assertEqual(result["balance"], 90)
            self.assertIsNotNone(result["redemption_id"])
            with self.assertRaisesRegex(ValueError, "已经抽完"):
                store.draw_point_gift(chat_id, user_id, gift_id, "alice", "Alice")

    def test_point_rankings_active_raffle_and_join_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -9090
            for user_id in range(1, 106):
                store.adjust_points(
                    chat_id, user_id, user_id, "排行", 1,
                    f"user{user_id}", f"User {user_id}",
                )
            self.assertEqual(len(store.point_rankings(chat_id, 100)), 100)
            store.record_group_activity(
                chat_id, "Active", "", "supergroup", 1, "one", "One", messages=2
            )
            raffle_id = store.create_raffle(
                chat_id, 99, "礼品", 1, "2099-01-01 00:00:00",
                "activity_random", "2000-01-01 00:00:00", 2,
            )
            candidates = store.active_raffle_candidates(raffle_id)
            self.assertEqual([(row["user_id"], row["messages"]) for row in candidates], [(1, 2)])
            qualified = store.qualify_activity_raffles(
                chat_id, 1, "one", "One"
            )
            self.assertEqual([(row["id"], row["entries"]) for row in qualified], [(raffle_id, 1)])
            self.assertEqual(
                store.qualify_activity_raffles(chat_id, 1, "one", "One"), []
            )
            store.set_group_join_config(
                chat_id, 99, welcome_text="欢迎 {name}", verification_enabled=True
            )
            config = store.group_join_config(chat_id)
            self.assertEqual(config["welcome_text"], "欢迎 {name}")
            self.assertTrue(config["verification_enabled"])



    def test_private_notes_page_of_ten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for i in range(12):
                store.add_private_note("demo", f"note-{i}", 1)
            page0 = store.private_notes("demo", limit=10, offset=0)
            page1 = store.private_notes("demo", limit=10, offset=10)
            self.assertEqual(len(page0), 10)
            self.assertEqual(len(page1), 2)
            self.assertEqual(store.count_private_notes("demo"), 12)
            # newest first
            self.assertEqual(page0[0]["body"], "note-11")

    def test_suffix_entry_titles_appends_address_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            a = store.add_rich_submission(
                "v8", "rich://a", "addr-a", 1, "alice", status="approved",
            )
            b = store.add_rich_submission(
                "ok地址", "rich://b", "addr-b", 1, "alice", status="approved",
            )
            c = store.add_rich_submission(
                "XYZ", "rich://c", "addr-c", 1, "alice", status="pending",
            )
            first = store.suffix_entry_titles("地址")
            self.assertEqual(first["changed"], 2)
            self.assertEqual(first["skipped"], 1)
            self.assertEqual(store.get(a).title, "v8地址")
            self.assertEqual(store.get(b).title, "ok地址")
            self.assertEqual(store.get(c).title, "XYZ地址")
            second = store.suffix_entry_titles("地址")
            self.assertEqual(second["changed"], 0)
            self.assertEqual(second["skipped"], 3)

    def test_entry_title_address_suffix_runs_once_on_init(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "db.sqlite3"
            store = DirectoryStore(db)
            store.init()
            # Simulate pre-migration DB: clear the one-shot flag and seed old titles.
            with store.connect() as conn:
                conn.execute(
                    "DELETE FROM settings WHERE key='entry_titles_address_suffix_v1'"
                )
                conn.execute("DELETE FROM entries")
                conn.execute(
                    """INSERT INTO entries
                       (url, title, category, description, status, user_id, username)
                       VALUES ('u1','v8','other','','approved',1,'a'),
                              ('u2','已有地址','other','','approved',1,'a')"""
                )
            store.init()
            self.assertEqual(store.get_settings().get("entry_titles_address_suffix_v1"), "1")
            titles = {row.title for row in store.list_entries(status=None, limit=10)}
            self.assertEqual(titles, {"v8地址", "已有地址"})
            # Second init must not double-append.
            store.init()
            titles2 = {row.title for row in store.list_entries(status=None, limit=10)}
            self.assertEqual(titles2, {"v8地址", "已有地址"})

    def test_submission_keeps_source_message_until_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            entry_id = store.add_submission(
                Submission("https://example.com", "demo", "other", ""),
                7, "alice", "pending", -100, 321,
            )
            entry = store.get(entry_id)
            self.assertEqual((entry.source_chat_id, entry.source_message_id), (-100, 321))

    def test_records_monitors_quick_posts_and_invite_anti_cheat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -777, 77
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(chat_id, user_id, 100, "初始", 1, "alice", "Alice")
            gift_id = store.add_point_gift(chat_id, "礼品", 100, -1, 1)
            store.set_point_draw_config(chat_id, True, 10, 1.0, 1)
            draw = store.draw_point_gift(
                chat_id, user_id, gift_id, "alice", "Alice", 100
            )
            self.assertEqual(draw["points_spent"], 100)
            self.assertEqual(draw["probability"], 1.0)
            self.assertEqual(store.count_point_ledger(chat_id, user_id), 2)
            self.assertEqual(store.count_point_draw_winners(chat_id), 1)
            self.assertEqual(store.count_point_redemptions(chat_id), 1)

            monitor_id = store.upsert_tron_monitor(
                user_id, "T" + "a" * 33, "usdt", "10", "100", "50", ["tx1"]
            )
            self.assertEqual(store.list_tron_monitors(user_id)[0]["id"], monitor_id)
            store.disable_tron_monitor(user_id, monitor_id)
            self.assertFalse(store.list_tron_monitors(user_id)[0]["is_enabled"])
            store.schedule_message_deletion(user_id, 123, 1)
            with store.connect() as conn:
                conn.execute(
                    "UPDATE scheduled_message_deletions SET delete_at=CURRENT_TIMESTAMP"
                )
            due = store.due_message_deletions()
            self.assertEqual(due[0]["message_id"], 123)
            store.finish_message_deletion(int(due[0]["id"]))
            self.assertEqual(store.due_message_deletions(), [])

            post = store.quick_post(chat_id)
            store.update_quick_post(
                chat_id, user_id, text="内容", button_text="打开",
                button_url="https://example.com",
            )
            self.assertEqual(store.quick_post_by_code(post["share_code"])["text"], "内容")

            store.update_invite_config(
                chat_id, user_id, enabled=True, points_per_invite=25
            )
            self.assertEqual(store.invite_config(chat_id)["points_per_invite"], 25)
            link_id = store.save_invite_link(
                chat_id, user_id, "https://t.me/+test", username="owner77",
                display_name="Owner 77",
            )
            self.assertEqual(
                store.invite_link_by_owner_query(chat_id, "@owner77")["id"], link_id
            )
            self.assertTrue(store.record_invite_join(
                chat_id, 88, user_id, link_id, points_awarded=25,
                username="member88", display_name="Member 88",
            ))
            self.assertFalse(store.record_invite_join(chat_id, 88, 99, link_id))
            self.assertEqual(store.invite_stats(chat_id)["invites"], 1)
            store.record_group_activity(
                chat_id, "Test", "test", "supergroup",
                88, "member88", "Member 88", messages=1,
            )
            owner = store.invite_owner_stats(chat_id)[0]
            self.assertEqual(
                (owner["user_id"], owner["links"], owner["invites"], owner["exits"]),
                (user_id, 1, 1, 0),
            )
            departed = store.record_invite_leave(chat_id, 88)
            self.assertEqual(
                (departed["inviter_id"], departed["points_awarded"]),
                (user_id, 25),
            )
            owner = store.invite_owner_stats(chat_id)[0]
            self.assertEqual((owner["invites"], owner["exits"]), (1, 1))
            members = store.invite_link_members(chat_id, link_id)
            self.assertEqual(len(members), 1)
            self.assertTrue(members[0]["spoken"])
            self.assertTrue(members[0]["shown_last_spoken_at"])
            first_operation = store.record_group_operation(
                chat_id, "join", 88, "member", "Member"
            )
            duplicate_operation = store.record_group_operation(
                chat_id, "join", 88, "member", "Member"
            )
            store.record_group_operation(
                chat_id, "intercept", 88, "member", "Member", "链接"
            )
            self.assertEqual(first_operation, duplicate_operation)
            self.assertEqual(
                store.group_operation_totals(chat_id, 0),
                {"join": 1, "leave": 0, "intercept": 1, "ban": 0},
            )

    def test_admin_deduction_can_go_negative_but_point_draw_cannot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -800, 55
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(
                chat_id, user_id, -20, "管理员扣分", 1,
                allow_negative=True,
            )
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], -20)
            gift_id = store.add_point_gift(chat_id, "测试礼品", 100, 1, 1)
            store.set_point_draw_config(chat_id, True, 10, 5.0, 1)
            with self.assertRaisesRegex(ValueError, "积分余额不足"):
                store.draw_point_gift(chat_id, user_id, gift_id, "", "测试")
            self.assertEqual(
                store.point_draw_probability(10, 100, 5.0), 0.5
            )

    def test_group_polls_create_list_delete_and_menu_button(self):
        from tg_directory_bot.bot import (
            GROUP_PERMISSIONS, group_menu_keyboard, group_poll_delete_view,
            parse_group_poll_options,
        )
        self.assertIn("polls", GROUP_PERMISSIONS)
        self.assertIn("diceodds", GROUP_PERMISSIONS)
        self.assertIn("renamehist", GROUP_PERMISSIONS)
        self.assertEqual(
            parse_group_poll_options("同意\n反对\n同意\n弃权"),
            ["同意", "反对", "弃权"],
        )
        with self.assertRaises(ValueError):
            parse_group_poll_options("只有一个")
        menu = group_menu_keyboard(is_super=True)
        callbacks = {
            button.callback_data
            for row in menu.inline_keyboard for button in row
            if button.callback_data
        }
        self.assertIn("group:polls", callbacks)
        hidden = group_menu_keyboard(permissions={"recent"})
        hidden_callbacks = {
            button.callback_data
            for row in hidden.inline_keyboard for button in row
            if button.callback_data
        }
        self.assertNotIn("group:polls", hidden_callbacks)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            poll_id = store.create_group_poll(
                -100, 88, 7, "周末聚餐？", ["去", "不去"],
            )
            self.assertEqual(store.group_poll_count(-100), 1)
            rows = store.list_group_polls(-100)
            self.assertEqual(rows[0]["id"], poll_id)
            self.assertEqual(rows[0]["question"], "周末聚餐？")
            text, keyboard = group_poll_delete_view(store, -100, 0)
            self.assertIn("周末聚餐？", text)
            self.assertTrue(any(
                "grouppoll:delete:item:" in (button.callback_data or "")
                for row in keyboard.inline_keyboard for button in row
            ))
            deleted = store.delete_group_poll(-100, poll_id)
            self.assertIsNotNone(deleted)
            self.assertEqual(store.group_poll_count(-100), 0)

    def test_dice_enabled_default_and_game_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -42420, 4242
            config = store.points_config(chat_id)
            self.assertEqual(int(config["dice_enabled"]), 1)
            store.set_dice_enabled(chat_id, False, 7)
            self.assertEqual(int(store.points_config(chat_id)["dice_enabled"]), 0)
            store.set_dice_enabled(chat_id, True, 7)
            self.assertEqual(int(store.points_config(chat_id)["dice_enabled"]), 1)

            rid = store.add_point_game_record(
                chat_id, user_id, "dice",
                side="小", dice_value=2, stake=4, delta=-4,
                balance_after=6, is_win=False, detail="押小4 · 点数2(小双)",
            )
            self.assertGreater(rid, 0)
            self.assertEqual(store.count_point_game_records(chat_id, user_id), 1)
            rows = store.point_game_records(chat_id, user_id, 10, 0)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["game_type"], "dice")
            self.assertEqual(rows[0]["side"], "小")
            self.assertEqual(int(rows[0]["stake"]), 4)

            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(chat_id, user_id, 100, "seed", 1, "u", "U")
            gift_id = store.add_point_gift(chat_id, "礼品A", 10, 1, 1)
            store.set_point_draw_config(chat_id, True, 10, 1.0, 1)
            result = store.draw_point_gift(chat_id, user_id, gift_id, "u", "U")
            self.assertTrue(result["is_winner"])
            self.assertEqual(store.count_point_game_records(chat_id, user_id), 2)
            latest = store.point_game_records(chat_id, user_id, 1, 0)[0]
            self.assertEqual(latest["game_type"], "draw")
            self.assertEqual(int(latest["is_win"]), 1)
            self.assertEqual(int(latest["delta"]), -10)
            self.assertEqual(latest["detail"], "礼品A")

            gift_id2 = store.add_point_gift(chat_id, "礼品B", 1000, -1, 1)
            store.set_point_draw_config(chat_id, True, 10, 0.0, 1)
            lose = store.draw_point_gift(chat_id, user_id, gift_id2, "u", "U")
            self.assertFalse(lose["is_winner"])
            lose_row = store.point_game_records(chat_id, user_id, 1, 0)[0]
            self.assertEqual(lose_row["detail"], "未中奖")
            self.assertEqual(int(lose_row["is_win"]), 0)

    def test_set_dice_odds_bounds_and_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -88001
            config = store.points_config(chat_id)
            self.assertEqual(int(config["dice_odds"]), 2000)
            store.set_dice_odds(chat_id, 1950, 7)
            self.assertEqual(int(store.points_config(chat_id)["dice_odds"]), 1950)
            store.set_dice_odds(chat_id, 1700, 7)
            self.assertEqual(int(store.points_config(chat_id)["dice_odds"]), 1700)
            store.set_dice_odds(chat_id, 2000, 7)
            self.assertEqual(int(store.points_config(chat_id)["dice_odds"]), 2000)
            with self.assertRaises(ValueError):
                store.set_dice_odds(chat_id, 1699, 7)
            with self.assertRaises(ValueError):
                store.set_dice_odds(chat_id, 2001, 7)
            with self.assertRaises(ValueError):
                store.set_dice_odds(chat_id, True, 7)  # type: ignore[arg-type]

    def test_dice_minimum_and_daily_schedule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -88002
            config = store.points_config(chat_id)
            self.assertEqual(str(config["dice_min_bet"]), "1.0")
            self.assertEqual(int(config["dice_schedule_enabled"]), 0)
            store.set_dice_min_bet(chat_id, "2.50", 7)
            store.set_dice_schedule(chat_id, True, "22:30", "06:15", 7)
            config = store.points_config(chat_id)
            self.assertEqual(str(config["dice_min_bet"]), "2.5")
            self.assertEqual(int(config["dice_schedule_enabled"]), 1)
            self.assertEqual(config["dice_open_time"], "22:30")
            self.assertEqual(config["dice_close_time"], "06:15")
            with self.assertRaises(ValueError):
                store.set_dice_min_bet(chat_id, 0, 7)
            with self.assertRaises(ValueError):
                store.set_dice_schedule(chat_id, True, "25:00", "06:00", 7)

    def test_multiple_quick_posts_buttons_and_schedule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -88003
            first = store.quick_post(chat_id)
            second = store.create_quick_post(chat_id, "消息2", 7)
            store.update_quick_post(chat_id, 7, int(second["id"]), text="第二条")
            self.assertEqual(len(store.quick_posts(chat_id)), 2)
            button1 = store.add_quick_post_button(
                int(second["id"]), "绿色按钮", "https://example.com/1", "success"
            )
            store.add_quick_post_button(
                int(second["id"]), "红色按钮", "https://example.com/2", "danger"
            )
            buttons = store.quick_post_buttons(int(second["id"]))
            self.assertEqual([row["color"] for row in buttons], ["success", "danger"])
            self.assertTrue(store.update_quick_post_button(
                int(second["id"]), button1, "🎉 输入内容", "https://example.com/new",
                "primary", "short",
            ))
            edited = store.quick_post_buttons(int(second["id"]))[0]
            self.assertEqual(
                (edited["text"], edited["color"], edited["width"]),
                ("🎉 输入内容", "primary", "short"),
            )
            self.assertTrue(store.delete_quick_post_button(int(second["id"]), button1))
            schedule_id = store.schedule_quick_post(
                chat_id, int(second["id"]), "2000-01-01 00:00:00", 7
            )
            self.assertEqual(int(store.due_quick_posts()[0]["id"]), schedule_id)
            store.finish_quick_post_schedule(schedule_id)
            self.assertEqual(store.pending_quick_post_schedules(chat_id), [])
            self.assertTrue(store.delete_quick_post(chat_id, int(second["id"])))
            self.assertEqual(int(store.quick_post(chat_id)["id"]), int(first["id"]))


    def test_invite_source_cycle_flag_resets_at_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -9501, 81
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(
                chat_id, user_id, 10, "邀请成员 9 首次进群", 0, "a", "A"
            )
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 1
            )
            gift_id = store.add_point_gift(chat_id, "杯", 10, 3, 1)
            rid, _, bal = store.redeem_point_gift(chat_id, user_id, gift_id, "a", "A")
            self.assertEqual(bal, 0)
            rows = store.point_redemption_rows(chat_id, 5, 0)
            self.assertEqual(str(rows[0]["note"]), "积分来源邀请他人")
            # redeem that spent to 0 also cleared the cycle
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 0
            )
            store.adjust_points(chat_id, user_id, 10, "seed", 1, "a", "A")
            gift2 = store.add_point_gift(chat_id, "笔", 5, 3, 1)
            rid2, _, _ = store.redeem_point_gift(chat_id, user_id, gift2, "a", "A")
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 10, 0)}
            self.assertEqual(str(rows[rid2]["note"] or ""), "")

    def test_invite_source_cycle_clears_at_balance_five(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -9502, 82
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(
                chat_id, user_id, 20, "邀请成员 1 首次进群", 0, "b", "B"
            )
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 1
            )
            # Balance 6 keeps the cycle flag.
            store.adjust_points(chat_id, user_id, -14, "花掉", 1, "b", "B")
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 6)
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 1
            )
            # Balance 5 clears the cycle.
            store.adjust_points(chat_id, user_id, -1, "再花", 1, "b", "B")
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 5)
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 0
            )
            # Rising above 5 without invite does not reflag.
            store.adjust_points(chat_id, user_id, 10, "签到", 0, "b", "B")
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 15)
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 0
            )
            gift = store.add_point_gift(chat_id, "无备注礼", 5, 3, 1)
            rid, _, _ = store.redeem_point_gift(chat_id, user_id, gift, "b", "B")
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 10, 0)}
            self.assertEqual(str(rows[rid]["note"] or ""), "")
            # Invite award that rises above 5 sets the flag again; notes return.
            store.adjust_points(
                chat_id, user_id, 10, "邀请成员 2 首次进群", 0, "b", "B"
            )
            self.assertGreater(store.point_account(chat_id, user_id)["balance"], 5)
            self.assertEqual(
                int(store.point_account(chat_id, user_id)["invite_source_cycle"]), 1
            )
            gift2 = store.add_point_gift(chat_id, "有备注礼", 5, 3, 1)
            rid2, _, _ = store.redeem_point_gift(chat_id, user_id, gift2, "b", "B")
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 10, 0)}
            self.assertEqual(str(rows[rid2]["note"]), "积分来源邀请他人")

    def test_dice_payout_math_helpers(self):
        from tg_directory_bot.storage import normalize_points

        def win_payout(stake, odds) -> Decimal:
            return normalize_points(
                normalize_points(stake) * Decimal(odds) / Decimal(1000)
            )

        def win_delta(stake, odds) -> Decimal:
            stake_n = normalize_points(stake)
            return normalize_points(win_payout(stake_n, odds) - stake_n)

        def lose_delta(stake) -> Decimal:
            return normalize_points(-normalize_points(stake))

        self.assertEqual(win_payout(1000, 1950), Decimal("1950"))
        self.assertEqual(win_delta(1000, 1950), Decimal("950"))
        self.assertEqual(lose_delta(1000), Decimal("-1000"))
        self.assertEqual(win_delta(100, 2000), Decimal("100"))
        self.assertEqual(win_delta(1000, 2000), Decimal("1000"))
        self.assertEqual(win_payout(Decimal("1.5"), 2000), Decimal("3"))
        self.assertEqual(win_delta(Decimal("1.5"), 2000), Decimal("1.5"))

    def test_decimal_points_adjust_insufficient_and_format(self):
        from tg_directory_bot.storage import format_points, normalize_points
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "t.sqlite3")
            store.init()
            chat_id, user_id = -1001, 42
            store.set_points_enabled(chat_id, True, 1)
            self.assertEqual(
                store.adjust_points(chat_id, user_id, "1.5", "a", 1),
                Decimal("1.5"),
            )
            self.assertEqual(
                store.adjust_points(chat_id, user_id, "0.25", "b", 1),
                Decimal("1.75"),
            )
            balance = normalize_points(
                store.point_account(chat_id, user_id)["balance"]
            )
            self.assertEqual(balance, Decimal("1.75"))
            with self.assertRaises(ValueError):
                store.adjust_points(chat_id, user_id, "-2", "c", 1)
            self.assertEqual(format_points(Decimal("10.50")), "10.5")
            self.assertEqual(format_points(Decimal("10.00")), "10")
            self.assertEqual(normalize_points("1.239"), Decimal("1.24"))

    def test_decimal_gift_redeem_and_draw_still_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "t.sqlite3")
            store.init()
            chat_id, user_id = -1002, 7
            store.set_points_enabled(chat_id, True, 1)
            store.set_point_draw_config(chat_id, True, "1.5", 1.0, 1)
            store.adjust_points(chat_id, user_id, "10.5", "seed", 1, "u", "U")
            gift_id = store.add_point_gift(chat_id, "小数礼品", "2.5", 5, 1)
            rid, name, bal = store.redeem_point_gift(
                chat_id, user_id, gift_id, "u", "U"
            )
            self.assertEqual(name, "小数礼品")
            self.assertEqual(bal, Decimal("8"))
            gift_id2 = store.add_point_gift(chat_id, "抽奖礼", "100", -1, 1)
            result = store.draw_point_gift(
                chat_id, user_id, gift_id2, "u", "U", "1.5"
            )
            self.assertEqual(result["points_spent"], Decimal("1.5"))
            self.assertEqual(result["balance"], Decimal("6.5"))


if __name__ == "__main__":
    unittest.main()
