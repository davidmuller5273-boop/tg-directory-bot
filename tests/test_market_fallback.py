"""💹 OKX 没有的币（如 XMR）：火币 / KuCoin / Gate.io / MEXC 备用行情源（全部 mock HTTP）。"""

import asyncio
import time
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import httpx

from tg_directory_bot import bot as handlers
from tg_directory_bot import crypto_alert as ca
from tg_directory_bot import crypto_price as cp
from tg_directory_bot import market_sources as ms

from tests.test_price_alerts import AppBase

# 2026-10-19 03:00 北京时间
WALL = datetime(2026, 10, 19, 3, 0, tzinfo=cp.BJT).timestamp()
MIDNIGHT = int(datetime(2026, 10, 19, 0, 0, tzinfo=cp.BJT).timestamp())

OKX_MISSING = {"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."}
HTX_INVALID = {"status": "error", "err-code": "invalid-parameter", "err-msg": "invalid symbol"}
KUCOIN_NULL = {"code": "200000", "data": {"symbol": "X-USDT", "last": None, "high": None, "low": None,
                                          "changePrice": None}}


def http_error(status, url="https://example.test"):
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(status, request=request))


def htx_merged(close="575.71", open_="563.07"):
    return {"status": "ok", "tick": {"open": float(open_), "close": float(close), "low": 557.52, "high": 607.67}}


def htx_kline(rows):
    return {"status": "ok", "data": rows}


class FakeHttp:
    """URL → response（dict/list）或异常；支持按 (url, symbol-ish) 区分。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url == cp.FX_URL:
            return {"rates": {"CNY": "7.00"}}
        handler = self.routes.get(url)
        if handler is None:
            raise AssertionError(f"unexpected url {url}")
        value = handler(params) if callable(handler) else handler
        if isinstance(value, BaseException):
            raise value
        if asyncio.iscoroutine(value):
            return await value
        return value

    def urls(self):
        return [u for u, _ in self.calls if u != cp.FX_URL]


def htx_kline_handler(params):
    if params["period"] == "1day":
        return htx_kline([{"id": MIDNIGHT, "open": 581.03, "close": 575.0}])
    return htx_kline([])


def xmr_routes(**override):
    routes = {
        cp.TICKER_URL: OKX_MISSING,
        ms.HTX_MERGED_URL: htx_merged(),
        ms.HTX_KLINE_URL: htx_kline_handler,
        ms.KUCOIN_STATS_URL: KUCOIN_NULL,
        ms.GATE_TICKER_URL: http_error(400),
        ms.MEXC_TICKER_URL: http_error(400),
    }
    routes.update(override)
    return routes


def service(fetch, now):
    router = ms.SourceRouter(clock=lambda: now[0], wall=lambda: WALL)
    return cp.PriceService(fetch_json=fetch, clock=lambda: now[0], router=router)


class SourceParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_exchange_ticker_day_open_and_candles(self):
        fetch = FakeHttp({
            ms.HTX_MERGED_URL: htx_merged(),
            ms.HTX_KLINE_URL: lambda p: htx_kline(
                [{"id": MIDNIGHT, "open": 581.03}] if p["period"] == "1day"
                else [{"id": 1_800_000_060, "open": 576.65}, {"id": 1_800_000_000, "open": 575.89}]),
            ms.KUCOIN_STATS_URL: {"code": "200000", "data": {
                "last": "575.93", "changePrice": "13.5", "high": "609.55", "low": "552.34"}},
            ms.KUCOIN_CANDLES_URL: lambda p: {"code": "200000", "data": (
                [[str(MIDNIGHT), "582.46", "593.38", "596.09", "581.88", "1", "1"]] if p["type"] == "1hour"
                else [["1800000060", "575.32", "575.15", "1", "1", "1", "1"]])},
            ms.GATE_TICKER_URL: [{"currency_pair": "BTC_USDT", "last": "80881.7", "change_percentage": "-0.9",
                                  "high_24h": "82285.8", "low_24h": "80817"}],
            ms.GATE_CANDLES_URL: lambda p: (
                [[str(MIDNIGHT), "1", "81176.1", "81252.1", "81038.3", "81077.8", "1", "true"]]
                if p["interval"] == "1h" else [["1800000000", "1", "80886.1", "1", "1", "80883", "1", "true"]]),
            ms.MEXC_TICKER_URL: {"symbol": "XMRUSDT", "lastPrice": "576.01", "openPrice": "561.83",
                                 "highPrice": "609.1", "lowPrice": "554.27"},
            ms.MEXC_KLINES_URL: lambda p: (
                [[MIDNIGHT * 1000, "582.45", "594.91", "582.08", "592.65", "1", 0, "1"]]
                if p["interval"] == "60m" else [[1_800_000_000_000, "576.17", "1", "1", "1", "1", 0, "1"]]),
        })
        htx, kucoin, gate, mexc = (ms.SOURCES_BY_NAME[n] for n in ("htx", "kucoin", "gate", "mexc"))
        t = await htx.ticker(fetch, "XMR")
        self.assertEqual((t.last, t.open24h, t.high24h, t.low24h),
                         (Decimal("575.71"), Decimal("563.07"), Decimal("607.67"), Decimal("557.52")))
        self.assertEqual(fetch.calls[-1][1], {"symbol": "xmrusdt"})
        self.assertEqual(await htx.day_open(fetch, "XMR", MIDNIGHT), Decimal("581.03"))
        self.assertEqual(await htx.day_open(fetch, "XMR", MIDNIGHT + 86400), None)  # 不是当天 K 线
        self.assertEqual(await htx.candles_1m(fetch, "XMR", 1_800_000_100),
                         [(1_800_000_060.0, Decimal("576.65")), (1_800_000_000.0, Decimal("575.89"))])
        t = await kucoin.ticker(fetch, "XMR")
        self.assertEqual((t.last, t.open24h), (Decimal("575.93"), Decimal("562.43")))
        self.assertEqual(await kucoin.day_open(fetch, "XMR", MIDNIGHT), Decimal("582.46"))
        self.assertEqual(await kucoin.candles_1m(fetch, "XMR", 1_800_000_100), [(1_800_000_060.0, Decimal("575.32"))])
        t = await gate.ticker(fetch, "BTC")
        self.assertEqual(t.last, Decimal("80881.7"))
        self.assertAlmostEqual(float(t.open24h), 80881.7 / 0.991, places=4)
        self.assertEqual(await gate.day_open(fetch, "BTC", MIDNIGHT), Decimal("81077.8"))
        self.assertEqual(await gate.candles_1m(fetch, "BTC", 0), [(1_800_000_000.0, Decimal("80883"))])
        t = await mexc.ticker(fetch, "XMR")
        self.assertEqual((t.last, t.open24h, t.high24h), (Decimal("576.01"), Decimal("561.83"), Decimal("609.1")))
        self.assertEqual(await mexc.day_open(fetch, "XMR", MIDNIGHT), Decimal("582.45"))
        self.assertEqual(await mexc.candles_1m(fetch, "XMR", 0), [(1_800_000_000.0, Decimal("576.17"))])

    async def test_not_listed_and_unavailable_mapping(self):
        fetch = FakeHttp({
            ms.HTX_MERGED_URL: HTX_INVALID,
            ms.KUCOIN_STATS_URL: KUCOIN_NULL,
            ms.GATE_TICKER_URL: http_error(400),
            ms.MEXC_TICKER_URL: {"msg": "invalid symbol", "code": -1121},
        })
        for name in ("htx", "kucoin", "gate", "mexc"):
            with self.assertRaises(ms.NotListed, msg=name):
                await ms.SOURCES_BY_NAME[name].ticker(fetch, "NOPE")
        fetch.routes[ms.MEXC_TICKER_URL] = http_error(429)
        with self.assertRaises(ms.Unavailable) as ctx:
            await ms.SOURCES_BY_NAME["mexc"].ticker(fetch, "XMR")
        self.assertEqual(ctx.exception.cooldown, ms.RATE_LIMIT_COOLDOWN_SECONDS)
        fetch.routes[ms.KUCOIN_STATS_URL] = {"code": "429000", "msg": "Too Many Requests"}
        with self.assertRaises(ms.Unavailable):
            await ms.SOURCES_BY_NAME["kucoin"].ticker(fetch, "XMR")
        fetch.routes[ms.HTX_MERGED_URL] = httpx.ConnectError("boom")
        with self.assertRaises(ms.Unavailable):
            await ms.SOURCES_BY_NAME["htx"].ticker(fetch, "XMR")


class PriceFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_xmr_from_htx_with_source_label_changes_and_route_cache(self):
        fetch = FakeHttp(xmr_routes())
        now = [100.0]
        svc = service(fetch, now)
        quote = await svc.quote("xmr")
        self.assertEqual(quote.source, "火币")
        self.assertEqual(quote.sod_utc8, Decimal("581.03"))
        text = cp.quote_text(quote, datetime(2026, 10, 19, 3, 0, tzinfo=cp.BJT))
        self.assertIn("💹 XMR/USDT（火币 现货）", text)
        self.assertIn("来源：火币", text)
        self.assertIn("最新价：575.71 USDT", text)
        self.assertIn("24h 涨跌：+2.24% 📈", text)                # (575.71-563.07)/563.07
        self.assertIn("日涨跌（北京时间0点起）：-0.92%", text)     # (575.71-581.03)/581.03
        self.assertIn("24h 最高 / 最低：607.67 / 557.52", text)
        self.assertIn("¥4,029.97", text)
        self.assertEqual(fetch.urls(), [cp.TICKER_URL, ms.HTX_MERGED_URL, ms.HTX_KLINE_URL])
        self.assertEqual(svc.router.route("XMR"), "htx")
        # 价格缓存过期后：直接走火币，不再查 OKX；日开盘价当天只取一次
        fetch.calls.clear()
        now[0] += 11
        await svc.quote("XMR")
        self.assertEqual(fetch.urls(), [ms.HTX_MERGED_URL])
        # 来源缓存过期后重新先查 OKX
        fetch.calls.clear()
        now[0] += ms.ROUTE_TTL_SECONDS + 1
        await svc.quote("XMR")
        self.assertEqual(fetch.urls()[0], cp.TICKER_URL)

    async def test_okx_coin_unchanged(self):
        fetch = FakeHttp({cp.TICKER_URL: {"code": "0", "data": [
            {"last": "100", "open24h": "95", "high24h": "101", "low24h": "94", "sodUtc8": "99"}]}})
        quote = await service(fetch, [0.0]).quote("BTC")
        text = cp.quote_text(quote)
        self.assertIn("（OKX 现货）", text)
        self.assertNotIn("来源：", text)
        self.assertEqual(fetch.urls(), [cp.TICKER_URL])

    async def test_next_exchange_when_htx_missing_or_rate_limited(self):
        kucoin = {"code": "200000", "data": {"last": "575.93", "changePrice": "13.5", "high": "609", "low": "552"}}
        candles = {"code": "200000", "data": [[str(MIDNIGHT), "582.46", "1", "1", "1", "1", "1"]]}
        fetch = FakeHttp(xmr_routes(**{ms.HTX_MERGED_URL: HTX_INVALID, ms.KUCOIN_STATS_URL: kucoin,
                                       ms.KUCOIN_CANDLES_URL: candles}))
        quote = await service(fetch, [0.0]).quote("XMR")
        self.assertEqual(quote.source, "KuCoin")
        self.assertEqual(quote.sod_utc8, Decimal("582.46"))
        self.assertIn("来源：KuCoin", cp.quote_text(quote))
        # 火币 429 → 冷却，下次不再请求火币
        now = [0.0]
        fetch = FakeHttp(xmr_routes(**{ms.HTX_MERGED_URL: http_error(429), ms.KUCOIN_STATS_URL: kucoin,
                                       ms.KUCOIN_CANDLES_URL: candles}))
        svc = service(fetch, now)
        self.assertEqual((await svc.quote("XMR")).source, "KuCoin")
        self.assertEqual(svc.router.route("XMR"), "kucoin")
        fetch.calls.clear()
        now[0] += 11
        svc._cache.clear()
        svc.router._routes.clear()
        await svc.quote("XMR")
        self.assertNotIn(ms.HTX_MERGED_URL, fetch.urls())
        fetch.calls.clear()
        now[0] += ms.RATE_LIMIT_COOLDOWN_SECONDS
        svc._cache.clear()
        svc.router._routes.clear()
        await svc.quote("XMR")
        self.assertIn(ms.HTX_MERGED_URL, fetch.urls())

    async def test_timeout_skips_slow_exchange(self):
        async def slow():
            await asyncio.sleep(5)
            return htx_merged()
        mexc = {"lastPrice": "576.01", "openPrice": "561.83", "highPrice": "609", "lowPrice": "554"}
        fetch = FakeHttp(xmr_routes(**{ms.HTX_MERGED_URL: lambda p: slow(), ms.MEXC_TICKER_URL: mexc,
                                       ms.MEXC_KLINES_URL: []}))
        with mock.patch.object(ms, "SOURCE_TIMEOUT_SECONDS", 0.05), \
                mock.patch.object(ms.call, "__defaults__", (0.05,)):
            started = time.monotonic()
            quote = await service(fetch, [0.0]).quote("XMR")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(quote.source, "MEXC")
        self.assertIsNone(quote.sod_utc8)  # 取不到当天开盘价时不显示日涨跌，24h 涨跌照常
        self.assertIn("24h 涨跌：+2.52%", cp.quote_text(quote))

    async def test_all_missing_negative_cache_and_all_unavailable(self):
        fetch = FakeHttp(xmr_routes(**{ms.HTX_MERGED_URL: HTX_INVALID}))
        now = [0.0]
        svc = service(fetch, now)
        with self.assertRaises(cp.PriceError) as ctx:
            await svc.quote("NOPE")
        self.assertIn("OKX 没有 NOPE/USDT 交易对，火币、KuCoin、Gate.io、MEXC 也都没有", str(ctx.exception))
        fetch.calls.clear()
        now[0] += 11
        with self.assertRaises(cp.PriceError):
            await svc.quote("NOPE")
        self.assertEqual(fetch.urls(), [cp.TICKER_URL])  # 负缓存：不再逐个问备用交易所
        down = FakeHttp({cp.TICKER_URL: OKX_MISSING, ms.HTX_MERGED_URL: http_error(503),
                         ms.KUCOIN_STATS_URL: httpx.ReadTimeout("t"), ms.GATE_TICKER_URL: http_error(429),
                         ms.MEXC_TICKER_URL: httpx.ConnectError("x")})
        with self.assertRaises(cp.PriceError) as ctx:
            await service(down, [0.0]).quote("XMR")
        self.assertIn("备用交易所行情暂时查不到", str(ctx.exception))

    async def test_okx_down_uses_fallback_without_caching_route(self):
        fetch = FakeHttp(xmr_routes(**{cp.TICKER_URL: httpx.ConnectError("okx down")}))
        svc = service(fetch, [0.0])
        quote = await svc.quote("XMR")
        self.assertEqual(quote.source, "火币")
        self.assertEqual(svc.router.route("XMR"), "")
        all_down = FakeHttp({cp.TICKER_URL: httpx.ConnectError("x"), ms.HTX_MERGED_URL: http_error(500),
                             ms.KUCOIN_STATS_URL: http_error(500), ms.GATE_TICKER_URL: http_error(500),
                             ms.MEXC_TICKER_URL: http_error(500)})
        with self.assertRaises(cp.PriceError) as ctx:
            await service(all_down, [0.0]).quote("BTC")
        self.assertIn("连接不上", str(ctx.exception))

    def test_xmr_is_known_bare_symbol(self):
        self.assertEqual(cp.bare_symbol("xmr", cp.known_symbols("")), "XMR")


def okx_tickers(prices):
    return {"code": "0", "data": [
        {"instId": f"{s}-USDT", "last": str(p), "sodUtc8": "100", "open24h": "100"} for s, p in prices.items()]}


class PollerFallbackTest(unittest.IsolatedAsyncioTestCase):
    def make(self, xmr_price, okx=None):
        state = {"xmr": Decimal(xmr_price), "okx": okx}

        def okx_handler(params):
            return state["okx"] if state["okx"] is not None else okx_tickers({"BTC": 100})

        def kline(params):
            if params["period"] == "1day":
                return htx_kline([{"id": MIDNIGHT, "open": 500}])
            # 1 分钟 K 线：最新在前，10 分钟前开盘价 560
            base = int(self.now[0]) // 60 * 60
            return htx_kline([{"id": base - 60 * i, "open": 550 + i} for i in range(11)])
        fetch = FakeHttp({
            ca.TICKERS_URL: okx_handler,
            ca.CANDLES_URL: {"code": "0", "data": []},
            ms.HTX_MERGED_URL: lambda p: htx_merged(close=str(state["xmr"]), open_="560"),
            ms.HTX_KLINE_URL: kline,
            ms.KUCOIN_STATS_URL: KUCOIN_NULL, ms.GATE_TICKER_URL: http_error(400),
            ms.MEXC_TICKER_URL: http_error(400),
        })
        self.now = [float(MIDNIGHT + 3 * 3600)]
        router = ms.SourceRouter(clock=lambda: self.now[0], wall=lambda: self.now[0])
        poller = ca.AlertPoller(fetch_json=fetch, clock=lambda: self.now[0], router=router)
        return poller, fetch, state

    async def test_xmr_snapshot_daily_and_10min_from_htx(self):
        poller, fetch, state = self.make("575")
        snaps, refs = await poller.snapshot({"BTC", "XMR"}, {"XMR"})
        xmr = snaps["XMR"]
        self.assertEqual((xmr.source, xmr.last, xmr.sod_utc8, xmr.open24h),
                         ("火币", Decimal("575"), Decimal("500"), Decimal("560")))
        self.assertEqual(xmr.daily_change, Decimal("15"))
        self.assertEqual(snaps["BTC"].source, "OKX")
        self.assertEqual(refs["XMR"], Decimal("560"))   # 火币 1 分钟 K 线补齐的 10 分钟前价格
        fired = ca.evaluate({"daily_pct": 10, "fast_pct": 2, "last_daily_up": "", "last_daily_down": "",
                             "last_fast_at": 0}, xmr, refs["XMR"], self.now[0], "2026-10-19")
        self.assertEqual([(f.kind, f.direction) for f in fired], [("daily", "up"), ("fast", "up")])
        text = ca.alert_text("XMR", xmr, fired, Decimal("7"), fast_change=None)
        self.assertIn("数据来源：火币 现货 USDT 交易对", text)
        self.assertIn("触发：10分钟涨跌 上涨 +2.68%", text)
        # 之后每轮：OKX 一次 + 火币行情一次（来源已缓存、日开盘价已缓存、历史已补齐）
        fetch.calls.clear()
        self.now[0] += 30
        state["xmr"] = Decimal("580")
        snaps, _ = await poller.snapshot({"BTC", "XMR"}, {"XMR"})
        self.assertEqual(fetch.urls(), [ca.TICKERS_URL, ms.HTX_MERGED_URL])
        self.assertEqual(snaps["XMR"].last, Decimal("580"))

    async def test_okx_down_keeps_routed_fallback_coins(self):
        poller, fetch, state = self.make("575")
        await poller.snapshot({"BTC", "XMR"}, set())
        state["okx"] = {"code": "50011", "msg": "Too Many Requests"}
        fetch.calls.clear()
        snaps, _ = await poller.snapshot({"BTC", "XMR"}, set())
        self.assertEqual(set(snaps), {"XMR"})
        # 没有任何币能取到行情时仍抛错，交给后台任务记录日志
        with self.assertRaises(Exception):
            await poller.snapshot({"BTC"}, set())

    async def test_unknown_coin_ignored(self):
        poller, fetch, _ = self.make("575")
        fetch.routes[ms.HTX_MERGED_URL] = HTX_INVALID
        snaps, refs = await poller.snapshot({"BTC", "NOPE"}, {"NOPE"})
        self.assertEqual(set(snaps), {"BTC"})
        fetch.calls.clear()
        self.now[0] += 30
        await poller.snapshot({"BTC", "NOPE"}, {"NOPE"})
        self.assertEqual(fetch.urls(), [ca.TICKERS_URL])  # 负缓存


class BotFallbackTest(AppBase):
    async def test_xmr_price_button_and_alert_end_to_end(self):
        fetch = FakeHttp(xmr_routes())
        router = ms.SourceRouter(wall=lambda: WALL)
        self.app.bot_data[handlers.MARKET_ROUTER_KEY] = router
        self.app.bot_data[handlers.PRICE_SERVICE_KEY] = cp.PriceService(fetch_json=fetch, router=router)
        context = SimpleNamespace(application=self.app, bot=self.app.bot)
        text = await handlers.price_reply_text(context, "XMR")
        self.assertIn("XMR/USDT（火币 现货）", text)
        self.assertIn("来源：火币", text)
        buttons = [b.callback_data for r in handlers.price_refresh_markup("XMR").inline_keyboard for b in r]
        self.assertIn("palert:set:XMR", buttons)
        # 「🔔 监控此币涨跌」向导
        await self.run_wizard("XMR", "5", "2")
        self.assertIn("✅ 已设置 XMR 涨跌监控", "\n".join(self.texts()))
        # 后台监控：OKX 行情里没有 XMR → 火币，共用同一个来源缓存
        clock = [time.time()]
        poll_fetch = FakeHttp({
            ca.TICKERS_URL: okx_tickers({"BTC": 100}),
            ms.HTX_MERGED_URL: htx_merged(close="620", open_="560"),
            ms.HTX_KLINE_URL: lambda p: htx_kline([]),
        })
        self.app.bot_data[handlers.PRICE_ALERT_POLLER_KEY] = ca.AlertPoller(
            fetch_json=poll_fetch, clock=lambda: clock[0], router=handlers.market_router(self.app.bot_data))
        self.request.calls.clear()
        await handlers.poll_price_alerts(context)
        self.assertNotIn(ms.KUCOIN_STATS_URL, poll_fetch.urls())  # 来源缓存命中，直接火币
        sent = [p["text"] for api, p in self.request.calls if api == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertIn("🔔 XMR 涨跌提醒", sent[0])
        self.assertIn("数据来源：火币 现货 USDT 交易对", sent[0])
        # 日涨跌用查价时已缓存的火币北京时间 0 点开盘价 581.03
        self.assertIn("日涨跌（北京时间0点起） 上涨 +6.71%（阈值 5%）", sent[0])


if __name__ == "__main__":
    unittest.main()
