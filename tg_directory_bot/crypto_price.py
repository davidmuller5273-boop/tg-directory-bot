"""💹 币价查询：OKX 现货 USDT 价格 + 人民币换算 + 24h 涨跌。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import httpx

MENU_BUTTON_TEXT = "💹 币价"
TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
FX_URL = "https://open.er-api.com/v6/latest/USD"
PRICE_CACHE_SECONDS = 10
FX_CACHE_SECONDS = 3600
MAX_EXTRA_SYMBOLS = 50
SETTING_EXTRA = "price_extra_symbols"

# 市值靠前的常见币种（OKX 有 USDT 现货交易对）
DEFAULT_SYMBOLS = (
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "SHIB", "DOT", "LINK", "BCH", "LTC", "NEAR", "POL", "UNI", "ICP", "ETC",
    "APT", "XLM", "FIL", "ATOM", "ARB", "OP", "HBAR", "SUI", "PEPE", "WIF",
    "BONK", "FLOKI", "AAVE", "GRT", "ALGO", "SAND", "MANA", "AXS", "EOS", "XTZ",
    "IMX", "RENDER", "LDO", "CRV", "STX", "TIA", "JUP", "ORDI", "WLD", "FET",
    "ENS", "DYDX", "GALA", "INJ", "SEI", "USDC", "OKB", "TRUMP", "ONDO", "ENA",
)
# 单独发送时容易和日常聊天混淆的短词：只有全大写才触发
AMBIGUOUS = {"OP", "ONE", "GAS", "ENS", "ARB", "SAND", "MANA", "JUP", "WIF", "FET"}

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")
_TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_BTC_RE = re.compile(r"^(?:[13][1-9A-HJ-NP-Za-km-z]{25,34}|bc1[02-9ac-hj-np-z]{11,71})$", re.I)
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_QUERY_RE = re.compile(r"^\s*币价\s*[:：]?\s*([A-Za-z0-9]{2,12})\s*$")

BJT = timezone(timedelta(hours=8))


class PriceError(Exception):
    """User-facing failure."""


def is_address(text: str) -> bool:
    raw = (text or "").strip()
    return bool(_TRON_RE.match(raw) or _BTC_RE.match(raw) or _EVM_RE.match(raw))


def normalize_symbol(text: str) -> str:
    raw = (text or "").strip()
    if not _SYMBOL_RE.match(raw) or is_address(raw):
        return ""
    return raw.upper()


def known_symbols(extra: str | list[str] | None = "") -> set[str]:
    if isinstance(extra, str):
        extra_list = [s for s in re.split(r"[\s,，]+", extra) if s]
    else:
        extra_list = list(extra or [])
    return set(DEFAULT_SYMBOLS) | {s.upper() for s in extra_list if normalize_symbol(s)}


def parse_query(text: str) -> str:
    """「币价 btc」/「币价BTC」→ "BTC"（任何合法代码，不限已知列表）。"""
    match = _QUERY_RE.match(text or "")
    return normalize_symbol(match.group(1)) if match else ""


def bare_symbol(text: str, symbols: set[str]) -> str:
    """单独发送一个已知币种代码时触发；地址不触发。"""
    raw = (text or "").strip()
    symbol = normalize_symbol(raw)
    if not symbol or symbol not in symbols or symbol == "USDT":
        return ""
    if symbol in AMBIGUOUS and raw != symbol:
        return ""
    return symbol


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PriceError("行情数据格式异常，请稍后再试。")


def format_price(value: Decimal) -> str:
    value = Decimal(value)
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    # 小于 1：保留 6 位有效数字
    text = f"{value:.12f}".rstrip("0")
    digits = text.split(".")[1]
    lead = len(digits) - len(digits.lstrip("0"))
    return f"{value:.{min(12, lead + 6)}f}".rstrip("0").rstrip(".")


@dataclass
class Quote:
    symbol: str
    last: Decimal
    open24h: Decimal
    high24h: Decimal
    low24h: Decimal
    cny_rate: Decimal | None
    rate_source: str
    fetched_at: float

    @property
    def change_pct(self) -> Decimal:
        if not self.open24h:
            return Decimal("0")
        return (self.last - self.open24h) / self.open24h * 100


FetchJson = Callable[[str, dict], Awaitable[Any]]
RateFetcher = Callable[[], Awaitable[Decimal]]


async def _default_fetch(url: str, params: dict) -> Any:
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers={"user-agent": "Mozilla/5.0 TGDirectoryBot/2.0"})
        response.raise_for_status()
        return response.json()


class PriceService:
    def __init__(self, fetch_json: FetchJson | None = None,
                 c2c_rate: RateFetcher | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.fetch_json = fetch_json or _default_fetch
        self.c2c_rate = c2c_rate
        self.clock = clock
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._fx: tuple[float, Decimal] | None = None

    async def cny_rate(self) -> tuple[Decimal | None, str]:
        if self.c2c_rate is not None:
            try:
                rate = await self.c2c_rate()
                if rate and rate > 0:
                    return Decimal(rate), "OKX C2C"
            except Exception:  # noqa: BLE001 - 退回公开汇率
                pass
        now = self.clock()
        if self._fx and now - self._fx[0] < FX_CACHE_SECONDS:
            return self._fx[1], "美元汇率"
        try:
            payload = await self.fetch_json(FX_URL, {})
            rate = _decimal((payload.get("rates") or {})["CNY"])
        except Exception:  # noqa: BLE001
            return None, ""
        self._fx = (now, rate)
        return rate, "美元汇率"

    async def quote(self, symbol: str) -> Quote:
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise PriceError("币种代码格式不正确，例如：币价 btc")
        if symbol == "USDT":
            raise PriceError("USDT 对人民币价格请用 /rate 查看 OKX 商户报价。")
        now = self.clock()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < PRICE_CACHE_SECONDS:
            return cached[1]
        try:
            payload = await self.fetch_json(TICKER_URL, {"instId": f"{symbol}-USDT"})
        except httpx.HTTPError as exc:
            raise PriceError("行情服务暂时连接不上，请稍后再试。") from exc
        except Exception as exc:  # noqa: BLE001
            raise PriceError("行情服务暂时不可用，请稍后再试。") from exc
        if str(payload.get("code")) != "0" or not payload.get("data"):
            raise PriceError(f"OKX 没有 {symbol}/USDT 交易对，请检查币种代码。")
        row = payload["data"][0]
        rate, source = await self.cny_rate()
        quote = Quote(
            symbol, _decimal(row["last"]), _decimal(row.get("open24h") or row["last"]),
            _decimal(row.get("high24h") or row["last"]), _decimal(row.get("low24h") or row["last"]),
            rate, source, now,
        )
        self._cache[symbol] = (now, quote)
        if len(self._cache) > 500:
            self._cache.clear()
        return quote


def quote_text(quote: Quote, now: datetime | None = None) -> str:
    change = quote.change_pct
    arrow = "📈" if change > 0 else ("📉" if change < 0 else "➖")
    lines = [
        f"💹 {quote.symbol}/USDT（OKX 现货）",
        f"最新价：{format_price(quote.last)} USDT",
    ]
    if quote.cny_rate:
        lines.append(
            f"折合：¥{format_price(quote.last * quote.cny_rate)}"
            f"（{quote.rate_source} 1 USDT≈¥{quote.cny_rate.quantize(Decimal('0.01'))}）"
        )
    else:
        lines.append("折合人民币：汇率暂时获取失败")
    lines.append(f"24h 涨跌：{'+' if change > 0 else ''}{change.quantize(Decimal('0.01'))}% {arrow}")
    lines.append(f"24h 最高 / 最低：{format_price(quote.high24h)} / {format_price(quote.low24h)}")
    stamp = (now or datetime.now(BJT)).astimezone(BJT).strftime("%H:%M:%S")
    lines.append(f"更新时间：{stamp}（北京时间）")
    return "\n".join(lines)


def group_enabled(settings: dict[str, str], chat_id: int) -> bool:
    return settings.get(f"price_enabled:{chat_id}", "1") != "0"


HELP_TEXT = (
    "💹 币价查询\n\n"
    "• 发送 /price btc，或发送「币价 btc」查询任意币种。\n"
    "• 直接发送常见币种代码（如 BTC、ETH、SOL）也会回复价格。\n"
    "• 价格来自 OKX 现货 USDT 交易对，人民币按 OKX C2C 价格换算（失败时用美元汇率）。\n"
    "• 群管理员可在 群设置 → 💹 币价 中开关本群的币价回复。"
)
