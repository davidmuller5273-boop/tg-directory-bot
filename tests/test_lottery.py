from pathlib import Path
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from tg_directory_bot.bot import (
    lottery_history_page,
    lottery_subscription_keyboard,
    lottery_poll_window,
    parse_private_note_command,
    refresh_lottery_results,
    poll_lottery_results,
    next_lottery_draw,
    parse_lottery_draw_time,
)
from tg_directory_bot.lottery import (
    LOTTERY_GAMES,
    LotteryResult,
    LotteryService,
    classify_three_digit,
    format_lottery_result,
    format_mark_six_numbers,
    is_valid_mark_six_result,
    is_valid_three_digit_result,
    normalize_lottery_result,
    normalize_mark_six_numbers,
    normalize_three_digit_numbers,
    resolve_lottery_code,
    resolve_lottery_history_keyword,
)
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.time_utils import (
    beijing_datetime_to_utc_text,
    format_beijing_time,
    format_beijing_timestamp_ms,
)


class LotteryTest(unittest.TestCase):
    def test_latest_waits_for_all_providers_and_selects_newest_issue(self):
        old = LotteryResult(
            "cwl", "ssq", "双色球", "2026001", "2026-01-01",
            ("01", "02", "03", "04", "05", "06"), ("16",),
        )
        new = LotteryResult(
            "realtime168", "ssq", "双色球", "2026002", "2026-01-03",
            ("07", "08", "09", "10", "11", "12"), ("15",),
        )
        service = LotteryService(SimpleNamespace())

        completed = []

        async def slow_old():
            await asyncio.sleep(0.03)
            completed.append("old")
            return old

        async def fast_new():
            await asyncio.sleep(0.01)
            completed.append("new")
            return new

        service.latest_providers = lambda game: [
            fast_new(), slow_old(), slow_old(), slow_old(), slow_old(),
        ]
        result = asyncio.run(asyncio.wait_for(
            service.latest("ssq", "2026001"), timeout=0.2
        ))
        self.assertEqual(result.issue, "2026002")
        self.assertEqual(result.source, "realtime168")
        self.assertEqual(len(completed), 5)

    def test_parse_cwl_ssq(self):
        result = LotteryService.parse_cwl(
            LOTTERY_GAMES["ssq"],
            {"result": [{
                "code": "2026099", "date": "2026-08-25", "red": "01,02,03,04,05,06",
                "blue": "16", "detailsLink": "/c/2026/demo.shtml",
            }]},
        )
        self.assertEqual(result.primary, ("01", "02", "03", "04", "05", "06"))
        self.assertEqual(result.secondary, ("16",))
        rendered = format_lottery_result(result)
        self.assertIn("福彩双色球第:2026099期开奖结果:", rendered)
        self.assertIn("🔴01 02 03 04 05 06", rendered)
        self.assertIn("🔵16", rendered)
        self.assertIn("开奖时间：2026-08-25", rendered)

    def test_parse_sport_dlt(self):
        result = LotteryService.parse_sport(
            LOTTERY_GAMES["dlt"],
            {"value": {"list": [{
                "lotteryDrawNum": "26099", "lotteryDrawTime": "2026-08-25",
                "lotteryDrawResult": "01 02 03 04 05 06 07",
            }]}},
        )
        self.assertEqual(result.primary, ("01", "02", "03", "04", "05"))
        self.assertEqual(result.secondary, ("06", "07"))
        self.assertEqual(resolve_lottery_code("大乐透"), "dlt")
        self.assertEqual(resolve_lottery_code("福彩"), "cwl")

    def test_parse_public_repo_history(self):
        payload = {"draws": [{
            "issue": "26096", "draw_date": "2026-08-24",
            "number_raw": "08 09 10 11 25 04 12",
        }]}
        result = LotteryService.parse_public_repo_history(LOTTERY_GAMES["dlt"], payload)[0]
        self.assertEqual(result.primary, ("08", "09", "10", "11", "25"))
        self.assertEqual(result.secondary, ("04", "12"))
        self.assertEqual(result.source, "sport")

    def test_history_parser_and_keyword(self):
        payload = {"result": [
            {"code": f"2026{i:03d}", "date": "2026-08-25", "red": "01,02,03", "blue": ""}
            for i in range(100, 0, -1)
        ]}
        results = LotteryService.parse_cwl_history(LOTTERY_GAMES["kl8"], payload)
        self.assertEqual(len(results), 100)
        self.assertEqual(results[0].issue, "2026100")
        keyword_cases = {
            "双色球历史": "ssq", "福彩3D历史": "fc3d", "七乐彩历史": "qlc",
            "快乐8历史": "kl8", "大乐透历史": "dlt", "排列3历史": "pl3",
            "排列5历史": "pl5", "7星彩历史": "qxc",
        }
        for keyword, code in keyword_cases.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(resolve_lottery_history_keyword(keyword), code)
        self.assertEqual(resolve_lottery_history_keyword("双色球 历史"), "ssq")
        self.assertIsNone(resolve_lottery_history_keyword("快乐8"))

    def test_mark_six_parsers_aliases_and_subscription_buttons(self):
        hk_payload = {"data": {"lotteryDraws": [{
            "year": 2026, "no": 93, "drawDate": "2026-08-25T00:00:00+08:00",
            "status": "Result",
            "drawResult": {"drawnNo": [1, 18, 19, 25, 34, 38], "xDrawnNo": 7},
        }]}}
        hk = LotteryService.parse_hkjc_history(LOTTERY_GAMES["hklhc"], hk_payload)[0]
        self.assertEqual(hk.issue, "2026093")
        self.assertEqual(hk.secondary, ("07",))

        latest = LotteryService.parse_marksix6_latest(
            LOTTERY_GAMES["macau_lhc"],
            {"expect": "2026238", "openTime": "2026-08-26 22:32:32",
             "numbers": ["16", "11", "25", "36", "03", "07", "48"]},
        )
        self.assertEqual(latest.primary, ("16", "11", "25", "36", "03", "07"))
        page = '''<section class="card" id="newMacau">
        <div class="history-line"><span class="period">2026238期</span>
        <span class="ball-sm red">35</span><span class="ball-sm blue">44</span>
        <span class="ball-sm red">23</span><span class="ball-sm blue">04</span>
        <span class="ball-sm green">07</span><span class="ball-sm red">21</span>
        <span class="ball-sm blue">17</span></div></section>'''
        rows = LotteryService.parse_marksix6_history_html(
            LOTTERY_GAMES["new_macau_lhc"], page
        )
        self.assertEqual(rows[0].secondary, ("17",))
        self.assertEqual(resolve_lottery_code("香港六合彩"), "hklhc")
        self.assertEqual(resolve_lottery_history_keyword("澳门六合彩历史"), "macau_lhc")

        macaujc = LotteryService.parse_macaujc_payload(
            LOTTERY_GAMES["new_macau_lhc"],
            [{
                "expect": "2026239", "openTime": "2026-08-27 21:32:32",
                "openCode": "47,43,34,17,22,07,05",
                "wave": "blue,green,red,green,green,red,green",
                "zodiac": "猴,鼠,雞,虎,雞,鼠,虎",
            }],
        )[0]
        rendered = format_lottery_result(macaujc)
        self.assertIn("47 43 34 17 22 07 + 05", rendered)
        self.assertIn("猴 鼠 雞 虎 雞 鼠 + 虎", rendered)
        self.assertIn("🔵 🟢 🔴 🟢 🟢 🔴 + 🟢", rendered)
        self.assertIn("macaujc.com", rendered)
        self.assertEqual(macaujc.zodiac, ("猴", "鼠", "雞", "虎", "雞", "鼠", "虎"))
        self.assertEqual(
            format_mark_six_numbers(
                "2026239", "", macaujc.primary, macaujc.secondary,
                zodiac=macaujc.zodiac, wave=macaujc.wave,
            ).count("\n"),
            2,
        )

    def test_parse_realtime_national_lottery(self):
        result = LotteryService.parse_realtime(
            LOTTERY_GAMES["ssq"],
            {
                "errorCode": 0,
                "result": {"data": {
                    "preDrawIssue": "2026099",
                    "preDrawTime": "2026-08-27 21:30:00",
                    "preDrawCode": "01,12,14,18,30,31,02",
                }},
            },
        )
        self.assertEqual(result.source, "realtime168")
        self.assertEqual(result.primary, ("01", "12", "14", "18", "30", "31"))
        self.assertEqual(result.secondary, ("02",))

        result_with_next = LotteryService.parse_realtime(
            LOTTERY_GAMES["ssq"],
            {
                "errorCode": 0,
                "result": {"data": {
                    "preDrawIssue": "2026099",
                    "preDrawTime": "2026-08-27 21:30:00",
                    "preDrawCode": "01,12,14,18,30,31,02",
                    "drawTime": "2026-08-30 21:30:00",
                }},
            },
        )
        self.assertEqual(result_with_next.next_draw_time, "2026-08-30 21:30:00")

    def test_lottery_draw_window_schedule(self):
        current = parse_lottery_draw_time("2026-08-28 22:00:00")
        self.assertIsNotNone(current)
        self.assertEqual(
            next_lottery_draw("ssq", current).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-08-30 21:15:00",
        )

    def test_every_lottery_starts_at_least_five_provider_requests(self):
        service = LotteryService(SimpleNamespace())

        async def close_providers():
            for game in LOTTERY_GAMES.values():
                providers = service.latest_providers(game)
                self.assertGreaterEqual(len(providers), 5)
                for provider in providers:
                    provider.close()

        asyncio.run(close_providers())

    def test_lottery_polling_stops_after_forty_five_minutes(self):
        within = parse_lottery_draw_time("2026-08-30 21:59:59")
        expired = parse_lottery_draw_time("2026-08-30 22:00:01")
        self.assertIsNotNone(lottery_poll_window("ssq", within))
        self.assertIsNone(lottery_poll_window("ssq", expired))

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_lottery_subscription(-1001, "hklhc", 7)
            keyboard = lottery_subscription_keyboard(store, -1001)
            labels = [button.text for row in keyboard.inline_keyboard for button in row]
            self.assertIn("✅ 香港六合彩", labels)
            self.assertIn("❌ 澳门六合彩", labels)
            self.assertIn("❌ 新澳六合彩", labels)

    def test_cwl_request_uses_cookie_friendly_page_size(self):
        service = LotteryService(SimpleNamespace(cwl_lottery_url="https://example.test/cwl"))
        self.assertIn("Mozilla/5.0 (Windows NT", service.headers["user-agent"])

    def test_history_cache_is_paginated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for i in range(100, 0, -1):
                store.save_lottery_result(LotteryResult(
                    "cwl", "kl8", "快乐8", f"2026{i:03d}", "2026-08-25",
                    ("01", "02", "03"),
                ))
            text, keyboard = lottery_history_page(store, "kl8", 0)
            self.assertIn("第 1/10 页", text)
            self.assertIn("第2026100期", text)
            self.assertNotIn("第2026089期", text)
            self.assertIsNotNone(keyboard)
            self.assertEqual(store.latest_lottery_results("kl8")[0]["issue"], "2026100")
            latest = {row["game_code"]: row["issue"] for row in store.latest_lottery_results()}
            self.assertEqual(latest["kl8"], "2026100")

    def test_private_note_command_parser(self):
        self.assertEqual(parse_private_note_command("1 客户A 备注内容"), ("1", "客户A", "备注内容"))
        self.assertEqual(parse_private_note_command("1，客户A 备注内容"), ("1", "客户A", "备注内容"))
        self.assertEqual(parse_private_note_command("2 客户A"), ("2", "客户A", ""))
        self.assertIsNone(parse_private_note_command("客户A"))

    def test_beijing_time_rendering(self):
        self.assertEqual(format_beijing_time("2026-08-25 16:00:00"), "2026-08-26 00:00:00")
        self.assertEqual(format_beijing_timestamp_ms(0), "链上未提供")
        self.assertEqual(
            beijing_datetime_to_utc_text("2099-08-27 21:30"),
            "2099-08-27 13:30:00",
        )

    def test_new_issue_is_queued_once(self):
        first = LotteryResult(
            "cwl", "ssq", "双色球", "2026001", "2026-01-01",
            ("01", "02", "03", "04", "05", "06"), ("16",),
        )
        second = LotteryResult(
            "cwl", "ssq", "双色球", "2026002", "2026-01-03",
            ("07", "08", "09", "10", "11", "12"), ("15",),
        )

        class FakeLottery:
            def __init__(self, result):
                self.result = result

            async def latest_all(self, game_codes=None, known_issues=None):
                return [self.result], {}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_lottery_subscription(-1001, "all", 99)
            fake = FakeLottery(first)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"store": store, "lottery": fake})
            )
            asyncio.run(refresh_lottery_results(context, ("ssq",)))
            self.assertEqual(len(store.pending_outbox()), 0)
            fake.result = second
            asyncio.run(refresh_lottery_results(context, ("ssq",)))
            self.assertEqual(len(store.pending_outbox()), 1)
            asyncio.run(refresh_lottery_results(context, ("ssq",)))
            self.assertEqual(len(store.pending_outbox()), 1)


    def test_poll_lottery_always_includes_subscribed_codes(self):
        """Outside the soft 30-minute window, subscribed codes are still polled."""
        called = {}

        class FakeLottery:
            async def latest_all(self, game_codes=None, known_issues=None):
                called["codes"] = tuple(game_codes or ())
                return [], {}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            far = parse_lottery_draw_time("2026-08-26 12:00:00")
            self.assertIsNone(lottery_poll_window("ssq", far))
            self.assertIsNone(lottery_poll_window("dlt", far))
            store.add_lottery_subscription(-2001, "ssq", 7)
            store.add_lottery_subscription(-2001, "dlt", 7)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={
                    "store": store,
                    "lottery": FakeLottery(),
                    "lottery_completed_draws": {},
                })
            )
            with patch(
                "tg_directory_bot.bot.datetime"
            ) as mocked_datetime:
                mocked_datetime.now.return_value = far
                mocked_datetime.side_effect = None
                asyncio.run(poll_lottery_results(context))
            self.assertEqual(set(called.get("codes", ())), {"ssq", "dlt"})

    def test_three_digit_classify_and_format(self):
        self.assertEqual(classify_three_digit(("2", "2", "2")), "豹子")
        self.assertEqual(classify_three_digit(("1", "2", "3")), "顺子")
        self.assertEqual(classify_three_digit(("8", "9", "0")), "顺子")
        self.assertEqual(classify_three_digit(("9", "0", "1")), "顺子")
        self.assertEqual(classify_three_digit(("1", "1", "2")), "组三")
        self.assertEqual(classify_three_digit(("2", "7", "2")), "组三")
        self.assertEqual(classify_three_digit(("1", "4", "7")), "组六")
        self.assertEqual(normalize_three_digit_numbers(("02", "07", "02")), ("2", "7", "2"))
        result = LotteryService.parse_cwl(
            LOTTERY_GAMES["fc3d"],
            {"result": [{
                "code": "2026103", "date": "2026-09-10",
                "red": "02,07,02", "blue": "",
            }]},
        )
        self.assertEqual(result.primary, ("2", "7", "2"))
        rendered = format_lottery_result(result)
        self.assertIn("开奖号码：2 7 2（组三）", rendered)
        pl3 = LotteryService.parse_sport(
            LOTTERY_GAMES["pl3"],
            {"value": {"list": [{
                "lotteryDrawNum": "25123", "lotteryDrawTime": "2026-09-10",
                "lotteryDrawResult": "8 9 0",
            }]}},
        )
        self.assertEqual(pl3.primary, ("8", "9", "0"))
        self.assertIn("开奖号码：8 9 0（顺子）", format_lottery_result(pl3))

    def test_three_digit_rejects_incomplete(self):
        self.assertFalse(is_valid_three_digit_result(("02",)))
        self.assertFalse(is_valid_three_digit_result(("02", "07")))
        self.assertFalse(is_valid_three_digit_result(("10", "1", "2")))
        with self.assertRaises(ValueError):
            normalize_three_digit_numbers(("02",))
        with self.assertRaises(ValueError):
            LotteryService.parse_realtime(
                LOTTERY_GAMES["fc3d"],
                {
                    "errorCode": 0,
                    "result": {"data": {
                        "preDrawIssue": "2026103",
                        "preDrawTime": "2026-09-10 21:30:00",
                        "preDrawCode": "02",
                    }},
                },
            )

    def test_mark_six_rejects_zero_and_incomplete(self):
        self.assertFalse(is_valid_mark_six_result(
            ("00", "01", "02", "03", "04", "05"), ("06",)
        ))
        self.assertFalse(is_valid_mark_six_result(
            ("01", "02", "03", "04", "05"), ("06",)
        ))
        with self.assertRaises(ValueError):
            normalize_mark_six_numbers(numbers=["00", "11", "25", "36", "03", "07", "48"])
        with self.assertRaises(ValueError):
            LotteryService.parse_marksix6_latest(
                LOTTERY_GAMES["macau_lhc"],
                {"expect": "2026238", "openTime": "2026-08-26 22:32:32",
                 "numbers": ["00", "11", "25", "36", "03", "07", "48"]},
            )
        skipped = LotteryService.parse_macaujc_payload(
            LOTTERY_GAMES["new_macau_lhc"],
            [{
                "expect": "2026239", "openTime": "2026-08-27 21:32:32",
                "openCode": "00,43,34,17,22,07,05",
            }],
        )
        self.assertEqual(skipped, [])
        aligned = format_mark_six_numbers(
            "2026001", "2026-01-01",
            ("10", "06", "39", "47", "37", "17"), ("14",),
        )
        lines = aligned.splitlines()
        self.assertEqual(lines[0], "10 06 39 47 37 17 + 14")
        self.assertTrue(lines[1].endswith("+ " + lines[1].split(" + ")[-1]))
        self.assertIn(" + ", lines[2])
        self.assertEqual(lines[2].count(" + "), 1)

    def test_invalid_three_digit_not_saved_or_broadcast(self):
        bad = LotteryResult(
            "huiniao", "fc3d", "福彩3D", "2026999", "2026-09-10", ("02",),
        )
        good = LotteryResult(
            "cwl", "fc3d", "福彩3D", "2026999", "2026-09-10", ("2", "7", "2"),
        )

        class FakeLottery:
            def __init__(self):
                self.result = bad

            async def latest_all(self, game_codes=None, known_issues=None):
                return [self.result], {}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_lottery_subscription(-1001, "fc3d", 99)
            fake = FakeLottery()
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"store": store, "lottery": fake})
            )
            asyncio.run(refresh_lottery_results(context, ("fc3d",)))
            self.assertEqual(store.latest_lottery_results("fc3d"), [])
            self.assertEqual(len(store.pending_outbox()), 0)
            # seed a good result first so later broadcast can fire
            store.save_lottery_result(LotteryResult(
                "cwl", "fc3d", "福彩3D", "2026998", "2026-09-09", ("1", "2", "3"),
            ))
            fake.result = good
            asyncio.run(refresh_lottery_results(context, ("fc3d",)))
            self.assertEqual(store.latest_lottery_results("fc3d")[0]["primary_numbers"], "2 7 2")
            self.assertEqual(len(store.pending_outbox()), 1)
            body = store.pending_outbox()[0]["body"]
            self.assertIn("开奖号码：2 7 2（组三）", body)




class BeijingDatetimeParseTest(unittest.TestCase):
    def test_beijing_datetime_allow_past_for_stats_start(self):
        past = beijing_datetime_to_utc_text("2020-01-01 12:00", allow_past=True)
        self.assertTrue(past.startswith("2020-01-01"))
        with self.assertRaisesRegex(ValueError, "必须晚于当前时间"):
            beijing_datetime_to_utc_text("2020-01-01 12:00")

    def test_beijing_datetime_accepts_seconds_and_slashes(self):
        a = beijing_datetime_to_utc_text("2099-08-27 21:30:00")
        b = beijing_datetime_to_utc_text("2099/08/27 21:30")
        self.assertEqual(a, b)

    def test_beijing_datetime_accepts_unpadded(self):
        self.assertEqual(
            beijing_datetime_to_utc_text("2099-8-7 9:05"),
            beijing_datetime_to_utc_text("2099-08-07 09:05"),
        )


if __name__ == "__main__":
    unittest.main()
