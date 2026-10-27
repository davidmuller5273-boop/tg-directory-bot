"""AI reference prediction: no network, no real key (httpx.MockTransport)."""
import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from tg_directory_bot import tron_ai
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.tron_ai import AiPredictor, build_messages, build_stats_text, parse_pick, request_pick
from tg_directory_bot.tron_draw import TronDrawRecorder, ai_trend
from tests.test_tron_draw import FakeChain, make_hash

DUMMY = "sk-xxxx"
S, D = "单", "双"


def make_config(temp, **extra):
    kwargs = dict(
        bot_token="", admin_ids=set(), super_admin_ids=set(),
        db_path=Path(temp) / "db.sqlite3", categories=("other",), blocked_keywords=(),
        admin_username="admin", admin_password="testing-password",
        session_secret="x" * 40, backups_dir=Path(temp) / "backups", tron_draw_stats=True,
        ai_api_key=DUMMY,
    )
    kwargs.update(extra)
    return Config(**kwargs)


def reply(text):
    return {"choices": [{"message": {"content": text}}]}


def fill(store, count, start=1):
    for i in range(count):
        n = 20 * (start + i)
        store.record_tron_draw(n, make_hash(n, "1" if (i * 7 + i // 3) % 2 else "2"), 1_700_000_000_000 + n)


class PromptTest(unittest.TestCase):
    def test_parse_only_single_or_double(self):
        self.assertEqual(parse_pick("单"), S)
        self.assertEqual(parse_pick(" 双。"), D)
        self.assertEqual(parse_pick("我觉得双，不是单"), D)  # first one found
        self.assertIsNone(parse_pick("odd"))
        self.assertIsNone(parse_pick(""))
        self.assertIsNone(parse_pick(None))

    def test_stats_windows_and_fields(self):
        results = ([S] * 4 + [D] * 2 + [S, D] * 3) * 120  # 1440 results
        text = build_stats_text(results)
        for label in ("最近10期", "最近30期", "最近50期", "最近100期", "最近300期", "最近1000期", "全部历史（1440期）"):
            self.assertIn(label, text)
        self.assertIn("最长单连4期", text)
        self.assertIn("最长双连2期", text)
        self.assertIn("最长连跳", text)
        self.assertIn("当前连", text)
        self.assertIn("%", text)
        self.assertEqual(text.splitlines()[-1], "最近10期（旧→新）：" + "".join(results[-10:]))

    def test_stats_values_exact(self):
        text = build_stats_text([S, S, S, D, D, S, D, S, D, D])
        first = text.splitlines()[0]
        self.assertIn("单5期(50%) 双5期(50%)", first)
        self.assertIn("当前连双2期", first)
        self.assertIn("最长单连3期", first)
        self.assertIn("最长双连2期", first)
        self.assertIn("最长连跳(单双交替)5期", first)

    def test_small_history_and_no_raw_sequence(self):
        self.assertIn("最近：", build_stats_text([S, D, S]))
        results = ([S, D, D] * 700)
        messages = build_messages(results)
        joined = json.dumps(messages, ensure_ascii=False)
        self.assertLess(len(joined), 1500)  # statistics only, never the whole sequence
        self.assertNotIn("".join(results[-30:]), joined)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])

    def test_payload_shape(self):
        small = tron_ai._payload("gpt-4.1-mini", [])
        self.assertEqual(small["max_tokens"], 5)
        self.assertNotIn("max_completion_tokens", small)
        big = tron_ai._payload("gpt-5-mini-2025-08-07", [])
        self.assertNotIn("max_tokens", big)
        self.assertGreaterEqual(big["max_completion_tokens"], 256)


class RequestTest(unittest.TestCase):
    def settings(self, temp, **extra):
        return make_config(temp, **extra)

    def test_request_uses_url_header_and_parses(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=reply("单"))

        with tempfile.TemporaryDirectory() as temp:
            cfg = self.settings(temp, ai_base_url="https://relay.example/v1/")
            pick = asyncio.run(request_pick(cfg, [S, D] * 20, transport=httpx.MockTransport(handler)))
        self.assertEqual(pick, S)
        self.assertEqual(seen["url"], "https://relay.example/v1/chat/completions")
        self.assertEqual(seen["auth"], f"Bearer {DUMMY}")
        self.assertEqual(seen["body"]["model"], "gpt-4.1-mini")
        self.assertEqual(seen["body"]["max_tokens"], 5)
        self.assertNotIn(DUMMY, json.dumps(seen["body"]))  # key only in the header

    def test_config_defaults_and_secrecy(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = make_config(temp)
            self.assertEqual(cfg.ai_base_url, "https://api.mixroute.ai/v1")
            self.assertEqual(cfg.ai_model, "gpt-4.1-mini")
            self.assertTrue(cfg.tron_ai_enabled)
            self.assertNotIn(DUMMY, repr(cfg))
            self.assertFalse(make_config(temp, ai_api_key="").tron_ai_enabled)
            self.assertFalse(make_config(temp, tron_ai_predict=False).tron_ai_enabled)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_write_once_and_pre_draw_only(self):
        fill(self.store, 3)  # blocks 20, 40, 60
        self.assertTrue(self.store.record_tron_ai_prediction(80, S, "m1"))
        self.assertFalse(self.store.record_tron_ai_prediction(80, D, "m1"))  # write-once
        self.assertFalse(self.store.record_tron_ai_prediction(40, D, "m1"))  # draw already exists
        self.assertFalse(self.store.record_tron_ai_prediction(100, "x", "m1"))
        self.assertEqual(self.store.tron_ai_predictions(0, 999), {80: S})
        self.assertEqual(self.store.tron_ai_predictions(0, 999, "other"), {})
        self.assertEqual(self.store.tron_ai_predictions(0, 999, "m1"), {80: S})

    def test_ai_trend_runs_and_gaps(self):
        blocks = [20, 40, 60, 80, 100, 120]
        results = [S, S, D, S, D, D]
        stored = {20: S, 40: S, 60: S, 100: D, 120: D}  # 80 has none
        data = ai_trend(blocks, results, 6, stored)
        self.assertEqual([i["ok"] for i in data["items"]], [True, True, False, None, True, True])
        self.assertEqual((data["hits"], data["total"]), (4, 5))
        self.assertEqual(data["max_win"]["len"], 2)
        self.assertEqual(data["max_loss"]["len"], 1)
        self.assertIsNone(data["items"][3]["predicted"])


class PredictorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.temp.name)
        self.store = DirectoryStore(self.cfg.db_path)
        self.store.init()
        fill(self.store, 12)  # blocks 20..240; next is 260

    def tearDown(self):
        self.temp.cleanup()

    def run_schedule(self, predictor, rounds=1):
        async def go():
            for _ in range(rounds):
                predictor.schedule()
                if predictor.task:
                    await predictor.task
        asyncio.run(go())

    def test_stores_one_prediction_per_period(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=reply("双"))

        predictor = AiPredictor(self.cfg, self.store, transport=httpx.MockTransport(handler))
        self.run_schedule(predictor, rounds=3)
        self.assertEqual(len(calls), 1)  # one call per period
        self.assertEqual(self.store.tron_ai_predictions(260, 260), {260: D})
        self.assertEqual(predictor.stats["stored"], 1)

    def test_existing_prediction_is_never_overwritten_or_asked_again(self):
        self.store.record_tron_ai_prediction(260, S, self.cfg.ai_model)
        calls = []
        predictor = AiPredictor(
            self.cfg, self.store,
            transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(200, json=reply("双"))))
        self.run_schedule(predictor)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.tron_ai_predictions(260, 260), {260: S})

    def test_failures_store_nothing(self):
        def boom(request):
            raise httpx.ConnectError("down")

        for handler in (
            boom,
            lambda r: httpx.Response(500, json={"error": "x"}),
            lambda r: httpx.Response(200, json=reply("不知道")),
            lambda r: httpx.Response(200, text="not json"),
        ):
            predictor = AiPredictor(self.cfg, self.store, transport=httpx.MockTransport(handler))
            self.run_schedule(predictor)
            self.assertEqual(self.store.tron_ai_predictions(260, 260), {})
            self.assertEqual(predictor.stats["failed"], 1)

    def test_timeout_is_bounded(self):
        async def slow(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json=reply(S))

        cfg = make_config(self.temp.name, ai_timeout_seconds=0.05)
        predictor = AiPredictor(cfg, self.store, transport=httpx.MockTransport(slow))
        original = tron_ai.TIMEOUT_GRACE
        tron_ai.TIMEOUT_GRACE = 0
        try:
            self.run_schedule(predictor)
        finally:
            tron_ai.TIMEOUT_GRACE = original
        self.assertEqual(predictor.stats["failed"], 1)
        self.assertEqual(predictor.stats["stored"], 0)

    def test_disabled_without_key(self):
        cfg = make_config(self.temp.name, ai_api_key="")
        predictor = AiPredictor(cfg, self.store, transport=httpx.MockTransport(lambda r: 1 / 0))
        self.assertFalse(predictor.schedule())

    def test_hourly_cap(self):
        cfg = make_config(self.temp.name, tron_ai_max_per_hour=2)
        clock = [0.0]
        predictor = AiPredictor(
            cfg, self.store, clock=lambda: clock[0],
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply(S))))
        started = []
        for index in range(4):
            fill(self.store, 1, start=13 + index)
            async def go():
                started.append(predictor.schedule())
                if predictor.task:
                    await predictor.task
            asyncio.run(go())
        self.assertEqual(started, [True, True, False, False])
        clock[0] = 4000
        fill(self.store, 1, start=17)
        async def again():
            started.append(predictor.schedule())
            if predictor.task:
                await predictor.task
        asyncio.run(again())
        self.assertTrue(started[-1])

    def test_key_never_logged(self):
        def boom(request):
            raise httpx.ConnectError(f"cannot reach with {DUMMY}")  # even if a lib leaks it

        predictor = AiPredictor(self.cfg, self.store, transport=httpx.MockTransport(boom))
        with self.assertLogs("tg_directory_bot.tron_ai", level="WARNING") as logs:
            self.run_schedule(predictor)
        self.assertNotIn(DUMMY, "\n".join(logs.output))

    def test_prediction_failure_never_breaks_recording(self):
        class Broken:
            def schedule(self):
                raise RuntimeError("ai exploded")

        chain = FakeChain(head=400)
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=3, ai=Broken())
        with self.assertLogs("tg_directory_bot.tron_draw", level="ERROR"):
            asyncio.run(rec.step(400))
        self.assertGreater(self.store.tron_draw_total(), 12)

    def test_recorder_hook_calls_ai_only_for_new_draw(self):
        class Spy:
            n = 0

            def schedule(self):
                self.n += 1

        spy = Spy()
        chain = FakeChain(head=400)
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=3, ai=spy)
        asyncio.run(rec.step(400))
        first = spy.n
        asyncio.run(rec.step(400))
        self.assertGreaterEqual(first, 1)
        self.assertEqual(spy.n, first)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class AiWebTest(unittest.TestCase):
    def client(self, temp, **extra):
        from tg_directory_bot.web import create_app
        cfg = make_config(temp, **extra)
        return TestClient(create_app(cfg)), DirectoryStore(cfg.db_path)

    def test_api_ai_fields_and_no_key(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                fill(store, 40)
                # predictions made before their draws: insert directly for past blocks
                with store.connect() as conn:
                    for block in range(20 * 21, 20 * 41, 20):
                        pick = S if block % 40 else D
                        conn.execute(
                            "INSERT OR REPLACE INTO tron_ai_predictions (block_number,pick,model) VALUES (?,?,?)",
                            (block, pick, "gpt-4.1-mini"))
                store.record_tron_ai_prediction(20 * 41, D, "gpt-4.1-mini")
                text = client.get("/api/tron-draw?limit=30&ai_limit=20&mine_limit=20").text
                data = json.loads(text)
                self.assertNotIn(DUMMY, text)
                ai = data["ai"]
                self.assertTrue(ai["enabled"])
                self.assertEqual(ai["model"], "gpt-4.1-mini")
                self.assertEqual(ai["next"], {"block": 820, "pick": D})
                trend = ai["trend"]
                self.assertEqual(trend["limit"], 20)
                self.assertEqual(len(trend["items"]), 20)
                self.assertEqual(trend["total"], sum(1 for i in trend["items"] if i["ok"] is not None))
                self.assertEqual(trend["hits"], sum(1 for i in trend["items"] if i["ok"]))
                self.assertIn("max_win", trend)
                self.assertIn("max_loss", trend)
                self.assertEqual(len(data["blocks"]), len(data["cells"]))
                self.assertEqual(ai["window"]["limit"], 30)
                self.assertNotIn(DUMMY, client.get("/tron-draw").text)

    def test_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp, ai_api_key="")
            with client:
                fill(store, 10)
                self.assertIsNone(client.get("/api/tron-draw?ai_limit=20").json()["ai"])

    def test_block_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                fill(store, 5)
                with store.connect() as conn:
                    conn.execute("INSERT INTO tron_ai_predictions (block_number,pick,model) VALUES (60,'单','gpt-4.1-mini')")
                response = client.get("/api/tron-draw?block=60")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["block"], 60)
                self.assertEqual(len(data["hash"]), 64)
                self.assertIn(data["result"], (S, D))
                self.assertRegex(data["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
                self.assertEqual(data["ai"]["pick"], S)
                self.assertIn(data["ai"]["ok"], (True, False))
                self.assertIn("algo", data)
                self.assertEqual(client.get("/api/tron-draw?block=99999").status_code, 404)

    def test_page_layout_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp)
            with client:
                html = client.get("/tron-draw").text
        order = [html.index(t) for t in ("区块走势", "AI走势", "算法走势")]
        self.assertEqual(order, sorted(order))
        for token in (
            "AI预测", "算法预测", "srcA", "srcI", "aicard", "detailbox", "RETURN_MS", "pauseUntil",
            "AI参考", "开奖随机，不保证命中", "ai_limit", "K+\"src\"",
        ):
            self.assertIn(token, html)
        self.assertNotIn("我的走势", html)
        self.assertNotIn(DUMMY, html)


if __name__ == "__main__":
    unittest.main()
