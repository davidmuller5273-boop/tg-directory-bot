from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_directory_bot.auto_delete import persistent_message
from datetime import datetime, timedelta, timezone

from tg_directory_bot.bot import (
    GROUP_PERMISSIONS,
    GROUP_PERMISSION_LABELS,
    build_raffle_extras_from_pro,
    count_user_chat_boosts,
    evaluate_raffle_unmet,
    name_history_view,
    parse_group_permissions,
    parse_raffle_condition_text,
    raffle_keyboard,
    raffle_needs_join_button,
    raffle_pro_answers_from_row,
    raffle_text,
)
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.time_utils import BEIJING_TZ, utc_after_minutes_text


class NameHistoryAndRaffleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.tmp.name) / "test.db")
        self.store.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_renamehist_permission_parse(self):
        self.assertIn("renamehist", GROUP_PERMISSIONS)
        self.assertEqual(GROUP_PERMISSION_LABELS["renamehist"], "改名记录")
        self.assertEqual(parse_group_permissions("改名记录"), {"renamehist"})
        self.assertIn("renamehist", parse_group_permissions("全部"))

    def test_name_history_record_and_query(self):
        chat_id, user_id = -1001, 42
        changed = self.store.sight_group_member_profile(
            chat_id, user_id, first_name="张", last_name="三",
            display_name="张 三", username="zhang",
        )
        self.assertFalse(changed)
        changed = self.store.sight_group_member_profile(
            chat_id, user_id, first_name="李", last_name="四",
            display_name="李 四", username="zhang",
        )
        self.assertTrue(changed)
        self.assertTrue(self.store.user_has_name_change_history(chat_id, user_id))
        rows = self.store.group_name_history(chat_id, user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old_display"], "张 三")
        self.assertEqual(rows[0]["new_display"], "李 四")
        text = name_history_view(self.store, chat_id, user_id)
        self.assertIn("改名记录", text)
        self.assertIn("张 三", text)
        self.assertIn("李 四", text)

    def test_redemption_note_includes_rename(self):
        chat_id, user_id = -1002, 7
        self.store.set_points_enabled(chat_id, True, 1)
        self.store.adjust_points(chat_id, user_id, 100, "测试", 1, "u", "U")
        self.store.sight_group_member_profile(
            chat_id, user_id, first_name="A", last_name="", display_name="A",
        )
        self.store.sight_group_member_profile(
            chat_id, user_id, first_name="B", last_name="", display_name="B",
        )
        gift_id = self.store.add_point_gift(chat_id, "礼品", 10, 5, 1)
        rid, name, _bal = self.store.redeem_point_gift(
            chat_id, user_id, gift_id, "u", "U",
        )
        self.assertEqual(name, "礼品")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT note FROM point_redemptions WHERE id=?", (rid,),
            ).fetchone()
        self.assertIn("已改过名字姓氏", str(row["note"]))

    def test_raffle_tree_format_and_boost_unmet_note(self):
        chat_id, creator = -1003, 1
        conditions = {
            "channel": "@demo", "messages": 5, "keyword": "抽奖", "boosts": 4,
            "require_channel": True, "require_messages": True,
            "require_keyword": True, "require_boosts": True,
        }
        rid = self.store.create_raffle(
            chat_id, creator, "1*测试奖 | 2*纪念币", 3,
            utc_after_minutes_text(30),
            title="春日福利",
            rules_json=json.dumps(["规则1", "规则2"], ensure_ascii=False),
            conditions_json=json.dumps(conditions, ensure_ascii=False),
            how_to_join="发送关键词或点按钮",
            join_keyword="抽奖",
            channel_ref="@demo",
            min_messages=5,
            min_boosts=4,
            stats_start_mode="immediate",
            stats_start_at=utc_after_minutes_text(0),
        )
        raffle = self.store.get_raffle(rid)
        body = raffle_text(raffle, show_count=True)
        self.assertIn("规则1", body)
        self.assertIn("├活动类型: 通用抽奖", body)
        self.assertIn("├定时开奖:", body)
        self.assertIn("├奖品列表:", body)
        self.assertIn("  ├ 测试奖 x 1", body)
        self.assertIn("  ├ 纪念币 x 2", body)
        self.assertIn("[如何参与？]", body)

        self.store.join_raffle(rid, 99, "loser", "Loser", via_keyword=False)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"store": self.store}),
            bot=SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="left"),
                ),
                get_user_chat_boosts=AsyncMock(
                    # ⚡0 — Telegram chat boosts list empty
                    return_value=SimpleNamespace(boosts=[]),
                ),
            ),
        )
        unmet = asyncio.run(evaluate_raffle_unmet(context, raffle, 99))
        self.assertIn("关注频道", unmet)
        self.assertIn("发言不足", unmet)
        self.assertIn("未发关键词", unmet)
        self.assertIn("助推不足", unmet)
        note = "未达标:" + "/".join(unmet)
        self.assertTrue(note.startswith("未达标:"))
        self.assertIn("助推不足", note)

        # ⚡4 = four ChatBoost entries from getUserChatBoosts (not a keyword counter)
        context.bot.get_user_chat_boosts = AsyncMock(
            return_value=SimpleNamespace(
                boosts=[object(), object(), object(), object()],
            ),
        )
        boosts = asyncio.run(count_user_chat_boosts(context, chat_id, 99))
        self.assertEqual(boosts, 4)
        unmet2 = asyncio.run(evaluate_raffle_unmet(context, raffle, 99))
        self.assertNotIn("助推不足", unmet2)

        ok = self.store.complete_raffle(rid, [99], {99: note})
        self.assertTrue(ok)
        winners = self.store.raffle_winners(rid)
        self.assertEqual(str(winners[0]["note"]), note)

    def test_persistent_message_context_manager(self):
        with persistent_message():
            pass
        self.assertTrue(callable(persistent_message))

    def test_parse_boost_condition_uses_chat_boost_count(self):
        parsed = parse_raffle_condition_text("助推 4")
        self.assertTrue(parsed["require_boosts"])
        self.assertEqual(parsed["boosts"], 4)


    def test_universal_message_auto_join_and_no_button(self):
        chat_id, creator, user_id = -2001, 1, 55
        rid = self.store.create_raffle(
            chat_id, creator, "奖品A", 1, utc_after_minutes_text(60),
            title="发言自动参与",
            conditions_json=json.dumps({"messages": 2}, ensure_ascii=False),
            min_messages=2,
            stats_start_mode="immediate",
            stats_start_at=utc_after_minutes_text(0),
        )
        raffle = self.store.get_raffle(rid)
        self.assertFalse(raffle_needs_join_button(raffle))
        self.assertIsNone(raffle_keyboard(rid, 0, True, raffle=raffle))
        body = raffle_text(raffle, show_count=True)
        self.assertIn("自动参与", body)
        self.assertIn("无需点击按钮", body)

        # one message — not enough
        self.store.record_group_activity(
            chat_id, "G", "", "supergroup", user_id, "u55", "U55", messages=1,
        )
        self.assertEqual(
            self.store.qualify_activity_raffles(chat_id, user_id, "u55", "U55"), []
        )
        # second message — auto join
        self.store.record_group_activity(
            chat_id, "G", "", "supergroup", user_id, "u55", "U55", messages=1,
        )
        qualified = self.store.qualify_activity_raffles(
            chat_id, user_id, "u55", "U55",
        )
        self.assertEqual([(int(r["id"]), int(r["entries"])) for r in qualified], [(rid, 1)])
        # draw sync still finds the same candidate
        candidates = self.store.universal_message_candidates(rid)
        self.assertEqual([int(r["user_id"]) for r in candidates], [user_id])
        self.store.sync_raffle_entries(rid, candidates)
        entries = self.store.raffle_entries(rid)
        self.assertEqual([int(r["user_id"]) for r in entries], [user_id])

        # without min_messages, join button remains
        rid2 = self.store.create_raffle(
            chat_id, creator, "奖品B", 1, utc_after_minutes_text(60),
            title="点按钮",
        )
        raffle2 = self.store.get_raffle(rid2)
        self.assertTrue(raffle_needs_join_button(raffle2))
        self.assertIsNotNone(raffle_keyboard(rid2, 0, True, raffle=raffle2))

    def test_raffle_pro_prefill_and_update_keeps_entries(self):
        chat_id, creator = -1010, 3
        conditions = {
            "channel": "@demo", "messages": 10, "keyword": "抽奖", "boosts": 1,
            "require_channel": True, "require_messages": True,
            "require_keyword": True, "require_boosts": True,
        }
        template = {
            "title": "春日福利",
            "rules": ["规则A"],
            "conditions": conditions,
            "how_to_join": "发送关键词",
            "prize": "1*一等奖 | 2*二等奖",
            "winner_count": 3,
            "recur_daily": 1,
            "stats_start_mode": "immediate",
            "draw_clock": "2099-09-15 21:00",
        }
        rid = self.store.create_raffle(
            chat_id, creator, "1*一等奖 | 2*二等奖", 3,
            utc_after_minutes_text(60),
            title="春日福利",
            rules_json=json.dumps(["规则A"], ensure_ascii=False),
            conditions_json=json.dumps(conditions, ensure_ascii=False),
            how_to_join="发送关键词",
            join_keyword="抽奖",
            channel_ref="@demo",
            min_messages=10,
            min_boosts=1,
            recur_daily=1,
            stats_start_mode="immediate",
            stats_start_at=utc_after_minutes_text(0),
            template_json=json.dumps(template, ensure_ascii=False),
        )
        self.store.join_raffle(rid, 88, "member", "成员")
        answers = raffle_pro_answers_from_row(self.store.get_raffle(rid))
        self.assertEqual(len(answers), 9)
        self.assertEqual(answers[8], "0")
        self.assertEqual(answers[0], "春日福利")
        self.assertEqual(answers[1], "规则A")
        self.assertEqual(answers[2], "2099-09-15 21:00")
        self.assertEqual(answers[3], "立即")
        self.assertIn("频道 @demo", answers[4])
        self.assertIn("发言 10", answers[4])
        self.assertIn("关键词 抽奖", answers[4])
        self.assertIn("助推 1", answers[4])
        self.assertEqual(answers[5], "发送关键词")
        self.assertIn("1*一等奖", answers[6])
        self.assertIn("2*二等奖", answers[6])
        self.assertEqual(answers[7], "是")
        self.assertTrue(self.store.update_raffle(
            rid, chat_id, "1*新奖品", 1, utc_after_minutes_text(90),
            title="新标题", rules_json="[]", conditions_json="{}",
            how_to_join="点按钮", join_keyword="", channel_ref="",
            min_messages=0, min_boosts=0, recur_daily=0,
            stats_start_mode="immediate", stats_start_at="",
            template_json="{}",
        ))
        updated = self.store.get_raffle(rid)
        self.assertEqual(updated["prize"], "1*新奖品")
        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(updated["winner_count"], 1)
        self.assertEqual(int(updated["entries"]), 1)
        self.assertEqual(len(self.store.raffle_entries(rid)), 1)
        self.assertFalse(self.store.update_raffle(
            rid, -999, "x", 1, utc_after_minutes_text(120),
        ))

    def _insert_message_at(self, chat_id, user_id, created_at_utc: str, n: int = 1):
        with self.store.connect() as conn:
            for _ in range(n):
                conn.execute(
                    """INSERT INTO group_message_events
                       (chat_id, user_id, username, display_name, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (chat_id, user_id, "u", "U", created_at_utc),
                )

    def test_recur_daily_message_window_only_counts_draw_day(self):
        chat_id, creator, user_id = -3001, 1, 77
        # Draw at Beijing 2099-09-24 21:00 → UTC 2099-09-24 13:00
        ends_bj = datetime(2099, 9, 24, 21, 0, 0, tzinfo=BEIJING_TZ)
        ends_at = ends_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Old absolute stats_start from previous day (Beijing Sep 23 09:00)
        old_stats_bj = datetime(2099, 9, 23, 9, 0, 0, tzinfo=BEIJING_TZ)
        old_stats = old_stats_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rid = self.store.create_raffle(
            chat_id, creator, "奖品", 1, ends_at,
            title="每日发言",
            conditions_json=json.dumps({"messages": 2}, ensure_ascii=False),
            min_messages=2,
            recur_daily=1,
            stats_start_mode="datetime",
            stats_start_at=old_stats,
        )
        raffle = self.store.get_raffle(rid)
        start, end = DirectoryStore._raffle_message_window(raffle)
        # Day stats start = Sep 24 09:00 Beijing
        expect_start_bj = datetime(2099, 9, 24, 9, 0, 0, tzinfo=BEIJING_TZ)
        expect_start = expect_start_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(start, expect_start)
        self.assertEqual(end, ends_at)

        # Yesterday messages (after old absolute start) must NOT qualify
        yesterday = datetime(2099, 9, 23, 12, 0, 0, tzinfo=BEIJING_TZ)
        self._insert_message_at(
            chat_id, user_id,
            yesterday.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            n=5,
        )
        self.assertEqual(self.store.universal_message_candidates(rid), [])
        self.assertEqual(
            self.store.qualify_activity_raffles(chat_id, user_id, "u", "U"), []
        )

        # Same-day messages after day_stats_start DO qualify
        today_msg = datetime(2099, 9, 24, 10, 0, 0, tzinfo=BEIJING_TZ)
        self._insert_message_at(
            chat_id, user_id,
            today_msg.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            n=2,
        )
        candidates = self.store.universal_message_candidates(rid)
        self.assertEqual([int(r["user_id"]) for r in candidates], [user_id])
        qualified = self.store.qualify_activity_raffles(chat_id, user_id, "u", "U")
        self.assertEqual([int(r["id"]) for r in qualified], [rid])

        body = raffle_text(raffle, show_count=True)
        self.assertIn("当天发言达到", body)
        self.assertNotIn("活动期间群内发言达到", body)

    def test_non_recur_keeps_absolute_stats_start(self):
        chat_id, creator, user_id = -3002, 1, 88
        ends_bj = datetime(2026, 9, 24, 21, 0, 0, tzinfo=BEIJING_TZ)
        ends_at = ends_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        old_stats_bj = datetime(2026, 9, 22, 9, 0, 0, tzinfo=BEIJING_TZ)
        old_stats = old_stats_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rid = self.store.create_raffle(
            chat_id, creator, "奖品", 1, ends_at,
            conditions_json=json.dumps({"messages": 2}, ensure_ascii=False),
            min_messages=2,
            recur_daily=0,
            stats_start_mode="datetime",
            stats_start_at=old_stats,
        )
        raffle = self.store.get_raffle(rid)
        start, end = DirectoryStore._raffle_message_window(raffle)
        self.assertEqual(start, old_stats)
        self.assertEqual(end, ends_at)
        # Messages on Sep 23 (between old start and ends) count
        mid = datetime(2026, 9, 23, 12, 0, 0, tzinfo=BEIJING_TZ)
        self._insert_message_at(
            chat_id, user_id,
            mid.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            n=2,
        )
        candidates = self.store.universal_message_candidates(rid)
        self.assertEqual([int(r["user_id"]) for r in candidates], [user_id])
        body = raffle_text(raffle, show_count=True)
        self.assertIn("活动期间群内发言达到", body)

    def test_spawn_sets_fresh_day_stats_start(self):
        chat_id, creator = -3003, 1
        # Prior raffle drawn; template has datetime mode with clock from day-1
        old_ends_bj = datetime(2026, 9, 23, 21, 0, 0, tzinfo=BEIJING_TZ)
        old_ends = old_ends_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        old_stats_bj = datetime(2026, 9, 23, 8, 30, 0, tzinfo=BEIJING_TZ)
        old_stats = old_stats_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        template = {
            "title": "每日",
            "rules": [],
            "conditions": {"messages": 3},
            "how_to_join": "当天发言达到 3 条即自动参与，无需点击按钮。",
            "prize": "奖",
            "winner_count": 1,
            "recur_daily": 1,
            "stats_start_mode": "datetime",
            "draw_clock": "21:00",
        }
        rid = self.store.create_raffle(
            chat_id, creator, "奖", 1, old_ends,
            title="每日",
            conditions_json=json.dumps({"messages": 3}, ensure_ascii=False),
            how_to_join=template["how_to_join"],
            min_messages=3,
            recur_daily=1,
            stats_start_mode="datetime",
            stats_start_at=old_stats,
            template_json=json.dumps(template, ensure_ascii=False),
        )
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE raffles SET status='drawn',
                       drawn_at=CURRENT_TIMESTAMP WHERE id=?""",
                (rid,),
            )
        # Simulate spawn computation (same as bot.spawn_daily_recur_raffles)
        from tg_directory_bot.time_utils import beijing_datetime_to_utc_text
        ends_at = beijing_datetime_to_utc_text("21:00")
        stats_start_at = DirectoryStore.day_stats_start_utc(
            ends_at,
            stats_start_mode="datetime",
            stats_start_at=old_stats,
            created_at="",
        )
        new_id = self.store.create_raffle(
            chat_id, creator, "奖", 1, ends_at,
            title="每日",
            conditions_json=json.dumps({"messages": 3}, ensure_ascii=False),
            how_to_join=template["how_to_join"],
            min_messages=3,
            recur_daily=1,
            stats_start_mode="datetime",
            stats_start_at=stats_start_at,
            template_json=json.dumps(template, ensure_ascii=False),
        )
        new_raffle = self.store.get_raffle(new_id)
        stored = str(new_raffle["stats_start_at"])
        self.assertNotEqual(stored, old_stats)
        # Clock must remain 08:30 Beijing on the new draw day
        new_ends_dt = DirectoryStore._parse_utc_text(str(new_raffle["ends_at"]))
        draw_day = new_ends_dt.astimezone(BEIJING_TZ).date()
        expect_bj = datetime(
            draw_day.year, draw_day.month, draw_day.day, 8, 30, 0, tzinfo=BEIJING_TZ,
        )
        expect = expect_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(stored, expect)
        window_start, _ = DirectoryStore._raffle_message_window(new_raffle)
        self.assertEqual(window_start, expect)

    def test_recur_immediate_day_start_midnight(self):
        chat_id, creator = -3004, 1
        ends_bj = datetime(2026, 9, 24, 21, 0, 0, tzinfo=BEIJING_TZ)
        ends_at = ends_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # created earlier on same day afternoon — window maxes with created_at
        created_bj = datetime(2026, 9, 24, 14, 0, 0, tzinfo=BEIJING_TZ)
        created = created_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rid = self.store.create_raffle(
            chat_id, creator, "奖", 1, ends_at,
            min_messages=1,
            conditions_json=json.dumps({"messages": 1}, ensure_ascii=False),
            recur_daily=1,
            stats_start_mode="immediate",
            stats_start_at=created,
        )
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE raffles SET created_at=? WHERE id=?", (created, rid),
            )
        raffle = self.store.get_raffle(rid)
        start, _ = DirectoryStore._raffle_message_window(raffle)
        self.assertEqual(start, created)
        # Without same-day created clamp → midnight
        midnight = DirectoryStore.day_stats_start_utc(
            ends_at, "immediate", stats_start_at=created, created_at="",
        )
        expect_mid = datetime(2026, 9, 24, 0, 0, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(
            midnight,
            expect_mid.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

    def test_build_raffle_extras_recur_how_to_says_same_day(self):
        # answers: title, rules, ends, stats, conditions, how_to, prize, recur
        answers = [
            "标题",
            "无",
            "2099-12-31 21:00",
            "立即",
            "发言 5",
            "点按钮",
            "1 | 奖品",
            "是",
        ]
        ends_at, winner_count, prize, extras = build_raffle_extras_from_pro(answers)
        self.assertEqual(extras["recur_daily"], 1)
        self.assertIn("当天发言达到 5 条", extras["how_to_join"])
        self.assertNotIn("活动期间", extras["how_to_join"])
        answers_no = list(answers)
        answers_no[7] = "否"
        _, _, _, extras2 = build_raffle_extras_from_pro(answers_no)
        self.assertEqual(extras2["recur_daily"], 0)
        self.assertIn("活动期间群内发言达到 5 条", extras2["how_to_join"])


if __name__ == "__main__":
    unittest.main()
