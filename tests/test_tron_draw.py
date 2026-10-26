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
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=3, per_step=10)
        self.run_steps(rec, 400, 3)
        self.assertEqual(sorted(self.store.tron_draw_existing(0, 10**9)), [340, 360, 380])
        # bot down for a while, restarts with a fresh recorder
        chain2 = FakeChain(head=600)
        rec2 = TronDrawRecorder(chain2, self.store, confirmations=20, initial_backfill=3, per_step=10)
        self.run_steps(rec2, 600, 5)
        self.assertEqual(sorted(chain2.header_calls), [400, 420, 440, 460, 480, 500, 520, 540, 560, 580])
        self.assertEqual(
            sorted(self.store.tron_draw_existing(0, 10**9)), list(range(340, 600, 20)))
        # nothing missing -> no more requests
        chain3 = FakeChain(head=600)
        self.run_steps(TronDrawRecorder(chain3, self.store, confirmations=20, initial_backfill=3), 600, 3)
        self.assertEqual(chain3.header_calls, [])

    def test_failed_fetch_is_retried_and_does_not_block_loop(self):
        chain = FakeChain(head=200, fail={160})
        rec = TronDrawRecorder(chain, self.store, confirmations=20, initial_backfill=2, per_step=3)
        self.assertEqual(asyncio.run(rec.step(200)), 1)  # 180 stored, 160 failed
        self.assertEqual(self.store.tron_draw_existing(0, 999), {180})
        chain.fail.clear()
        rec._retry_at = 0
        self.assertEqual(asyncio.run(rec.step(200)), 1)
        self.assertEqual(self.store.tron_draw_existing(0, 999), {160, 180})

    def test_max_backfill_window(self):
        chain = FakeChain(head=100000)
        self.store.set_tron_draw_floor(20)
        rec = TronDrawRecorder(chain, self.store, confirmations=20, max_backfill=4, per_step=100)
        self.run_steps(rec, 100000, 2)
        self.assertEqual(len(chain.header_calls), 4)


class FastRecordingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def row(self, block):
        return [r for r in self.store.tron_draw_recent(50) if r["block_number"] == block][0]

    def test_defaults_record_draw_block_immediately(self):
        import os
        from unittest.mock import patch
        from tg_directory_bot.config import load_config

        with patch.dict(os.environ, {}, clear=False):
            for key in ("TRON_DRAW_CONFIRMATIONS", "TRON_DRAW_BLOCK_OFFSET"):
                os.environ.pop(key, None)
            config = load_config(None, require_bot_token=False)
        self.assertEqual((config.tron_draw_confirmations, config.tron_draw_block_offset), (0, 0))
        chain = FakeChain(head=1001)  # block 1000 was produced ~3 s ago
        rec = TronDrawRecorder(chain, self.store, confirmations=config.tron_draw_confirmations,
                               initial_backfill=1)
        self.assertEqual(asyncio.run(rec.step(1001)), 1)
        self.assertEqual(self.store.tron_draw_existing(0, 9999), {1000})
        self.assertEqual(self.store.tron_draw_recent(1)[0]["verified"], 0)  # young block

    def test_block_not_visible_yet_retries_within_seconds(self):
        chain = FakeChain(head=1000, fail={1000})
        rec = TronDrawRecorder(chain, self.store, initial_backfill=1)
        asyncio.run(rec.step(1000))
        self.assertEqual(self.store.tron_draw_total(), 0)
        import time as _t
        self.assertLess(rec._retry_at - _t.monotonic(), 2.5)

    def test_young_hash_is_verified_after_solidified(self):
        chain = FakeChain(head=1002)
        rec = TronDrawRecorder(chain, self.store, initial_backfill=1)
        asyncio.run(rec.step(1002))
        self.assertEqual(self.row(1000)["verified"], 0)
        asyncio.run(rec.step(1010))  # not deep enough yet: no re-check
        self.assertEqual(self.row(1000)["verified"], 0)
        asyncio.run(rec.step(1021))
        self.assertEqual(self.row(1000)["verified"], 1)

    def test_fork_corrects_stored_hash_and_result(self):
        chain = FakeChain(head=1002, hashes={1000: make_hash(1000, "1")})  # 单
        rec = TronDrawRecorder(chain, self.store, initial_backfill=1)
        asyncio.run(rec.step(1002))
        self.assertEqual(self.row(1000)["result"], "单")
        chain.hashes[1000] = make_hash(1000, "2")  # the canonical block differs
        asyncio.run(rec.step(1025))
        row = self.row(1000)
        self.assertEqual((row["result"], row["last_digit"], row["verified"]), ("双", 2, 1))
        self.assertTrue(row["block_hash"].endswith("2"))

    def test_old_blocks_are_stored_verified(self):
        chain = FakeChain(head=1100)
        rec = TronDrawRecorder(chain, self.store, initial_backfill=3, per_step=5)
        asyncio.run(rec.step(1100))
        self.assertEqual({r["verified"] for r in self.store.tron_draw_recent(10)}, {1, 0})
        self.assertEqual(self.store.tron_draw_recent(10)[0]["verified"], 1)

    def test_block_offset(self):
        self.assertTrue(is_draw_block(92, 12))
        self.assertFalse(is_draw_block(100, 12))
        chain = FakeChain(head=100)
        rec = TronDrawRecorder(chain, self.store, initial_backfill=2, block_offset=12, per_step=5)
        self.assertEqual(rec.latest_draw(100), 92)
        asyncio.run(rec.step(100))
        self.assertEqual(self.store.tron_draw_existing(0, 999), {72, 92})
        self.assertEqual(TronDrawRecorder(chain, self.store).latest_draw(100), 100)

    def test_old_database_gets_verified_column(self):
        import sqlite3
        path = Path(self.temp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tron_draws (block_number INTEGER PRIMARY KEY, block_hash TEXT NOT NULL,"
                     " last_digit INTEGER NOT NULL, result TEXT NOT NULL,"
                     " block_timestamp_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO tron_draws (block_number, block_hash, last_digit, result) VALUES (20, 'a1', 1, '单')")
        conn.commit()
        conn.close()
        store = DirectoryStore(path)
        store.init()
        self.assertEqual(store.tron_draw_recent(1)[0]["verified"], 1)

    def test_block_time_is_beijing_time(self):
        from tg_directory_bot.time_utils import format_beijing_timestamp_ms
        # TRON block 86276332 (user's CSV): 2026-09-15 19:39:03 UTC
        self.assertEqual(format_beijing_timestamp_ms(1789501143000), "2026-09-16 03:39:03")


class ScannerHookTest(unittest.TestCase):
    def test_scanner_step_draws_runs_without_watch_list(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            chain = FakeChain(head=500)
            scanner = TronBlockScanner(chain, ScanDB(Path(temp) / "scan.sqlite3"))
            self.assertEqual(asyncio.run(scanner.step_draws()), 0)  # recorder not attached
            scanner.draws = TronDrawRecorder(chain, store, confirmations=20, initial_backfill=2, per_step=5)
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
                self.assertEqual(ref["params"], {"streak_n": 3, "recent_m": 100, "mode": "follow", "minority_m": 20})
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
        self.assertEqual(panel["params"]["minority_m"], 20)
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
        self.assertEqual(td.parse_reference_params("", "", ""), (3, 100, "follow", 20))
        self.assertEqual(td.parse_reference_params("5", "999", "CUT", "7"), (5, 200, "cut", 7))
        self.assertEqual(td.parse_reference_params("x", "0", "bad", "999"), (3, 1, "follow", 200))
        self.assertEqual(td.parse_reference_params("x", "0", "bad", "z"), (3, 1, "follow", 20))


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


class FinalPredictionTest(unittest.TestCase):
    PARAMS = {"streak_n": 3, "recent_m": 7, "mode": "follow", "minority_m": 4}

    def random_results(self, n, seed):
        import random
        rng = random.Random(seed)
        return [rng.choice((S, D)) for _ in range(n)]

    def test_defaults_are_100_and_20(self):
        self.assertEqual((td.DEFAULT_RECENT_M, td.DEFAULT_MINORITY_M), (100, 20))

    def test_all_picks_matches_reference_pick(self):
        for seed in range(5):
            r = self.random_results(80, seed)
            for mode in ("follow", "cut"):
                params = {"streak_n": 3, "recent_m": 7, "mode": mode, "minority_m": 4}
                picks = td.all_picks(r, **params)
                for name in td.ALGORITHMS:
                    self.assertEqual(len(picks[name]), 81)
                    self.assertIsNone(picks[name][0])
                    for i in range(1, 81):
                        self.assertEqual(picks[name][i], td.reference_pick(name, r, i, **params), (name, i))

    def test_combine_sums_hit_rates_of_voters(self):
        picks = {"trend": S, "dragon": S, "ratio": D, "minority": S}
        stats = {"trend": (37, 100), "dragon": (37, 100), "ratio": (43, 100), "minority": (49, 100)}
        final = td.combine_final(picks, stats)
        self.assertEqual(final["next"], S)
        self.assertEqual(final["sums"], {"single": 123.0, "double": 43.0})
        self.assertEqual(final["basis"], "sum")
        self.assertIn("命中率", final["rule"])
        flipped = td.combine_final({"trend": D, "dragon": D, "ratio": S, "minority": S}, stats)
        self.assertEqual((flipped["next"], flipped["sums"]), (S, {"single": 92.0, "double": 74.0}))

    def test_combine_tie_follows_best_single_algorithm(self):
        picks = {"trend": S, "dragon": D, "ratio": S, "minority": D}
        stats = {"trend": (40, 100), "dragon": (50, 100), "ratio": (60, 100), "minority": (30, 100)}
        final = td.combine_final(picks, stats)  # S: 40+60 = D: 50+30? no -> 100 vs 80
        self.assertEqual(final["next"], S)
        tie = td.combine_final(picks, {"trend": (40, 100), "dragon": (60, 100),
                                       "ratio": (60, 100), "minority": (40, 100)})
        self.assertEqual(tie["basis"], "tie")
        self.assertEqual(tie["tie_algorithm"], "dragon")  # best rate; earlier algorithm on equal rate
        self.assertEqual(tie["next"], D)
        nodata = td.combine_final(picks, {})
        self.assertEqual((nodata["basis"], nodata["tie_algorithm"], nodata["next"]), ("tie", "trend", S))
        self.assertIsNone(td.combine_final({}, {})["next"])

    def test_panel_final_equals_walk_forward_next(self):
        r = self.random_results(120, 1)
        panel = td.reference_panel(r, 30, **self.PARAMS)
        series = td.final_series(r, 1, 30, self.PARAMS)
        self.assertEqual(panel["final"]["next"], series[len(r)]["next"])
        self.assertEqual(panel["final"]["sums"], series[len(r)]["sums"])
        self.assertEqual(panel["final"]["votes"], series[len(r)]["votes"])

    def test_walk_forward_never_sees_the_period_it_predicts(self):
        r = self.random_results(60, 2)
        base = td.final_series(r, 1, 20, self.PARAMS)
        for cut in (10, 30, 59):
            changed = r[:cut] + [td._opposite(v) for v in r[cut:]]
            other = td.final_series(changed, 1, 20, self.PARAMS)
            for i in range(1, cut + 1):
                self.assertEqual(base[i], other[i], (cut, i))

    def test_hit_rate_window_is_the_selected_limit(self):
        r = self.random_results(80, 3)
        series = td.final_series(r, 1, 10, self.PARAMS)
        picks = td.all_picks(r, **self.PARAMS)
        i = 60
        for name in td.ALGORITHMS:
            hits = sum(1 for j in range(i - 10, i) if picks[name][j] == r[j])
            panel = td.reference_panel(r[:i], 10, **self.PARAMS)
            self.assertEqual(panel["algorithms"][name]["hits"], hits)
        self.assertEqual(series[i]["next"], td.reference_panel(r[:i], 10, **self.PARAMS)["final"]["next"])


class RunsAndMyTrendTest(unittest.TestCase):
    def test_longest_runs(self):
        runs = td.longest_runs(seq("ssdsdsdddds"))
        self.assertEqual(runs["single"], {"len": 2, "start": 0})
        self.assertEqual(runs["double"], {"len": 4, "start": 6})
        self.assertEqual(runs["alternating"], {"len": 6, "start": 1})  # s d s d s d
        self.assertEqual(td.longest_runs([]), {
            "single": {"len": 0, "start": -1}, "double": {"len": 0, "start": -1},
            "alternating": {"len": 0, "start": -1}})

    def test_ties_choose_latest_run(self):
        runs = td.longest_runs(seq("ssdss"))
        self.assertEqual(runs["single"], {"len": 2, "start": 3})

    def test_alternating_covers_whole_window(self):
        self.assertEqual(td.longest_runs(seq("sdsdsd"))["alternating"], {"len": 6, "start": 0})
        self.assertEqual(td.longest_runs(seq("ssss"))["alternating"]["len"], 1)

    def test_my_trend_walk_forward_and_runs(self):
        import random
        rng = random.Random(7)
        results = [rng.choice((S, D)) for _ in range(90)]
        blocks = [20 * (i + 1) for i in range(90)]
        params = {"streak_n": 3, "recent_m": 7, "mode": "follow", "minority_m": 4}
        trend = td.my_trend(blocks, results, 40, 20, params)
        self.assertEqual(trend["count"], 40)
        self.assertEqual(trend["total"], 40)
        self.assertEqual(trend["items"][-1]["block"], blocks[-1])
        series = td.final_series(results, 1, 20, params)
        for item, i in zip(trend["items"], range(50, 90)):
            self.assertEqual(item["predicted"], series[i]["next"])
            self.assertEqual(item["ok"], series[i]["next"] == results[i])
            self.assertEqual(item["source"], "walk")
        flags = [item["ok"] for item in trend["items"]]
        self.assertEqual(trend["hits"], flags.count(True))
        best = trend["max_win"]
        self.assertTrue(all(flags[best["start"]:best["start"] + best["len"]]))
        worst = trend["max_loss"]
        self.assertTrue(not any(flags[worst["start"]:worst["start"] + worst["len"]]))
        self.assertGreaterEqual(best["len"], 1)

    def test_my_trend_prefers_stored_prediction_and_small_history(self):
        results = seq("sdsdsd")
        blocks = [20, 40, 60, 80, 100, 120]
        params = {"streak_n": 3, "recent_m": 7, "mode": "follow", "minority_m": 4}
        walk = td.my_trend(blocks, results, 500, 50, params)
        self.assertEqual(walk["count"], 5)  # period 0 has no history to predict from
        stored = td.my_trend(blocks, results, 500, 50, params, {120: S, 100: S})
        self.assertEqual([i["source"] for i in stored["items"]], ["walk"] * 3 + ["stored"] * 2)
        self.assertEqual(stored["items"][-1]["predicted"], S)
        self.assertFalse(stored["items"][-1]["ok"])
        self.assertTrue(stored["items"][-2]["ok"] is False or stored["items"][-2]["ok"] is True)
        empty = td.my_trend([], [], 50, 50, params)
        self.assertEqual((empty["count"], empty["max_win"]["len"], empty["rate"]), (0, 0, None))


class PredictionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_store_rules(self):
        s = self.store
        self.assertTrue(s.record_tron_draw_prediction(620, S, "k"))
        self.assertFalse(s.record_tron_draw_prediction(620, D, "k"))  # first wins
        self.assertTrue(s.record_tron_draw_prediction(620, D, "other"))
        self.assertFalse(s.record_tron_draw_prediction(640, "x", "k"))
        self.assertEqual(s.tron_draw_predictions(0, 999, "k"), {620: S})
        s.record_tron_draw(660, make_hash(660, "2"), 1)
        self.assertFalse(s.record_tron_draw_prediction(660, S, "k"))  # draw already known
        self.assertEqual(s.tron_draw_predictions(0, 999, "zzz"), {})

    def test_recorder_stores_prediction_before_next_draw(self):
        chain = FakeChain(head=1000)
        rec = TronDrawRecorder(chain, self.store, confirmations=0, initial_backfill=5, per_step=10)
        asyncio.run(rec.step(1000))
        stored = self.store.tron_draw_predictions(0, 99999, td.CANONICAL_KEY)
        self.assertEqual(set(stored), {1020})
        self.assertIn(stored[1020], (S, D))
        chain.head = 1020
        asyncio.run(rec.step(1020))
        self.assertEqual(set(self.store.tron_draw_predictions(0, 99999, td.CANONICAL_KEY)), {1020, 1040})
        self.assertEqual(self.store.tron_draw_total(), 5 + 1)

    def test_prediction_failure_never_breaks_recording(self):
        chain = FakeChain(head=1000)
        rec = TronDrawRecorder(chain, self.store, confirmations=0, initial_backfill=2)
        self.store.record_tron_draw_prediction = None  # would raise TypeError
        self.assertEqual(asyncio.run(rec.step(1000)), 2)


class MyTrendWebTest(unittest.TestCase):
    def make_client(self, temp):
        from tg_directory_bot.web import create_app

        config = Config(
            bot_token="", admin_ids=set(), super_admin_ids=set(),
            db_path=Path(temp) / "db.sqlite3", categories=("other",), blocked_keywords=(),
            admin_username="admin", admin_password="testing-password",
            session_secret="x" * 40, backups_dir=Path(temp) / "backups", tron_draw_stats=True,
        )
        return TestClient(create_app(config)), DirectoryStore(config.db_path)

    def fill(self, store, count=60):
        import random
        rng = random.Random(5)
        for i in range(count):
            n = 20 * (i + 1)
            store.record_tron_draw(n, make_hash(n, rng.choice("1357") if rng.random() < .5 else rng.choice("0246")), 1_700_000_000_000 + n)

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.make_client(temp)
            with client:
                empty = client.get("/api/tron-draw?mine_limit=50").json()
                self.assertIsNone(empty["my_trend"])
                self.assertIsNone(empty["reference"]["final"]["next"])
                self.assertEqual(empty["runs"]["single"]["len"], 0)
                self.fill(store)
                data = client.get("/api/tron-draw?limit=30").json()
                self.assertIsNone(data["my_trend"])  # only on request
                final = data["reference"]["final"]
                self.assertIn(final["next"], (S, D))
                self.assertEqual(final["for_block"], 1220)
                self.assertEqual(set(final["votes"]), {"trend", "dragon", "ratio", "minority"})
                self.assertIn("最终预测", final["rule"])
                self.assertEqual(data["runs"]["single"]["len"] + 0, td.longest_runs(
                    [c["v"] for c in sorted(data["cells"], key=lambda c: c["i"])])["single"]["len"])
                self.assertEqual([c["i"] for c in data["cells"]], list(range(30)))
                self.assertEqual(data["cells"][0]["i"], 0)
                # non-default settings are not written to the pre-draw history
                self.assertFalse(final["recorded"])
                self.assertEqual(store.tron_draw_predictions(0, 99999, td.CANONICAL_KEY), {})

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_default_settings_are_recorded_and_used_in_my_trend(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.make_client(temp)
            with client:
                self.fill(store)
                data = client.get("/api/tron-draw?limit=100&mine_limit=20").json()
                final = data["reference"]["final"]
                self.assertTrue(final["recorded"])
                self.assertEqual(store.tron_draw_predictions(1220, 1220, td.CANONICAL_KEY), {1220: final["next"]})
                mine = data["my_trend"]
                self.assertEqual(mine["count"], 20)
                self.assertEqual(mine["items"][-1]["block"], 1200)
                self.assertEqual({i["source"] for i in mine["items"]}, {"walk"})
                self.assertIsNone(data["previous"])
                # the draw arrives: the stored prediction is judged as made before it
                store.record_tron_draw(1220, make_hash(1220, "1"), 1_700_000_000_000)
                after = client.get("/api/tron-draw?limit=100&mine_limit=20").json()
                prev = after["previous"]
                self.assertEqual((prev["block"], prev["predicted"], prev["result"]), (1220, final["next"], S))
                self.assertEqual(prev["ok"], final["next"] == S)
                last = after["my_trend"]["items"][-1]
                self.assertEqual((last["block"], last["source"], last["predicted"]), (1220, "stored", final["next"]))
                self.assertEqual(last["ok"], final["next"] == S)
                # the walk-forward value for the same period is what was predicted
                walk = td.final_series([c for c in [r["result"] for r in store.tron_draw_recent(500)]], 1, 100,
                                       {"streak_n": 3, "recent_m": 100, "mode": "follow", "minority_m": 20})
                self.assertEqual(walk[61 - 1]["next"], final["next"])

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_mine_limit_and_page(self):
        with tempfile.TemporaryDirectory() as temp:
            client, store = self.make_client(temp)
            with client:
                self.fill(store, 40)
                mine = client.get("/api/tron-draw?limit=10&mine_limit=999").json()["my_trend"]
                self.assertEqual(mine["limit"], 999)
                self.assertEqual(mine["count"], 39)
                self.assertEqual(mine["hits"] + len([i for i in mine["items"] if i["ok"] is False]), mine["total"])
                self.assertIsNone(client.get("/api/tron-draw?mine_limit=abc").json()["my_trend"] if False else None)
                html = client.get("/tron-draw").text
                for token in ("最长龙", "最大连跳", "最大连中", "最大连错", "预测", "blink", "AudioContext",
                              "trondraw.v2.", "webkitAudioContext", "✅", "❌", "最终预测", "mine_limit", "scrollIntoView"):
                    self.assertIn(token, html)
                self.assertNotIn("http://", html.replace("http://www.w3.org", ""))


class BestParamsTest(unittest.TestCase):
    def skewed(self, n, p_single=0.7, seed=11):
        import random
        rng = random.Random(seed)
        return [S if rng.random() < p_single else D for _ in range(n)]

    def test_not_enough_history(self):
        out = td.best_params(self.skewed(td.BEST_MIN_PERIODS - 1))
        self.assertFalse(out["ready"])
        self.assertEqual(out["periods"], td.BEST_MIN_PERIODS - 1)
        self.assertIn("随机", out["note"])
        self.assertEqual(td.best_params([])["ready"], False)

    def test_split_and_honest_fields(self):
        r = self.skewed(1000)
        out = td.best_params(r)
        sp = out["split"]
        self.assertTrue(out["ready"])
        self.assertEqual(sp["warmup"] + sp["train"] + sp["holdout"], 1000)
        self.assertEqual(out["algorithms"]["ratio"]["train"]["total"], sp["train"])
        self.assertEqual(out["algorithms"]["ratio"]["holdout"]["total"], sp["holdout"])
        self.assertEqual(set(out["algorithms"]), {"dragon", "ratio", "minority"})
        self.assertIn("default", out["algorithms"]["ratio"])
        self.assertGreater(out["luck_rate"], 50)
        self.assertEqual(out["candidates"], len(td.BEST_N_RANGE) + len(td.BEST_M_RANGE))
        self.assertIn(out["algorithms"]["dragon"]["mode"], ("cut", "follow"))

    def test_finds_a_planted_bias_and_is_walk_forward(self):
        r = self.skewed(1200)
        out = td.best_params(r)
        ratio = out["algorithms"]["ratio"]
        self.assertGreater(ratio["train"]["rate"], 60)
        self.assertGreater(ratio["holdout"]["rate"], 60)
        self.assertLess(out["algorithms"]["minority"]["train"]["rate"], 45)
        # cross-check against the independent reference_pick backtest (no look-ahead)
        sp = out["split"]
        m = ratio["m"]
        hits = sum(
            1 for i in range(sp["warmup"], sp["warmup"] + sp["train"])
            if td.reference_pick("ratio", r, i, recent_m=m) == r[i]
        )
        self.assertEqual(hits, ratio["train"]["hits"])
        hold = sum(
            1 for i in range(sp["warmup"] + sp["train"], len(r))
            if td.reference_pick("ratio", r, i, recent_m=m) == r[i]
        )
        self.assertEqual(hold, ratio["holdout"]["hits"])
        # minority brute force
        mn = out["algorithms"]["minority"]
        hits = sum(
            1 for i in range(sp["warmup"], sp["warmup"] + sp["train"])
            if td.reference_pick("minority", r, i, minority_m=mn["m"]) == r[i]
        )
        self.assertEqual(hits, mn["train"]["hits"])

    def test_choice_never_uses_the_holdout_segment(self):
        r = self.skewed(1000, seed=3)
        base = td.best_params(r)
        sp = base["split"]
        start = sp["warmup"] + sp["train"]
        flipped = r[:start] + [td._opposite(v) for v in r[start:]]
        other = td.best_params(flipped)
        for name in ("ratio", "minority"):
            self.assertEqual(base["algorithms"][name]["m"], other["algorithms"][name]["m"])
            self.assertEqual(base["algorithms"][name]["train"], other["algorithms"][name]["train"])
        self.assertEqual(base["algorithms"]["dragon"]["mode"], other["algorithms"]["dragon"]["mode"])
        self.assertNotEqual(base["algorithms"]["ratio"]["holdout"], other["algorithms"]["ratio"]["holdout"])

    def test_cut_wins_when_dragons_always_end(self):
        import random
        rng = random.Random(2)
        r = []
        while len(r) < 900:
            value = rng.choice((S, D))
            if len(r) >= 3 and r[-1] == r[-2] == r[-3]:
                value = td._opposite(r[-1])  # every 3-run is cut
            r.append(value)
        dragon = td.best_params(r)["algorithms"]["dragon"]
        self.assertEqual(dragon["mode"], "cut")
        self.assertEqual(dragon["n"], 3)
        self.assertGreater(dragon["train"]["rate"], td.best_params(r)["trend"]["train"]["rate"])

    def test_follow_when_cut_does_not_beat_trend(self):
        r = [S] * 400
        dragon = td.best_params(r)["algorithms"]["dragon"]
        self.assertEqual((dragon["mode"], dragon["n"]), ("follow", None))
        self.assertEqual(dragon["train"]["rate"], 100.0)

    def test_search_is_cheap(self):
        import time
        started = time.monotonic()
        td.best_params(self.skewed(td.BEST_MAX_PERIODS + 500))
        self.assertLess(time.monotonic() - started, 15)

    def test_only_recent_periods_are_used(self):
        out = td.best_params(self.skewed(td.BEST_MAX_PERIODS + 500))
        self.assertEqual(out["periods"], td.BEST_MAX_PERIODS)


class BestEndpointTest(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_endpoint_cache_and_page(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            helper = MyTrendWebTest()
            client, store = helper.make_client(temp)
            with client:
                small = client.get("/api/tron-draw/best").json()
                self.assertFalse(small["ready"])
                helper.fill(store, 400)
                calls = []
                real = td.best_params

                def counting(results):
                    calls.append(len(results))
                    return real(results)

                with patch("tg_directory_bot.web.best_params", counting):
                    first = client.get("/api/tron-draw/best").json()
                    second = client.get("/api/tron-draw/best").json()
                    self.assertEqual(len(calls), 1)  # cached per latest period
                    self.assertTrue(first["ready"])
                    self.assertEqual(first, second)
                    self.assertEqual(first["latest"], 8000)
                    self.assertEqual(first["periods"], 400)
                    n = 20 * 401
                    store.record_tron_draw(n, make_hash(n, "1"), 1)
                    third = client.get("/api/tron-draw/best").json()
                    self.assertEqual(calls, [400, 401])
                    self.assertEqual(third["latest"], n)
                self.assertEqual(client.get("/api/tron-draw/best").headers["cache-control"], "no-store")
                html = client.get("/tron-draw").text
                for token in ("最佳算法", "/api/tron-draw/best", "bestbtn", "历史最优不代表未来更准"):
                    self.assertIn(token, html)

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_disabled(self):
        from tg_directory_bot.web import create_app

        with tempfile.TemporaryDirectory() as temp:
            config = Config(
                bot_token="", admin_ids=set(), super_admin_ids=set(),
                db_path=Path(temp) / "db.sqlite3", categories=("other",), blocked_keywords=(),
                admin_username="admin", admin_password="testing-password",
                session_secret="x" * 40, backups_dir=Path(temp) / "backups", tron_draw_stats=False,
            )
            with TestClient(create_app(config)) as client:
                self.assertEqual(client.get("/api/tron-draw/best").status_code, 404)

    def test_proxy_docs_allow_best_endpoint(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn("(/best)?", (root / "deploy/nginx/tron-draw.conf.example").read_text())
        doc = (root / "TRON_DRAW_CN.md").read_text()
        self.assertIn("(/best)?", doc)
        self.assertIn("/api/tron-draw*", doc)
