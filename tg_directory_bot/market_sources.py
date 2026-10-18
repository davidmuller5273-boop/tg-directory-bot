"""备用行情源：OKX 没有某币的 USDT 现货交易对时（例如 XMR 已下架），依次尝试
火币(HTX)、KuCoin、Gate.io、MEXC 的公开接口（无需 API Key）。

- 每个币种记住可用的来源（默认 6 小时），之后直接走该交易所，不再逐个试；
  到期后重新先查 OKX（防止 OKX 重新上架）。
- 所有交易所都没有的代码负缓存 10 分钟，避免乱输的代码反复请求 5 家交易所。
- 单次请求有超时；遇到 429/418 限流或超时，该交易所冷却一段时间后再用，
  其余交易所照常尝试，不会让整次查询失败或卡住。
- 日涨跌（北京时间 0 点起）用北京时间 0 点那根 K 线的开盘价，每币每天只取一次；
  10 分钟涨跌的历史补齐用各交易所 1 分钟 K 线。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import httpx

FetchJson = Callable[[str, dict], Awaitable[Any]]
BJT = timezone(timedelta(hours=8))

SOURCE_TIMEOUT_SECONDS = 6
ROUTE_TTL_SECONDS = 6 * 3600
NOT_LISTED_TTL_SECONDS = 600
RATE_LIMIT_COOLDOWN_SECONDS = 60
TIMEOUT_COOLDOWN_SECONDS = 20
DAY_OPEN_RETRY_SECONDS = 120

HTX_MERGED_URL = "https://api.huobi.pro/market/detail/merged"
HTX_KLINE_URL = "https://api.huobi.pro/market/history/kline"
KUCOIN_STATS_URL = "https://api.kucoin.com/api/v1/market/stats"
KUCOIN_CANDLES_URL = "https://api.kucoin.com/api/v1/market/candles"
GATE_TICKER_URL = "https://api.gateio.ws/api/v4/spot/tickers"
GATE_CANDLES_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/24hr"
MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"


class SourceError(Exception):
    pass


class NotListed(SourceError):
    """该交易所没有这个 USDT 交易对。"""


class Unavailable(SourceError):
    """超时、限流、网络或数据格式问题（暂时性）。"""

    def __init__(self, message: str = "", cooldown: float = 0) -> None:
        super().__init__(message)
        self.cooldown = cooldown


@dataclass
class SourceTicker:
    last: Decimal
    open24h: Decimal
    high24h: Decimal
    low24h: Decimal


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        raise Unavailable("empty value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Unavailable(f"bad number {value!r}") from None
    if not result.is_finite():
        raise Unavailable(f"bad number {value!r}")
    return result


def _ticker(last: Any, open_: Any, high: Any, low: Any) -> SourceTicker:
    if last is None or last == "":
        raise NotListed("no last price")
    last_d = _dec(last)
    if last_d <= 0:
        raise NotListed("zero price")

    def opt(value: Any) -> Decimal:
        try:
            parsed = _dec(value)
        except Unavailable:
            return last_d
        return parsed if parsed > 0 else last_d
    return SourceTicker(last_d, opt(open_), opt(high), opt(low))


async def call(fetch: FetchJson, url: str, params: dict,
               timeout: float = SOURCE_TIMEOUT_SECONDS) -> Any:
    """Fetch JSON and map failures to NotListed / Unavailable."""
    try:
        return await asyncio.wait_for(fetch(url, params), timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise Unavailable("timeout", TIMEOUT_COOLDOWN_SECONDS) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (400, 404):
            # Gate.io / MEXC 对不存在的交易对返回 400
            raise NotListed(f"HTTP {status}") from exc
        if status in (403, 418, 429):
            raise Unavailable(f"rate limited HTTP {status}", RATE_LIMIT_COOLDOWN_SECONDS) from exc
        raise Unavailable(f"HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise Unavailable(f"network: {exc}") from exc
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都只算该源暂时不可用
        raise Unavailable(f"error: {exc!r}") from exc


class Source:
    name = ""
    label = ""

    async def ticker(self, fetch: FetchJson, symbol: str) -> SourceTicker:
        raise NotImplementedError

    async def candles_1m(self, fetch: FetchJson, symbol: str, now: float, limit: int = 11) -> list[tuple[float, Decimal]]:
        """[(开盘时间秒, 开盘价)]。"""
        raise NotImplementedError

    async def day_open(self, fetch: FetchJson, symbol: str, midnight: int) -> Decimal | None:
        """北京时间 0 点那根小时/日 K 线的开盘价。"""
        raise NotImplementedError


class HTXSource(Source):
    name, label = "htx", "火币"

    @staticmethod
    def _check(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise Unavailable("bad payload")
        if payload.get("status") != "ok":
            code = str(payload.get("err-code") or "")
            if code in {"invalid-parameter", "bad-request", "base-symbol-error"} or "symbol" in str(payload.get("err-msg") or ""):
                raise NotListed(code)
            if "limit" in code or "too-many" in code:
                raise Unavailable(code, RATE_LIMIT_COOLDOWN_SECONDS)
            raise Unavailable(code or "status not ok")
        return payload

    async def ticker(self, fetch, symbol):
        payload = self._check(await call(fetch, HTX_MERGED_URL, {"symbol": f"{symbol.lower()}usdt"}))
        tick = payload.get("tick") or {}
        return _ticker(tick.get("close"), tick.get("open"), tick.get("high"), tick.get("low"))

    async def candles_1m(self, fetch, symbol, now, limit=11):
        payload = self._check(await call(
            fetch, HTX_KLINE_URL, {"symbol": f"{symbol.lower()}usdt", "period": "1min", "size": str(limit)}))
        return [(float(row["id"]), _dec(row["open"])) for row in payload.get("data") or []]

    async def day_open(self, fetch, symbol, midnight):
        # 火币日 K 线按北京时间（UTC+8）0 点切分
        payload = self._check(await call(
            fetch, HTX_KLINE_URL, {"symbol": f"{symbol.lower()}usdt", "period": "1day", "size": "1"}))
        for row in payload.get("data") or []:
            if int(row.get("id") or 0) == midnight:
                return _dec(row["open"])
        return None


class KuCoinSource(Source):
    name, label = "kucoin", "KuCoin"

    @staticmethod
    def _check(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise Unavailable("bad payload")
        code = str(payload.get("code"))
        if code == "200000":
            return payload
        if code.startswith("429"):
            raise Unavailable(code, RATE_LIMIT_COOLDOWN_SECONDS)
        if code in {"400100", "900001"}:
            raise NotListed(code)
        raise Unavailable(code)

    async def ticker(self, fetch, symbol):
        data = self._check(await call(fetch, KUCOIN_STATS_URL, {"symbol": f"{symbol}-USDT"})).get("data") or {}
        last = data.get("last")
        if last in (None, ""):
            raise NotListed("no last")  # 不存在的交易对返回全 null
        open_ = None
        if data.get("changePrice") not in (None, ""):
            open_ = _dec(last) - _dec(data["changePrice"])
        return _ticker(last, open_, data.get("high"), data.get("low"))

    async def candles_1m(self, fetch, symbol, now, limit=11):
        start = int(now) - 60 * limit
        payload = self._check(await call(fetch, KUCOIN_CANDLES_URL, {
            "symbol": f"{symbol}-USDT", "type": "1min", "startAt": str(start), "endAt": str(int(now))}))
        # [time, open, close, high, low, volume, turnover]
        return [(float(row[0]), _dec(row[1])) for row in payload.get("data") or []]

    async def day_open(self, fetch, symbol, midnight):
        payload = self._check(await call(fetch, KUCOIN_CANDLES_URL, {
            "symbol": f"{symbol}-USDT", "type": "1hour", "startAt": str(midnight), "endAt": str(midnight + 60)}))
        for row in payload.get("data") or []:
            if int(row[0]) == midnight:
                return _dec(row[1])
        return None


class GateSource(Source):
    name, label = "gate", "Gate.io"

    async def ticker(self, fetch, symbol):
        payload = await call(fetch, GATE_TICKER_URL, {"currency_pair": f"{symbol}_USDT"})
        if isinstance(payload, dict) and payload.get("label"):
            raise NotListed(str(payload.get("label")))
        if not isinstance(payload, list):
            raise Unavailable("bad payload")
        if not payload:
            raise NotListed("empty")
        row = payload[0]
        last = row.get("last")
        open_ = None
        if last not in (None, "") and row.get("change_percentage") not in (None, ""):
            ratio = 1 + _dec(row["change_percentage"]) / 100
            if ratio > 0:
                open_ = _dec(last) / ratio
        return _ticker(last, open_, row.get("high_24h"), row.get("low_24h"))

    async def candles_1m(self, fetch, symbol, now, limit=11):
        payload = await call(fetch, GATE_CANDLES_URL, {
            "currency_pair": f"{symbol}_USDT", "interval": "1m", "limit": str(limit)})
        if not isinstance(payload, list):
            raise Unavailable("bad payload")
        # [time, quote_volume, close, high, low, open, base_volume, closed]
        return [(float(row[0]), _dec(row[5])) for row in payload]

    async def day_open(self, fetch, symbol, midnight):
        payload = await call(fetch, GATE_CANDLES_URL, {
            "currency_pair": f"{symbol}_USDT", "interval": "1h", "from": str(midnight), "to": str(midnight + 60)})
        if not isinstance(payload, list):
            raise Unavailable("bad payload")
        for row in payload:
            if int(row[0]) == midnight:
                return _dec(row[5])
        return None


class MEXCSource(Source):
    name, label = "mexc", "MEXC"

    @staticmethod
    def _check(payload: Any) -> Any:
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200"):
            if str(payload.get("code")) in {"-1121", "-1100", "30014"}:
                raise NotListed(str(payload.get("code")))
            raise Unavailable(str(payload.get("code")))
        return payload

    async def ticker(self, fetch, symbol):
        row = self._check(await call(fetch, MEXC_TICKER_URL, {"symbol": f"{symbol}USDT"}))
        if isinstance(row, list):
            row = row[0] if row else {}
        if not isinstance(row, dict):
            raise Unavailable("bad payload")
        return _ticker(row.get("lastPrice"), row.get("openPrice"), row.get("highPrice"), row.get("lowPrice"))

    async def candles_1m(self, fetch, symbol, now, limit=11):
        payload = self._check(await call(fetch, MEXC_KLINES_URL, {
            "symbol": f"{symbol}USDT", "interval": "1m", "limit": str(limit)}))
        if not isinstance(payload, list):
            raise Unavailable("bad payload")
        # [openTime(ms), open, high, low, close, volume, closeTime, quoteVolume]
        return [(int(row[0]) / 1000, _dec(row[1])) for row in payload]

    async def day_open(self, fetch, symbol, midnight):
        payload = self._check(await call(fetch, MEXC_KLINES_URL, {
            "symbol": f"{symbol}USDT", "interval": "60m",
            "startTime": str(midnight * 1000), "endTime": str((midnight + 3599) * 1000)}))
        if not isinstance(payload, list):
            raise Unavailable("bad payload")
        for row in payload:
            if int(row[0]) == midnight * 1000:
                return _dec(row[1])
        return None


FALLBACK_SOURCES: tuple[Source, ...] = (HTXSource(), KuCoinSource(), GateSource(), MEXCSource())
SOURCES_BY_NAME = {source.name: source for source in FALLBACK_SOURCES}
FALLBACK_LABELS = "、".join(source.label for source in FALLBACK_SOURCES)


def bjt_midnight(wall: float) -> tuple[int, str]:
    now = datetime.fromtimestamp(wall, BJT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp()), midnight.strftime("%Y-%m-%d")


class SourceRouter:
    """记住每个币种走哪个备用交易所，并处理限流冷却。"""

    def __init__(self, sources: tuple[Source, ...] = FALLBACK_SOURCES,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.sources = sources
        self.by_name = {source.name: source for source in sources}
        self.clock = clock
        self.wall = wall
        self._routes: dict[str, tuple[str, float]] = {}
        self._not_listed: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._day_open: dict[tuple[str, str], tuple[str, Decimal | None, float]] = {}

    # ---- 缓存 ----
    def route(self, symbol: str) -> str:
        """缓存的备用来源名（未过期），没有则返回空串。"""
        item = self._routes.get(symbol)
        if not item:
            return ""
        if self.clock() - item[1] >= ROUTE_TTL_SECONDS:
            self._routes.pop(symbol, None)
            return ""
        return item[0]

    def label(self, name: str) -> str:
        source = self.by_name.get(name)
        return source.label if source else name

    def _cooling(self, name: str) -> bool:
        until = self._cooldown.get(name, 0)
        return bool(until and self.clock() < until)

    def _note_failure(self, name: str, exc: Unavailable) -> None:
        if exc.cooldown:
            self._cooldown[name] = self.clock() + exc.cooldown

    async def fallback_ticker(self, fetch: FetchJson, symbol: str,
                              okx_missing: bool = True) -> tuple[str, SourceTicker]:
        """Try cached source first, then the rest in order.

        okx_missing=True: OKX 明确没有该交易对 → 结果写入来源缓存 / 负缓存。
        okx_missing=False: OKX 只是暂时连不上 → 临时借用备用源，不改缓存。
        Raises NotListed (all sources said no) or Unavailable."""
        now = self.clock()
        if okx_missing:
            until = self._not_listed.get(symbol, 0)
            if until and now < until:
                raise NotListed(symbol)
        cached = self.route(symbol)
        order = sorted(self.sources, key=lambda s: s.name != cached)
        unavailable = False
        for source in order:
            if self._cooling(source.name):
                unavailable = True
                continue
            try:
                ticker = await source.ticker(fetch, symbol)
            except NotListed:
                if source.name == cached:
                    self._routes.pop(symbol, None)
                continue
            except Unavailable as exc:
                self._note_failure(source.name, exc)
                unavailable = True
                continue
            if okx_missing:
                self._routes[symbol] = (source.name, self.clock())
                self._not_listed.pop(symbol, None)
            return source.name, ticker
        if unavailable:
            raise Unavailable(f"{symbol}: fallback sources unavailable")
        if okx_missing:
            self._not_listed[symbol] = self.clock() + NOT_LISTED_TTL_SECONDS
            if len(self._not_listed) > 1000:
                self._not_listed.clear()
        raise NotListed(symbol)

    async def day_open(self, fetch: FetchJson, name: str, symbol: str) -> Decimal | None:
        """北京时间当天 0 点开盘价；失败返回 None（调用方退回 24h 开盘价）。"""
        source = self.by_name.get(name)
        if source is None:
            return None
        midnight, day = bjt_midnight(self.wall())
        key = (name, symbol)
        cached = self._day_open.get(key)
        now = self.clock()
        if cached and cached[0] == day and (cached[1] is not None or now - cached[2] < DAY_OPEN_RETRY_SECONDS):
            return cached[1]
        value: Decimal | None = None
        if not self._cooling(name):
            try:
                value = await source.day_open(fetch, symbol, midnight)
            except Unavailable as exc:
                self._note_failure(name, exc)
            except SourceError:
                value = None
            except (IndexError, KeyError, TypeError, ValueError):
                value = None
        if value is not None and value <= 0:
            value = None
        self._day_open[key] = (day, value, now)
        if len(self._day_open) > 2000:
            self._day_open.clear()
        return value

    async def candles_1m(self, fetch: FetchJson, name: str, symbol: str, now: float,
                         limit: int = 11) -> list[tuple[float, Decimal]]:
        source = self.by_name.get(name)
        if source is None or self._cooling(name):
            return []
        try:
            return await source.candles_1m(fetch, symbol, now, limit)
        except Unavailable as exc:
            self._note_failure(name, exc)
        except (SourceError, IndexError, KeyError, TypeError, ValueError):
            pass
        return []
