import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from tg_directory_bot.chain import ChainQueryError
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.tron_draw import (
    TronDrawRecorder, bead_road, draw_result, is_draw_block, last_digit, parse_limit,
)
from tg_directory_bot import tron_draw as td
from tg_directory_bot.tron_scanner import ScanDB, TronBlockScanner


def make_hash(number: int, tail: str = "") -> str:
    base = f"{number:x}".rjust(64, "0")
    return (base[: 64 - len(tail)] + tail) if tail else base


class FakeChain:
    def __init__(self, head, hashes=None, fail=()):
        self.head = head
        self.hashes = hashes or {}
        self.fail = set(fail)
        self.header_calls = []

    async def tron_head(self):
        return self.head, 1_700_000_000_000

    async def tron_block_header(self, number):
        self.header_calls.append(number)
        if number in self.fail:
            raise ChainQueryError("busy")
        return self.hashes.get(number) or make_hash(number, "1"), 1_700_000_000_000 + number


class RulesTest(unittest.TestCase):
    def test_draw_blocks(self):
        self.assertTrue(all(is_draw_block(n) for n in (20, 40, 60, 80, 100, 86796340 - 40 + 20)))
        self.assertFalse(any(is_draw_block(n) for n in (0, 1, 19, 21, 50, 86796341)))

    def test_last_digit_ignores_letters(self):
        self.assertEqual(last_digit("0" * 59 + "a3B45"), 5)
        self.assertEqual(draw_result("0" * 59 + "a3B45"), (5, "单"))
        self.assertEqual(draw_result("0" * 60 + "abcd"), (0, "双"))
        self.assertEqual(draw_result("f" * 60 + "12ef"), (2, "双"))
        self.assertEqual(draw_result("ff12ab7cde"), (7, "单"))
        self.assertIsNone(draw_result("abcdef"))
        for digit in "13579":
            self.assertEqual(draw_result("ab" + digit)[1], "单")
        for digit in "02468":
            self.assertEqual(draw_result("ab" + digit)[1], "双")

    def test_parse_limit(self):
        self.assertEqual(parse_limit("abc"), 100)
        self.assertEqual(parse_limit("0"), 1)
        self.assertEqual(parse_limit("999999"), 5000)
        self.assertEqual(parse_limit(" 50 "), 50)


class BeadRoadTest(unittest.TestCase):
    def pos(self, results, rows=6):
        cells, columns = bead_road(results, rows)
        return [(c["c"], c["r"]) for c in cells], columns

    def test_stack_and_flip(self):
        pos, cols = self.pos(list("单单单双双单"))
        self.assertEqual(pos, [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (2, 0)])
        self.assertEqual(cols, 3)

    def test_tail_turns_right_after_six_rows(self):
        pos, cols = self.pos(["单"] * 9 + ["双", "双"])
        self.assertEqual(pos[:6], [(0, r) for r in range(6)])
        self.assertEqual(pos[6:9], [(1, 5), (2, 5), (3, 5)])
        self.assertEqual(pos[9:], [(4, 0), (4, 1)])
        self.assertEqual(cols, 5)

    def test_empty(self):
        self.assertEqual(bead_road([]), ([], 0))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_record_is_permanent_and_idempotent(self):
        h = make_hash(100, "a3B45")
        self.assertTrue(self.store.record_tron_draw(100, h, 1234))
        self.assertFalse(self.store.record_tron_draw(100, h, 1234))
        self.assertFalse(self.store.record_tron_draw(101, h, 1))  # not a draw block
        self.assertFalse(self.store.record_tron_draw(120, "abcdef", 1))  # no digit
        row = self.store.tron_draw_recent(10)[0]
        self.assertEqual((row["block_number"], row["last_digit"], row["result"]), (100, 5, "单"))
        self.assertEqual(row["block_timestamp_ms"], 1234)
        # re-init (restart / deploy) keeps history
        DirectoryStore(Path(self.temp.name) / "db.sqlite3").init()
        self.assertEqual(self.store.tron_draw_total(), 1)

    def test_recent_returns_latest_oldest_first(self):
        for n in range(20, 220, 20):
            self.store.record_tron_draw(n, make_hash(n, "1"), n)
        rows = self.store.tron_draw_recent(3)
        self.assertEqual([r["block_number"] for r in rows], [160, 180, 200])


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def run_steps(self, recorder, head, times=50):
        total = 0
        for _ in range(times):
            total += asyncio.run(recorder.step(head))
        return total

    def test_initial_backfill_newest_first_and_rate_limited(self):
        chain = FakeChain(head=1000)
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=5, per_step=2)
        self.assertEqual(asyncio.run(rec.step(1000)), 2)
        self.assertEqual(chain.header_calls, [980, 960])  # at most per_step requests / step
        self.run_steps(rec, 1000)
        self.assertEqual(sorted(chain.header_calls), [900, 920, 940, 960, 980])
        self.assertEqual(self.store.tron_draw_total(), 5)

    def test_confirmations_delay_latest_draw(self):
        chain = FakeChain(head=1010)
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=1)
        self.run_steps(rec, 1010)
        self.assertEqual(chain.header_calls, [980])  # 1000 not yet 20 blocks deep

    def test_backfills_gap_after_downtime_without_refetching(self):
        chain = FakeChain(head=400)
        rec = TronDrawRecorder(chain, self.store, initial_backfill=3, per_step=10)
        self.run_steps(rec, 400, 3)
        self.assertEqual(sorted(self.store.tron_draw_existing(0, 10**9)), [340, 360, 380])
        # bot down for a while, restarts with a fresh recorder
        chain2 = FakeChain(head=600)
        rec2 = TronDrawRecorder(chain2, self.store, initial_backfill=3, per_step=10)
        self.run_steps(rec2, 600, 5)
        self.assertEqual(sorted(chain2.header_calls), [400, 420, 440, 460, 480, 500, 520, 540, 560, 580])
        self.assertEqual(
            sorted(self.store.tron_draw_existing(0, 10**9)), list(range(340, 600, 20)))
        # nothing missing -> no more requests
        chain3 = FakeChain(head=600)
        self.run_steps(TronDrawRecorder(chain3, self.store, initial_backfill=3), 600, 3)
        self.assertEqual(chain3.header_calls, [])

    def test_failed_fetch_is_retried_and_does_not_block_loop(self):
        chain = FakeChain(head=200, fail={160})
        rec = TronDrawRecorder(chain, self.store, initial_backfill=2, per_step=3)
        self.assertEqual(asyncio.run(rec.step(200)), 1)  # 180 stored, 160 failed
        self.assertEqual(self.store.tron_draw_existing(0, 999), {180})
        chain.fail.clear()
        rec._retry_at = 0
        self.assertEqual(asyncio.run(rec.step(200)), 1)
        self.assertEqual(self.store.tron_draw_existing(0, 999), {160, 180})

    def test_max_backfill_window(self):
        chain = FakeChain(head=100000)
        self.store.set_tron_draw_floor(20)
        rec = TronDrawRecorder(chain, self.store, max_backfill=4, per_step=100)
        self.run_steps(rec, 100000, 2)
        self.assertEqual(len(chain.header_calls), 4)


class ScannerHookTest(unittest.TestCase):
    def test_scanner_step_draws_runs_without_watch_list(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            chain = FakeChain(head=500)
            scanner = TronBlockScanner(chain, ScanDB(Path(temp) / "scan.sqlite3"))
            self.assertEqual(asyncio.run(scanner.step_draws()), 0)  # recorder not attached
            scanner.draws = TronDrawRecorder(chain, store, initial_backfill=2, per_step=5)
            self.assertEqual(asyncio.run(scanner.step_draws()), 2)  # nothing is watched
            self.assertEqual(store.tron_draw_existing(0, 999), {460, 480})


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class WebTest(unittest.TestCase):
    def make_client(self, temp, enabled=True):
        from tg_directory_bot.web import create_app

        config = Config(
            bot_token="", admin_ids=set(), super_admin_ids=set(),
            db_path=Path(temp) / "db.sqlite3", categories=("other",), blocked_keywords=(),
            admin_username="admin", admin_password="testing-password",
            session_secret="x" * 40, backups_dir=Path(temp) / "backups",
            tron_draw_stats=enabled,
        )
        return TestClient(create_app(config)), DirectoryStore(config.db_path)

    def test_api_and_page_public(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.make_client(temp)
            with client:
                empty = client.get("/api/tron-draw").json()
                self.assertEqual((empty["count"], empty["cells"], empty["latest"]), (0, [], None))
                for i, n in enumerate(range(20, 20 * 31, 20)):
                    store.record_tron_draw(n, make_hash(n, "3" if i % 3 else "4"), 1_700_000_000_000 + n)
                data = client.get("/api/tron-draw?limit=10").json()
                self.assertEqual(data["count"], 10)
                self.assertEqual(data["total_recorded"], 30)
                self.assertEqual(data["counts"]["single"] + data["counts"]["double"], 10)
                self.assertEqual(data["latest"], 600)
                self.assertEqual(data["next_draw_block"], 620)
                self.assertEqual(len(data["cells"]), 10)
                self.assertEqual(data["recent"][0]["block"], 600)
                ref = data["reference"]
                self.assertEqual(set(ref["algorithms"]), {"trend", "dragon", "ratio", "minority"})
                self.assertEqual(ref["algorithms"]["minority"]["total"], 10)
                self.assertIn("统计仅供参考", ref["note"])
                self.assertEqual(ref["params"], {"streak_n": 3, "recent_m": 20, "mode": "follow", "minority_m": 10})
                self.assertEqual(ref["algorithms"]["trend"]["total"], 10)  # prior history feeds window
                tuned = client.get("/api/tron-draw?limit=10&streak_n=2&recent_m=5&mode=cut&minority_m=7").json()
                self.assertEqual(tuned["reference"]["params"], {"streak_n": 2, "recent_m": 5, "mode": "cut", "minority_m": 7})
                self.assertEqual(tuned["count"], 10)
                self.assertEqual(client.get("/api/tron-draw?limit=500").json()["count"], 30)
                self.assertEqual(client.get("/api/tron-draw?limit=zzz").json()["limit"], 100)
                page = client.get("/tron-draw")
                self.assertEqual(page.status_code, 200)
                self.assertIn("开奖走势", page.text)
                self.assertIn("区块走势", page.text)
                # admin area stays behind login
                self.assertEqual(client.get("/entries", follow_redirects=False).status_code, 303)

    def test_disabled_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            client, _ = self.make_client(temp, enabled=False)
            with client:
                self.assertEqual(client.get("/api/tron-draw").status_code, 404)
                self.assertEqual(client.get("/tron-draw").status_code, 404)


if __name__ == "__main__":
    unittest.main()


S, D = "单", "双"


def seq(text):
    return [S if c == "s" else D for c in text]


class ReferenceTest(unittest.TestCase):
    def test_streak_longest_omission(self):
        r = seq("ssddds")
        self.assertEqual(td.streak_at(r), 1)
        self.assertEqual(td.streak_at(r, 5), 3)
        self.assertEqual(td.streak_at([]), 0)
        self.assertEqual(td.longest_streak(r), 3)
        self.assertEqual(td.longest_streak([]), 0)
        self.assertEqual(td.omissions(r), {"single": 0, "double": 1})
        self.assertEqual(td.omissions(seq("sddd")), {"single": 3, "double": 0})
        self.assertEqual(td.omissions(seq("ss")), {"single": 0, "double": 2})

    def test_trend(self):
        self.assertEqual(td.reference_pick("trend", seq("sd")), D)
        self.assertEqual(td.reference_pick("trend", seq("ds")), S)
        self.assertIsNone(td.reference_pick("trend", []))

    def test_dragon_follow_and_cut(self):
        r = seq("dsss")  # streak 3 of 单
        self.assertEqual(td.reference_pick("dragon", r, streak_n=3, mode="follow"), S)
        self.assertEqual(td.reference_pick("dragon", r, streak_n=3, mode="cut"), D)
        # below threshold -> follow last in both modes
        self.assertEqual(td.reference_pick("dragon", r, streak_n=4, mode="cut"), S)
        self.assertEqual(td.reference_pick("dragon", seq("sdd"), streak_n=2, mode="cut"), S)
        self.assertEqual(td.reference_pick("dragon", seq("sdd"), streak_n=3, mode="cut"), D)

    def test_ratio_majority_tie_and_window(self):
        self.assertEqual(td.reference_pick("ratio", seq("ssdsd"), recent_m=5), S)
        self.assertEqual(td.reference_pick("ratio", seq("dddss"), recent_m=5), D)
        self.assertEqual(td.reference_pick("ratio", seq("ssdd"), recent_m=4), D)  # tie -> last
        self.assertEqual(td.reference_pick("ratio", seq("ddss"), recent_m=4), S)
        # only the last M periods count
        self.assertEqual(td.reference_pick("ratio", seq("ddddsss"), recent_m=3), S)

    def test_minority_picks_less_frequent_side(self):
        self.assertEqual(td.reference_pick("minority", seq("ssdsd"), minority_m=5), D)  # 3单2双
        self.assertEqual(td.reference_pick("minority", seq("dddss"), minority_m=5), S)  # 3双2单
        self.assertEqual(td.reference_pick("minority", seq("ssdd"), minority_m=4), D)  # tie -> last
        self.assertEqual(td.reference_pick("minority", seq("ddss"), minority_m=4), S)
        # only the last M periods count (older 单 ignored)
        self.assertEqual(td.reference_pick("minority", seq("ssssdds"), minority_m=3), S)
        self.assertEqual(td.reference_pick("minority", seq("sssddds"), minority_m=3), S)
        self.assertEqual(td.reference_pick("minority", seq("dsssdd"), minority_m=3), S)
        self.assertIsNone(td.reference_pick("minority", []))
        # exactly the opposite of 近期占比 unless tied
        for text in ("sdsssd", "ddsdsd", "sssdd"):
            r = seq(text)
            a = td.reference_pick("ratio", r, recent_m=5)
            b = td.reference_pick("minority", r, minority_m=5)
            window = r[-5:]
            if window.count(S) != window.count(D):
                self.assertNotEqual(a, b)
            else:
                self.assertEqual(a, b)

    def test_minority_backtest(self):
        # m=2 on s s d d s d: idx2 window(ss)->d hit; idx3 (sd tie)->last d hit;
        # idx4 (dd)->s hit; idx5 (ds tie)->last s miss(d)  => 3/4 (idx1: window s -> d vs s miss)
        r = seq("ssddsd")
        self.assertEqual(td.backtest("minority", r, minority_m=2), {"hits": 3, "total": 5, "rate": 60.0})

    def test_backtest_counts(self):
        # trend on s s d d s: picks for idx1..4 = s,s,d,d vs s,d,d,s -> hits 2/4
        self.assertEqual(td.backtest("trend", seq("ssdds")), {"hits": 2, "total": 4, "rate": 50.0})
        self.assertEqual(td.backtest("trend", seq("s")), {"hits": 0, "total": 0, "rate": None})
        # window start: only indexes >= 3 are judged
        self.assertEqual(td.backtest("trend", seq("ssdds"), 3)["total"], 2)

    def test_backtest_has_no_look_ahead(self):
        base = seq("sdssdddsdsssddsd")
        self.assertIn("minority", td.ALGORITHMS)
        for algo in td.ALGORITHMS:
            for params in ({"streak_n": 2, "recent_m": 4, "mode": "cut", "minority_m": 3},
                           {"streak_n": 3, "recent_m": 7, "mode": "follow", "minority_m": 6}):
                for i in range(1, len(base)):
                    picks = td.reference_pick(algo, base, i, **params)
                    # changing the judged period and anything after it must not change the pick
                    altered = base[:i] + [S if v == D else D for v in base[i:]]
                    self.assertEqual(picks, td.reference_pick(algo, altered, i, **params))
                    self.assertEqual(picks, td.reference_pick(algo, base[:i], **params))
        # whole-backtest equivalence: flipping the final result only changes the last judgement
        flipped = base[:-1] + [S if base[-1] == D else D]
        for algo, kw in (("ratio", {"recent_m": 5}), ("minority", {"minority_m": 5})):
            a = td.backtest(algo, base, **kw)
            b = td.backtest(algo, flipped, **kw)
            self.assertEqual(a["total"], b["total"])
            self.assertEqual(abs(a["hits"] - b["hits"]), 1)

    def test_panel_window_uses_prior_lookback(self):
        r = seq("sssddsdsd")
        panel = td.reference_panel(r, 4, streak_n=3, recent_m=5, mode="cut")
        # window = last 4 (d s d s? -> "sdsd"); all 4 judged from earlier history
        self.assertEqual(panel["algorithms"]["trend"]["total"], 4)
        self.assertEqual(panel["algorithms"]["minority"]["total"], 4)
        self.assertIn(panel["algorithms"]["minority"]["next"], (S, D))
        self.assertEqual(panel["params"]["minority_m"], 10)
        self.assertEqual(panel["current_streak"], 1)
        self.assertEqual(panel["longest_streak"], 1)
        self.assertEqual(panel["ratio"], {"single": 50.0, "double": 50.0})
        self.assertEqual(panel["omission"], {"single": 1, "double": 0})
        self.assertEqual(panel["algorithms"]["trend"]["next"], D)
        self.assertEqual(panel["note"], "开奖为哈希末位，单双各约50%，统计仅供参考")
        empty = td.reference_panel([], 50)
        self.assertIsNone(empty["algorithms"]["ratio"]["next"])
        self.assertEqual(empty["algorithms"]["trend"]["total"], 0)

    def test_param_parsing(self):
        self.assertEqual(td.parse_reference_params("", "", ""), (3, 20, "follow", 10))
        self.assertEqual(td.parse_reference_params("5", "999", "CUT", "7"), (5, 200, "cut", 7))
        self.assertEqual(td.parse_reference_params("x", "0", "bad", "999"), (3, 1, "follow", 200))
        self.assertEqual(td.parse_reference_params("x", "0", "bad", "z"), (3, 1, "follow", 10))


class WiringTest(unittest.TestCase):
    def test_setup_attaches_recorder_and_job_runs_it(self):
        from unittest.mock import AsyncMock
        from tg_directory_bot.bot import scan_tron_blocks, setup_tron_monitoring

        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            config = SimpleNamespace(
                tron_block_scan=True, tron_draw_stats=True, is_clone=False,
                db_path=Path(temp) / "db.sqlite3", tron_scan_lag_blocks=1,
                tron_scan_max_catchup_blocks=1200, tron_draw_confirmations=20,
                tron_draw_initial_backfill=3, tron_draw_max_backfill=100,
                tron_draw_backfill_per_step=3,
            )
            chain = FakeChain(head=300)
            chain.governor = SimpleNamespace(keys=[], describe=lambda: "test")
            app = SimpleNamespace(bot_data={"store": store})
            setup_tron_monitoring(app, config, chain)
            scanner = app.bot_data["tron_scanner"]
            self.assertIsInstance(scanner.draws, TronDrawRecorder)
            asyncio.run(scan_tron_blocks(SimpleNamespace(application=app)))
            self.assertEqual(store.tron_draw_existing(0, 999), {240, 260, 280})
            config.tron_draw_stats = False
            app2 = SimpleNamespace(bot_data={"store": store})
            setup_tron_monitoring(app2, config, chain)
            self.assertIsNone(app2.bot_data["tron_scanner"].draws)
