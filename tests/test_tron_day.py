"""一天最优 (newest 1440 periods only) + actual result in every trend cell. Mock data only."""
import asyncio
import json
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
from tg_directory_bot.tron_draw import (
    DAY_PERIODS, DRAW_INTERVAL, TronDrawRecorder, auto_best_pick, day_best_pick, record_next_auto_prediction,
    record_next_day_prediction,
)
from tests.test_tron_ai import D, S, make_config
from tests.test_tron_auto import fill_random, new_store, results_of
from tests.test_tron_draw import make_hash


class DayStorageTest(unittest.TestCase):
    def test_write_once_and_never_after_the_draw(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 10)
            nxt = 20 * 11
            self.assertTrue(store.record_tron_day_prediction(nxt, S, '{"a":1}'))
            self.assertFalse(store.record_tron_day_prediction(nxt, D, '{"a":2}'))
            self.assertEqual(store.tron_day_predictions(nxt, nxt), {nxt: S})
            self.assertEqual(json.loads(store.tron_day_params(nxt)), {"a": 1})
            self.assertFalse(store.record_tron_day_prediction(20 * 5, S))   # already drawn
            self.assertFalse(store.record_tron_day_prediction(nxt + 20, "x"))
            self.assertEqual(store.tron_day_params(999999), "")

    def test_own_table_not_mixed_with_auto(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 10)
            nxt = 20 * 11
            store.record_tron_day_prediction(nxt, S, "{}")
            self.assertEqual(store.tron_auto_predictions(0, 10**9), {})
            store.record_tron_auto_prediction(nxt, D, "{}")
            self.assertEqual(store.tron_day_predictions(0, 10**9), {nxt: S})
            self.assertEqual(store.tron_auto_predictions(0, 10**9), {nxt: D})
            fill_random(store, 1, seed=3, start=11)
            self.assertEqual(store.tron_day_scored()[0][0], nxt)
            hits, total = store.tron_day_overall()
            self.assertEqual(total, 1)
            self.assertIn(hits, (0, 1))


class DayWindowTest(unittest.TestCase):
    def test_uses_only_the_newest_1440_periods(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 1600, seed=11)
            hist = results_of(store)
            self.assertEqual(len(hist), 1600)
            self.assertTrue(record_next_day_prediction(store))
            nxt = 20 * 1601
            stored = store.tron_day_predictions(nxt, nxt)[nxt]
            expect = auto_best_pick(hist[-DAY_PERIODS:])
            self.assertEqual((stored, json.loads(store.tron_day_params(nxt))), (expect[0], expect[1]))
            # the 160 oldest periods are ignored: changing them cannot change the result
            altered = [S if x == D else D for x in hist[:160]] + hist[160:]
            self.assertEqual(day_best_pick(altered), day_best_pick(hist))
            # while the same change DOES count for a search over all history
            self.assertEqual(day_best_pick(hist), auto_best_pick(hist[-1440:]))

    def test_no_look_ahead(self):
        """The pick for the period after N results only depends on those N results (never a later one)."""
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 400, seed=2)
            hist = results_of(store)
            self.assertTrue(record_next_day_prediction(store))
            first = store.tron_day_predictions(20 * 401, 20 * 401)[20 * 401]
            self.assertEqual(first, day_best_pick(hist)[0])
            # a later draw (and its own prediction) does not rewrite the stored one
            fill_random(store, 1, seed=8, start=401)
            self.assertTrue(record_next_day_prediction(store))
            self.assertEqual(store.tron_day_predictions(20 * 401, 20 * 401)[20 * 401], first)
            self.assertFalse(record_next_day_prediction(store))   # nothing new until the next draw

    def test_short_history(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 299)
            self.assertFalse(record_next_day_prediction(store))
            self.assertEqual(store.tron_day_predictions(0, 10**9), {})
            fill_random(store, 1, seed=4, start=300)               # exactly 300: allowed (uses what exists)
            self.assertTrue(record_next_day_prediction(store))
            self.assertIsNone(day_best_pick([S, D] * 100))

    def test_empty_store(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(record_next_day_prediction(new_store(temp)))


class DayRecorderTest(unittest.TestCase):
    def test_job_stores_both_and_one_failure_does_not_stop_the_other(self):
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 305)
            rec = TronDrawRecorder(object(), store, auto_best=True)
            asyncio.run(rec._auto_job())
            self.assertIn(20 * 306, store.tron_auto_predictions(20 * 306, 20 * 306))
            self.assertIn(20 * 306, store.tron_day_predictions(20 * 306, 20 * 306))
        with tempfile.TemporaryDirectory() as temp:
            store = new_store(temp)
            fill_random(store, 305)
            rec = TronDrawRecorder(object(), store, auto_best=True)
            with mock.patch.object(tron_draw, "record_next_auto_prediction", side_effect=RuntimeError("boom")):
                asyncio.run(rec._auto_job())                        # must not raise
            self.assertIn(20 * 306, store.tron_day_predictions(20 * 306, 20 * 306))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class DayWebTest(unittest.TestCase):
    def client(self, temp, **extra):
        from tg_directory_bot.web import create_app
        cfg = make_config(temp, **extra)
        from tg_directory_bot.storage import DirectoryStore
        return TestClient(create_app(cfg)), DirectoryStore(cfg.db_path)

    def test_payload_day_object_and_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp, ai_api_key="")
            with client:
                fill_random(store, 40)
                data = client.get("/api/tron-draw?day_limit=20").json()
                self.assertTrue(data["day"]["enabled"])
                self.assertIsNone(data["day"]["next"])                # < 300 periods
                fill_random(store, 300, seed=6, start=41)
                data = client.get("/api/tron-draw?day_limit=50").json()
                day = data["day"]
                nxt = data["next_draw_block"]
                self.assertEqual(day["next"]["block"], nxt)
                self.assertIn(day["next"]["pick"], (S, D))
                self.assertEqual(set(day["params"]), {"streak_n", "mode", "recent_m", "minority_m"})
                self.assertEqual(len(day["trend"]["items"]), 50)
                self.assertTrue(all(i["predicted"] is None for i in day["trend"]["items"]))   # starts empty
                self.assertTrue(all(i["result"] in (S, D) for i in day["trend"]["items"]))     # cells show the draw
                self.assertEqual(client.get("/api/tron-draw").json()["day"]["next"], day["next"])  # write-once
                digit = 2 if day["next"]["pick"] == S else 1
                store.record_tron_draw(nxt, make_hash(nxt, str(digit)), 1_700_000_000_000 + nxt)
                data2 = client.get("/api/tron-draw?day_limit=50").json()
                self.assertEqual(data2["day"]["prev"], {"block": nxt, "pick": day["next"]["pick"], "ok": False})
                last = data2["day"]["trend"]["items"][-1]
                self.assertEqual((last["block"], last["predicted"], last["ok"]), (nxt, day["next"]["pick"], False))
                self.assertEqual(last["result"], D if day["next"]["pick"] == S else S)
                self.assertEqual(client.get(f"/api/tron-draw?block={nxt}").json()["day"],
                                 {"pick": day["next"]["pick"], "ok": False})
                self.assertIsNone(client.get(f"/api/tron-draw?block={nxt - 20}").json()["day"])

    def test_day_feed_shape_and_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp, ai_api_key="")
            with client:
                data = client.get("/api/tron-draw/day-broadcast").json()
                self.assertEqual(set(data), {"enabled", "reason"})
                self.assertFalse(data["enabled"])
                self.assertIn("300", data["reason"])
                fill_random(store, 120)
                self.assertIn("120", client.get("/api/tron-draw/day-broadcast").json()["reason"])
                fill_random(store, 200, seed=9, start=121)
                response = client.get("/api/tron-draw/day-broadcast")
                feed = response.json()
                auto = client.get("/api/tron-draw/auto-broadcast").json()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(feed), set(auto))                     # same shape as auto-broadcast
        self.assertEqual(set(feed), {
            "enabled", "reason", "latest_block", "latest_ts", "next_block", "next_pick",
            "recent", "window", "overall", "streak"})
        self.assertTrue(feed["enabled"])
        self.assertEqual((feed["latest_block"], feed["next_block"]), (20 * 320, 20 * 321))
        self.assertIn(feed["next_pick"], (S, D))
        self.assertEqual(feed["recent"], [])
        self.assertEqual(feed["window"], {"periods": 180, "hits": 0, "total": 0, "max_win": 0, "max_loss": 0})
        self.assertEqual(feed["overall"], {"hits": 0, "total": 0})

    def test_day_feed_normal_uses_its_own_records(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.client(temp, ai_api_key="")
            with client:
                fill_random(store, 320)
                for k in range(300, 310):
                    n = 20 * (k + 1)
                    with store.connect() as conn:
                        conn.execute("INSERT INTO tron_day_predictions (block_number,pick,params) VALUES (?,?,?)", (n, S, "{}"))
                feed = client.get("/api/tron-draw/day-broadcast").json()
                auto = client.get("/api/tron-draw/auto-broadcast").json()
        self.assertEqual(feed["overall"]["total"], 10)
        self.assertEqual(len(feed["recent"]), 10)
        self.assertTrue(all(r["pick"] == S for r in feed["recent"]))
        self.assertEqual(auto["overall"]["total"], 0)               # the auto record is separate

    def test_not_reachable_through_the_public_allowlist(self):
        conf = (Path(__file__).resolve().parent.parent / "deploy/nginx/tron-draw.conf.example").read_text()
        pattern = re.search(r"location ~ (\^/api/tron-draw[^ ]*) \{", conf).group(1)
        self.assertFalse(re.search(pattern, "/api/tron-draw/day-broadcast"))
        self.assertNotIn("day-broadcast", conf)

    def test_page_cells_card_button_and_tips(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.client(temp)
            with client:
                html = client.get("/tron-draw").text
        # the cell: actual result circle + hit/miss mark (dot for no prediction), hover title with both
        for token in ("mk rc", "ic(it.result)", "✅", "❌", "<span class=mm>", "实际开奖", "cellLegend",
                      ".mk.rc .ic", "--single:#12b886;--double:#f03e3e;--t-single:#1fb36b;--t-double:#e53935", ".mk.rc .mm{display:block;flex:none;width:22px;height:26px", ".mgrid .mk{min-width:26px;height:26px;align-self:start}", ".mk.rc .ic{flex:none;width:18px;height:18px", ".mk.rc .ic.s{background:var(--t-single)}"):
            self.assertIn(token, html)
        self.assertNotIn("<span class=mm>·</span>", html)             # no prediction = empty mark slot (keeps width/alignment)
        self.assertIn(".mk.rc{width:48px;height:26px;padding:0;flex-direction:row", html)   # mark to the RIGHT of the circle
        self.assertNotIn("flex-direction:column;justify-content:flex-start;gap:2px", html)
        self.assertIn("grid-template-rows:repeat(6,26px);row-gap:6px;column-gap:16px", html)  # tight inside, airy between
        self.assertIn(".bead{width:26px;height:26px;border-radius:50%;color:#fff;font-size:13px;font-weight:600", html)  # 区块走势 original size
        self.assertIn(".bead.s{background:var(--single)}", html)      # 区块走势 keeps the original vivid colors
        self.assertNotIn(".bead.s{background:var(--t-single)}", html)
        self.assertIn(".grid{display:grid;grid-auto-flow:column;gap:4px", html)     # 区块走势 layout untouched
        self.assertIn('g.style.gridTemplateRows="18px repeat("+d.max_rows+",26px)"', html)
        self.assertEqual(html.count('data-tk="cellLegend"'), 4)      # AI / 自动最优 / 一天最优 / 算法走势
        # source buttons in the requested order
        order = [html.index(f'id="{i}"') for i in ("srcI", "srcO", "srcD", "srcA")]
        self.assertEqual(order, sorted(order))
        for token in ('data-src="day"', ">一天最优算法预测<", "day_limit", "open.day", "daylimit", 'watchRoad($("droad"))',
                      'bindFold("day")', "一天最优参考", 'preRowHtml(d.day,"一天最优"', 'x.day'):
            self.assertIn(token, html)
        # card sits after 自动最优走势 and before 算法走势; collapsed
        self.assertIn('id="dbody" hidden', html)
        pos = [html.index(t) for t in ('id="ofold"', 'id="dfold"', 'id="mfold"')]
        self.assertEqual(pos, sorted(pos))
        tips = dict(re.findall(r"^ (\w+):\"([^\"]+)\"", html, re.M))
        for key in ("sday", "dayrow", "foldDay", "cellLegend"):
            self.assertGreater(len(tips[key]), 12)
        self.assertIn("1440", tips["sday"])
        self.assertIn('data-tk="sday"', html)
        self.assertNotIn("tron-draw/day-broadcast", html)


if __name__ == "__main__":
    unittest.main()
