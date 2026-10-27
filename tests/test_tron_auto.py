"""自动最优 prediction: stored before each draw (write-once), no look-ahead, API + page tokens. Mock data only."""
import asyncio
import json
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from tg_directory_bot import tron_draw
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.tron_draw import (
    DRAW_INTERVAL, TronDrawRecorder, auto_best_pick, best_params, record_next_auto_prediction,
)
from tests.test_tron_ai import D, DUMMY, S, make_config
from tests.test_tron_draw import make_hash


def new_store(temp):
    store = DirectoryStore(Path(temp) / "db.sqlite3")
    store.init()
    return store


def fill_random(store, count, seed=5, start=1):
    rng = random.Random(seed)
    out = []
    for i in range(count):
        n = DRAW_INTERVAL * (start + i)
        digit = rng.randint(0, 9)
        store.record_tron_draw(n, make_hash(n, str(digit)), 1_700_000_000_000 + n)
        out.append(S if digit % 2 else D)
    return out


def results_of(store):
    return [str(r["result"]) for r in store.tron_draw_recent(5000)]


class AutoStorageTest(unittest.TestCase):
    def test_write_once_and_never_after_the_draw(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 10)
            nxt = 20 * 11
            self.assertTrue(store.record_tron_auto_prediction(nxt, S, '{"a":1}'))
            self.assertFalse(store.record_tron_auto_prediction(nxt, D, '{"a":2}'))  # write-once
            self.assertEqual(store.tron_auto_predictions(nxt, nxt), {nxt: S})
            self.assertEqual(json.loads(store.tron_auto_params(nxt)), {"a": 1})
            self.assertFalse(store.record_tron_auto_prediction(20 * 5, S))  # block already drawn: no look-ahead
            self.assertFalse(store.record_tron_auto_prediction(nxt + 20, "x"))
            self.assertEqual(store.tron_auto_params(999999), "")


class AutoPickTest(unittest.TestCase):
    def test_needs_history(self):
        self.assertIsNone(auto_best_pick([S, D] * 100))  # < 300 periods

    def test_uses_best_params_of_given_history_only(self):
        rng = random.Random(1)
        results = [rng.choice([S, D]) for _ in range(420)]
        pick, params = auto_best_pick(results)
        self.assertIn(pick, (S, D))
        self.assertEqual(params, best_params(results)["final"]["params"])
        self.assertEqual(set(params), {"streak_n", "mode", "recent_m", "minority_m"})
        # deterministic
        self.assertEqual(auto_best_pick(results), (pick, params))

    def test_record_next_is_pre_draw_write_once_and_no_look_ahead(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            self.assertFalse(record_next_auto_prediction(store))  # empty
            fill_random(store, 310)
            self.assertTrue(record_next_auto_prediction(store))
            nxt = 20 * 311
            stored = store.tron_auto_predictions(nxt, nxt)
            self.assertEqual(stored[nxt], auto_best_pick(results_of(store))[0])
            self.assertFalse(record_next_auto_prediction(store))  # second call: nothing new
            before = dict(stored)
            # the draw arrives (opposite of the stored pick): the stored record must not change
            digit = 2 if stored[nxt] == S else 1
            store.record_tron_draw(nxt, make_hash(nxt, str(digit)), 1_700_000_000_000 + nxt)
            self.assertEqual(store.tron_auto_predictions(nxt, nxt), before)
            self.assertFalse(store.record_tron_auto_prediction(nxt, S))
            # the next period is predicted from history that includes the new draw only
            self.assertTrue(record_next_auto_prediction(store))
            nxt2 = nxt + 20
            hist = results_of(store)
            self.assertEqual(len(hist), 311)
            self.assertEqual(store.tron_auto_predictions(nxt2, nxt2)[nxt2], auto_best_pick(hist)[0])

    def test_too_little_history_stores_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 120)
            self.assertFalse(record_next_auto_prediction(store))
            self.assertEqual(store.tron_auto_predictions(0, 10**9), {})


class AutoRecorderTest(unittest.TestCase):
    def test_background_job_swallows_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            rec = TronDrawRecorder(object(), store, auto_best=True)
            with mock.patch.object(tron_draw, "record_next_auto_prediction", side_effect=RuntimeError("boom")):
                asyncio.run(rec._auto_job())  # must not raise

    def test_job_stores_prediction(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 305)
            rec = TronDrawRecorder(object(), store, auto_best=True)
            asyncio.run(rec._auto_job())
            self.assertIn(20 * 306, store.tron_auto_predictions(20 * 306, 20 * 306))

    def test_off_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = TronDrawRecorder(object(), new_store(temp))
            self.assertFalse(rec.auto_best)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class AutoWebTest(unittest.TestCase):
    def client(self, temp, **extra):
        from tg_directory_bot.web import create_app
        cfg = make_config(temp, **extra)
        return TestClient(create_app(cfg)), DirectoryStore(cfg.db_path)

    def test_api_auto_fields_start_empty_then_accumulate(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                fill_random(store, 320)
                data = client.get("/api/tron-draw?limit=100&auto_limit=50").json()
                auto = data["auto"]
                nxt = data["next_draw_block"]
                self.assertTrue(auto["enabled"])
                self.assertEqual(auto["next"]["block"], nxt)
                self.assertIn(auto["next"]["pick"], (S, D))
                self.assertEqual(set(auto["params"]), {"streak_n", "mode", "recent_m", "minority_m"})
                # an honest record starts empty: nothing is back-filled for earlier periods
                self.assertEqual(auto["window"]["total"], 0)
                self.assertIsNone(auto["prev"])
                self.assertEqual(len(auto["trend"]["items"]), 50)
                self.assertTrue(all(i["predicted"] is None for i in auto["trend"]["items"]))
                self.assertEqual(store.tron_auto_predictions(0, 10**9), {nxt: auto["next"]["pick"]})
                # same pick on every poll within the period (write-once)
                self.assertEqual(client.get("/api/tron-draw").json()["auto"]["next"], auto["next"])
                # the draw arrives, then the next period is stored and the old one is judged
                digit = 2 if auto["next"]["pick"] == S else 1
                store.record_tron_draw(nxt, make_hash(nxt, str(digit)), 1_700_000_000_000 + nxt)
                data2 = client.get("/api/tron-draw?auto_limit=50").json()
                self.assertEqual(data2["auto"]["next"]["block"], nxt + 20)
                self.assertEqual(data2["auto"]["prev"], {"block": nxt, "pick": auto["next"]["pick"], "ok": False})
                self.assertEqual(data2["auto"]["window"]["total"], 1)
                self.assertEqual(data2["auto"]["window"]["hits"], 0)
                t = data2["auto"]["trend"]
                self.assertEqual((t["total"], t["hits"], t["max_loss"]["len"]), (1, 0, 1))
                self.assertEqual(client.get(f"/api/tron-draw?block={nxt}").json()["auto"], {"pick": auto["next"]["pick"], "ok": False})

    def test_auto_enabled_without_ai_key_and_small_history(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp, ai_api_key="")
            with client:
                fill_random(store, 40)
                data = client.get("/api/tron-draw?auto_limit=20").json()
                self.assertIsNone(data["ai"])
                self.assertTrue(data["auto"]["enabled"])
                self.assertIsNone(data["auto"]["next"])  # < 300 periods: no prediction, no fake one
                self.assertEqual(data["auto"]["window"]["total"], 0)

    def test_ai_prev_and_broadcast_contract_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp)
            with client:
                fill_random(store, 40)
                store.record_tron_ai_prediction(20 * 41, S, "gpt-4.1-mini")
                fill_random(store, 1, seed=9, start=41)
                data = client.get("/api/tron-draw").json()
                self.assertEqual(data["ai"]["prev"]["block"], 820)
                self.assertIn(data["ai"]["prev"]["ok"], (True, False))
                text = client.get("/api/tron-draw").text
                self.assertNotIn(DUMMY, text)
                self.assertEqual(client.get("/api/tron-draw/ai-broadcast").status_code, 200)

    def test_page_tokens_sources_icons_and_tooltips(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp)
            with client:
                html = client.get("/tron-draw").text
        # three sources in this order
        order = [html.index(f'id="{i}"') for i in ("srcI", "srcO", "srcA")]
        self.assertEqual(order, sorted(order))
        for token in (
            'data-src="ai"', 'data-src="auto"', 'data-src="input"', ">AI预测<", ">自动最优算法预测<", ">输入参数算法预测<",
            "本期预测", "curpred", "shown.", "pickOf", "rememberAll", "renderPre", "auto_limit", "open.auto",
        ):
            self.assertIn(token, html)
        # 自动最优走势 is collapsed and sits between AI走势 and 算法走势, under 区块走势
        self.assertIn('id="obody" hidden', html)
        order = [html.index(t) for t in ('id="road"', 'id="afold"', 'id="ofold"', 'id="mfold"', 'id="setn"')]
        self.assertEqual(order, sorted(order))
        self.assertGreater(html.index("优化建议，请联系上级，谢谢！"), html.index('id="detailbox"'))
        # circle icons instead of plain 单/双 text, no emoji dependency
        for token in (".ic.s", ".ic.d", "function ic(", "<i class='ic "):
            self.assertIn(token, html)
        self.assertNotIn("gpt-", html)
        self.assertNotIn(DUMMY, html)

    def test_every_term_has_a_hover_explanation(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp)
            with client:
                html = client.get("/tron-draw").text
        self.assertIn("window.TRONDRAW_TIP_MS||1000", html)  # ~1 s hover delay
        self.assertIn('"touchstart"', html)  # tap / long-press on phones
        tips = dict(re.findall(r"^ (\w+):\"([^\"]+)\"", html, re.M))
        for key in (
            "n", "mode", "m", "mn", "best", "auto", "final", "thRef", "thBt", "trend", "dragon", "ratio", "minority",
            "ai", "autorow", "longest", "alt", "snd", "tsA", "tsB", "judge", "srcl", "sai", "sauto", "sinput",
            "foldAi", "foldAuto", "foldMine", "maxwin", "maxloss",
        ):
            self.assertIn(key, tips)
            self.assertGreater(len(tips[key]), 12)
        self.assertIn("少数方M=20", tips["mn"])
        # the parameter labels AND their inputs carry the explanation
        for tk in ("n", "mode", "m", "mn"):
            self.assertGreaterEqual(html.count(f'data-tk="{tk}"'), 2)
        for tk in ("snd", "tsA", "tsB", "judge", "srcl", "sai", "sauto", "sinput", "best", "auto", "thRef", "thBt", "final"):
            self.assertIn(f'data-tk="{tk}"', html)


if __name__ == "__main__":
    unittest.main()
