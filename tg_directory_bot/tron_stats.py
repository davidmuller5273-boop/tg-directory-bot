"""Visitor statistics for the public /tron-draw page.

* 使用次数 = one page open (the HTML page load).  API polls never count as a use.
* 用户 = a unique visitor: HMAC(random server-side secret, client IP + NUL + User-Agent), truncated.
  Raw IPs / user agents are never stored or logged.
* 今日 = Asia/Shanghai calendar day; 历史 = all time (daily aggregate table + compact visitor-hash table).
* 在线 = distinct visitors seen by a poll heartbeat in the last 60 s (in memory only, no DB write per poll).
* Localhost callers without a forwarded header (the bot itself) and obvious crawlers are never counted.
Everything is best-effort: any failure is swallowed so the page is never slowed or broken.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Callable, Mapping

from .time_utils import BEIJING_TZ

LOGGER = logging.getLogger(__name__)

ONLINE_WINDOW_SECONDS = 60
USE_DEBOUNCE_SECONDS = 3         # the same visitor reloading faster than this counts once
SNAPSHOT_CACHE_SECONDS = 5
_BOT_UA = re.compile(
    r"bot|spider|crawl|slurp|curl|wget|python|httpclient|http-client|go-http|libwww|scrapy|headless|"
    r"monitor|uptime|preview|facebookexternalhit|lighthouse|java/|node-fetch|axios",
    re.IGNORECASE,
)
_LOOPBACK = {"127.0.0.1", "::1"}


def is_bot_user_agent(user_agent: str) -> bool:
    ua = (user_agent or "").strip()
    return not ua or bool(_BOT_UA.search(ua))


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def resolve_client_ip(peer: str | None, real_ip: str = "", forwarded_for: str = "") -> str | None:
    """Real client IP, or None for a local (non-visitor) caller.

    X-Real-IP / X-Forwarded-For are trusted ONLY when the TCP peer is loopback (our nginx).  Direct
    connections ignore them, so a visitor cannot spoof an address.  X-Forwarded-For uses its LAST entry
    (the one nginx appended); the left part is client-controlled.  Loopback with no header = the bot itself.
    """
    peer_ip = _valid_ip(peer or "")
    if peer_ip is None:
        return None
    if peer_ip in _LOOPBACK:
        candidate = _valid_ip(real_ip)
        if candidate is None and forwarded_for:
            for part in reversed([p for p in forwarded_for.split(",") if p.strip()]):
                candidate = _valid_ip(part)
                break
        if candidate is None or candidate in _LOOPBACK:
            return None
        return candidate
    return peer_ip


class TronStats:
    def __init__(self, store, now: Callable[[], float] = time.time) -> None:
        self.store = store
        self._now = now
        self._lock = threading.Lock()
        self._secret: bytes | None = None
        self._online: dict[str, float] = {}
        self._last_day: dict[str, str] = {}
        self._last_use: dict[str, float] = {}
        self._cache: tuple[float, dict] | None = None
        self._ready = False

    # ---- helpers ----
    def today(self) -> str:
        return datetime.fromtimestamp(self._now(), BEIJING_TZ).strftime("%Y-%m-%d")

    def _ensure_tables(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tron_stats_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tron_stats_daily (
                    day TEXT PRIMARY KEY, uses INTEGER NOT NULL DEFAULT 0, users INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tron_stats_visitors (
                    visitor TEXT PRIMARY KEY, first_day TEXT NOT NULL, last_day TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )
        self._ready = True

    def _get_secret(self) -> bytes:
        if self._secret is None:
            self._ensure_tables()
            with self.store.connect() as conn:
                row = conn.execute("SELECT value FROM tron_stats_meta WHERE key='secret'").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT OR IGNORE INTO tron_stats_meta (key, value) VALUES ('secret', ?)",
                        (secrets.token_hex(32),),
                    )
                    row = conn.execute("SELECT value FROM tron_stats_meta WHERE key='secret'").fetchone()
            self._secret = str(row["value"]).encode()
        return self._secret

    def visitor_id(self, ip: str, user_agent: str) -> str:
        message = f"{ip}\0{user_agent or ''}".encode("utf-8", "replace")
        return hmac.new(self._get_secret(), message, hashlib.sha256).hexdigest()[:32]

    def _identify(self, ip: str | None, user_agent: str) -> str | None:
        if ip is None or is_bot_user_agent(user_agent):
            return None
        return self.visitor_id(ip, user_agent)

    # ---- recording ----
    def _touch(self, visitor: str, day: str, count_use: bool) -> None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT last_day FROM tron_stats_visitors WHERE visitor=?", (visitor,)).fetchone()
            first_today = row is None or str(row["last_day"]) != day
            if row is None:
                conn.execute(
                    "INSERT INTO tron_stats_visitors (visitor, first_day, last_day) VALUES (?, ?, ?)",
                    (visitor, day, day),
                )
            elif first_today:
                conn.execute("UPDATE tron_stats_visitors SET last_day=? WHERE visitor=?", (day, visitor))
            uses = 1 if count_use else 0
            users = 1 if first_today else 0
            if uses or users:
                conn.execute(
                    """INSERT INTO tron_stats_daily (day, uses, users) VALUES (?, ?, ?)
                       ON CONFLICT(day) DO UPDATE SET uses = uses + excluded.uses, users = users + excluded.users""",
                    (day, uses, users),
                )

    def _prune(self, now: float) -> None:
        if len(self._online) > 5000:
            cutoff = now - 300
            self._online = {k: v for k, v in self._online.items() if v >= cutoff}
        if len(self._last_day) > 50000:
            self._last_day.clear()
        if len(self._last_use) > 20000:
            self._last_use = {k: v for k, v in self._last_use.items() if v >= now - 60}

    def record_use(self, ip: str | None, user_agent: str) -> None:
        """Count one page open.  Never raises."""
        try:
            visitor = self._identify(ip, user_agent)
            if visitor is None:
                return
            now = self._now()
            with self._lock:
                if now - self._last_use.get(visitor, -1e9) < USE_DEBOUNCE_SECONDS:
                    return
                self._last_use[visitor] = now
                self._online[visitor] = now
                day = self.today()
                self._touch(visitor, day, count_use=True)
                self._last_day[visitor] = day
                self._cache = None
                self._prune(now)
        except Exception:
            LOGGER.warning("tron stats record_use failed", exc_info=False)

    def heartbeat(self, ip: str | None, user_agent: str) -> None:
        """Mark a visitor online (poll request).  Memory only, except once per visitor per new day
        (a tab left open past midnight still counts as a user of the new day).  Never raises."""
        try:
            visitor = self._identify(ip, user_agent)
            if visitor is None:
                return
            now = self._now()
            self._online[visitor] = now
            day = self.today()
            if self._last_day.get(visitor) != day:
                with self._lock:
                    self._touch(visitor, day, count_use=False)
                    self._last_day[visitor] = day
                    self._cache = None
            if len(self._online) > 5000:
                with self._lock:
                    self._prune(now)
        except Exception:
            LOGGER.warning("tron stats heartbeat failed", exc_info=False)

    # ---- reading ----
    def snapshot(self) -> dict:
        """{history_uses, history_users, today_uses, today_users, online}; zeros on any failure."""
        now = self._now()
        empty = {"history_uses": 0, "history_users": 0, "today_uses": 0, "today_users": 0, "online": 0}
        try:
            cached = self._cache
            day = self.today()
            if cached is None or now - cached[0] > SNAPSHOT_CACHE_SECONDS or cached[1].get("_day") != day:
                self._ensure_tables()
                with self.store.connect() as conn:
                    total = conn.execute("SELECT COALESCE(SUM(uses),0) AS n FROM tron_stats_daily").fetchone()["n"]
                    users = conn.execute("SELECT COUNT(*) AS n FROM tron_stats_visitors").fetchone()["n"]
                    today = conn.execute("SELECT uses, users FROM tron_stats_daily WHERE day=?", (day,)).fetchone()
                data = {
                    "history_uses": int(total), "history_users": int(users),
                    "today_uses": int(today["uses"]) if today else 0,
                    "today_users": int(today["users"]) if today else 0,
                    "_day": day,
                }
                cached = (now, data)
                self._cache = cached
            result = {k: v for k, v in cached[1].items() if not k.startswith("_")}
            cutoff = now - ONLINE_WINDOW_SECONDS
            result["online"] = sum(1 for seen in list(self._online.values()) if seen >= cutoff)
            return result
        except Exception:
            LOGGER.warning("tron stats snapshot failed", exc_info=False)
            return empty
