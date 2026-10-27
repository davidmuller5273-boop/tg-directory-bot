"""Top stats bar of /tron-draw: unique counting, bot exclusion, Shanghai day, online window, IP trust. Mock data only."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.tron_stats import TronStats, is_bot_user_agent, resolve_client_ip
from tests.test_tron_ai import make_config

BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile Safari/604.1"
BROWSER2 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def ts(text):
    """'2026-10-27 15:59:30' given in UTC -> epoch."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.tmp.name) / "db.sqlite3")
        self.store.init()
        self.clock = Clock(ts("2026-10-27 03:00:00"))  # 11:00 Shanghai
        self.stats = TronStats(self.store, now=self.clock)
        return self

    def __exit__(self, *a):
        self.tmp.cleanup()


class IpTrustTest(unittest.TestCase):
    def test_direct_peer_ignores_forwarded_headers(self):
        self.assertEqual(resolve_client_ip("8.8.8.8", "1.1.1.1", "2.2.2.2"), "8.8.8.8")

    def test_loopback_peer_trusts_nginx_headers(self):
        self.assertEqual(resolve_client_ip("127.0.0.1", "203.0.113.5", ""), "203.0.113.5")
        self.assertEqual(resolve_client_ip("::1", "", "6.6.6.6, 203.0.113.9"), "203.0.113.9")  # last = nginx-appended
        self.assertEqual(resolve_client_ip("127.0.0.1", "", "garbage, 203.0.113.9"), "203.0.113.9")

    def test_local_call_without_header_or_bad_header_is_not_a_visitor(self):
        self.assertIsNone(resolve_client_ip("127.0.0.1"))
        self.assertIsNone(resolve_client_ip("127.0.0.1", "not-an-ip", "also bad"))
        self.assertIsNone(resolve_client_ip("127.0.0.1", "127.0.0.1"))
        self.assertIsNone(resolve_client_ip(None))
        self.assertIsNone(resolve_client_ip("testclient"))

    def test_bot_user_agents(self):
        for ua in ("", "curl/8.0", "Googlebot/2.1", "python-requests/2.31", "Baiduspider", "Wget/1.21", "UptimeRobot"):
            self.assertTrue(is_bot_user_agent(ua), ua)
        self.assertFalse(is_bot_user_agent(BROWSER))
        self.assertFalse(is_bot_user_agent(BROWSER2))


class CountingTest(unittest.TestCase):
    def test_unique_counting_and_no_raw_data_stored(self):
        with Env() as e:
            e.clock.t += 10
            e.stats.record_use("203.0.113.1", BROWSER)
            e.clock.t += 10
            e.stats.record_use("203.0.113.1", BROWSER)     # same visitor again: a 2nd use, same user
            e.clock.t += 10
            e.stats.record_use("203.0.113.2", BROWSER)     # other ip
            e.clock.t += 10
            e.stats.record_use("203.0.113.1", BROWSER2)    # same ip, other device
            snap = e.stats.snapshot()
            self.assertEqual(
                (snap["history_uses"], snap["history_users"], snap["today_uses"], snap["today_users"]), (4, 3, 4, 3),
            )
            with e.store.connect() as conn:
                dump = " ".join(
                    str(tuple(r)) for t in ("tron_stats_meta", "tron_stats_daily", "tron_stats_visitors")
                    for r in conn.execute(f"SELECT * FROM {t}")
                )
            for raw in ("203.0.113", "Mozilla", "iPhone"):
                self.assertNotIn(raw, dump)

    def test_fast_reload_is_debounced(self):
        with Env() as e:
            e.stats.record_use("203.0.113.1", BROWSER)
            e.clock.t += 1
            e.stats.record_use("203.0.113.1", BROWSER)
            self.assertEqual(e.stats.snapshot()["today_uses"], 1)

    def test_bots_and_local_calls_are_excluded_everywhere(self):
        with Env() as e:
            e.stats.record_use("203.0.113.1", "curl/8.0")
            e.stats.record_use("203.0.113.1", "Googlebot/2.1")
            e.stats.record_use(None, BROWSER)       # local bot call
            e.stats.heartbeat("203.0.113.1", "python-requests/2.31")
            e.stats.heartbeat(None, BROWSER)
            self.assertEqual(e.stats.snapshot(), {
                "history_uses": 0, "history_users": 0, "today_uses": 0, "today_users": 0, "online": 0,
            })

    def test_shanghai_day_rollover_and_history(self):
        with Env() as e:
            e.clock.t = ts("2026-10-27 15:59:30")      # 23:59:30 Shanghai, Oct 27
            e.stats.record_use("203.0.113.1", BROWSER)
            e.stats.record_use("203.0.113.2", BROWSER)
            self.assertEqual(e.stats.today(), "2026-10-27")
            self.assertEqual(e.stats.snapshot()["today_uses"], 2)
            e.clock.t = ts("2026-10-27 16:00:30")      # 00:00:30 Shanghai, Oct 28
            self.assertEqual(e.stats.today(), "2026-10-28")
            snap = e.stats.snapshot()
            self.assertEqual((snap["today_uses"], snap["today_users"]), (0, 0))
            self.assertEqual((snap["history_uses"], snap["history_users"]), (2, 2))
            e.stats.record_use("203.0.113.1", BROWSER)  # returning visitor: new use, counted as today's user
            e.stats.record_use("203.0.113.3", BROWSER)
            snap = e.stats.snapshot()
            self.assertEqual((snap["today_uses"], snap["today_users"]), (2, 2))
            self.assertEqual((snap["history_uses"], snap["history_users"]), (4, 3))   # visitors never double-counted

    def test_open_tab_past_midnight_counts_as_todays_user_but_not_a_use(self):
        with Env() as e:
            e.clock.t = ts("2026-10-27 15:59:50")
            e.stats.record_use("203.0.113.1", BROWSER)
            e.clock.t = ts("2026-10-27 16:00:10")
            e.stats.heartbeat("203.0.113.1", BROWSER)
            e.stats.heartbeat("203.0.113.1", BROWSER)
            snap = e.stats.snapshot()
            self.assertEqual((snap["today_uses"], snap["today_users"]), (0, 1))
            self.assertEqual((snap["history_uses"], snap["history_users"]), (1, 1))

    def test_online_window_is_60_seconds(self):
        with Env() as e:
            e.stats.heartbeat("203.0.113.1", BROWSER)
            e.stats.heartbeat("203.0.113.2", BROWSER)
            e.stats.heartbeat("203.0.113.2", BROWSER2)
            self.assertEqual(e.stats.snapshot()["online"], 3)
            e.clock.t += 45
            e.stats.heartbeat("203.0.113.2", BROWSER)           # refreshed
            e.clock.t += 30                                     # .1 is 75 s old, .2/BROWSER 30 s, .2/BROWSER2 75 s
            self.assertEqual(e.stats.snapshot()["online"], 1)
            e.clock.t += 40
            self.assertEqual(e.stats.snapshot()["online"], 0)

    def test_heartbeats_do_not_write_to_the_database_every_poll(self):
        with Env() as e:
            e.stats.record_use("203.0.113.1", BROWSER)
            calls = []
            real = e.store.connect
            e.store.connect = lambda: (calls.append(1), real())[1]
            for _ in range(20):
                e.clock.t += 4
                e.stats.heartbeat("203.0.113.1", BROWSER)
            self.assertEqual(calls, [])

    def test_failures_are_swallowed(self):
        with Env() as e:
            e.store.connect = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
            e.stats.record_use("203.0.113.1", BROWSER)
            e.stats.heartbeat("203.0.113.1", BROWSER)
            self.assertEqual(e.stats.snapshot()["history_uses"], 0)

    def test_visitor_hash_is_stable_across_restarts(self):
        with Env() as e:
            a = e.stats.visitor_id("203.0.113.1", BROWSER)
            again = TronStats(e.store, now=e.clock).visitor_id("203.0.113.1", BROWSER)
            self.assertEqual(a, again)
            self.assertNotEqual(a, e.stats.visitor_id("203.0.113.2", BROWSER))
            e.stats.record_use("203.0.113.1", BROWSER)
            restarted = TronStats(e.store, now=e.clock)
            e.clock.t += 100
            restarted.record_use("203.0.113.1", BROWSER)
            self.assertEqual(restarted.snapshot()["history_users"], 1)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class WebTest(unittest.TestCase):
    def make(self, temp, peer):
        from tg_directory_bot.web import create_app
        return TestClient(create_app(make_config(temp)), client=peer)

    def test_page_counts_use_poll_counts_online_and_stats_in_json(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.make(temp, ("203.0.113.7", 50000))
            hdr = {"user-agent": BROWSER}
            with client:
                self.assertEqual(client.get("/tron-draw", headers=hdr).status_code, 200)
                first = client.get("/api/tron-draw", headers=hdr).json()["stats"]
                client.get("/api/tron-draw", headers=hdr)
                again = client.get("/api/tron-draw", headers=hdr).json()["stats"]
                self.assertEqual(first, again)                      # polls are not uses
                self.assertEqual(again, {
                    "history_uses": 1, "history_users": 1, "today_uses": 1, "today_users": 1, "online": 1,
                })
                # block-detail requests, the best endpoint and the bot's local endpoints add nothing
                client.get("/api/tron-draw?block=20", headers={"user-agent": BROWSER2})
                self.assertEqual(client.get("/api/tron-draw", headers=hdr).json()["stats"]["online"], 1)

    def test_local_bot_and_crawler_calls_are_not_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.make(temp, ("127.0.0.1", 50000))
            with client:
                client.get("/tron-draw", headers={"user-agent": BROWSER})                 # local, no forwarded header
                client.get("/api/tron-draw/ai-broadcast")
                client.get("/api/tron-draw/auto-broadcast")
                stats = client.get("/api/tron-draw", headers={"user-agent": BROWSER}).json()["stats"]
                self.assertEqual(stats, {
                    "history_uses": 0, "history_users": 0, "today_uses": 0, "today_users": 0, "online": 0,
                })
                client.get("/tron-draw", headers={"user-agent": "Googlebot/2.1", "x-real-ip": "203.0.113.8"})
                self.assertEqual(client.get("/api/tron-draw", headers={"user-agent": BROWSER}).json()["stats"]["history_uses"], 0)

    def test_nginx_headers_trusted_from_loopback_only(self):
        with tempfile.TemporaryDirectory() as temp:
            hdr = {"user-agent": BROWSER, "x-real-ip": "203.0.113.20", "x-forwarded-for": "203.0.113.20"}
            with self.make(temp, ("127.0.0.1", 50000)) as proxied:
                proxied.get("/tron-draw", headers=hdr)
                self.assertEqual(proxied.get("/api/tron-draw", headers=hdr).json()["stats"]["today_users"], 1)
                # a different real client behind the same nginx = a different user
                other = dict(hdr, **{"x-real-ip": "203.0.113.21", "x-forwarded-for": "203.0.113.21"})
                proxied.get("/tron-draw", headers=other)
                self.assertEqual(proxied.get("/api/tron-draw", headers=other).json()["stats"]["today_users"], 2)
        with tempfile.TemporaryDirectory() as temp:
            with self.make(temp, ("198.51.100.5", 50000)) as direct:   # spoofed headers from a non-local peer
                for i in range(3):
                    direct.get("/tron-draw", headers=dict(hdr, **{"x-real-ip": f"203.0.113.{30 + i}"}))
                    direct.app.state.tron_stats._last_use.clear()
                self.assertEqual(direct.get("/api/tron-draw", headers=hdr).json()["stats"]["today_users"], 1)

    def test_page_has_stats_bar_at_the_top_and_red_text_last(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.make(temp, ("203.0.113.7", 50000)) as client:
                html = client.get("/tron-draw", headers={"user-agent": BROWSER}).text
        self.assertLess(html.index('id="vstats"'), html.index("开奖走势</h1>"))
        self.assertIn("<title>波场一分单双 开奖走势</title>", html)
        self.assertIn("<h1>波场一分单双 开奖走势</h1>", html)
        for label in ("历史使用次数", "历史用户", "今日使用次数", "今日用户", "在线用户"):
            self.assertIn(f"<span>{label}</span>", html)
        self.assertIn("按访问设备去重，不记录真实IP", html)
        self.assertIn("toLocaleString", html)
        self.assertGreater(html.index("redend\">"), html.index('id="vstats"'))
        self.assertLess(html.rindex("redend\">"), html.index("<script"))

    def test_every_road_is_mouse_draggable_and_a_drag_is_not_a_click(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.make(temp, ("203.0.113.7", 50000)) as client:
                html = client.get("/tron-draw", headers={"user-agent": BROWSER}).text
        for road in ("road", "aroad", "oroad", "mroad"):
            self.assertIn(f'watchRoad($("{road}"))', html)   # block / AI / 自动最优 / 算法 trend strips
            self.assertIn(f'class="road" id="{road}"', html)
        self.assertIn("cursor:grab;", html)
        self.assertIn(".road.dragging{cursor:grabbing}", html)
        self.assertIn("DRAG_PX=5", html)
        self.assertIn("e.stopPropagation()", html)           # drag swallows the click that follows it


if __name__ == "__main__":
    unittest.main()
