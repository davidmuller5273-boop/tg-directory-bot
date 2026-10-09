"""🔔 币价涨跌监控 + 贴纸复制私聊 10 分钟撤回。"""

import asyncio
import itertools
import json
import tempfile
import time
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from telegram import Update

from tg_directory_bot import bot as handlers
from tg_directory_bot import crypto_alert as ca
from tg_directory_bot import crypto_price as cp
from tg_directory_bot import sticker_clone
from tg_directory_bot.auto_delete import AutoDeleteBot, private_delete_after
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore

from tests.test_group_wizard_save import ADMIN, BOT_ID, GROUP, FakeRequest

OTHER = 8
TODAY = "2026-10-10"


def row(**kw):
    base = {"id": 1, "chat_id": 5, "symbol": "BTC", "daily_pct": 5, "fast_pct": 2,
            "direction": "both", "last_daily_up": "", "last_daily_down": "", "last_fast_at": 0}
    base.update(kw)
    return base


def snap(last, sod="100", open24h="100", symbol="BTC"):
    return ca.Snapshot(symbol, Decimal(last), Decimal(sod), Decimal(open24h))


class CommandParsingTest(unittest.TestCase):
    def test_set_variants(self):
        cmd = ca.parse_command(["btc", "5", "2"])
        self.assertEqual((cmd.action, cmd.symbol, cmd.daily, cmd.fast, cmd.direction),
                         ("set", "BTC", Decimal("5.00"), Decimal("2.00"), "both"))
        cmd = ca.parse_command(["eth", "3%"])
        self.assertEqual((cmd.symbol, cmd.daily, cmd.fast), ("ETH", Decimal("3.00"), Decimal("0")))
        self.assertEqual(ca.parse_command(["sol", "0", "1.5", "跌"]).direction, "down")
        self.assertEqual(ca.parse_command(["sol", "4", "0", "涨"]).direction, "up")
        self.assertEqual(ca.parse_command(["sol", "4", "0", "双向"]).direction, "both")

    def test_other_actions_and_errors(self):
        self.assertEqual(ca.parse_command([]).action, "help")
        self.assertEqual(ca.parse_command(["list"]).action, "list")
        cmd = ca.parse_command(["del", "btc"])
        self.assertEqual((cmd.action, cmd.symbol), ("del", "BTC"))
        for bad in (["btc"], ["btc", "0", "0"], ["btc", "x"], ["btc", "-1"], ["btc", "5", "2", "横盘"],
                    ["del"], ["usdt", "5"], ["btc", "2000"]):
            with self.assertRaises(ValueError, msg=bad):
                ca.parse_command(bad)


class EvaluateTest(unittest.TestCase):
    def test_daily_up_down_and_threshold(self):
        fired = ca.evaluate(row(fast_pct=0), snap("105"), None, 1000, TODAY)
        self.assertEqual([(f.kind, f.direction) for f in fired], [("daily", "up")])
        self.assertEqual(fired[0].change, Decimal("5"))
        self.assertEqual(ca.evaluate(row(fast_pct=0), snap("104.9"), None, 1000, TODAY), [])
        fired = ca.evaluate(row(fast_pct=0), snap("94"), None, 1000, TODAY)
        self.assertEqual([(f.kind, f.direction) for f in fired], [("daily", "down")])

    def test_daily_uses_beijing_day_open_not_24h_open(self):
        # sodUtc8=100 → +5%；open24h=90 would be +16.7%
        fired = ca.evaluate(row(daily_pct=10, fast_pct=0), snap("105", "100", "90"), None, 0, TODAY)
        self.assertEqual(fired, [])

    def test_direction_filter(self):
        self.assertEqual(ca.evaluate(row(direction="down", fast_pct=0), snap("110"), None, 0, TODAY), [])
        self.assertEqual(len(ca.evaluate(row(direction="down", fast_pct=0), snap("90"), None, 0, TODAY)), 1)
        self.assertEqual(ca.evaluate(row(direction="up", fast_pct=0), snap("90"), None, 0, TODAY), [])
        self.assertEqual(ca.evaluate(row(direction="up", daily_pct=0), snap("100"), Decimal("103"), 0, TODAY), [])
        self.assertEqual(len(ca.evaluate(row(direction="down", daily_pct=0), snap("100"), Decimal("103"), 0, TODAY)), 1)

    def test_daily_cooldown_once_per_day_per_direction(self):
        done_up = row(fast_pct=0, last_daily_up=TODAY)
        self.assertEqual(ca.evaluate(done_up, snap("110"), None, 0, TODAY), [])
        self.assertEqual(len(ca.evaluate(done_up, snap("90"), None, 0, TODAY)), 1)  # 跌方向未提醒过
        self.assertEqual(len(ca.evaluate(done_up, snap("110"), None, 0, "2026-10-11")), 1)  # 新的一天

    def test_fast_rule_and_cooldown(self):
        fired = ca.evaluate(row(daily_pct=0), snap("102"), Decimal("100"), 5000, TODAY)
        self.assertEqual([(f.kind, f.direction) for f in fired], [("fast", "up")])
        self.assertEqual(ca.evaluate(row(daily_pct=0), snap("101.9"), Decimal("100"), 5000, TODAY), [])
        self.assertEqual(ca.evaluate(row(daily_pct=0), snap("102"), None, 5000, TODAY), [])
        cooling = row(daily_pct=0, last_fast_at=5000)
        self.assertEqual(ca.evaluate(cooling, snap("97"), Decimal("100"), 5000 + 599, TODAY), [])
        self.assertEqual(len(ca.evaluate(cooling, snap("97"), Decimal("100"), 5000 + 600, TODAY)), 1)

    def test_both_rules_fire_together(self):
        fired = ca.evaluate(row(), snap("106"), Decimal("103"), 0, TODAY)
        self.assertEqual({f.kind for f in fired}, {"daily", "fast"})

    def test_alert_text(self):
        fired = ca.evaluate(row(fast_pct=0), snap("106"), None, 0, TODAY)
        text = ca.alert_text("BTC", snap("106"), fired, Decimal("7.2"),
                             datetime(2026, 10, 10, 9, 30, tzinfo=cp.BJT), Decimal("-0.5"))
        self.assertIn("🔔 BTC 涨跌提醒 📈", text)
        self.assertIn("当前价：106 USDT（≈¥763.2）", text)
        self.assertIn("触发：日涨跌（北京时间0点起） 上涨 +6.00%（阈值 5%）", text)
        self.assertIn("10分钟涨跌：-0.50%", text)
        self.assertIn("时间：2026-10-10 09:30:00（北京时间）", text)


def tickers(prices, sod=None):
    return {"code": "0", "data": [
        {"instId": f"{s}-USDT", "last": str(p), "sodUtc8": str((sod or {}).get(s, p)), "open24h": str(p)}
        for s, p in prices.items()
    ] + [{"instId": "BTC-USDC", "last": "1", "sodUtc8": "1", "open24h": "1"}]}


class HistoryAndPollerTest(unittest.IsolatedAsyncioTestCase):
    def test_history_price_ago(self):
        history = ca.PriceHistory()
        for i in range(30):
            history.record("BTC", 1000 + i * 30, Decimal(100 + i))
        now = 1000 + 29 * 30
        self.assertEqual(history.price_ago("BTC", now), Decimal(100 + 9))
        self.assertIsNone(history.price_ago("ETH", now))
        fresh = ca.PriceHistory()
        fresh.record("BTC", now - 60, Decimal(1))
        self.assertIsNone(fresh.price_ago("BTC", now))
        self.assertLessEqual(history.samples["BTC"][-1][0] - history.samples["BTC"][0][0], ca.HISTORY_KEEP_SECONDS)

    async def test_poller_one_tickers_call_and_candles_only_for_fast(self):
        calls = []
        now = [1_800_000_000.0]
        candle_rows = [[str(int((now[0] - 60 * i) * 1000)), str(100 + i), "0", "0", "0"] for i in range(11)]

        async def fetch(url, params):
            calls.append((url, dict(params)))
            if url == ca.TICKERS_URL:
                return tickers({"BTC": 100, "ETH": 10, "SOL": 5})
            return {"code": "0", "data": candle_rows}

        poller = ca.AlertPoller(fetch_json=fetch, clock=lambda: now[0])
        snaps, refs = await poller.snapshot({"BTC", "ETH"}, {"BTC"})
        self.assertEqual(set(snaps), {"BTC", "ETH"})
        self.assertEqual([u for u, _ in calls].count(ca.TICKERS_URL), 1)
        candle_calls = [p for u, p in calls if u == ca.CANDLES_URL]
        self.assertEqual(candle_calls, [{"instId": "BTC-USDT", "bar": "1m", "limit": "11"}])
        self.assertEqual(refs["BTC"], Decimal("110"))  # 10 分钟前那根 K 线的开盘价
        calls.clear()
        now[0] += 30
        await poller.snapshot({"BTC", "ETH"}, {"BTC"})
        self.assertEqual([u for u, _ in calls], [ca.TICKERS_URL])

    async def test_no_monitors_no_request(self):
        async def fetch(url, params):
            raise AssertionError("should not fetch")
        self.assertEqual(await ca.AlertPoller(fetch_json=fetch).snapshot(set(), set()), ({}, {}))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DirectoryStore(Path(self.temp.name) / "a.sqlite3")
        self.store.init()
        self.store.init()  # 迁移可重复

    def test_upsert_cap_delete_and_cooldown_reset(self):
        alert_id = self.store.upsert_price_alert(5, 5, "btc", 5, 2, "both", 2)
        self.store.mark_price_alert_fired(alert_id, daily_up=TODAY, fast_at=123)
        again = self.store.upsert_price_alert(5, 5, "BTC", 6, 0, "up", 2)
        self.assertEqual(again, alert_id)
        saved = self.store.price_alert(alert_id)
        self.assertEqual((saved["daily_pct"], saved["fast_pct"], saved["direction"]), (6, 0, "up"))
        self.assertEqual((saved["last_daily_up"], saved["last_fast_at"]), ("", 0))
        self.store.upsert_price_alert(5, 5, "ETH", 1, 0, "both", 2)
        with self.assertRaises(ValueError):
            self.store.upsert_price_alert(5, 5, "SOL", 1, 0, "both", 2)
        self.store.upsert_price_alert(5, 5, "SOL", 1, 0, "both", 0)  # 0 = 不限
        self.store.upsert_price_alert(-100, 5, "SOL", 1, 0)
        self.assertEqual([r["symbol"] for r in self.store.list_price_alerts(5)], ["BTC", "ETH", "SOL"])
        self.assertTrue(self.store.delete_price_alert(5, "eth"))
        self.assertFalse(self.store.delete_price_alert(5, "eth"))
        self.assertEqual(self.store.disable_price_alerts(5, "blocked"), 2)
        self.assertEqual([r["chat_id"] for r in self.store.active_price_alerts()], [-100])


class ButtonsTest(unittest.TestCase):
    def test_price_reply_has_alert_button(self):
        markup = handlers.price_refresh_markup("BTC")
        buttons = [(b.text, b.callback_data) for r in markup.inline_keyboard for b in r]
        self.assertIn(("🔔 监控此币涨跌", "palert:set:BTC"), buttons)
        self.assertIn(("🔄 刷新", "price:q:BTC"), buttons)

    def test_price_menu_has_list_button(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = DirectoryStore(Path(temp.name) / "a.sqlite3")
        store.init()
        _, markup = handlers.price_menu_view(store, False)
        self.assertIn("palert:list", [b.callback_data for r in markup.inline_keyboard for b in r])
        self.assertIn("/pricealert", cp.HELP_TEXT)
        self.assertIn("/pricealert", handlers.HELP_TEXT)

    def test_quote_shows_beijing_day_change(self):
        quote = cp.Quote("BTC", Decimal("105"), Decimal("90"), Decimal("110"), Decimal("80"),
                         None, "", 0.0, Decimal("100"))
        self.assertIn("日涨跌（北京时间0点起）：+5.00%", cp.quote_text(quote))


class BlockingRequest(FakeRequest):
    blocked: set = set()

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if api == "sendMessage" and int(params.get("chat_id", 0)) in self.blocked:
            self.calls.append((api, params))
            return 403, json.dumps({"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was blocked by the user"}).encode()
        return await super().do_request(url, method, request_data, **kwargs)


class AppBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config(
            bot_token="1:test", admin_ids=set(), super_admin_ids=set(),
            db_path=Path(self.temp.name) / "bot.sqlite3", categories=("tools",),
            blocked_keywords=(), tron_block_scan=False,
        )
        self.app = handlers.build_application(config)
        self.request = BlockingRequest()
        self.request.blocked = set()
        object.__setattr__(self.app.bot, "_request", (self.request, self.request))
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.store = self.app.bot_data["store"]
        self.store.set_group_admin_permissions(GROUP, ADMIN, {"points"}, 1)
        self.ids = itertools.count(1)
        self.message_ids = itertools.count(5000)
        self.private = False
        self.user_id = ADMIN

    def chat(self):
        if self.private:
            return {"id": self.user_id, "type": "private", "first_name": "U"}
        return {"id": GROUP, "type": "supergroup", "title": "G"}

    def user(self):
        return {"id": self.user_id, "is_bot": False, "first_name": "U", "username": f"u{self.user_id}"}

    async def send(self, text):
        message = {"message_id": next(self.message_ids), "date": int(time.time()),
                   "chat": self.chat(), "from": self.user(), "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": next(self.ids), "message": message}, self.app.bot))
        return message["message_id"]

    async def tap(self, data, message_id=777):
        payload = {"update_id": next(self.ids), "callback_query": {
            "id": str(next(self.ids)), "from": self.user(), "chat_instance": "ci", "data": data,
            "message": {"message_id": message_id, "date": int(time.time()), "chat": self.chat(),
                        "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}, "text": "panel"},
        }}
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    def button(self, label):
        for api, params in reversed(self.request.calls):
            markup = params.get("reply_markup")
            if api not in {"sendMessage", "editMessageText"} or not markup:
                continue
            markup = json.loads(markup) if isinstance(markup, str) else markup
            for r in markup["inline_keyboard"]:
                for b in r:
                    if b.get("text") == label:
                        return b["callback_data"], int(params.get("message_id") or 0)
        self.fail(f"button {label!r} not found")

    def texts(self):
        return [str(p.get("text", "")) for api, p in self.request.calls if api in {"sendMessage", "editMessageText"}]

    async def run_wizard(self, symbol, daily, fast, direction):
        await self.tap(f"palert:set:{symbol}")
        await self.send(daily)
        await self.send(fast)
        data, mid = self.button(direction)
        await self.tap(data, mid)
        data, mid = self.button("确认保存")
        self.request.calls.clear()
        await self.tap(data, mid)



class AppTest(AppBase):
    async def test_group_admin_wizard_saves_group_alert(self):
        await self.run_wizard("BTC", "5", "2", "双向")
        joined = "\n".join(self.texts())
        self.assertNotIn("保存未完成", joined)
        self.assertIn("✅ 已设置 BTC 涨跌监控", joined)
        self.assertIn("提醒发送到：本群", joined)
        saved = self.store.price_alert_for(GROUP, "BTC")
        self.assertEqual((saved["daily_pct"], saved["fast_pct"], saved["direction"], saved["owner_id"]),
                         (5, 2, "both", ADMIN))
        self.assertNotIn("menu_mode", self.app.user_data[ADMIN])

    async def test_private_wizard_saves_personal_alert_and_edit_prefills(self):
        self.private = True
        await self.run_wizard("ETH", "0", "1.5", "只跌")
        saved = self.store.price_alert_for(ADMIN, "ETH")
        self.assertEqual((saved["daily_pct"], saved["fast_pct"], saved["direction"]), (0, 1.5, "down"))
        self.assertIn("提醒发送到：私聊", "\n".join(self.texts()))
        # 再次打开：带出当前值
        self.request.calls.clear()
        await self.tap("palert:set:ETH")
        self.assertTrue(any("当前值：0" in t for t in self.texts()), self.texts())

    async def test_wizard_rejects_both_zero(self):
        self.private = True
        await self.run_wizard("SOL", "0", "0", "双向")
        self.assertIn("保存未完成", "\n".join(self.texts()))
        self.assertIsNone(self.store.price_alert_for(ADMIN, "SOL"))

    async def test_group_non_admin_denied(self):
        self.user_id = OTHER
        await self.tap("palert:set:BTC")
        answers = [p.get("text") for api, p in self.request.calls if api == "answerCallbackQuery"]
        self.assertIn(handlers.PRICE_ALERT_GROUP_DENIED, answers)
        await self.send("/pricealert btc 5 2")
        self.assertIn(handlers.PRICE_ALERT_GROUP_DENIED, self.texts())
        self.assertEqual(self.store.list_price_alerts(GROUP), [])

    async def test_commands(self):
        self.private = True
        await self.send("/pricealert btc 5 2")
        self.assertEqual(self.store.price_alert_for(ADMIN, "BTC")["fast_pct"], 2)
        await self.send("/pricealert sol 3 0 涨")
        self.assertEqual(self.store.price_alert_for(ADMIN, "SOL")["direction"], "up")
        self.request.calls.clear()
        await self.send("/pricealerts")
        listing = self.texts()[-1]
        self.assertIn("BTC：日涨跌 5%，10分钟 2%，双向", listing)
        self.assertIn("SOL：日涨跌 3%，10分钟 不监控，只涨", listing)
        await self.send("/pricealert del btc")
        self.assertIsNone(self.store.price_alert_for(ADMIN, "BTC"))
        self.request.calls.clear()
        await self.send("/pricealert btc abc")
        self.assertIn("不是有效数字", self.texts()[-1])
        # 群管理员用命令设置群监控
        self.private = False
        await self.send("/pricealert eth 4")
        self.assertEqual(self.store.price_alert_for(GROUP, "ETH")["daily_pct"], 4)

    async def test_list_delete_button(self):
        self.private = True
        alert_id = self.store.upsert_price_alert(ADMIN, ADMIN, "BTC", 5, 2)
        await self.tap("palert:list")
        self.assertIn("BTC：日涨跌 5%", self.texts()[-1])
        await self.tap(f"palert:del:{alert_id}")
        self.assertIsNone(self.store.price_alert(alert_id))

    async def test_poll_job_sends_alert_with_cooldown_and_disables_unreachable(self):
        self.store.upsert_price_alert(ADMIN, ADMIN, "BTC", 5, 2)
        self.store.upsert_price_alert(GROUP, ADMIN, "BTC", 50, 0)  # 不触发
        self.store.upsert_price_alert(4242, 4242, "ETH", 1, 0)    # 用户屏蔽了机器人
        self.request.blocked = {4242}
        prices = {"BTC": Decimal("106"), "ETH": Decimal("110")}
        clock = [1_800_000_000.0]

        async def fetch(url, params):
            if url == ca.TICKERS_URL:
                return tickers(prices, {"BTC": 100, "ETH": 100})
            return {"code": "0", "data": []}

        async def no_fx(url, params):
            return {"rates": {"CNY": "7.10"}}

        self.app.bot_data[handlers.PRICE_ALERT_POLLER_KEY] = ca.AlertPoller(fetch_json=fetch, clock=lambda: clock[0])
        self.app.bot_data[handlers.PRICE_SERVICE_KEY] = cp.PriceService(fetch_json=no_fx)
        context = SimpleNamespace(application=self.app, bot=self.app.bot)
        self.request.calls.clear()
        await handlers.poll_price_alerts(context)
        sent = [(int(p["chat_id"]), p["text"]) for api, p in self.request.calls if api == "sendMessage"]
        self.assertEqual([c for c, _ in sent], [ADMIN, 4242])
        text = sent[0][1]
        self.assertIn("🔔 BTC 涨跌提醒", text)
        self.assertIn("日涨跌（北京时间0点起） 上涨 +6.00%（阈值 5%）", text)
        self.assertIn("≈¥752.6", text)
        self.assertIn("北京时间", text)
        self.assertEqual(self.store.price_alert_for(4242, "ETH")["enabled"], 0)
        # 冷却：同一天同方向不再提醒
        self.request.calls.clear()
        clock[0] += 30
        await handlers.poll_price_alerts(context)
        self.assertEqual([p for api, p in self.request.calls if api == "sendMessage"], [])
        # 10 分钟规则：价格 10 分钟内 +2% 以上
        for _ in range(20):
            clock[0] += 30
            await handlers.poll_price_alerts(context)
        prices["BTC"] = Decimal("108.5")
        self.request.calls.clear()
        clock[0] += 30
        await handlers.poll_price_alerts(context)
        sent = [p["text"] for api, p in self.request.calls if api == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertIn("触发：10分钟涨跌 上涨 +2.36%（阈值 2%）", sent[0])


class StickerPrivateCleanupTest(AppBase):
    def seconds_left(self, chat_id):
        with self.store.connect() as conn:
            return {int(r[0]): int(r[1]) for r in conn.execute(
                "SELECT message_id, strftime('%s', delete_at) - strftime('%s', 'now') "
                "FROM scheduled_message_deletions WHERE chat_id=?", (chat_id,))}

    async def test_private_jx_messages_deleted_after_ten_minutes(self):
        self.private = True
        self.request.calls.clear()
        command_id = await self.send("/jx")
        prompt = [p for api, p in self.request.calls if api == "sendMessage"]
        self.assertTrue(prompt and "贴纸包" in prompt[-1]["text"])
        left = self.seconds_left(ADMIN)
        self.assertIn(command_id, left)  # 用户自己的 /jx
        self.assertTrue(len(left) >= 2, left)  # 以及机器人的提示
        self.assertTrue(all(590 <= value <= 600 for value in left.values()), left)
        # 内存里的撤回也是 10 分钟（不是默认的 180 秒）
        bot = self.app.bot
        task = bot._delete_by_message.get((ADMIN, command_id))
        self.assertIsNotNone(task)

    async def test_group_jx_unaffected(self):
        await self.send("/jx")
        self.assertEqual(self.seconds_left(GROUP), {})

    async def test_scope_skips_groups_and_channels_and_overrides_persistent(self):
        recorded = []
        bot = AutoDeleteBot("123:token", auto_delete_seconds=180)
        object.__setattr__(bot, "deletion_recorder", lambda c, m, s: recorded.append((c, m, s)))
        from telegram import Chat, Message
        private_msg = Message(1, datetime.now(), Chat(7, "private"))
        channel_msg = Message(2, datetime.now(), Chat(-1001, "channel"))
        from tg_directory_bot.auto_delete import persistent_message
        with private_delete_after(600), persistent_message():
            bot._schedule_delete(private_msg)
            bot._schedule_delete(channel_msg)
        self.assertEqual(recorded, [(7, 1, 600)])
        self.assertIn((7, 1), bot._delete_by_message)
        self.assertNotIn((-1001, 2), bot._delete_by_message)
        for task in list(bot._delete_tasks):
            task.cancel()

    async def test_run_job_progress_and_result_cleanup(self):
        recorded = []

        class FakeBot:
            def schedule_private_cleanup(self, chat_id, message_id, seconds):
                recorded.append((chat_id, message_id, seconds))
                return chat_id > 0

        class Progress:
            chat_id = 7
            message_id = 99

            async def edit_text(self, *args, **kwargs):
                return None

        async def failing_clone(*args, **kwargs):
            raise sticker_clone.StickerCloneError("boom")

        original = sticker_clone.clone_sticker_set
        sticker_clone.clone_sticker_set = failing_clone
        try:
            await sticker_clone.run_job(FakeBot(), {}, 7, "src", "t", "bot", Progress())
        finally:
            sticker_clone.clone_sticker_set = original
        self.assertIn((7, 99, 600), recorded)
