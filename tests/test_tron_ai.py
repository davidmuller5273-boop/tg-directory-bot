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
        for label in ("最近10期", "最近30期", "最近50期", "最近100期", "最近300期", "最近500期", "最近1000期", "全部历史（1440期）"):
            self.assertIn(label, text)
        self.assertIn("最长单连4期", text)
        self.assertIn("最长双连2期", text)
        self.assertIn("最长连跳", text)
        self.assertIn("当前连龙：连", text)
        self.assertIn("%", text)
        self.assertEqual(text.splitlines()[-1], "最近10期（旧→新）：" + "".join(results[-10:]))

    def test_stats_values_exact(self):
        text = build_stats_text([S, S, S, D, D, S, D, S, D, D])
        first = text.splitlines()[0]
        self.assertIn("单5期(50%) 双5期(50%)", first)
        self.assertIn("当前连龙：连双2期", first)
        self.assertIn("最长龙3期（最长单连3期，最长双连2期）", first)
        self.assertIn("当前连跳：最新1期单双交替", first)
        self.assertIn("最长单连3期", first)
        self.assertIn("最长双连2期", first)
        self.assertIn("最长连跳(单双交替)5期", first)

    def test_windows_include_500_in_order(self):
        self.assertEqual(tron_ai.WINDOWS, (10, 30, 50, 100, 300, 500, 1000))
        text = build_stats_text([S, D, D] * 200)  # 600 results: 500 yes, 1000 not yet
        heads = [line.split("：")[0] for line in text.splitlines()]
        got = [h for h in heads if h in {f"最近{n}期" for n in tron_ai.WINDOWS}]
        self.assertEqual(got, ["最近10期", "最近30期", "最近50期", "最近100期", "最近300期", "最近500期"])
        self.assertIn("最近500期", build_stats_text([S, D] * 600))
        self.assertIn("最近1000期", build_stats_text([S, D] * 600))

    def test_every_window_has_longest_alternation_and_current_alternation(self):
        results = [S, D] * 600  # 1200, strictly alternating to the newest end
        lines = build_stats_text(results).splitlines()
        windows = [l for l in lines if l.startswith("最近") and "期：单" in l or l.startswith("全部历史")]
        self.assertEqual(len(windows), 8)  # 10,30,50,100,300,500,1000 + all
        sizes = [10, 30, 50, 100, 300, 500, 1000, 1200]
        for line, size in zip(windows, sizes):
            self.assertIn(f"最长连跳(单双交替){size}期", line)
            self.assertIn(f"当前连跳：最新{size}期单双交替", line)
            self.assertIn("最长龙1期", line)
            self.assertIn("当前连龙：连", line)
        mixed = [S, S, D, D] * 300 + [S, D, S, D, S, D]
        for line in build_stats_text(mixed).splitlines()[:8]:
            self.assertIn("当前连跳：最新7期单双交替", line)   # D,S,D,S,D,S,D at the end
            self.assertIn("最长连跳(单双交替)7期", line) if "最近10期" in line else None
        self.assertIn("最长龙2期（最长单连2期，最长双连2期）", build_stats_text(mixed).splitlines()[0])

    def test_streak_lines_exact(self):
        text = build_stats_text([S, S, S, D, D, S, D, S, D, D])
        self.assertIn("当前连龙：连双2期（历史最长龙3期；当前龙长度是历史最长的67%）；当前连跳（单双交替）已1期", text)
        self.assertIn("历史龙长度分布（全部历史，每条龙算一次）：1期×3 2期×2 3期×1", text)
        # runs S3 (longer than 2: continued) and D2 (ended at 2): 1 of 2
        self.assertIn("历史上龙长到2期（含）之后，下一期继续同方向：1次／共2次(50%)", text)
        alt = build_stats_text([S, S, D, S, D, S])
        self.assertIn("当前连龙：连单1期", alt)
        self.assertIn("当前连跳（单双交替）已5期", alt)
        long = build_stats_text([D] * 12 + [S, S, S])
        self.assertIn("9期及以上×1", long)

    def test_prompt_demands_weighing_and_forbids_reasoning(self):
        p = tron_ai.SYSTEM_PROMPT
        for needle in ("连龙", "历史最长", "龙长度分布", "连跳", "占比", "不要盲目顺着龙跟", "也不要盲目逆着龙反",
                       "独立判断", "不要因为上一期你答过某一边就重复它", "自我纠正", "只回答一个字：单 或 双", "不要理由"):
            self.assertIn(needle, p)
        self.assertNotIn("sk-", p)
        self.assertNotIn("gpt", p.casefold())

    def test_self_line_shows_own_streak_and_hits(self):
        own = [(S, S), (S, D), (D, D), (D, S), (D, D), (D, D)]
        text = build_stats_text([S, D] * 20, own)
        self.assertIn("你自己最近6次的回答（旧→新）：单单双双双双，对应实际开奖：单双双单双双，命中4/6；你已连续4次答双", text)
        self.assertNotIn("你自己最近", build_stats_text([S, D] * 20))
        capped = build_stats_text([S, D] * 20, [(S, S)] * 15)
        self.assertIn("最近10次的回答", capped)

    def test_small_history_and_no_raw_sequence(self):
        self.assertIn("最近：", build_stats_text([S, D, S]))
        results = ([S, D, D] * 700)
        messages = build_messages(results)
        joined = json.dumps(messages, ensure_ascii=False)
        self.assertLess(len(joined), 2600)  # statistics only, never the whole sequence
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

    def test_prompt_carries_own_past_answers_only_never_the_future(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content)["messages"][1]["content"])
            return httpx.Response(200, json=reply("双"))

        model = self.cfg.ai_model
        # earlier pre-draw answers for already drawn blocks 200, 220, 240 (+ an older one with a gap before it)
        with self.store.connect() as conn:
            for block, pick in ((160, S), (200, D), (220, D), (240, D)):
                conn.execute(
                    "INSERT INTO tron_ai_predictions (block_number, pick, model) VALUES (?, ?, ?)", (block, pick, model))
        predictor = AiPredictor(self.cfg, self.store, transport=httpx.MockTransport(handler))
        self.run_schedule(predictor)
        self.assertEqual(len(bodies), 1)
        results = {int(r["block_number"]): str(r["result"]) for r in self.store.tron_draw_recent(50)}
        want = "".join(results[b] for b in (200, 220, 240))
        self.assertIn(f"你自己最近3次的回答（旧→新）：双双双，对应实际开奖：{want}", bodies[0])  # 160 is cut off by the gap
        self.assertIn("你已连续3次答双", bodies[0])
        self.assertEqual(self.store.tron_ai_predictions(260, 260), {260: D})   # only the new period is written
        self.assertEqual(self.store.tron_ai_predictions(200, 240), {200: D, 220: D, 240: D})  # old records untouched

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
            "AI参考", "开奖随机，不保证命中", "afold", "mfold", "autobtn", "每期自动最优", "autoRun", "autoPending", "自动已更新参数", "优化建议，请联系上级，谢谢！", "redend", "abody", "mbody", "open.ai", "open.mine", "pendingJump", "ai_limit", "K+\"src\"",
        ):
            self.assertIn(token, html)
        self.assertNotIn("我的走势", html)
        # both trend sections start collapsed and sit right after the block trend
        self.assertIn('id="abody" hidden', html)
        self.assertIn('id="mbody" hidden', html)
        self.assertLess(html.index('id="road"'), html.index('id="afold"'))
        self.assertLess(html.index('id="afold"'), html.index('id="mfold"'))
        self.assertLess(html.index('id="mfold"'), html.index('id="setn"'))
        self.assertLess(html.index('id="setn"'), html.index('id="detailbox"'))
        self.assertIn('"&mine_limit="+(openS.mine?mine:0)', html)
        self.assertNotIn(DUMMY, html)
        # the page never renders the model name (the API field `model` stays)
        self.assertNotIn("x.ai.model", html)
        self.assertNotIn("a.model", html)
        self.assertNotIn("d.ai.model", html)
        self.assertNotIn("gpt-", html)
        # red notice is the very last block of the page
        self.assertGreater(html.index("优化建议，请联系上级，谢谢！"), html.index('id="detailbox"'))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class AiBroadcastApiTest(unittest.TestCase):
    def client(self, temp, **extra):
        from tg_directory_bot.web import create_app
        cfg = make_config(temp, **extra)
        return TestClient(create_app(cfg)), DirectoryStore(cfg.db_path)

    @staticmethod
    def seed(store, picks, model="gpt-4.1-mini", count=None):
        """Draw k (block 20*(k+1)) has result S for even k, D for odd k; picks[k] is the stored pick or None."""
        count = count or len(picks)
        for k in range(count):
            n = 20 * (k + 1)
            store.record_tron_draw(n, make_hash(n, "1" if k % 2 == 0 else "2"), 1_700_000_000_000 + k * 60_000)
        with store.connect() as conn:
            for k in range(min(count, len(picks))):
                if picks[k]:
                    conn.execute(
                        "INSERT INTO tron_ai_predictions (block_number,pick,model) VALUES (?,?,?)",
                        (20 * (k + 1), picks[k], model))

    def test_disabled_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp, ai_api_key="")
            with client:
                response = client.get("/api/tron-draw/ai-broadcast")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"enabled": False, "reason": "AI_API_KEY 未配置"})
                self.assertEqual(response.headers["cache-control"], "no-store")
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp, tron_ai_predict=False)
            with client:
                data = client.get("/api/tron-draw/ai-broadcast").json()
                self.assertFalse(data["enabled"])
                self.assertIn("TRON_AI_PREDICT", data["reason"])

    def test_empty_database(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp)
            with client:
                data = client.get("/api/tron-draw/ai-broadcast").json()
        self.assertTrue(data["enabled"])
        self.assertEqual((data["latest_block"], data["latest_ts"], data["next_block"]), (0, 0, 0))
        self.assertIsNone(data["next_pick"])
        self.assertEqual(data["recent"], [])
        self.assertEqual(data["streak"], {"kind": "none", "count": 0})
        self.assertEqual(data["overall"], {"hits": 0, "total": 0})

    def test_fields_and_semantics(self):
        # 15 draws: results S,D,S,D,...; no prediction for k=0..2 (before AI was on) and k=8
        picks = [None, None, None, S, S, D, D, D, None, S, S, D, D, S, S]
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                self.seed(store, picks)
                store.record_tron_ai_prediction(20 * 16, D, "gpt-4.1-mini")        # next period
                # other model's stored prediction must be ignored
                with store.connect() as conn:
                    conn.execute("INSERT INTO tron_ai_predictions (block_number,pick,model) VALUES (?,?,?)",
                                 (20 * 1, S, "other-model"))
                response = client.get("/api/tron-draw/ai-broadcast")
                data = response.json()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(data), {
            "enabled", "reason", "latest_block", "latest_ts", "next_block", "next_pick",
            "recent", "window", "overall", "streak"})
        self.assertEqual((data["enabled"], data["reason"]), (True, ""))
        self.assertEqual(data["latest_block"], 300)
        self.assertEqual(data["latest_ts"], 1_700_000_000_000 + 14 * 60_000)
        self.assertEqual((data["next_block"], data["next_pick"]), (320, D))
        result = lambda k: S if k % 2 == 0 else D  # noqa: E731
        scored = [(20 * (k + 1), picks[k], result(k)) for k in range(15) if picks[k]]
        self.assertEqual(len(scored), 11)
        recent = data["recent"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent, [{"block": b, "result": r, "pick": p} for b, p, r in scored[-10:]])
        self.assertEqual([item["block"] for item in recent], sorted(item["block"] for item in recent))
        self.assertTrue(all(item["result"] in (S, D) and item["pick"] in (S, D) for item in recent))
        hits = sum(1 for _, p, r in scored if p == r)
        # L W W L W L W W L L W (gaps skipped): longest hit run 2, longest miss run 2
        self.assertEqual(data["window"], {"periods": 180, "hits": hits, "total": 11, "max_win": 2, "max_loss": 2})
        self.assertEqual(data["overall"], {"hits": hits, "total": 11})
        # newest scored: k=14 pick S result S (win), k=13 pick S result D (loss) -> win streak of 1
        self.assertEqual(data["streak"], {"kind": "win", "count": 1})

    def test_window_vs_overall_and_streak_skips_gaps(self):
        count = 200
        # the first 10 predictions are wrong but fall outside the 180-draw window
        picks = []
        for k in range(count):
            real = S if k % 2 == 0 else D
            wrong = D if real == S else S
            picks.append(wrong if k < 10 else real)
        picks[count - 1] = None  # newest has no prediction: skipped
        picks[count - 2] = None
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                self.seed(store, picks)
                data = client.get("/api/tron-draw/ai-broadcast").json()
        self.assertEqual(data["overall"], {"hits": 188, "total": 198})   # 198 scored, 10 wrong
        self.assertEqual(data["window"]["periods"], 180)
        self.assertEqual(data["window"]["total"], 178)                   # 180 newest draws minus 2 gaps
        self.assertEqual(data["window"]["hits"], 178)                    # the 10 wrong ones are older than the 180-draw window
        self.assertEqual(data["streak"], {"kind": "win", "count": 178})
        self.assertEqual(len(data["recent"]), 10)
        self.assertEqual(data["recent"][-1]["block"], 20 * (count - 2))  # k=197 is the newest with a pick

    def test_loss_streak_and_no_key_leak(self):
        picks = [S, D, S, D, S, S, D, S]
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                self.seed(store, picks)
                response = client.get("/api/tron-draw/ai-broadcast")
        data = response.json()
        self.assertEqual(data["streak"], {"kind": "loss", "count": 3})
        self.assertNotIn(DUMMY, response.text)
        self.assertIsNone(data["next_pick"])

    def test_not_in_nginx_allowlist(self):
        root = Path(__file__).resolve().parents[1]
        conf = (root / "deploy/nginx/tron-draw.conf.example").read_text()
        self.assertNotIn("ai-broadcast", conf)


if __name__ == "__main__":
    unittest.main()
