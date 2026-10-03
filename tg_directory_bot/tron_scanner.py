"""Block scanner for TRON monitors (replaces per-address high-frequency polling).

One scanner (mother process only) follows the chain head and, per block,
downloads in memory only:

* USDT ``Transfer`` events of the block — TronGrid event server
  (``/v1/contracts/<USDT>/events?block_number=N``, ~4 KB gzip) when an API key
  is configured, otherwise / on empty or failed answers the node receipts
  ``/wallet/gettransactioninfobyblocknum`` (~12 KB gzip). Both contain every
  Transfer log incl. transferFrom and contract-internal transfers.
* the full block ``/wallet/getblockbynum`` (~45 KB gzip, visible=false) — only
  while at least one monitor watches TRX (TransferContract) or balance
  thresholds that TRX fees can move.

Only transfers touching a monitored address are persisted (``matches`` table
in a small SQLite file shared with the clone processes); all other block data
is discarded immediately. Each bot process (mother + clones) publishes the
addresses it monitors and consumes its matches from the same file, so clones
never scan or poll the chain at high frequency themselves.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .chain import (
    USDT_TRC20_CONTRACT, ChainQueryError, TronTransaction, tron_address_hex,
    tron_hex_to_base58,
)

logger = logging.getLogger(__name__)

TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDT_HEX = tron_address_hex(USDT_TRC20_CONTRACT)  # 41-prefixed
SUN = Decimal(1_000_000)
ACTIVITY = "ACTIVITY"


@dataclass(frozen=True)
class ChainEvent:
    tx_id: str
    block_number: int
    timestamp_ms: int
    asset: str  # TRX | USDT | ACTIVITY (any tx sent by the address: fees change TRX)
    sender: str
    recipient: str
    amount: Decimal


def _base58(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("0x"):
        value = "41" + value[2:]
    if len(value) == 42 and value.startswith("41"):
        return tron_hex_to_base58(value)
    return value


def _topic_address(topic: str) -> str:
    topic = str(topic or "")
    return _base58("41" + topic[-40:]) if len(topic) >= 40 else ""


def parse_block_events(block: dict) -> list[ChainEvent]:
    """TRX TransferContract transfers + per-tx owner activity of one block."""
    header = (block.get("block_header") or {}).get("raw_data") or {}
    number = int(header.get("number") or 0)
    timestamp = int(header.get("timestamp") or 0)
    events: list[ChainEvent] = []
    for tx in block.get("transactions") or []:
        rets = tx.get("ret") or [{}]
        if (rets[0] or {}).get("contractRet", "SUCCESS") != "SUCCESS":
            continue
        contracts = (tx.get("raw_data") or {}).get("contract") or []
        if not contracts:
            continue
        contract = contracts[0]
        value = (contract.get("parameter") or {}).get("value") or {}
        owner = _base58(value.get("owner_address") or "")
        tx_id = str(tx.get("txID") or "")
        if not tx_id or not owner:
            continue
        if contract.get("type") == "TransferContract":
            try:
                amount = Decimal(int(value.get("amount") or 0)) / SUN
            except (ValueError, TypeError, InvalidOperation):
                continue
            events.append(ChainEvent(
                tx_id, number, timestamp, "TRX", owner,
                _base58(value.get("to_address") or ""), amount,
            ))
        else:
            events.append(ChainEvent(tx_id, number, timestamp, ACTIVITY, owner, "", Decimal(0)))
    return events


def parse_txinfo_events(infos: list[dict], block_number: int = 0) -> list[ChainEvent]:
    """USDT Transfer logs (+ internal TRX call-value transfers) of one block."""
    events: list[ChainEvent] = []
    for info in infos or []:
        tx_id = str(info.get("id") or "")
        number = int(info.get("blockNumber") or block_number or 0)
        timestamp = int(info.get("blockTimeStamp") or 0)
        if not tx_id:
            continue
        for log in info.get("log") or []:
            address = str(log.get("address") or "").casefold()
            if address not in {USDT_HEX[2:], USDT_HEX}:
                continue
            topics = [str(item).casefold() for item in log.get("topics") or []]
            if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                continue
            try:
                amount = Decimal(int(str(log.get("data") or "0")[:64] or "0", 16)) / SUN
            except (ValueError, InvalidOperation):
                continue
            events.append(ChainEvent(
                tx_id, number, timestamp, "USDT",
                _topic_address(topics[1]), _topic_address(topics[2]), amount,
            ))
        for internal in info.get("internal_transactions") or []:
            if internal.get("rejected"):
                continue
            for call in internal.get("callValueInfo") or []:
                if call.get("tokenId") or not call.get("callValue"):
                    continue
                events.append(ChainEvent(
                    tx_id, number, timestamp, "TRX",
                    _base58(internal.get("caller_address") or ""),
                    _base58(internal.get("transferTo_address") or ""),
                    Decimal(int(call["callValue"])) / SUN,
                ))
    return events


def parse_usdt_event_rows(rows: list[dict]) -> list[ChainEvent]:
    """Rows of TronGrid ``/v1/contracts/<USDT>/events?event_name=Transfer``."""
    events: list[ChainEvent] = []
    for row in rows or []:
        if row.get("event_name") not in (None, "Transfer"):
            continue
        if row.get("contract_address") not in (None, USDT_TRC20_CONTRACT):
            continue
        result = row.get("result") or {}
        try:
            amount = Decimal(int(str(result.get("value", result.get("2", "0"))))) / SUN
        except (ValueError, InvalidOperation):
            continue
        tx_id = str(row.get("transaction_id") or "")
        if not tx_id:
            continue
        events.append(ChainEvent(
            tx_id, int(row.get("block_number") or 0), int(row.get("block_timestamp") or 0),
            "USDT", _base58(result.get("from") or result.get("0") or ""),
            _base58(result.get("to") or result.get("1") or ""), amount,
        ))
    return events


def match_events(
    events: list[ChainEvent], watched: set[str] | dict[str, str],
) -> list[tuple[str, ChainEvent, str]]:
    """(address, event, direction) for every event touching a watched address."""
    found: list[tuple[str, ChainEvent, str]] = []
    for event in events:
        if event.asset == ACTIVITY:
            if event.sender in watched:
                found.append((event.sender, event, "活动"))
            continue
        if event.recipient in watched:
            found.append((event.recipient, event, "转入"))
        if event.sender in watched and event.sender != event.recipient:
            found.append((event.sender, event, "转出"))
    return found


def event_transaction(event: ChainEvent, direction: str) -> TronTransaction:
    counterparty = event.recipient if direction == "转出" else event.sender
    return TronTransaction(
        event.tx_id, event.timestamp_ms, direction, event.asset, event.amount,
        counterparty, event.block_number,
    )


def shared_scan_db_path(config) -> Path | None:
    base = ""
    if getattr(config, "is_clone", False):
        base = str(getattr(config, "mother_db_path", "") or "")
    else:
        base = str(getattr(config, "db_path", "") or "")
    if not base:
        return None
    return Path(base).resolve().parent / "tron-scan.sqlite3"


class ScanDB:
    """Small SQLite file shared by mother + clones (matches only, no block data)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.init()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scan_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watch (
                    process_key TEXT NOT NULL, address TEXT NOT NULL,
                    assets TEXT NOT NULL DEFAULT 'both', updated_at REAL NOT NULL,
                    PRIMARY KEY (process_key, address));
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL, tx_id TEXT NOT NULL,
                    direction TEXT NOT NULL, asset TEXT NOT NULL,
                    amount TEXT NOT NULL, counterparty TEXT NOT NULL DEFAULT '',
                    block_number INTEGER NOT NULL, timestamp_ms INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (address, tx_id, direction, asset, amount));
                CREATE INDEX IF NOT EXISTS idx_matches_created ON matches(created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str, default: str = "") -> str:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM scan_state WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def set(self, key: str, value: object) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO scan_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()

    def publish_watch(self, process_key: str, addresses: dict[str, str]) -> None:
        now = time.time()
        conn = self.connect()
        try:
            conn.execute("DELETE FROM watch WHERE process_key=?", (process_key,))
            conn.executemany(
                "INSERT INTO watch (process_key, address, assets, updated_at) VALUES (?,?,?,?)",
                [(process_key, address, assets, now) for address, assets in addresses.items()],
            )
            conn.commit()
        finally:
            conn.close()

    def watched(self, max_age: float = 180.0) -> dict[str, str]:
        """address -> 'usdt' | 'trx' | 'both' (union over live processes)."""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT address, assets FROM watch WHERE updated_at >= ?",
                (time.time() - max_age,),
            ).fetchall()
        finally:
            conn.close()
        merged: dict[str, set[str]] = {}
        for row in rows:
            parts = {"usdt", "trx"} if row["assets"] == "both" else {str(row["assets"])}
            merged.setdefault(str(row["address"]), set()).update(parts)
        return {
            address: ("both" if parts >= {"usdt", "trx"} else next(iter(parts)))
            for address, parts in merged.items()
        }

    def watch_count(self, exclude: str = "", max_age: float = 180.0) -> int:
        """Addresses watched by the other live bot processes."""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM watch WHERE updated_at >= ? AND process_key <> ?",
                (time.time() - max_age, exclude),
            ).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def add_matches(self, rows: list[tuple[str, TronTransaction]]) -> int:
        if not rows:
            return 0
        now = time.time()
        conn = self.connect()
        try:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO matches (address, tx_id, direction, asset, amount, "
                "counterparty, block_number, timestamp_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (address, tx.tx_id, tx.direction, tx.asset, str(tx.amount),
                     tx.counterparty, tx.block_number, tx.timestamp_ms, now)
                    for address, tx in rows
                ],
            )
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()

    def max_match_id(self) -> int:
        conn = self.connect()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM matches").fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def matches_after(self, match_id: int, limit: int = 1000) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT * FROM matches WHERE id > ? ORDER BY id LIMIT ?",
                (int(match_id), int(limit)),
            ).fetchall()
        finally:
            conn.close()

    def prune(self, max_age: float = 3 * 86400) -> None:
        conn = self.connect()
        try:
            conn.execute("DELETE FROM matches WHERE created_at < ?", (time.time() - max_age,))
            conn.execute("DELETE FROM watch WHERE updated_at < ?", (time.time() - 86400,))
            conn.commit()
        finally:
            conn.close()

    def scanner_healthy(self, max_age: float = 60.0) -> bool:
        try:
            return time.time() - float(self.get("heartbeat", "0") or 0) <= max_age
        except ValueError:
            return False


def row_transaction(row) -> TronTransaction:
    return TronTransaction(
        str(row["tx_id"]), int(row["timestamp_ms"]), str(row["direction"]),
        str(row["asset"]), Decimal(str(row["amount"])), str(row["counterparty"]),
        int(row["block_number"]),
    )


class TronBlockScanner:
    """Follows the head (with ``lag`` confirmations) block by block."""

    def __init__(
        self, chain, db: ScanDB, *, lag: int = 1, max_catchup: int = 1200,
        use_events: bool = True, max_blocks_per_step: int = 40,
        time_budget: float = 20.0,
    ):
        self.chain = chain
        self.db = db
        self.lag = max(0, int(lag))
        self.max_catchup = max(20, int(max_catchup))
        self.use_events = use_events
        self.max_blocks_per_step = max(1, int(max_blocks_per_step))
        self.time_budget = time_budget
        self.head = 0
        self.head_ts = 0
        self._next_head_check = 0.0
        self.stats = {"blocks": 0, "matches": 0, "requests": 0, "skipped": 0}

    async def _refresh_head(self, force: bool = False) -> None:
        now = time.time()
        if not force and now < self._next_head_check:
            return
        head, head_ts = await self.chain.tron_head()
        self.stats["requests"] += 1
        if head:
            self.head, self.head_ts = head, head_ts
        # TRON produces a block every 3 s: ask again right after the next one
        expected = (self.head_ts / 1000 + 3.3) if self.head_ts else 0
        self._next_head_check = expected if expected > now else now + 1.0

    async def _usdt_events(self, number: int) -> list[ChainEvent]:
        if self.use_events:
            try:
                rows = await self.chain.tron_usdt_events(number)
                self.stats["requests"] += 1
                if rows:
                    return parse_usdt_event_rows(rows)
                # empty: block without USDT transfers or event server not
                # indexed yet -> confirm with the node receipts (never miss)
            except ChainQueryError:
                pass
        infos = await self.chain.tron_block_txinfo(number)
        self.stats["requests"] += 1
        return parse_txinfo_events(infos, number)

    async def scan_block(self, number: int, watched: dict[str, str]) -> int:
        need_usdt = any(assets in {"usdt", "both"} for assets in watched.values())
        need_block = any(assets in {"trx", "both"} for assets in watched.values())
        events: list[ChainEvent] = []
        if need_usdt:
            events.extend(await self._usdt_events(number))
        if need_block:
            block = await self.chain.tron_block(number)
            self.stats["requests"] += 1
            if not block.get("block_header"):
                raise ChainQueryError(f"区块 {number} 尚不可用")
            events.extend(parse_block_events(block))
        rows: list[tuple[str, TronTransaction]] = []
        for address, event, direction in match_events(events, watched):
            assets = watched.get(address, "both")
            if event.asset != ACTIVITY and assets != "both" and event.asset.casefold() != assets:
                continue
            rows.append((address, event_transaction(event, direction)))
        # events list (block data) goes out of scope here: nothing else is kept
        added = self.db.add_matches(rows)
        self.stats["matches"] += added
        self.stats["blocks"] += 1
        return added

    async def step(self) -> int:
        """Scan newly confirmed blocks; returns the number of blocks scanned."""
        watched = self.db.watched()
        if not watched:
            # nothing to watch: idle, and restart from the head later
            self.db.set("last_block", "")
            self.db.set("heartbeat", time.time())
            return 0
        last_raw = self.db.get("last_block", "")
        if not last_raw or self.head - self.lag <= int(last_raw or 0):
            await self._refresh_head(force=not last_raw or not self.head)
        if not self.head:
            return 0
        target = self.head - self.lag
        if not last_raw:
            self.db.set("last_block", target)
            self.db.set("heartbeat", time.time())
            logger.info("TRON block scanner starting at block %s", target)
            return 0
        last = int(last_raw)
        if target - last > self.max_catchup:
            skipped_to = target - self.max_catchup
            logger.warning(
                "TRON scanner far behind (%s blocks); skipping %s..%s, "
                "per-address reconciliation will cover the gap",
                target - last, last + 1, skipped_to,
            )
            self.db.set("skipped", json.dumps({
                "from": last + 1, "to": skipped_to, "at": int(time.time()),
            }))
            self.db.set("reconcile_requested", time.time())
            self.stats["skipped"] += skipped_to - last
            last = skipped_to
            self.db.set("last_block", last)
        scanned = 0
        deadline = time.monotonic() + self.time_budget
        while last < target and scanned < self.max_blocks_per_step and time.monotonic() < deadline:
            number = last + 1
            await self.scan_block(number, watched)
            last = number
            scanned += 1
            self.db.set("last_block", last)
        self.db.set("heartbeat", time.time())
        self.db.set("head", self.head)
        return scanned
