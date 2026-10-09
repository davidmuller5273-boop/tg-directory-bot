"""🔔 币价涨跌监控：日涨跌（北京时间 0 点起）和 10 分钟涨跌提醒。

设计：
- 每个机器人进程一个后台任务，只有存在启用的监控时才请求 OKX；
  每轮只调用一次 /api/v5/market/tickers?instType=SPOT 拿到全部现货行情（公开接口，
  限速 20 次/2 秒，本任务默认 30 秒一次，母/子机器人各自轮询也远低于限速）。
- 10 分钟涨跌：用每轮行情在内存里保存约 15 分钟的价格样本；刚启动或刚添加的币种
  没有 10 分钟历史时，用 1 分钟 K 线（limit=11）补齐，每轮最多补 5 个币种。
- 涨跌都提醒（双向，按绝对值判断）。
- 冷却：日涨跌规则每个北京时间自然日、每个方向（涨/跌）最多提醒一次；
  10 分钟规则提醒后该监控冷却 10 分钟。冷却状态写入数据库，重启后不会重复提醒。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .crypto_price import BJT, FetchJson, _default_fetch, format_price, normalize_symbol

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
FAST_WINDOW_SECONDS = 600
FAST_TOLERANCE_SECONDS = 120
HISTORY_KEEP_SECONDS = 15 * 60
FAST_COOLDOWN_SECONDS = 600
SEED_PER_POLL = 5
SEED_RETRY_SECONDS = 300
MAX_THRESHOLD = Decimal("1000")
DEFAULT_MAX_PER_CHAT = 50
DEFAULT_POLL_SECONDS = 30

# 旧语法里的方向参数：仍然接受，但一律按双向处理
LEGACY_DIRECTION_WORDS = {
    "双向", "both", "all", "涨跌", "只涨", "涨", "up", "上涨", "只跌", "跌", "down", "下跌",
}
DAILY_LABEL = "日涨跌（北京时间0点起）"
FAST_LABEL = "10分钟涨跌"
BUTTON_TEXT = "🔔 监控此币涨跌"
LIST_BUTTON_TEXT = "🔔 我的涨跌监控"

USAGE = (
    "🔔 币价涨跌监控用法：\n"
    "• /pricealert btc 5 2：BTC 日涨跌达 5% 或 10 分钟涨跌达 2% 时提醒\n"
    "• /pricealert btc 5：只监控日涨跌 5%；/pricealert btc 0 2：只监控10分钟涨跌 2%\n"
    "• /pricealert del btc：删除监控；/pricealerts：查看全部\n"
    "• 也可以在币价查询结果下点「🔔 监控此币涨跌」\n"
    "私聊设置提醒发给自己；群里由群管理员设置，提醒发到本群。"
)


def parse_threshold(text: Any) -> Decimal:
    raw = str(text if text is not None else "").strip().rstrip("%％").strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"阈值「{text}」不是有效数字") from None
    if not value.is_finite() or value < 0:
        raise ValueError("阈值不能小于 0")
    if value > MAX_THRESHOLD:
        raise ValueError(f"阈值不能大于 {MAX_THRESHOLD}%")
    return value.quantize(Decimal("0.01"))


@dataclass
class AlertCommand:
    action: str  # set / del / list / help
    symbol: str = ""
    daily: Decimal = Decimal("0")
    fast: Decimal = Decimal("0")


def parse_command(args: Iterable[str]) -> AlertCommand:
    """/pricealert btc 5 2 · /pricealert del btc · /pricealert list（涨跌均提醒）"""
    items = [str(item).strip() for item in args if str(item).strip()]
    if not items:
        return AlertCommand("help")
    head = items[0].casefold()
    if head in {"list", "ls", "列表", "查看"}:
        return AlertCommand("list")
    if head in {"del", "delete", "rm", "删除", "取消"}:
        symbol = normalize_symbol(items[1]) if len(items) > 1 else ""
        if not symbol:
            raise ValueError("用法：/pricealert del btc")
        return AlertCommand("del", symbol)
    symbol = normalize_symbol(items[0])
    if not symbol or symbol == "USDT":
        raise ValueError("币种代码格式不正确，例如：/pricealert btc 5 2")
    if len(items) < 2:
        raise ValueError("请填写阈值，例如：/pricealert btc 5 2（日 5%，10分钟 2%）")
    daily = parse_threshold(items[1])
    fast = parse_threshold(items[2]) if len(items) > 2 else Decimal("0")
    extra = items[3:]
    # 兼容旧语法 /pricealert btc 5 2 涨：方向参数忽略，始终双向
    if extra and extra[0].casefold() in LEGACY_DIRECTION_WORDS:
        extra = extra[1:]
    if extra:
        raise ValueError("参数过多，例如：/pricealert btc 5 2（日涨跌 5%，10分钟涨跌 2%，涨跌都提醒）")
    if daily <= 0 and fast <= 0:
        raise ValueError("日涨跌和10分钟涨跌至少设置一个大于 0 的阈值")
    return AlertCommand("set", symbol, daily, fast)


def pct_text(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    return f"{'+' if value > 0 else ''}{value}%"


def threshold_text(value: Any) -> str:
    value = Decimal(str(value or 0))
    if value <= 0:
        return "不监控"
    return f"{value.normalize():f}%"


def monitor_summary(row: Any) -> str:
    return (
        f"{row['symbol']}：日涨跌 {threshold_text(row['daily_pct'])}，"
        f"10分钟 {threshold_text(row['fast_pct'])}"
    )


# ---- 行情快照与历史 -------------------------------------------------------

@dataclass
class Snapshot:
    symbol: str
    last: Decimal
    sod_utc8: Decimal
    open24h: Decimal

    @property
    def daily_change(self) -> Decimal | None:
        base = self.sod_utc8 or self.open24h
        if not base:
            return None
        return (self.last - base) / base * 100


def _dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def parse_tickers(payload: Any) -> dict[str, Snapshot]:
    result: dict[str, Snapshot] = {}
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return result
    for row in payload.get("data") or []:
        inst = str(row.get("instId") or "")
        if not inst.endswith("-USDT"):
            continue
        symbol = inst[:-5]
        last = _dec(row.get("last"))
        if last <= 0:
            continue
        result[symbol] = Snapshot(symbol, last, _dec(row.get("sodUtc8")), _dec(row.get("open24h")))
    return result


@dataclass
class PriceHistory:
    samples: dict[str, deque] = field(default_factory=dict)

    def record(self, symbol: str, ts: float, price: Decimal) -> None:
        bucket = self.samples.setdefault(symbol, deque())
        if bucket and ts < bucket[-1][0]:
            # 补入较早的样本（K 线），保持时间顺序
            items = sorted([*bucket, (ts, price)], key=lambda item: item[0])
            bucket.clear()
            bucket.extend(items)
        else:
            bucket.append((ts, price))
        newest = bucket[-1][0]
        while len(bucket) > 1 and bucket[0][0] < newest - HISTORY_KEEP_SECONDS:
            bucket.popleft()

    def price_ago(self, symbol: str, now: float, seconds: int = FAST_WINDOW_SECONDS) -> Decimal | None:
        target = now - seconds
        best = None
        for ts, price in self.samples.get(symbol, ()):
            if ts <= target:
                best = (ts, price)
            else:
                break
        if best is None or best[0] < target - FAST_TOLERANCE_SECONDS:
            return None
        return best[1]

    def seed_candles(self, symbol: str, payload: Any) -> bool:
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            return False
        rows = payload.get("data") or []
        added = False
        for row in rows:
            try:
                ts = int(row[0]) / 1000
                price = _dec(row[1])
            except (IndexError, TypeError, ValueError):
                continue
            if price > 0:
                self.record(symbol, ts, price)
                added = True
        return added


# ---- 规则判断 ---------------------------------------------------------------

@dataclass
class Fired:
    kind: str        # daily / fast
    direction: str   # up / down
    change: Decimal
    threshold: Decimal


def evaluate(row: Any, snapshot: Snapshot, ref10: Decimal | None,
             now_ts: float, today: str) -> list[Fired]:
    """Return the rules that fire now (cooldowns already applied). Always
    bidirectional: a rise or a fall of at least the threshold fires."""
    fired: list[Fired] = []
    daily_pct = _dec(row["daily_pct"])
    fast_pct = _dec(row["fast_pct"])
    daily = snapshot.daily_change
    if daily_pct > 0 and daily is not None and abs(daily) >= daily_pct and daily != 0:
        sign = "up" if daily > 0 else "down"
        last_day = str(row["last_daily_up" if sign == "up" else "last_daily_down"] or "")
        if last_day != today:
            fired.append(Fired("daily", sign, daily, daily_pct))
    if fast_pct > 0 and ref10:
        change = (snapshot.last - ref10) / ref10 * 100
        if abs(change) >= fast_pct and change != 0:
            sign = "up" if change > 0 else "down"
            last_fast = float(row["last_fast_at"] or 0)
            if not last_fast or now_ts - last_fast >= FAST_COOLDOWN_SECONDS:
                fired.append(Fired("fast", sign, change, fast_pct))
    return fired


def alert_text(symbol: str, snapshot: Snapshot, fired: list[Fired],
               cny_rate: Decimal | None, now: datetime | None = None,
               fast_change: Decimal | None = None) -> str:
    up = any(item.direction == "up" for item in fired)
    head = "📈" if up else "📉"
    lines = [f"🔔 {symbol} 涨跌提醒 {head}", ""]
    price = f"当前价：{format_price(snapshot.last)} USDT"
    if cny_rate:
        price += f"（≈¥{format_price(snapshot.last * cny_rate)}）"
    lines.append(price)
    for item in fired:
        word = "上涨" if item.direction == "up" else "下跌"
        label = DAILY_LABEL if item.kind == "daily" else FAST_LABEL
        lines.append(
            f"触发：{label} {word} {pct_text(item.change)}（阈值 {threshold_text(item.threshold)}）"
        )
    if not any(item.kind == "daily" for item in fired) and snapshot.daily_change is not None:
        lines.append(f"{DAILY_LABEL}：{pct_text(snapshot.daily_change)}")
    if not any(item.kind == "fast" for item in fired) and fast_change is not None:
        lines.append(f"{FAST_LABEL}：{pct_text(fast_change)}")
    stamp = (now or datetime.now(BJT)).astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"时间：{stamp}（北京时间）")
    lines.append("数据来源：OKX 现货 USDT 交易对")
    return "\n".join(lines)


# ---- 轮询 -------------------------------------------------------------------

class AlertPoller:
    def __init__(self, fetch_json: FetchJson | None = None,
                 clock=time.time) -> None:
        self.fetch_json = fetch_json or _default_fetch
        self.clock = clock
        self.history = PriceHistory()
        self._seed_attempts: dict[str, float] = {}

    async def snapshot(self, symbols: set[str], fast_symbols: set[str]) -> tuple[dict[str, Snapshot], dict[str, Decimal]]:
        """One tickers request for everything; candles only to seed 10-minute history."""
        if not symbols:
            return {}, {}
        now = self.clock()
        payload = await self.fetch_json(TICKERS_URL, {"instType": "SPOT"})
        tickers = parse_tickers(payload)
        result = {symbol: tickers[symbol] for symbol in symbols if symbol in tickers}
        for symbol, snap in tickers.items():
            if symbol in symbols:
                self.history.record(symbol, now, snap.last)
        refs: dict[str, Decimal] = {}
        seeded = 0
        for symbol in sorted(fast_symbols & set(result)):
            ref = self.history.price_ago(symbol, now)
            if ref is None and seeded < SEED_PER_POLL and now - self._seed_attempts.get(symbol, 0) >= SEED_RETRY_SECONDS:
                self._seed_attempts[symbol] = now
                seeded += 1
                try:
                    candles = await self.fetch_json(
                        CANDLES_URL, {"instId": f"{symbol}-USDT", "bar": "1m", "limit": "11"},
                    )
                    self.history.seed_candles(symbol, candles)
                except Exception:  # noqa: BLE001 - 补历史失败下一轮再试
                    pass
                ref = self.history.price_ago(symbol, now)
            if ref is not None:
                refs[symbol] = ref
        # 不再监控的币种不保留历史
        for symbol in list(self.history.samples):
            if symbol not in symbols:
                self.history.samples.pop(symbol, None)
        return result, refs
