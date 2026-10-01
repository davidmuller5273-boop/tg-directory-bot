from __future__ import annotations

import json
import math
import os
import sqlite3
import secrets
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from datetime import datetime, timezone

from .time_utils import BEIJING_TZ
from .validation import Submission



ENTRY_KEYWORD_SUFFIX = "地址"
_INVISIBLE_KEYWORD_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def entry_keyword_key(value: str, suffix: str = ENTRY_KEYWORD_SUFFIX) -> str:
    """Normalize a 收录 keyword for exact matching and de-duplication.

    NFKC (full-width -> half-width), drop all whitespace and zero-width chars,
    casefold, then strip one trailing suffix (default 「地址」) so that
    「XX」 and 「XX地址」 share the same key.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_INVISIBLE_KEYWORD_CHARS)
    text = "".join(text.split()).casefold()
    suffix_key = "".join(unicodedata.normalize("NFKC", str(suffix or "")).split()).casefold()
    if suffix_key and len(text) > len(suffix_key) and text.endswith(suffix_key):
        text = text[:-len(suffix_key)]
    return text


POINTS_QUANT = Decimal("0.01")
POINTS_ABS_MAX = Decimal("1000000000")


def normalize_points(value) -> Decimal:
    """Parse/quantize point amounts to 2 decimal places (ROUND_HALF_UP)."""
    if isinstance(value, bool):
        raise ValueError("积分无效")
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, int):
            amount = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("积分无效")
            amount = Decimal(str(value))
        elif isinstance(value, str):
            raw = value.strip().replace(",", ".")
            if not raw or raw.lower() in {"nan", "inf", "+inf", "-inf"}:
                raise ValueError("积分无效")
            amount = Decimal(raw)
        else:
            raise ValueError("积分无效")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("积分无效") from exc
    if not amount.is_finite():
        raise ValueError("积分无效")
    amount = amount.quantize(POINTS_QUANT, rounding=ROUND_HALF_UP)
    if abs(amount) > POINTS_ABS_MAX:
        raise ValueError("积分超出范围")
    return amount


def format_points(value) -> str:
    """Display points, stripping useless trailing zeros (10.50→10.5, 10.00→10)."""
    amount = normalize_points(value)
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def points_to_db(value) -> float:
    """Persist quantized points as SQLite REAL."""
    return float(normalize_points(value))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(frozen=True)
class Entry:
    id: int
    url: str
    title: str
    category: str
    description: str
    status: str
    user_id: int
    username: str
    reason: str
    created_at: str
    updated_at: str
    reports_count: int
    content_text: str = ""
    media_file_id: str = ""
    media_type: str = ""
    media_name: str = ""
    source_chat_id: int = 0
    source_message_id: int = 0
    entities_json: str = "[]"
    buttons_json: str = "[]"
    copy_chat_id: int = 0
    copy_message_id: int = 0
    keyword_key: str = ""


class DirectoryStore:
    _REUSABLE_ID_TABLES = {"custom_buttons", "point_gifts", "raffles"}

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    @classmethod
    def _smallest_available_id(
        cls, conn: sqlite3.Connection, table: str
    ) -> int:
        if table not in cls._REUSABLE_ID_TABLES:
            raise ValueError("unsupported reusable ID table")
        row = conn.execute(
            f"""SELECT CASE
                    WHEN NOT EXISTS (SELECT 1 FROM {table} WHERE id=1) THEN 1
                    ELSE COALESCE(
                        (SELECT MIN(current.id + 1)
                           FROM {table} AS current
                           LEFT JOIN {table} AS following
                             ON following.id=current.id + 1
                          WHERE following.id IS NULL),
                        1
                    )
                END AS next_id"""
        ).fetchone()
        return int(row["next_id"])

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    reports_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_ads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    position TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT,
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, position)
                );
                CREATE TABLE IF NOT EXISTS group_points_config (
                    chat_id INTEGER PRIMARY KEY,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    checkin_min INTEGER NOT NULL DEFAULT 5,
                    checkin_max INTEGER NOT NULL DEFAULT 5,
                    streak_bonus INTEGER NOT NULL DEFAULT 2,
                    activity_enabled INTEGER NOT NULL DEFAULT 0,
                    activity_messages_min INTEGER NOT NULL DEFAULT 10,
                    activity_messages_max INTEGER NOT NULL DEFAULT 20,
                    activity_points_min INTEGER NOT NULL DEFAULT 1,
                    activity_points_max INTEGER NOT NULL DEFAULT 5,
                    draw_enabled INTEGER NOT NULL DEFAULT 0,
                    draw_cost INTEGER NOT NULL DEFAULT 1,
                    draw_rate_multiplier REAL NOT NULL DEFAULT 1.0,
                    dice_enabled INTEGER NOT NULL DEFAULT 1,
                    dice_odds INTEGER NOT NULL DEFAULT 2000,
                    updated_by INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS point_accounts (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    balance INTEGER NOT NULL DEFAULT 0,
                    earned_total INTEGER NOT NULL DEFAULT 0,
                    spent_total INTEGER NOT NULL DEFAULT 0,
                    invite_source_cycle INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS point_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS point_checkins (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    streak INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS point_activity_rewards (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    message_target INTEGER NOT NULL,
                    points INTEGER NOT NULL DEFAULT 0,
                    awarded_at TEXT,
                    PRIMARY KEY(chat_id, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS point_gifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    points_cost INTEGER NOT NULL,
                    stock INTEGER NOT NULL DEFAULT -1,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS point_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    gift_id INTEGER NOT NULL,
                    gift_name TEXT NOT NULL,
                    points_cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS point_draws (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    gift_id INTEGER NOT NULL,
                    gift_name TEXT NOT NULL,
                    points_spent INTEGER NOT NULL,
                    probability REAL NOT NULL,
                    random_value REAL NOT NULL,
                    is_winner INTEGER NOT NULL DEFAULT 0,
                    redemption_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS point_game_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    game_type TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT '',
                    dice_value INTEGER NOT NULL DEFAULT 0,
                    stake INTEGER NOT NULL DEFAULT 0,
                    delta INTEGER NOT NULL DEFAULT 0,
                    balance_after INTEGER NOT NULL DEFAULT 0,
                    is_win INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(entry_id, user_id),
                    FOREIGN KEY(entry_id) REFERENCES entries(id)
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    language_code TEXT NOT NULL DEFAULT '',
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    submissions_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS bot_usage_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    use_count INTEGER NOT NULL DEFAULT 1,
                    first_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    admin_name TEXT NOT NULL DEFAULT '',
                    telegram_message_id INTEGER,
                    target_admin_id INTEGER NOT NULL DEFAULT 0,
                    support_button_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    total INTEGER NOT NULL DEFAULT 0,
                    sent INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message',
                    body TEXT NOT NULL,
                    broadcast_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    FOREIGN KEY(broadcast_id) REFERENCES broadcasts(id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chain_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    address TEXT NOT NULL DEFAULT '',
                    query_type TEXT NOT NULL,
                    result_summary TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    chat_type TEXT NOT NULL DEFAULT 'group',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    joins_count INTEGER NOT NULL DEFAULT 0,
                    leaves_count INTEGER NOT NULL DEFAULT 0,
                    blocked_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_daily_stats (
                    chat_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    messages INTEGER NOT NULL DEFAULT 0,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, day)
                );
                CREATE TABLE IF NOT EXISTS group_activity_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    messages INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS group_message_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_join_config (
                    chat_id INTEGER PRIMARY KEY,
                    welcome_enabled INTEGER NOT NULL DEFAULT 1,
                    welcome_text TEXT NOT NULL DEFAULT '欢迎 {name} 加入本群！',
                    verification_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_by INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_violations (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    violations INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS search_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    normalized_query TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    chat_type TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    result_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS moderation_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    added_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS raffles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    creator_id INTEGER NOT NULL,
                    prize TEXT NOT NULL,
                    winner_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    ends_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    drawn_at TEXT
                );
                CREATE TABLE IF NOT EXISTS raffle_entries (
                    raffle_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(raffle_id, user_id),
                    FOREIGN KEY(raffle_id) REFERENCES raffles(id)
                );
                CREATE TABLE IF NOT EXISTS raffle_winners (
                    raffle_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY(raffle_id, user_id),
                    FOREIGN KEY(raffle_id) REFERENCES raffles(id)
                );
                CREATE TABLE IF NOT EXISTS runtime_status (
                    service TEXT PRIMARY KEY,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS auto_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    match_mode TEXT NOT NULL DEFAULT 'contains',
                    scope TEXT NOT NULL DEFAULT 'private',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    hits INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(keyword, match_mode)
                );
                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'admin',
                    permissions TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS custom_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'support',
                    label TEXT NOT NULL UNIQUE,
                    contact_name TEXT NOT NULL DEFAULT '',
                    contact_username TEXT NOT NULL DEFAULT '',
                    target_user_id INTEGER NOT NULL DEFAULT 0,
                    response_text TEXT NOT NULL DEFAULT '',
                    button_url TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS bot_clones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    bot_id INTEGER NOT NULL UNIQUE,
                    bot_username TEXT NOT NULL DEFAULT '',
                    token_cipher TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    reviewed_by INTEGER NOT NULL DEFAULT 0,
                    reviewed_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS private_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lottery_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    game_code TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    issue TEXT NOT NULL,
                    draw_time TEXT NOT NULL,
                    primary_numbers TEXT NOT NULL,
                    secondary_numbers TEXT NOT NULL DEFAULT '',
                    detail_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(game_code, issue)
                );
                CREATE TABLE IF NOT EXISTS lottery_subscriptions (
                    chat_id INTEGER NOT NULL,
                    selector TEXT NOT NULL,
                    created_by INTEGER NOT NULL DEFAULT 0,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, selector)
                );
                CREATE TABLE IF NOT EXISTS lottery_source_status (
                    source TEXT PRIMARY KEY,
                    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_success_at TEXT,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tron_monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    asset TEXT NOT NULL DEFAULT 'both',
                    notify_transfers INTEGER NOT NULL DEFAULT 1,
                    min_transfer_amount TEXT NOT NULL DEFAULT '0.1',
                    low_balance TEXT NOT NULL DEFAULT '',
                    high_balance TEXT NOT NULL DEFAULT '',
                    last_balance TEXT NOT NULL DEFAULT '',
                    seen_tx_ids TEXT NOT NULL DEFAULT '[]',
                    monitor_state TEXT NOT NULL DEFAULT 'live',
                    cursor_tx_id TEXT NOT NULL DEFAULT '',
                    cursor_block INTEGER NOT NULL DEFAULT 0,
                    cursor_timestamp_ms INTEGER NOT NULL DEFAULT 0,
                    alert_state TEXT NOT NULL DEFAULT '',
                    notification_delete_days INTEGER NOT NULL DEFAULT 7,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT NOT NULL DEFAULT (DATETIME('now','+7 days')),
                    last_checked_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_id, address, asset)
                );
                CREATE TABLE IF NOT EXISTS quick_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '消息1',
                    text TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    button_text TEXT NOT NULL DEFAULT '',
                    button_url TEXT NOT NULL DEFAULT '',
                    share_code TEXT NOT NULL UNIQUE,
                    updated_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS quick_post_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL,
                    url TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT 'default',
                    width TEXT NOT NULL DEFAULT 'long',
                    custom_emoji_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(post_id, position),
                    FOREIGN KEY(post_id) REFERENCES quick_posts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS scheduled_quick_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(post_id) REFERENCES quick_posts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS channel_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS broadcast_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    group_id INTEGER,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    added_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(group_id) REFERENCES channel_groups(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS channel_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '消息1',
                    text TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS channel_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id INTEGER NOT NULL DEFAULT 0,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(message_id) REFERENCES channel_messages(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS group_invite_config (
                    chat_id INTEGER PRIMARY KEY,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    expire_seconds INTEGER NOT NULL DEFAULT 0,
                    member_limit INTEGER NOT NULL DEFAULT 0,
                    points_per_invite INTEGER NOT NULL DEFAULT 0,
                    updated_by INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_invite_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    invite_link TEXT NOT NULL UNIQUE,
                    invite_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS group_invite_joins (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    inviter_id INTEGER NOT NULL,
                    link_id INTEGER NOT NULL,
                    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    left_at TEXT,
                    points_awarded INTEGER NOT NULL DEFAULT 0,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    has_spoken INTEGER NOT NULL DEFAULT 0,
                    last_spoken_at TEXT,
                    first_spoken_name TEXT NOT NULL DEFAULT '',
                    current_display_name TEXT NOT NULL DEFAULT '',
                    name_changed_at TEXT,
                    spoken_messages INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, user_id),
                    FOREIGN KEY(link_id) REFERENCES group_invite_links(id)
                );
                CREATE TABLE IF NOT EXISTS scheduled_message_deletions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    delete_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS group_admin_permissions (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    permissions TEXT NOT NULL DEFAULT '',
                    assigned_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS group_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    actor_user_id INTEGER NOT NULL DEFAULT 0,
                    actor_username TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_member_profiles (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS group_name_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    old_first TEXT NOT NULL DEFAULT '',
                    old_last TEXT NOT NULL DEFAULT '',
                    old_display TEXT NOT NULL DEFAULT '',
                    new_first TEXT NOT NULL DEFAULT '',
                    new_last TEXT NOT NULL DEFAULT '',
                    new_display TEXT NOT NULL DEFAULT '',
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_group_name_history_user
                    ON group_name_history(chat_id, user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
                CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category);
                CREATE INDEX IF NOT EXISTS idx_entries_title ON entries(title);
                CREATE INDEX IF NOT EXISTS idx_group_ads_due
                    ON group_ads(position, is_enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_point_accounts_rank
                    ON point_accounts(chat_id, balance DESC);
                CREATE INDEX IF NOT EXISTS idx_point_ledger_user
                    ON point_ledger(chat_id, user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_point_gifts_chat
                    ON point_gifts(chat_id, is_enabled, points_cost);
                CREATE INDEX IF NOT EXISTS idx_point_draws_user
                    ON point_draws(chat_id, user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_point_game_records_user
                    ON point_game_records(chat_id, user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_support_user ON support_messages(user_id, id);
                CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, id);
                CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_bot_usage_rank
                    ON bot_usage_users(use_count DESC, last_used_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chain_queries_created ON chain_queries(created_at);
                CREATE INDEX IF NOT EXISTS idx_group_activity_day ON group_activity_users(chat_id, day);
                CREATE INDEX IF NOT EXISTS idx_raffles_status_end ON raffles(status, ends_at);
                CREATE INDEX IF NOT EXISTS idx_raffles_chat_status_end
                    ON raffles(chat_id, status, ends_at, id DESC);
                CREATE INDEX IF NOT EXISTS idx_auto_replies_enabled ON auto_replies(is_enabled, match_mode);
                CREATE INDEX IF NOT EXISTS idx_bot_admins_role ON bot_admins(role);
                CREATE INDEX IF NOT EXISTS idx_private_notes_keyword
                    ON private_notes(keyword, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_private_notes_expires ON private_notes(expires_at);
                CREATE INDEX IF NOT EXISTS idx_group_activity_retention_day
                    ON group_activity_users(day);
                CREATE INDEX IF NOT EXISTS idx_group_message_events_range
                    ON group_message_events(chat_id, created_at, user_id);
                CREATE INDEX IF NOT EXISTS idx_group_daily_day ON group_daily_stats(day);
                CREATE INDEX IF NOT EXISTS idx_group_violations_count
                    ON group_violations(chat_id, violations DESC);
                CREATE INDEX IF NOT EXISTS idx_search_events_query
                    ON search_events(normalized_query, id DESC);
                CREATE INDEX IF NOT EXISTS idx_search_events_user
                    ON search_events(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_moderation_keywords_created
                    ON moderation_keywords(id DESC);
                CREATE INDEX IF NOT EXISTS idx_lottery_results_game ON lottery_results(game_code, id DESC);
                CREATE INDEX IF NOT EXISTS idx_lottery_results_issue
                    ON lottery_results(game_code, issue DESC);
                CREATE INDEX IF NOT EXISTS idx_lottery_subscriptions_selector
                    ON lottery_subscriptions(selector, is_enabled);
                CREATE INDEX IF NOT EXISTS idx_tron_monitors_due
                    ON tron_monitors(is_enabled, expires_at, id);
                CREATE INDEX IF NOT EXISTS idx_quick_posts_chat
                    ON quick_posts(chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_quick_post_buttons_post
                    ON quick_post_buttons(post_id, position, id);
                CREATE INDEX IF NOT EXISTS idx_scheduled_quick_posts_due
                    ON scheduled_quick_posts(status, run_at, id);
                CREATE INDEX IF NOT EXISTS idx_broadcast_channels_group
                    ON broadcast_channels(group_id, is_enabled, id);
                CREATE INDEX IF NOT EXISTS idx_channel_schedules_due
                    ON channel_schedules(status, run_at, id);
                CREATE INDEX IF NOT EXISTS idx_group_invite_links_user
                    ON group_invite_links(chat_id, user_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_group_invite_joins_inviter
                    ON group_invite_joins(chat_id, inviter_id, joined_at);
                CREATE INDEX IF NOT EXISTS idx_group_invite_joins_retention
                    ON group_invite_joins(joined_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_deletions_due
                    ON scheduled_message_deletions(delete_at, id);
                CREATE INDEX IF NOT EXISTS idx_group_operations_chat
                    ON group_operations(chat_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_group_operations_recent
                    ON group_operations(chat_id, action, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_point_ledger_retention
                    ON point_ledger(created_at);
                CREATE INDEX IF NOT EXISTS idx_point_draws_retention
                    ON point_draws(created_at);
                CREATE INDEX IF NOT EXISTS idx_point_game_records_retention
                    ON point_game_records(created_at);
                CREATE INDEX IF NOT EXISTS idx_point_redemptions_retention
                    ON point_redemptions(created_at);
                CREATE INDEX IF NOT EXISTS idx_group_admin_permissions_user
                    ON group_admin_permissions(user_id, chat_id);
                CREATE TABLE IF NOT EXISTS group_polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    creator_id INTEGER NOT NULL DEFAULT 0,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '[]',
                    is_anonymous INTEGER NOT NULL DEFAULT 1,
                    allows_multiple INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_group_polls_chat
                    ON group_polls(chat_id, id DESC);
                """
            )
            self._ensure_column(conn, "reports", "status", "TEXT NOT NULL DEFAULT 'open'")
            for table in ("group_ads", "quick_posts"):
                self._ensure_column(conn, table, "entities_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(
                conn, "quick_post_buttons", "width", "TEXT NOT NULL DEFAULT 'long'"
            )
            self._ensure_column(
                conn, "quick_post_buttons", "custom_emoji_id", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(conn, "auto_replies", "scope", "TEXT NOT NULL DEFAULT 'private'")
            self._ensure_column(conn, "group_activity_users", "username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "group_activity_users", "display_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "content_text", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "media_file_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "media_type", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "media_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "source_chat_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "entries", "source_message_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "entries", "keyword_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "entries", "entities_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "entries", "buttons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "entries", "copy_chat_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "entries", "copy_message_id", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_keyword_key ON entries(keyword_key, status)"
            )
            self._ensure_column(conn, "group_ads", "buttons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "group_ads", "source_chat_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "group_ads", "source_message_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "users", "language_code", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "group_points_config", "draw_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "group_points_config", "draw_cost", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(
                conn, "group_points_config", "draw_rate_multiplier",
                "REAL NOT NULL DEFAULT 1.0",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_enabled",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_odds",
                "INTEGER NOT NULL DEFAULT 2000",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_min_bet",
                "REAL NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_max_bet",
                "REAL NOT NULL DEFAULT 0",
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sticker_profiles (
                    user_id INTEGER PRIMARY KEY,
                    fixed_title TEXT NOT NULL DEFAULT '',
                    channel_id INTEGER NOT NULL DEFAULT 0,
                    channel_title TEXT NOT NULL DEFAULT '',
                    channel_username TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            self._ensure_column(
                conn, "group_points_config", "dice_schedule_enabled",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_open_time",
                "TEXT NOT NULL DEFAULT '00:00'",
            )
            self._ensure_column(
                conn, "group_points_config", "dice_close_time",
                "TEXT NOT NULL DEFAULT '23:59'",
            )
            self._ensure_column(
                conn, "point_accounts", "invite_source_cycle",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "point_redemptions", "note",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(conn, "raffles", "raffle_type", "TEXT NOT NULL DEFAULT 'universal'")
            self._ensure_column(conn, "raffles", "activity_start_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "activity_min_messages", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "raffles", "title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "rules_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "raffles", "conditions_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "raffles", "how_to_join", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "join_keyword", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "channel_ref", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "min_messages", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "raffles", "min_boosts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "raffles", "recur_daily", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "raffles", "stats_start_mode", "TEXT NOT NULL DEFAULT 'immediate'")
            self._ensure_column(conn, "raffles", "stats_start_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "template_json", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffles", "min_participants", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "private_notes", "is_permanent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "raffle_winners", "note", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raffle_entries", "via_keyword", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(
                conn, "tron_monitors", "notification_delete_days",
                "INTEGER NOT NULL DEFAULT 7",
            )
            self._ensure_column(
                conn, "tron_monitors", "notify_transfers",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn, "tron_monitors", "min_transfer_amount",
                "TEXT NOT NULL DEFAULT '0.1'",
            )
            self._ensure_column(conn, "tron_monitors", "started_at", "TEXT")
            self._ensure_column(
                conn, "tron_monitors", "monitor_state",
                "TEXT NOT NULL DEFAULT 'live'",
            )
            self._ensure_column(
                conn, "tron_monitors", "cursor_tx_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "tron_monitors", "cursor_block",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "tron_monitors", "cursor_timestamp_ms",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """UPDATE tron_monitors SET started_at=COALESCE(started_at, created_at)
                   WHERE started_at IS NULL OR started_at=''"""
            )
            self._ensure_column(
                conn, "group_invite_joins", "username",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_joins", "display_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_joins", "has_spoken",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "group_invite_joins", "last_spoken_at",
                "TEXT",
            )
            self._ensure_column(
                conn, "group_invite_joins", "first_spoken_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_joins", "current_display_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_joins", "name_changed_at", "TEXT",
            )
            self._ensure_column(
                conn, "group_invite_joins", "spoken_messages",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "group_invite_links", "username",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_links", "display_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "group_invite_config", "points_per_invite",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "point_activity_rewards", "message_baseline",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn, "point_activity_rewards", "reward_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "group_invite_joins", "left_at", "TEXT")
            self._ensure_column(
                conn, "group_invite_joins", "points_awarded",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "bot_admins", "permissions", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "support_messages", "target_admin_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "support_messages", "support_button_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "group_operations", "actor_user_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "group_operations", "actor_username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "group_operations", "actor_name", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """DELETE FROM group_operations
                   WHERE action='setting'
                     AND (detail LIKE 'invite_query:%'
                          OR detail='quickpost:publish')"""
            )
            self._ensure_column(conn, "bot_clones", "status", "TEXT NOT NULL DEFAULT 'pending'")
            self._ensure_column(conn, "bot_clones", "reviewed_by", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "bot_clones", "reviewed_at", "TEXT")
            self._ensure_column(conn, "bot_clones", "parent_clone_id", "INTEGER NOT NULL DEFAULT 0")
            # 旧记录均视为已通知；子机器人提交的新申请写入 notified=0，由母机器人推送审核
            self._ensure_column(conn, "bot_clones", "notified", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "bot_clones", "owner_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "bot_clones", "result_notified", "INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "DELETE FROM group_operations WHERE created_at<DATETIME('now','-7 days')"
            )
            conn.execute(
                "DELETE FROM point_ledger WHERE created_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM point_draws WHERE created_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM point_game_records WHERE created_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM point_redemptions WHERE created_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM point_checkins WHERE created_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM point_activity_rewards WHERE day<DATE('now','-6 months')"
            )
            conn.execute(
                "DELETE FROM group_invite_joins WHERE joined_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                """DELETE FROM group_invite_links
                   WHERE is_active=0 AND created_at<DATETIME('now','-6 months')"""
            )
            conn.execute(
                "DELETE FROM audit_log WHERE created_at<DATETIME('now','-7 days')"
            )
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (
                    ("site_name", "TG 网址收录后台"),
                    ("welcome_text", "欢迎使用网址收录机器人，请从下方菜单选择功能。"),
                    ("maintenance_mode", "0"),
                    ("support_enabled", "1"),
                    ("chain_enabled", "1"),
                    ("rate_enabled", "1"),
                    ("group_monitoring_enabled", "1"),
                    ("group_moderation_enabled", "0"),
                    ("group_block_all_links", "0"),
                    ("not_found_text", "地址没有收录，请联系管理员。发送 /contact 可直接留言。"),
                    ("support_ack_text", "消息已转给管理员，回复会直接发到这里。"),
                    ("auto_reply_enabled", "1"),
                    ("group_keyword_enabled", "1"),
                    ("group_directory_trigger", "地址"),
                    ("group_rate_trigger", "z0"),
                    ("lottery_enabled", "1"),
                    ("lottery_broadcast_enabled", "1"),
                ),
            )
            usage_scope_migrated = conn.execute(
                "SELECT 1 FROM settings WHERE key='bot_usage_selected_scope_v1'"
            ).fetchone()
            if not usage_scope_migrated:
                # Old rows mixed games and passive group activity, so they cannot be filtered reliably.
                conn.execute("DELETE FROM bot_usage_users")
                conn.execute(
                    "INSERT INTO settings (key,value) VALUES ('bot_usage_selected_scope_v1','1')"
                )
            keyword_suffix_done = conn.execute(
                "SELECT 1 FROM settings WHERE key='entry_titles_address_suffix_v1'"
            ).fetchone()
            if not keyword_suffix_done:
                suffix = "地址"
                rows = conn.execute("SELECT id, title FROM entries").fetchall()
                for row in rows:
                    title = str(row["title"] or "").strip()
                    if not title or title.endswith(suffix):
                        continue
                    conn.execute(
                        """UPDATE entries SET title=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        ((title + suffix)[:120], int(row["id"])),
                    )
                conn.execute(
                    "INSERT INTO settings (key,value) VALUES ('entry_titles_address_suffix_v1','1')"
                )
            # 关键词精确匹配键：每次启动校正一次（幂等，不删除任何数据）
            for row in conn.execute("SELECT id, title, keyword_key FROM entries").fetchall():
                key = entry_keyword_key(str(row["title"] or ""))
                if key != str(row["keyword_key"] or ""):
                    conn.execute(
                        "UPDATE entries SET keyword_key=? WHERE id=?", (key, int(row["id"]))
                    )

    def ensure_config_admins(
        self, admin_ids: set[int], super_admin_ids: set[int],
        developer_ids: set[int] | None = None,
    ) -> None:
        hierarchy_v2 = developer_ids is not None
        developer_ids = developer_ids or set()
        with self.connect() as conn:
            if hierarchy_v2:
                migrated = conn.execute(
                    "SELECT value FROM settings WHERE key='role_hierarchy_v2_migrated'"
                ).fetchone()
                if not migrated:
                    conn.execute("UPDATE bot_admins SET role='super' WHERE role='admin'")
                    conn.execute(
                        "INSERT INTO settings (key,value) VALUES ('role_hierarchy_v2_migrated','1')"
                    )
            for user_id in sorted(admin_ids | super_admin_ids | developer_ids):
                role = (
                    "developer" if user_id in developer_ids else
                    "super" if hierarchy_v2 or user_id in super_admin_ids else "admin"
                )
                conn.execute(
                    """INSERT INTO bot_admins (user_id, role, added_by)
                       VALUES (?, ?, 0)
                       ON CONFLICT(user_id) DO UPDATE SET
                         role=CASE
                           WHEN excluded.role='developer' THEN 'developer'
                           WHEN bot_admins.role='developer' THEN 'developer'
                           WHEN excluded.role='super' THEN 'super'
                           ELSE bot_admins.role
                         END,
                         updated_at=CURRENT_TIMESTAMP""",
                    (user_id, role),
                )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def touch_user(
        self, user_id: int, username: str = "", first_name: str = "",
        last_name: str = "", language_code: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, username, first_name, last_name, language_code, message_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username, first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    language_code = CASE WHEN excluded.language_code='' THEN users.language_code
                                         ELSE excluded.language_code END,
                    message_count = users.message_count + 1,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name, last_name, language_code),
            )

    def record_bot_usage(
        self, user_id: int, username: str = "", display_name: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM bot_usage_users WHERE last_used_at<DATETIME('now','-1 year')"
            )
            conn.execute(
                """INSERT INTO bot_usage_users
                   (user_id, username, display_name, use_count)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     use_count=bot_usage_users.use_count+1,
                     last_used_at=CURRENT_TIMESTAMP""",
                (user_id, username[:80], display_name[:160]),
            )

    def bot_usage_users(
        self, limit: int = 10, offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM bot_usage_users WHERE last_used_at<DATETIME('now','-1 year')"
            )
            total = int(conn.execute(
                "SELECT COUNT(*) FROM bot_usage_users"
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT * FROM bot_usage_users
                   ORDER BY last_used_at DESC, user_id
                   LIMIT ? OFFSET ?""",
                (max(1, limit), max(0, offset)),
            ).fetchall()
        return rows, total

    def is_user_blocked(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["is_blocked"])

    def is_bot_admin(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT user_id FROM bot_admins WHERE user_id=?", (user_id,)).fetchone()
        return bool(row)

    def is_super_admin(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT role FROM bot_admins WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["role"] in {"super", "developer"})

    def is_developer(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT role FROM bot_admins WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["role"] == "developer")

    def bot_admin_role(self, user_id: int) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT role FROM bot_admins WHERE user_id=?", (user_id,)).fetchone()
        return str(row["role"]) if row else ""

    def bot_admin_permissions(self, user_id: int) -> set[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT permissions FROM bot_admins WHERE user_id=?", (user_id,)).fetchone()
        return {item for item in str(row["permissions"] or "").split(",") if item} if row else set()

    def set_bot_admin_permissions(self, user_id: int, permissions: set[str]) -> bool:
        value = ",".join(sorted(permissions))
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE bot_admins SET permissions=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND role='admin'",
                (value, user_id),
            )
            return cursor.rowcount > 0

    def list_bot_admins(
        self, limit: int = 200, viewer_id: int = 0, include_all: bool = True
    ) -> list[sqlite3.Row]:
        where = "" if include_all else "WHERE a.role!='developer' AND (a.user_id=? OR a.added_by=?)"
        params: tuple[object, ...] = (limit,) if include_all else (viewer_id, viewer_id, limit)
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT a.*, u.username, u.first_name, u.last_name
                   FROM bot_admins a LEFT JOIN users u ON u.user_id=a.user_id
                   {where}
                   ORDER BY CASE a.role WHEN 'developer' THEN 0 WHEN 'super' THEN 1 ELSE 2 END, a.user_id
                   LIMIT ?""",
                params,
            ).fetchall()

    def group_admin_permissions(self, chat_id: int, user_id: int) -> set[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT permissions FROM group_admin_permissions WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
        return {item for item in str(row["permissions"] or "").split(",") if item} if row else set()

    def set_group_admin_permissions(
        self, chat_id: int, user_id: int, permissions: set[str], assigned_by: int
    ) -> None:
        value = ",".join(sorted(permissions))
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO group_admin_permissions
                       (chat_id, user_id, permissions, assigned_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                     permissions=excluded.permissions,
                     assigned_by=excluded.assigned_by,
                     updated_at=CURRENT_TIMESTAMP""",
                (chat_id, user_id, value, assigned_by),
            )

    def reset_group_admin_permissions(self, chat_id: int, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM group_admin_permissions WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            return cursor.rowcount > 0

    def list_group_admin_permissions(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT p.*, u.username, u.first_name, u.last_name
                   FROM group_admin_permissions p
                   LEFT JOIN users u ON u.user_id=p.user_id
                   WHERE p.chat_id=? ORDER BY p.updated_at DESC, p.user_id""",
                (chat_id,),
            ).fetchall()

    def add_bot_admin(self, user_id: int, role: str = "admin", added_by: int = 0) -> None:
        if role not in {"admin", "super", "developer"}:
            raise ValueError("无效管理员级别")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO bot_admins (user_id, role, added_by)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     role=excluded.role, updated_at=CURRENT_TIMESTAMP""",
                (user_id, role, added_by),
            )

    def remove_bot_admin(self, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM bot_admins WHERE user_id=?", (user_id,))
            return cursor.rowcount > 0

    def custom_buttons(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        where = "WHERE is_enabled=1" if enabled_only else ""
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM custom_buttons {where} ORDER BY sort_order, id"
            ).fetchall()

    def custom_button_by_label(self, label: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM custom_buttons WHERE label=? AND is_enabled=1", (label,)
            ).fetchone()

    def add_custom_button(
        self, kind: str, label: str, created_by: int, contact_name: str = "",
        contact_username: str = "", target_user_id: int = 0,
        response_text: str = "", button_url: str = "",
    ) -> int:
        if kind not in {"support", "menu"}:
            raise ValueError("按钮类型无效")
        label = " ".join(label.split())[:40]
        if not label:
            raise ValueError("按钮文字不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            button_id = self._smallest_available_id(conn, "custom_buttons")
            try:
                conn.execute(
                    """INSERT INTO custom_buttons
                       (id,kind,label,contact_name,contact_username,target_user_id,
                        response_text,button_url,sort_order,created_by)
                       VALUES (?,?,?,?,?,?,?,?,
                         COALESCE((SELECT MAX(sort_order)+1 FROM custom_buttons),0),?)""",
                    (
                        button_id, kind, label, contact_name[:80],
                        contact_username.lstrip("@")[:80],
                        target_user_id, response_text[:4000], button_url[:2048], created_by,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("这个按钮文字已存在") from exc
            return button_id

    def delete_custom_button(self, button_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM custom_buttons WHERE id=?", (button_id,))
            return cursor.rowcount > 0

    def save_bot_clone_request(
        self, owner_id: int, bot_id: int, bot_username: str, token_cipher: str,
        parent_clone_id: int = 0, notified: bool = True, owner_name: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO bot_clones
                       (owner_id,bot_id,bot_username,token_cipher,status,is_enabled,
                        parent_clone_id,notified,owner_name,result_notified)
                   VALUES (?,?,?,?,'pending',0,?,?,?,?)
                   ON CONFLICT(bot_id) DO UPDATE SET
                     owner_id=excluded.owner_id, bot_username=excluded.bot_username,
                     token_cipher=excluded.token_cipher, status='pending',
                     is_enabled=0, reviewed_by=0, reviewed_at=NULL, last_error='',
                     parent_clone_id=excluded.parent_clone_id,
                     notified=excluded.notified, owner_name=excluded.owner_name,
                     result_notified=excluded.result_notified,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    owner_id, bot_id, bot_username[:80], token_cipher,
                    int(parent_clone_id or 0), int(bool(notified)), owner_name[:120],
                    int(bool(notified)),
                ),
            )
            row = conn.execute("SELECT id FROM bot_clones WHERE bot_id=?", (bot_id,)).fetchone()
            return int(row["id"] if row else cursor.lastrowid)

    def bot_clone(self, clone_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM bot_clones WHERE id=?", (clone_id,)).fetchone()

    def list_bot_clones(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.*, u.username AS owner_username,
                          u.first_name AS owner_first_name
                   FROM bot_clones c LEFT JOIN users u ON u.user_id=c.owner_id
                   ORDER BY c.id DESC LIMIT ?""",
                (max(1, min(limit, 5000)),),
            ).fetchall()

    def bot_clone_by_bot_id(self, bot_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM bot_clones WHERE bot_id=?", (int(bot_id),)
            ).fetchone()

    def bot_clone_ancestors(self, clone_id: int) -> list[sqlite3.Row]:
        """Chain from the given clone up to (not including) the mother bot."""
        chain: list[sqlite3.Row] = []
        seen: set[int] = set()
        current = int(clone_id or 0)
        with self.connect() as conn:
            while current and current not in seen:
                seen.add(current)
                row = conn.execute("SELECT * FROM bot_clones WHERE id=?", (current,)).fetchone()
                if not row:
                    break
                chain.append(row)
                current = int(row["parent_clone_id"] or 0)
        return chain

    def bot_clone_descendants(self, clone_id: int) -> list[sqlite3.Row]:
        """All clones below the given clone (any depth), breadth first."""
        result: list[sqlite3.Row] = []
        seen = {int(clone_id)}
        frontier = [int(clone_id)]
        with self.connect() as conn:
            while frontier:
                marks = ",".join("?" for _ in frontier)
                rows = conn.execute(
                    f"SELECT * FROM bot_clones WHERE parent_clone_id IN ({marks}) ORDER BY id",
                    tuple(frontier),
                ).fetchall()
                frontier = []
                for row in rows:
                    row_id = int(row["id"])
                    if row_id in seen:
                        continue
                    seen.add(row_id)
                    result.append(row)
                    frontier.append(row_id)
        return result

    def delete_bot_clones(self, clone_ids: list[int]) -> int:
        if not clone_ids:
            return 0
        marks = ",".join("?" for _ in clone_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM bot_clones WHERE id IN ({marks})",
                tuple(int(item) for item in clone_ids),
            )
            return int(cursor.rowcount or 0)

    def unnotified_bot_clone_requests(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM bot_clones WHERE status='pending' AND notified=0
                   ORDER BY id"""
            ).fetchall()

    def bot_clone_results_for_parent(self, parent_clone_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM bot_clones WHERE parent_clone_id=? AND result_notified=0
                     AND status IN ('approved','rejected') ORDER BY id""",
                (int(parent_clone_id),),
            ).fetchall()

    def mark_bot_clone_result_notified(self, clone_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE bot_clones SET result_notified=1 WHERE id=?", (int(clone_id),)
            )

    def mark_bot_clone_notified(self, clone_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE bot_clones SET notified=1 WHERE id=?", (int(clone_id),))

    def pending_bot_clone_ids(self) -> list[int]:
        with self.connect() as conn:
            return [
                int(row["id"]) for row in conn.execute(
                    "SELECT id FROM bot_clones WHERE status<>'approved' OR is_enabled=0"
                ).fetchall()
            ]

    def review_bot_clone(self, clone_id: int, approved: bool, reviewed_by: int) -> bool:
        status = "approved" if approved else "rejected"
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE bot_clones SET status=?,is_enabled=?,reviewed_by=?,
                          reviewed_at=CURRENT_TIMESTAMP,last_error='',updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='pending'""",
                (status, int(approved), reviewed_by, clone_id),
            )
            return cursor.rowcount > 0

    def enabled_bot_clones(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM bot_clones WHERE is_enabled=1 AND status='approved' ORDER BY id"
            ).fetchall()

    def set_bot_clone_error(self, clone_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE bot_clones SET last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (error[:500], clone_id),
            )

    def all_admin_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT user_id FROM bot_admins ORDER BY user_id").fetchall()
        return [int(row["user_id"]) for row in rows]

    def add_submission(
        self, submission: Submission, user_id: int, username: str, status: str,
        source_chat_id: int = 0, source_message_id: int = 0,
    ) -> int:
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO entries
                       (url, title, category, description, status, user_id, username,
                        source_chat_id, source_message_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        submission.url, submission.title, submission.category,
                        submission.description, status, user_id, username,
                        source_chat_id, source_message_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("这个网址已存在或正在审核中") from exc
            entry_id = int(cursor.lastrowid)
            self._sync_entry_key(conn, entry_id)
            if status == "approved":
                self._dedupe_entry_keyword(conn, entry_id)
            conn.execute("UPDATE users SET submissions_count = submissions_count + 1 WHERE user_id = ?", (user_id,))
            return entry_id

    def add_rich_submission(
        self, keyword: str, unique_url: str, content_text: str, user_id: int,
        username: str, status: str = "pending", file_id: str = "",
        file_type: str = "", file_name: str = "", source_chat_id: int = 0,
        source_message_id: int = 0, entities_json: str = "[]",
        buttons_json: str = "[]", copy_chat_id: int = 0, copy_message_id: int = 0,
    ) -> int:
        keyword = " ".join(keyword.strip().split())
        entities_json = entities_json or "[]"
        if entities_json == "[]":
            content_text = content_text.strip()
        if not keyword:
            raise ValueError("收录关键词不能为空")
        if not entry_keyword_key(keyword):
            raise ValueError("收录关键词不能为空")
        if not content_text.strip() and not file_id and not copy_message_id:
            raise ValueError("请提供文字、地址、图片、视频或文件")
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO entries
                       (url, title, category, description, status, user_id, username,
                        content_text, media_file_id, media_type, media_name,
                        source_chat_id, source_message_id, entities_json,
                        buttons_json, copy_chat_id, copy_message_id, keyword_key)
                       VALUES (?, ?, 'other', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        unique_url[:2048], keyword[:120], content_text[:500], status,
                        user_id, username[:80], content_text[:4096], file_id[:512],
                        file_type[:40], file_name[:240], source_chat_id,
                        source_message_id, entities_json, buttons_json or "[]",
                        int(copy_chat_id or 0), int(copy_message_id or 0),
                        entry_keyword_key(keyword[:120]),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("这条内容已存在或正在审核中") from exc
            entry_id = int(cursor.lastrowid)
            if status == "approved":
                self._dedupe_entry_keyword(conn, entry_id)
            conn.execute(
                "UPDATE users SET submissions_count=submissions_count+1 WHERE user_id=?",
                (user_id,),
            )
            return entry_id

    @staticmethod
    def _sync_entry_key(conn: sqlite3.Connection, entry_id: int) -> None:
        row = conn.execute("SELECT title FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE entries SET keyword_key=? WHERE id=?",
                (entry_keyword_key(str(row["title"] or "")), entry_id),
            )

    @staticmethod
    def _dedupe_entry_keyword(conn: sqlite3.Connection, entry_id: int) -> list[int]:
        """Keep only the given (just approved) entry for its keyword.

        Deletes every other entry with the same keyword key that is already
        reviewed (approved / rejected / removed) plus older pending ones.
        Newer pending submissions stay so their review is not lost.
        """
        row = conn.execute(
            "SELECT keyword_key FROM entries WHERE id=? AND status='approved'", (entry_id,)
        ).fetchone()
        key = str(row["keyword_key"] or "") if row else ""
        if not key:
            return []
        ids = [
            int(item["id"]) for item in conn.execute(
                """SELECT id FROM entries WHERE keyword_key=? AND id<>?
                     AND (status<>'pending' OR id<?)""",
                (key, entry_id, entry_id),
            ).fetchall()
        ]
        for old_id in ids:
            conn.execute("DELETE FROM reports WHERE entry_id=?", (old_id,))
            conn.execute("DELETE FROM entries WHERE id=?", (old_id,))
        return ids

    def find_keyword_entry(self, text: str, extra_suffixes: tuple[str, ...] = ()) -> Entry | None:
        """Exact keyword match: the whole message must equal a keyword.

        「XX」 and 「XX地址」 both match an entry titled 「XX」 or 「XX地址」.
        Returns only the newest approved entry.
        """
        keys = {entry_keyword_key(text)}
        folded = "".join(unicodedata.normalize("NFKC", str(text or "")).split()).casefold()
        for suffix in extra_suffixes:
            suffix_key = "".join(unicodedata.normalize("NFKC", str(suffix or "")).split()).casefold()
            if suffix_key and len(folded) > len(suffix_key) and folded.endswith(suffix_key):
                keys.add(entry_keyword_key(folded[:-len(suffix_key)]))
        keys.discard("")
        if not keys:
            return None
        marks = ",".join("?" for _ in keys)
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT * FROM entries WHERE status='approved' AND keyword_key IN ({marks})
                    ORDER BY id DESC LIMIT 1""",
                tuple(keys),
            ).fetchone()
        return self._entry(row) if row else None

    def search_keyword_titles(self, query: str, limit: int = 10) -> list[Entry]:
        """Broader search for /search and the menu: keyword contains query.

        One (newest) entry per keyword, at most ``limit`` results.
        """
        key = entry_keyword_key(query)
        if not key:
            return []
        like = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entries WHERE id IN (
                       SELECT MAX(id) FROM entries WHERE status='approved'
                          AND keyword_key LIKE ? ESCAPE '\\' GROUP BY keyword_key)
                   ORDER BY id DESC LIMIT ?""",
                (like, max(1, min(int(limit), 20))),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def storage_usage_bytes(self) -> int:
        """Bytes used by this bot's data: SQLite file plus WAL/SHM.

        Media is stored on Telegram servers (file_id only), so the database
        is the complete local footprint of a bot.
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(str(self.db_path) + suffix)
            except OSError:
                pass
        return total

    def get(self, entry_id: int) -> Entry | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return self._entry(row) if row else None

    def list_entries(
        self,
        status: str | None = "approved",
        category: str | None = None,
        limit: int = 10,
        offset: int = 0,
        query: str = "",
    ) -> list[Entry]:
        clauses: list[str] = []
        args: list[object] = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if category:
            clauses.append("category = ?")
            args.append(category)
        if query:
            clauses.append("(title LIKE ? OR url LIKE ? OR username LIKE ? OR content_text LIKE ?)")
            like = f"%{query.strip()}%"
            args.extend((like, like, like, like))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.extend((limit, offset))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM entries{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", args
            ).fetchall()
        return [self._entry(row) for row in rows]

    def count_entries(self, status: str | None = None, query: str = "") -> int:
        clauses: list[str] = []
        args: list[object] = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if query:
            clauses.append("(title LIKE ? OR url LIKE ? OR username LIKE ? OR content_text LIKE ?)")
            like = f"%{query.strip()}%"
            args.extend((like, like, like, like))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM entries{where}", args).fetchone()
        return int(row["count"])

    def my_entries(self, user_id: int, limit: int = 10) -> list[Entry]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def search(self, query: str, limit: int = 10) -> list[Entry]:
        like = f"%{query.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entries WHERE status = 'approved'
                   AND (title LIKE ? OR url LIKE ? OR category LIKE ? OR description LIKE ?
                        OR content_text LIKE ?)
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (like, like, like, like, like, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def update_status(self, entry_id: int, status: str, reason: str = "") -> bool:
        if status not in {"pending", "approved", "rejected", "removed"}:
            raise ValueError("invalid entry status")
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE entries SET status = ?, reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, reason[:500], entry_id),
            )
            if cursor.rowcount and status == "approved":
                self._dedupe_entry_keyword(conn, entry_id)
            return cursor.rowcount > 0

    def update_entry(self, entry_id: int, title: str, url: str, category: str, description: str) -> bool:
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """UPDATE entries SET title = ?, url = ?, category = ?, description = ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (title[:120], url[:2048], category[:64], description[:500], entry_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("网址已存在") from exc
            if cursor.rowcount:
                self._sync_entry_key(conn, entry_id)
                self._dedupe_entry_keyword(conn, entry_id)
            return cursor.rowcount > 0

    def update_rich_entry(
        self, entry_id: int, title: str, content_text: str,
        category: str, description: str,
    ) -> bool:
        title = " ".join(title.strip().split())
        content_text = content_text.strip()
        entry = self.get(entry_id)
        if not entry or not title:
            return False
        if not content_text and not entry.media_file_id:
            raise ValueError("文字内容和媒体不能同时为空")
        with self.connect() as conn:
            content_changed = content_text != entry.content_text.strip()
            cursor = conn.execute(
                """UPDATE entries SET title=?, content_text=?, description=?, category=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title[:120], content_text[:4096], description[:500], category[:64], entry_id),
            )
            if cursor.rowcount and content_changed:
                # 文字被改写后，原格式与原消息不再对应
                conn.execute(
                    """UPDATE entries SET entities_json='[]', copy_chat_id=0,
                       copy_message_id=0 WHERE id=?""",
                    (entry_id,),
                )
            if cursor.rowcount:
                self._sync_entry_key(conn, entry_id)
                self._dedupe_entry_keyword(conn, entry_id)
            return cursor.rowcount > 0

    def suffix_entry_titles(self, suffix: str = "地址") -> dict[str, int]:
        """Append suffix to every entry title that does not already end with it.

        One-shot rename helper (e.g. v8 -> v8地址). Idempotent: titles that
        already end with the suffix are left unchanged. Titles are truncated
        to 120 characters after appending.
        """
        suffix = str(suffix or "").strip()
        if not suffix:
            raise ValueError("后缀不能为空")
        if len(suffix) > 40:
            raise ValueError("后缀最多40个字符")
        changed = 0
        skipped = 0
        with self.connect() as conn:
            rows = conn.execute("SELECT id, title FROM entries").fetchall()
            for row in rows:
                title = str(row["title"] or "").strip()
                if not title:
                    skipped += 1
                    continue
                if title.endswith(suffix):
                    skipped += 1
                    continue
                new_title = (title + suffix)[:120]
                if new_title == title:
                    skipped += 1
                    continue
                conn.execute(
                    """UPDATE entries SET title=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (new_title, int(row["id"])),
                )
                self._sync_entry_key(conn, int(row["id"]))
                changed += 1
        return {"changed": changed, "skipped": skipped, "total": changed + skipped}


    def add_report(self, entry_id: int, user_id: int, reason: str) -> bool:
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO reports (entry_id, user_id, reason) VALUES (?, ?, ?)",
                    (entry_id, user_id, reason[:240]),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "UPDATE entries SET reports_count = reports_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (entry_id,),
            )
            return True

    def list_reports(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT reports.*, entries.title, entries.url FROM reports
                   JOIN entries ON entries.id = reports.entry_id ORDER BY reports.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def list_users(self, query: str = "", limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if query:
                like = f"%{query.strip()}%"
                return conn.execute(
                    """SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?
                       ORDER BY last_seen_at DESC LIMIT ?""",
                    (like, like, like, limit),
                ).fetchall()
            return conn.execute("SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()

    def set_user_blocked(self, user_id: int, blocked: bool) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (int(blocked), user_id))
            return cursor.rowcount > 0

    def add_support_message(
        self,
        user_id: int,
        direction: str,
        body: str,
        admin_name: str = "",
        telegram_message_id: int | None = None,
        target_admin_id: int = 0,
        support_button_id: int = 0,
    ) -> int:
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("invalid message direction")
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO support_messages
                   (user_id, direction, body, admin_name, telegram_message_id,
                    target_admin_id, support_button_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, direction, body[:4000], admin_name[:80],
                    telegram_message_id, target_admin_id, support_button_id,
                ),
            )
            return int(cursor.lastrowid)

    def support_conversations(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT sm.user_id, u.username, u.first_name, MAX(sm.id) AS last_id,
                       MAX(sm.created_at) AS last_at,
                       (SELECT body FROM support_messages x WHERE x.user_id = sm.user_id ORDER BY x.id DESC LIMIT 1) AS last_body,
                       SUM(CASE WHEN sm.direction = 'incoming' AND sm.status = 'open' THEN 1 ELSE 0 END) AS unread
                FROM support_messages sm LEFT JOIN users u ON u.user_id = sm.user_id
                GROUP BY sm.user_id ORDER BY last_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def support_thread(self, user_id: int, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM support_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
            ).fetchall()
            conn.execute(
                "UPDATE support_messages SET status = 'read' WHERE user_id = ? AND direction = 'incoming'",
                (user_id,),
            )
        return list(reversed(rows))

    def queue_message(self, user_id: int, body: str, kind: str = "message", broadcast_id: int | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO outbox (user_id, kind, body, broadcast_id) VALUES (?, ?, ?, ?)",
                (user_id, kind, body[:4000], broadcast_id),
            )
            return int(cursor.lastrowid)

    def queue_support_reply(self, user_id: int, body: str, admin_name: str) -> int:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO support_messages (user_id, direction, body, admin_name, status)
                   VALUES (?, 'outgoing', ?, ?, 'queued')""",
                (user_id, body[:4000], admin_name[:80]),
            )
            cursor = conn.execute(
                "INSERT INTO outbox (user_id, kind, body) VALUES (?, 'support', ?)", (user_id, body[:4000])
            )
            return int(cursor.lastrowid)

    def pending_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)).fetchall()

    def finish_outbox(self, outbox_id: int, success: bool, error: str = "") -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT broadcast_id FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            status = "sent" if success else "failed"
            conn.execute(
                """UPDATE outbox SET status = ?, attempts = attempts + 1, last_error = ?,
                   sent_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE sent_at END WHERE id = ?""",
                (status, error[:500], int(success), outbox_id),
            )
            if row and row["broadcast_id"]:
                broadcast_id = int(row["broadcast_id"])
                field = "sent" if success else "failed"
                conn.execute(f"UPDATE broadcasts SET {field} = {field} + 1 WHERE id = ?", (broadcast_id,))
                counts = conn.execute(
                    "SELECT total, sent, failed FROM broadcasts WHERE id = ?", (broadcast_id,)
                ).fetchone()
                if counts and counts["sent"] + counts["failed"] >= counts["total"]:
                    conn.execute(
                        "UPDATE broadcasts SET status = 'finished', finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (broadcast_id,),
                    )

    def create_broadcast(self, title: str, body: str) -> int:
        with self.connect() as conn:
            users = conn.execute("SELECT user_id FROM users WHERE is_blocked = 0").fetchall()
            cursor = conn.execute(
                "INSERT INTO broadcasts (title, body, total) VALUES (?, ?, ?)", (title[:120], body[:4000], len(users))
            )
            broadcast_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO outbox (user_id, kind, body, broadcast_id) VALUES (?, 'broadcast', ?, ?)",
                ((int(user["user_id"]), body[:4000], broadcast_id) for user in users),
            )
            if not users:
                conn.execute(
                    "UPDATE broadcasts SET status = 'finished', finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (broadcast_id,),
                )
            return broadcast_id

    def list_broadcasts(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (key, value[:4000]),
            )

    def set_group_ad(
        self, chat_id: int, position: str, text: str, created_by: int,
        interval_seconds: int = 0, file_id: str = "", file_type: str = "",
        file_name: str = "", entities_json: str = "[]",
        buttons_json: str = "[]",
        source_chat_id: int = 0, source_message_id: int = 0,
    ) -> int:
        if position not in {"interval", "prefix", "suffix"}:
            raise ValueError("无效广告位置")
        if not text.strip() and not file_id.strip():
            raise ValueError("广告必须包含文字或媒体")
        if position == "interval" and interval_seconds < 60:
            raise ValueError("定时广告间隔不能少于1分钟")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO group_ads
                   (chat_id, position, interval_seconds, text, file_id, file_type,
                    file_name, entities_json, buttons_json, source_chat_id,
                    source_message_id, is_enabled, next_run_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                     CASE WHEN ?='interval' THEN DATETIME('now', '+' || ? || ' seconds') END, ?)
                   ON CONFLICT(chat_id, position) DO UPDATE SET
                     interval_seconds=excluded.interval_seconds, text=excluded.text,
                     file_id=excluded.file_id, file_type=excluded.file_type,
                     file_name=excluded.file_name, entities_json=excluded.entities_json,
                     buttons_json=excluded.buttons_json,
                     source_chat_id=excluded.source_chat_id,
                     source_message_id=excluded.source_message_id, is_enabled=1,
                     next_run_at=excluded.next_run_at, created_by=excluded.created_by,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    chat_id, position, max(0, interval_seconds), text,
                    file_id[:512], file_type[:40], file_name[:240], entities_json,
                    buttons_json or "[]", int(source_chat_id or 0),
                    int(source_message_id or 0), position,
                    max(0, interval_seconds), created_by,
                ),
            )
            row = conn.execute(
                "SELECT id FROM group_ads WHERE chat_id=? AND position=?",
                (chat_id, position),
            ).fetchone()
        return int(row["id"])

    def group_ad(self, chat_id: int, position: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM group_ads WHERE chat_id=? AND position=? AND is_enabled=1",
                (chat_id, position),
            ).fetchone()

    def list_group_ads(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM group_ads WHERE chat_id=? ORDER BY id", (chat_id,)
            ).fetchall()

    def disable_group_ad(self, chat_id: int, position: str | None = None) -> int:
        with self.connect() as conn:
            if position:
                cursor = conn.execute(
                    "UPDATE group_ads SET is_enabled=0, updated_at=CURRENT_TIMESTAMP "
                    "WHERE chat_id=? AND position=?", (chat_id, position),
                )
            else:
                cursor = conn.execute(
                    "UPDATE group_ads SET is_enabled=0, updated_at=CURRENT_TIMESTAMP "
                    "WHERE chat_id=?", (chat_id,),
                )
            return cursor.rowcount

    def due_group_ads(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM group_ads WHERE position='interval' AND is_enabled=1
                   AND next_run_at<=CURRENT_TIMESTAMP ORDER BY next_run_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE group_ads SET
                       next_run_at=DATETIME('now', '+' || interval_seconds || ' seconds'),
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (int(row["id"]),),
                )
        return rows

    def points_config(self, chat_id: int) -> sqlite3.Row:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO group_points_config (chat_id) VALUES (?)",
                (chat_id,),
            )
            return conn.execute(
                "SELECT * FROM group_points_config WHERE chat_id=?", (chat_id,)
            ).fetchone()

    def set_points_enabled(self, chat_id: int, enabled: bool, updated_by: int) -> None:
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET is_enabled=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (int(enabled), updated_by, chat_id),
            )


    def set_dice_enabled(self, chat_id: int, enabled: bool, updated_by: int) -> None:
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET dice_enabled=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (int(enabled), updated_by, chat_id),
            )


    def set_dice_odds(self, chat_id: int, odds: int, updated_by: int) -> None:
        if not isinstance(odds, int) or isinstance(odds, bool) or not 1700 <= odds <= 2000:
            raise ValueError("骰子赔率范围为1.7-2.0（也可写 1700-2000）")
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET dice_odds=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (odds, updated_by, chat_id),
            )

    def set_dice_min_bet(self, chat_id: int, minimum, updated_by: int) -> None:
        minimum = normalize_points(minimum)
        if minimum < Decimal("0.01") or minimum > Decimal("1000000"):
            raise ValueError("骰子最低积分范围为0.01-1000000")
        current_max = normalize_points(self.points_config(chat_id)["dice_max_bet"] or 0)
        if current_max > 0 and minimum > current_max:
            raise ValueError(
                f"最低参与积分不能高于单注上限 {format_points(current_max)}"
            )
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET dice_min_bet=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (points_to_db(minimum), updated_by, chat_id),
            )

    # ---- 表情包复制：固定模式设置（每个用户一份） ------------------------

    def sticker_profile(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sticker_profiles WHERE user_id=?", (int(user_id),)
            ).fetchone()

    def set_sticker_fixed_title(self, user_id: int, title: str) -> None:
        title = " ".join(str(title or "").split())
        if not title or len(title.encode("utf-16-le")) // 2 > 64:
            raise ValueError("标题需要 1-64 个字符")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sticker_profiles (user_id, fixed_title) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET fixed_title=excluded.fixed_title,
                   updated_at=CURRENT_TIMESTAMP""",
                (int(user_id), title),
            )

    def set_sticker_channel(
        self, user_id: int, channel_id: int, title: str = "", username: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sticker_profiles
                   (user_id, channel_id, channel_title, channel_username)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET channel_id=excluded.channel_id,
                   channel_title=excluded.channel_title,
                   channel_username=excluded.channel_username,
                   updated_at=CURRENT_TIMESTAMP""",
                (int(user_id), int(channel_id or 0), str(title or "")[:128],
                 str(username or "")[:64]),
            )

    def clear_sticker_channel(self, user_id: int) -> None:
        self.set_sticker_channel(user_id, 0, "", "")

    def clear_sticker_profile(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sticker_profiles WHERE user_id=?", (int(user_id),))

    def set_dice_max_bet(self, chat_id: int, maximum, updated_by: int) -> None:
        """单注最高积分；0 表示不限。"""
        maximum = normalize_points(maximum)
        if maximum < 0 or maximum > Decimal("1000000"):
            raise ValueError("单注上限范围为0-1000000（0 表示不限）")
        current_min = normalize_points(self.points_config(chat_id)["dice_min_bet"] or 0)
        if maximum > 0 and maximum < current_min:
            raise ValueError(
                f"单注上限不能低于最低参与积分 {format_points(current_min)}"
            )
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET dice_max_bet=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (points_to_db(maximum), updated_by, chat_id),
            )

    def set_dice_schedule(
        self, chat_id: int, enabled: bool, open_time: str,
        close_time: str, updated_by: int,
    ) -> None:
        for value in (open_time, close_time):
            try:
                hour, minute = (int(part) for part in value.split(":"))
            except (TypeError, ValueError):
                raise ValueError("时间格式应为 HH:MM，例如 09:00") from None
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("时间格式应为 HH:MM，例如 09:00")
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET dice_schedule_enabled=?,
                   dice_open_time=?, dice_close_time=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (int(enabled), open_time, close_time, updated_by, chat_id),
            )


    def set_checkin_points(
        self, chat_id: int, minimum, maximum,
        streak_bonus, updated_by: int,
    ) -> None:
        minimum = normalize_points(minimum)
        maximum = normalize_points(maximum)
        streak_bonus = normalize_points(streak_bonus)
        if minimum < 0 or maximum < minimum or maximum > 100000:
            raise ValueError("签到积分范围无效")
        if streak_bonus < 0 or streak_bonus > 100000:
            raise ValueError("连续签到奖励范围无效")
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET checkin_min=?, checkin_max=?,
                   streak_bonus=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                   WHERE chat_id=?""",
                (
                    points_to_db(minimum), points_to_db(maximum),
                    points_to_db(streak_bonus), updated_by, chat_id,
                ),
            )

    def set_activity_points(
        self, chat_id: int, messages_min: int, messages_max: int,
        points_min, points_max, updated_by: int,
        enabled: bool = True,
    ) -> None:
        if messages_min < 1 or messages_max < messages_min or messages_max > 100000:
            raise ValueError("活跃消息范围无效")
        points_min = normalize_points(points_min)
        points_max = normalize_points(points_max)
        if points_min < 0 or points_max < points_min or points_max > 100000:
            raise ValueError("活跃积分范围无效")
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET activity_enabled=?,
                   activity_messages_min=?, activity_messages_max=?,
                   activity_points_min=?, activity_points_max=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (
                    int(enabled), messages_min, messages_max,
                    points_to_db(points_min), points_to_db(points_max),
                    updated_by, chat_id,
                ),
            )

    def set_activity_points_enabled(
        self, chat_id: int, enabled: bool, updated_by: int
    ) -> None:
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET activity_enabled=?, updated_by=?,
                   updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                (int(enabled), updated_by, chat_id),
            )

    def set_point_draw_config(
        self, chat_id: int, enabled: bool, draw_cost,
        rate_multiplier: float, updated_by: int,
    ) -> None:
        draw_cost = normalize_points(draw_cost)
        if draw_cost < normalize_points("0.01") or draw_cost > 1000000:
            raise ValueError("每次抽奖消耗积分范围为0.01-1000000")
        if not math.isfinite(rate_multiplier) or rate_multiplier < 0 or rate_multiplier > 5:
            raise ValueError("中奖概率倍率范围为0-5")
        self.points_config(chat_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_points_config SET draw_enabled=?, draw_cost=?,
                   draw_rate_multiplier=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                   WHERE chat_id=?""",
                (
                    int(enabled), points_to_db(draw_cost),
                    float(rate_multiplier), updated_by, chat_id,
                ),
            )

    @staticmethod
    def point_draw_probability(
        draw_cost, gift_points, multiplier: float = 1.0,
    ) -> float:
        try:
            cost = normalize_points(draw_cost)
            gift = normalize_points(gift_points)
        except ValueError:
            return 0.0
        if cost <= 0 or gift <= 0:
            return 0.0
        return min(1.0, float(cost / gift) * max(0.0, multiplier))

    @staticmethod
    def _random_between(minimum: int, maximum: int) -> int:
        return minimum + secrets.randbelow(maximum - minimum + 1)

    @staticmethod
    def _random_points_between(minimum, maximum) -> Decimal:
        lo = normalize_points(minimum)
        hi = normalize_points(maximum)
        if hi < lo:
            hi = lo
        lo_cents = int((lo * 100).to_integral_value(rounding=ROUND_HALF_UP))
        hi_cents = int((hi * 100).to_integral_value(rounding=ROUND_HALF_UP))
        cents = lo_cents + secrets.randbelow(hi_cents - lo_cents + 1)
        return normalize_points(Decimal(cents) / Decimal(100))

    @staticmethod
    def _adjust_points_conn(
        conn: sqlite3.Connection, chat_id: int, user_id: int, delta,
        reason: str, created_by: int, username: str = "",
        display_name: str = "", allow_negative: bool = False,
    ) -> Decimal:
        delta = normalize_points(delta)
        conn.execute(
            """INSERT OR IGNORE INTO point_accounts
               (chat_id, user_id, username, display_name)
               VALUES (?, ?, ?, ?)""",
            (chat_id, user_id, username[:80], display_name[:160]),
        )
        row = conn.execute(
            "SELECT balance FROM point_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        current = normalize_points(row["balance"] if row else 0)
        balance = normalize_points(current + delta)
        if balance < 0 and not allow_negative:
            raise ValueError("积分余额不足")
        # Cycle flag: invite awards set it; balance at or below 5 clears the cycle.
        cycle_sql = "invite_source_cycle=invite_source_cycle"
        if balance <= normalize_points(5):
            cycle_sql = "invite_source_cycle=0"
        elif delta > 0 and "邀请" in str(reason or ""):
            cycle_sql = "invite_source_cycle=1"
        earned = normalize_points(max(Decimal("0"), delta))
        spent = normalize_points(max(Decimal("0"), -delta))
        conn.execute(
            f"""UPDATE point_accounts SET username=CASE WHEN ?='' THEN username ELSE ? END,
               display_name=CASE WHEN ?='' THEN display_name ELSE ? END,
               balance=?, earned_total=earned_total+?, spent_total=spent_total+?,
               {cycle_sql},
               updated_at=CURRENT_TIMESTAMP WHERE chat_id=? AND user_id=?""",
            (
                username, username[:80], display_name, display_name[:160],
                points_to_db(balance), points_to_db(earned), points_to_db(spent),
                chat_id, user_id,
            ),
        )
        conn.execute(
            """INSERT INTO point_ledger
               (chat_id, user_id, delta, balance_after, reason, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                chat_id, user_id, points_to_db(delta), points_to_db(balance),
                reason[:240], created_by,
            ),
        )
        return balance

    def adjust_points(
        self, chat_id: int, user_id: int, delta, reason: str,
        created_by: int, username: str = "", display_name: str = "",
        allow_negative: bool = False,
    ) -> Decimal:
        with self.connect() as conn:
            return self._adjust_points_conn(
                conn, chat_id, user_id, delta, reason, created_by,
                username, display_name, allow_negative,
            )

    def set_points_balance(
        self, chat_id: int, user_id: int, balance, created_by: int,
        reason: str = "管理员设置积分",
    ) -> Decimal:
        balance = normalize_points(balance)
        if balance < 0:
            raise ValueError("积分不能小于0")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT balance FROM point_accounts WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            current = normalize_points(row["balance"]) if row else Decimal("0")
            return self._adjust_points_conn(
                conn, chat_id, user_id, balance - current, reason, created_by
            )

    def clear_points(self, chat_id: int, created_by: int, user_id: int | None = None) -> int:
        with self.connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT user_id, balance FROM point_accounts WHERE chat_id=? AND balance!=0",
                    (chat_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT user_id, balance FROM point_accounts
                       WHERE chat_id=? AND user_id=? AND balance!=0""",
                    (chat_id, user_id),
                ).fetchall()
            for row in rows:
                self._adjust_points_conn(
                    conn, chat_id, int(row["user_id"]),
                    -normalize_points(row["balance"]),
                    "管理员清零积分", created_by,
                )
            return len(rows)

    def point_account(self, chat_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM point_accounts WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()

    def user_has_invite_point_source(
        self, chat_id: int, user_id: int,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """True if current points cycle includes invite-link earnings."""
        def _check(c: sqlite3.Connection) -> bool:
            row = c.execute(
                """SELECT invite_source_cycle FROM point_accounts
                   WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()
            return bool(row is not None and int(row["invite_source_cycle"] or 0))

        if conn is not None:
            return _check(conn)
        with self.connect() as own:
            return _check(own)

    def count_point_accounts(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM point_accounts WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])

    def point_rankings(
        self, chat_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM point_accounts WHERE chat_id=?
                   ORDER BY balance DESC, earned_total DESC, updated_at, user_id
                   LIMIT ? OFFSET ?""",
                (chat_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def count_point_ledger(self, chat_id: int, user_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM point_ledger
                   WHERE chat_id=? AND user_id=?
                     AND created_at>=DATETIME('now','-6 months')""",
                (chat_id, user_id),
            ).fetchone()[0])

    def point_ledger_rows(
        self, chat_id: int, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT l.*, u.username AS actor_username,
                          u.first_name AS actor_first_name,
                          u.last_name AS actor_last_name
                   FROM point_ledger l
                   LEFT JOIN users u ON u.user_id=l.created_by
                   WHERE l.chat_id=? AND l.user_id=?
                     AND l.created_at>=DATETIME('now','-6 months')
                   ORDER BY l.id DESC LIMIT ? OFFSET ?""",
                (chat_id, user_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def add_point_game_record(
        self, chat_id: int, user_id: int, game_type: str, *,
        side: str = "", dice_value: int = 0, stake=0,
        delta=0, balance_after=0, is_win: bool = False,
        detail: str = "", conn: sqlite3.Connection | None = None,
    ) -> int:
        game_type = str(game_type or "").strip()
        if game_type not in {"dice", "draw"}:
            raise ValueError("game_type 必须是 dice 或 draw")
        params = (
            chat_id, user_id, game_type, str(side or ""),
            int(dice_value or 0),
            points_to_db(stake or 0), points_to_db(delta or 0),
            points_to_db(balance_after or 0),
            int(bool(is_win)), str(detail or ""),
        )
        sql = """INSERT INTO point_game_records
                   (chat_id, user_id, game_type, side, dice_value, stake,
                    delta, balance_after, is_win, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        if conn is not None:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid)
        with self.connect() as own:
            cursor = own.execute(sql, params)
            return int(cursor.lastrowid)

    def count_point_game_records(self, chat_id: int, user_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM point_game_records
                   WHERE chat_id=? AND user_id=?
                     AND created_at>=DATETIME('now','-6 months')""",
                (chat_id, user_id),
            ).fetchone()[0])

    def point_game_records(
        self, chat_id: int, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM point_game_records
                   WHERE chat_id=? AND user_id=?
                     AND created_at>=DATETIME('now','-6 months')
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (chat_id, user_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def count_point_draw_winners(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM point_draws
                   WHERE chat_id=? AND is_winner=1
                     AND created_at>=DATETIME('now','-6 months')""",
                (chat_id,),
            ).fetchone()[0])

    def point_draw_winners(
        self, chat_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT d.*, a.username, a.display_name
                   FROM point_draws d
                   LEFT JOIN point_accounts a
                     ON a.chat_id=d.chat_id AND a.user_id=d.user_id
                   WHERE d.chat_id=? AND d.is_winner=1
                     AND d.created_at>=DATETIME('now','-6 months')
                   ORDER BY d.id DESC LIMIT ? OFFSET ?""",
                (chat_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def count_all_raffle_winners(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM point_draws
                      WHERE chat_id=? AND is_winner=1
                        AND created_at>=DATETIME('now','-6 months'))
                     +
                     (SELECT COUNT(*) FROM raffle_winners w
                      JOIN raffles r ON r.id=w.raffle_id WHERE r.chat_id=?)""",
                (chat_id, chat_id),
            ).fetchone()[0])

    def all_raffle_winners(
        self, chat_id: int, limit: int = 10, offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM (
                     SELECT d.id AS id, '积分抽奖' AS record_type, d.user_id,
                            d.gift_name, d.created_at, 1 AS winner_position,
                            1 AS raffle_winner_count,
                            COALESCE(a.username, '') AS username,
                            COALESCE(a.display_name, '') AS display_name
                     FROM point_draws d
                     LEFT JOIN point_accounts a
                       ON a.chat_id=d.chat_id AND a.user_id=d.user_id
                     WHERE d.chat_id=? AND d.is_winner=1
                       AND d.created_at>=DATETIME('now','-6 months')
                     UNION ALL
                     SELECT r.id AS id, '群抽奖' AS record_type, w.user_id,
                            r.prize AS gift_name,
                            COALESCE(r.drawn_at, r.ends_at) AS created_at,
                            w.position AS winner_position,
                            r.winner_count AS raffle_winner_count,
                            e.username, e.display_name
                     FROM raffle_winners w
                     JOIN raffles r ON r.id=w.raffle_id
                     JOIN raffle_entries e
                       ON e.raffle_id=w.raffle_id AND e.user_id=w.user_id
                     WHERE r.chat_id=?
                   ) ORDER BY created_at DESC, id DESC, winner_position ASC
                     LIMIT ? OFFSET ?""",
                (chat_id, chat_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def count_point_redemptions(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM point_redemptions WHERE chat_id=?
                   AND created_at>=DATETIME('now','-6 months')""", (chat_id,)
            ).fetchone()[0])

    def point_redemption_rows(
        self, chat_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT r.*, a.username, a.display_name
                   FROM point_redemptions r
                   LEFT JOIN point_accounts a
                     ON a.chat_id=r.chat_id AND a.user_id=r.user_id
                   WHERE r.chat_id=?
                     AND r.created_at>=DATETIME('now','-6 months')
                   ORDER BY r.id DESC LIMIT ? OFFSET ?""",
                (chat_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def find_group_user(self, chat_id: int, value: str) -> sqlite3.Row | None:
        target = value.strip().removeprefix("@").casefold()
        if not target:
            return None
        with self.connect() as conn:
            if target.isdigit():
                return conn.execute(
                    """SELECT user_id, username, display_name FROM point_accounts
                       WHERE chat_id=? AND user_id=?
                       UNION ALL
                       SELECT user_id, MAX(username), MAX(display_name)
                       FROM group_activity_users WHERE chat_id=? AND user_id=?
                       GROUP BY user_id LIMIT 1""",
                    (chat_id, int(target), chat_id, int(target)),
                ).fetchone()
            return conn.execute(
                """SELECT user_id, username, display_name FROM point_accounts
                   WHERE chat_id=? AND username=? COLLATE NOCASE
                   UNION ALL
                   SELECT user_id, MAX(username), MAX(display_name)
                   FROM group_activity_users
                   WHERE chat_id=? AND username=? COLLATE NOCASE
                   GROUP BY user_id LIMIT 1""",
                (chat_id, target, chat_id, target),
            ).fetchone()

    def find_known_user(self, value: str) -> sqlite3.Row | None:
        target = value.strip().removeprefix("@").casefold()
        if not target:
            return None
        with self.connect() as conn:
            if target.isdigit():
                return conn.execute(
                    "SELECT * FROM users WHERE user_id=?", (int(target),)
                ).fetchone()
            return conn.execute(
                """SELECT * FROM users WHERE username=? COLLATE NOCASE
                   ORDER BY last_seen_at DESC LIMIT 1""",
                (target,),
            ).fetchone()

    def find_any_group_user(self, value: str) -> sqlite3.Row | None:
        target = value.strip().removeprefix("@").casefold()
        if not target:
            return None
        with self.connect() as conn:
            if target.isdigit():
                return conn.execute(
                    """SELECT chat_id, user_id, username, display_name
                       FROM group_activity_users WHERE user_id=?
                       ORDER BY last_seen_at DESC LIMIT 1""",
                    (int(target),),
                ).fetchone()
            return conn.execute(
                """SELECT chat_id, user_id, username, display_name
                   FROM group_activity_users WHERE username=? COLLATE NOCASE
                   ORDER BY last_seen_at DESC LIMIT 1""",
                (target,),
            ).fetchone()

    def checkin_points(
        self, chat_id: int, user_id: int, username: str, display_name: str,
    ) -> tuple[int, int, int, int]:
        config = self.points_config(chat_id)
        if not config["is_enabled"]:
            raise ValueError("本群积分功能尚未开启")
        with self.connect() as conn:
            existing = conn.execute(
                """SELECT points, streak FROM point_checkins
                   WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                (chat_id, user_id),
            ).fetchone()
            if existing:
                raise ValueError("今天已经签到过了")
            previous = conn.execute(
                """SELECT day, streak FROM point_checkins
                   WHERE chat_id=? AND user_id=? ORDER BY day DESC LIMIT 1""",
                (chat_id, user_id),
            ).fetchone()
            streak = 1
            if previous:
                is_yesterday = conn.execute(
                    "SELECT ?=DATE('now','+8 hours','-1 day')", (previous["day"],)
                ).fetchone()[0]
                if is_yesterday:
                    streak = int(previous["streak"]) + 1
            points = self._random_points_between(
                config["checkin_min"], config["checkin_max"]
            )
            if streak >= 3:
                points = normalize_points(points + normalize_points(config["streak_bonus"]))
            today_number = int(conn.execute(
                """SELECT COUNT(*) FROM point_checkins
                   WHERE chat_id=? AND day=DATE('now','+8 hours')""",
                (chat_id,),
            ).fetchone()[0]) + 1
            balance = self._adjust_points_conn(
                conn, chat_id, user_id, points, "群签到", 0,
                username, display_name,
            )
            conn.execute(
                """INSERT INTO point_checkins
                   (chat_id, user_id, day, points, streak)
                   VALUES (?, ?, DATE('now','+8 hours'), ?, ?)""",
                (chat_id, user_id, points_to_db(points), streak),
            )
            return points, balance, streak, today_number

    def award_activity_points(
        self, chat_id: int, user_id: int, username: str, display_name: str,
    ) -> tuple[int, int, int, int] | None:
        config = self.points_config(chat_id)
        if not config["is_enabled"] or not config["activity_enabled"]:
            return None
        with self.connect() as conn:
            reward = conn.execute(
                """SELECT * FROM point_activity_rewards
                   WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                (chat_id, user_id),
            ).fetchone()
            if not reward:
                target = self._random_between(
                    int(config["activity_messages_min"]),
                    int(config["activity_messages_max"]),
                )
                conn.execute(
                    """INSERT INTO point_activity_rewards
                       (chat_id, user_id, day, message_target)
                       VALUES (?, ?, DATE('now','+8 hours'), ?)""",
                    (chat_id, user_id, target),
                )
                reward = conn.execute(
                    """SELECT * FROM point_activity_rewards
                       WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                    (chat_id, user_id),
                ).fetchone()
            messages = conn.execute(
                """SELECT messages FROM group_activity_users
                   WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                (chat_id, user_id),
            ).fetchone()
            current_messages = int(messages["messages"]) if messages else 0
            if (
                current_messages - int(reward["message_baseline"])
                < int(reward["message_target"])
            ):
                return None
            points = self._random_points_between(
                config["activity_points_min"],
                config["activity_points_max"],
            )
            balance = self._adjust_points_conn(
                conn, chat_id, user_id, points, "每日活跃奖励", 0,
                username, display_name,
            )
            next_target = self._random_between(
                int(config["activity_messages_min"]),
                int(config["activity_messages_max"]),
            )
            conn.execute(
                """UPDATE point_activity_rewards SET points=points+?,
                   message_baseline=?, message_target=?, reward_count=reward_count+1,
                   awarded_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                (points_to_db(points), current_messages, next_target, chat_id, user_id),
            )
            return points, balance, int(reward["message_target"]), current_messages

    def add_point_gift(
        self, chat_id: int, name: str, points_cost,
        stock: int, created_by: int,
    ) -> int:
        name = " ".join(name.strip().split())
        points_cost = normalize_points(points_cost)
        if not name or points_cost < normalize_points("0.01") or stock == 0 or stock < -1:
            raise ValueError("礼品名称、积分或库存无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            gift_id = self._smallest_available_id(conn, "point_gifts")
            conn.execute(
                """INSERT INTO point_gifts
                   (id, chat_id, name, points_cost, stock, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    gift_id, chat_id, name[:200], points_to_db(points_cost),
                    stock, created_by,
                ),
            )
            return gift_id

    def point_gifts(self, chat_id: int, enabled_only: bool = True) -> list[sqlite3.Row]:
        where = " AND is_enabled=1 AND stock!=0" if enabled_only else ""
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT * FROM point_gifts WHERE chat_id=?{where}
                    ORDER BY points_cost, id""",
                (chat_id,),
            ).fetchall()

    def disable_point_gift(self, chat_id: int, gift_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM point_gifts WHERE chat_id=? AND id=?",
                (chat_id, gift_id),
            )
            return cursor.rowcount > 0

    def redeem_point_gift(
        self, chat_id: int, user_id: int, gift_id: int,
        username: str, display_name: str,
    ) -> tuple[int, str, int]:
        config = self.points_config(chat_id)
        if not config["is_enabled"]:
            raise ValueError("本群积分功能尚未开启")
        with self.connect() as conn:
            gift = conn.execute(
                """SELECT * FROM point_gifts WHERE chat_id=? AND id=?
                   AND is_enabled=1 AND stock!=0""",
                (chat_id, gift_id),
            ).fetchone()
            if not gift:
                raise ValueError("礼品不存在或已经兑完")
            notes = []
            if self.user_has_invite_point_source(chat_id, user_id, conn=conn):
                notes.append("积分来源邀请他人")
            if self.user_has_name_change_history(chat_id, user_id, conn=conn):
                notes.append("已改过名字姓氏")
            invite_note = " · ".join(notes)
            balance = self._adjust_points_conn(
                conn, chat_id, user_id, -normalize_points(gift["points_cost"]),
                f"兑换礼品：{gift['name']}", 0, username, display_name,
            )
            if int(gift["stock"]) > 0:
                conn.execute(
                    "UPDATE point_gifts SET stock=stock-1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (gift_id,),
                )
            cursor = conn.execute(
                """INSERT INTO point_redemptions
                   (chat_id, user_id, gift_id, gift_name, points_cost, note)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, user_id, gift_id, gift["name"],
                    gift["points_cost"], invite_note,
                ),
            )
            return int(cursor.lastrowid), str(gift["name"]), balance

    def draw_point_gift(
        self, chat_id: int, user_id: int, gift_id: int,
        username: str, display_name: str, draw_cost_override=None,
    ) -> dict[str, object]:
        config = self.points_config(chat_id)
        if not config["is_enabled"]:
            raise ValueError("本群积分功能尚未开启")
        if not config["draw_enabled"]:
            raise ValueError("本群积分抽奖尚未开启")
        with self.connect() as conn:
            gift = conn.execute(
                """SELECT * FROM point_gifts WHERE chat_id=? AND id=?
                   AND is_enabled=1 AND stock!=0""",
                (chat_id, gift_id),
            ).fetchone()
            if not gift:
                raise ValueError("礼品不存在或已经抽完")
            minimum_cost = normalize_points(config["draw_cost"])
            draw_cost = (
                minimum_cost if draw_cost_override is None
                else normalize_points(draw_cost_override)
            )
            if draw_cost < minimum_cost:
                raise ValueError(f"本次抽奖最少消耗 {format_points(minimum_cost)} 积分")
            if draw_cost > 1000000:
                raise ValueError("本次抽奖最多消耗1000000积分")
            probability = self.point_draw_probability(
                draw_cost, gift["points_cost"],
                float(config["draw_rate_multiplier"]),
            )
            notes = []
            if self.user_has_invite_point_source(chat_id, user_id, conn=conn):
                notes.append("积分来源邀请他人")
            if self.user_has_name_change_history(chat_id, user_id, conn=conn):
                notes.append("已改过名字姓氏")
            invite_note = " · ".join(notes)
            balance = self._adjust_points_conn(
                conn, chat_id, user_id, -draw_cost,
                f"积分抽奖：{gift['name']}", 0, username, display_name,
            )
            random_value = secrets.randbelow(1000000) / 1000000
            is_winner = random_value < probability
            redemption_id = None
            if is_winner:
                if int(gift["stock"]) > 0:
                    changed = conn.execute(
                        """UPDATE point_gifts SET stock=stock-1,
                           updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND stock>0""",
                        (gift_id,),
                    )
                    if not changed.rowcount:
                        is_winner = False
                if is_winner:
                    redemption = conn.execute(
                        """INSERT INTO point_redemptions
                           (chat_id, user_id, gift_id, gift_name, points_cost,
                            status, note)
                           VALUES (?, ?, ?, ?, 0, 'won', ?)""",
                        (chat_id, user_id, gift_id, gift["name"], invite_note),
                    )
                    redemption_id = int(redemption.lastrowid)
            draw = conn.execute(
                """INSERT INTO point_draws
                   (chat_id, user_id, gift_id, gift_name, points_spent,
                    probability, random_value, is_winner, redemption_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, user_id, gift_id, gift["name"], points_to_db(draw_cost),
                    probability, random_value, int(is_winner), redemption_id,
                ),
            )
            detail = str(gift["name"]) if is_winner else "未中奖"
            if invite_note:
                detail = f"{detail} · {invite_note}"
            self.add_point_game_record(
                chat_id, user_id, "draw",
                stake=draw_cost, delta=-draw_cost, balance_after=balance,
                is_win=bool(is_winner),
                detail=detail,
                conn=conn,
            )
            return {
                "draw_id": int(draw.lastrowid),
                "gift_name": str(gift["name"]),
                "balance": balance,
                "probability": probability,
                "is_winner": is_winner,
                "redemption_id": redemption_id,
                "points_spent": draw_cost,
            }

    def upsert_tron_monitor(
        self, owner_id: int, address: str, asset: str,
        low_balance: str = "", high_balance: str = "",
        last_balance: str = "", seen_tx_ids: list[str] | None = None,
        notification_delete_days: int = 7,
        notify_transfers: bool = True,
        min_transfer_amount: str = "0.1",
        alert_state: str = "",
        monitor_state: str = "live",
        cursor_tx_id: str = "",
        cursor_block: int = 0,
        cursor_timestamp_ms: int = 0,
    ) -> int:
        address = address.strip()
        asset = asset.strip().lower()
        if asset not in {"trx", "usdt", "both"}:
            raise ValueError("监控币种只能是TRX、USDT或两者")
        if not address:
            raise ValueError("波场地址不能为空")
        if not 0 <= notification_delete_days <= 30:
            raise ValueError("提醒消息撤回天数范围为0-30")
        try:
            minimum = Decimal(str(min_transfer_amount))
        except InvalidOperation as exc:
            raise ValueError("最小交易播报金额无效") from exc
        if minimum < Decimal("0.1"):
            raise ValueError("最小交易播报金额不能小于0.1")
        if monitor_state not in {"bootstrapping", "live"}:
            raise ValueError("监控状态无效")
        seen = json.dumps((seen_tx_ids or [])[:1000], ensure_ascii=True)
        with self.connect() as conn:
            active_other_addresses = int(conn.execute(
                """SELECT COUNT(DISTINCT address) FROM tron_monitors
                   WHERE owner_id=? AND is_enabled=1 AND address<>?""",
                (owner_id, address[:128]),
            ).fetchone()[0])
            if active_other_addresses >= 5:
                raise ValueError("每个用户最多只能监控5个地址")
            conn.execute(
                """INSERT INTO tron_monitors
                   (owner_id, address, asset, low_balance, high_balance,
                    last_balance, seen_tx_ids, notification_delete_days,
                    notify_transfers, min_transfer_amount, alert_state,
                    monitor_state, cursor_tx_id, cursor_block,
                    cursor_timestamp_ms, last_checked_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT(owner_id, address, asset) DO UPDATE SET
                     low_balance=excluded.low_balance,
                     high_balance=excluded.high_balance,
                     last_balance=excluded.last_balance,
                     seen_tx_ids=excluded.seen_tx_ids,
                     notification_delete_days=excluded.notification_delete_days,
                     notify_transfers=excluded.notify_transfers,
                     min_transfer_amount=excluded.min_transfer_amount,
                     alert_state=excluded.alert_state,
                     monitor_state=excluded.monitor_state,
                     cursor_tx_id=excluded.cursor_tx_id,
                     cursor_block=excluded.cursor_block,
                     cursor_timestamp_ms=excluded.cursor_timestamp_ms,
                     is_enabled=1, started_at=CURRENT_TIMESTAMP,
                     last_checked_at=CURRENT_TIMESTAMP,
                     last_error='', updated_at=CURRENT_TIMESTAMP""",
                (
                    owner_id, address[:128], asset, low_balance[:80],
                    high_balance[:80], last_balance[:120], seen,
                    notification_delete_days, int(notify_transfers),
                    str(minimum), alert_state[:80], monitor_state,
                    cursor_tx_id[:128], max(0, int(cursor_block)),
                    max(0, int(cursor_timestamp_ms)),
                ),
            )
            row = conn.execute(
                """SELECT id FROM tron_monitors
                   WHERE owner_id=? AND address=? AND asset=?""",
                (owner_id, address[:128], asset),
            ).fetchone()
            return int(row["id"])

    def active_tron_monitors(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM tron_monitors WHERE is_enabled=1
                   ORDER BY last_checked_at ASC, id ASC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()

    def expired_tron_monitors(self, limit: int = 100) -> list[sqlite3.Row]:
        return []

    def list_tron_monitors(self, owner_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM tron_monitors WHERE owner_id=?
                   ORDER BY is_enabled DESC, id DESC""",
                (owner_id,),
            ).fetchall()


    def create_group_poll(
        self, chat_id: int, message_id: int, creator_id: int, question: str,
        options: list[str], is_anonymous: bool = True, allows_multiple: bool = False,
    ) -> int:
        question = question.strip()
        cleaned = [item.strip() for item in options if item.strip()]
        if not 1 <= len(question) <= 300:
            raise ValueError("投票问题需要 1-300 个字符")
        if not 2 <= len(cleaned) <= 10:
            raise ValueError("投票选项需要 2-10 个")
        if any(len(item) > 100 for item in cleaned):
            raise ValueError("每个选项最多 100 个字符")
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO group_polls
                   (chat_id, message_id, creator_id, question, options_json,
                    is_anonymous, allows_multiple)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, message_id, creator_id, question[:300],
                    json.dumps(cleaned, ensure_ascii=False),
                    int(is_anonymous), int(allows_multiple),
                ),
            )
            return int(cursor.lastrowid)

    def list_group_polls(
        self, chat_id: int, limit: int = 10, offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_polls WHERE chat_id=?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (chat_id, max(1, min(limit, 50)), max(0, offset)),
            ).fetchall()

    def group_poll_count(self, chat_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM group_polls WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            return int(row["total"])

    def get_group_poll(self, poll_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM group_polls WHERE id=?", (poll_id,),
            ).fetchone()

    def delete_group_poll(self, chat_id: int, poll_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM group_polls WHERE chat_id=? AND id=?",
                (chat_id, poll_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "DELETE FROM group_polls WHERE chat_id=? AND id=?",
                (chat_id, poll_id),
            )
            return row

    def can_add_tron_monitor(
        self, owner_id: int, address: str,
    ) -> bool:
        with self.connect() as conn:
            active_other_addresses = int(conn.execute(
                """SELECT COUNT(DISTINCT address) FROM tron_monitors
                   WHERE owner_id=? AND is_enabled=1 AND address<>?""",
                (owner_id, address[:128]),
            ).fetchone()[0])
        return active_other_addresses < 5

    def tron_monitor_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT address) AS addresses,
                          COUNT(DISTINCT owner_id) AS users
                   FROM tron_monitors WHERE is_enabled=1"""
            ).fetchone()
        return {
            "addresses": int(row["addresses"] or 0),
            "users": int(row["users"] or 0),
        }

    def tron_monitor_users(
        self, limit: int = 10, offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        with self.connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(DISTINCT owner_id) FROM tron_monitors WHERE is_enabled=1"
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT m.owner_id, COUNT(DISTINCT m.address) AS address_count,
                          COALESCE(NULLIF(u.username, ''), NULLIF(b.username, ''), '') AS username,
                          COALESCE(NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                                   NULLIF(b.display_name, ''), CAST(m.owner_id AS TEXT)) AS display_name,
                          MAX(m.updated_at) AS last_updated_at
                   FROM tron_monitors m
                   LEFT JOIN users u ON u.user_id=m.owner_id
                   LEFT JOIN bot_usage_users b ON b.user_id=m.owner_id
                   WHERE m.is_enabled=1
                   GROUP BY m.owner_id
                   ORDER BY address_count DESC, last_updated_at DESC, m.owner_id
                   LIMIT ? OFFSET ?""",
                (max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
        return rows, total

    def tron_monitor_owner_by_query(self, value: str) -> sqlite3.Row | None:
        target = value.strip().removeprefix("@").casefold()
        if not target:
            return None
        with self.connect() as conn:
            if target.isdigit():
                owner_id = int(target)
            else:
                owner = conn.execute(
                    """SELECT user_id FROM users WHERE username=? COLLATE NOCASE
                       UNION ALL
                       SELECT user_id FROM bot_usage_users WHERE username=? COLLATE NOCASE
                       UNION ALL
                       SELECT user_id FROM point_accounts WHERE username=? COLLATE NOCASE
                       UNION ALL
                       SELECT user_id FROM group_activity_users WHERE username=? COLLATE NOCASE
                       LIMIT 1""",
                    (target, target, target, target),
                ).fetchone()
                if not owner:
                    return None
                owner_id = int(owner["user_id"])
            return conn.execute(
                """SELECT m.owner_id, COUNT(DISTINCT m.address) AS address_count,
                          COALESCE(NULLIF(u.username, ''), NULLIF(b.username, ''), '') AS username,
                          COALESCE(NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                                   NULLIF(b.display_name, ''), CAST(m.owner_id AS TEXT)) AS display_name
                   FROM tron_monitors m
                   LEFT JOIN users u ON u.user_id=m.owner_id
                   LEFT JOIN bot_usage_users b ON b.user_id=m.owner_id
                   WHERE m.owner_id=? AND m.is_enabled=1
                   GROUP BY m.owner_id""",
                (owner_id,),
            ).fetchone()

    def update_tron_monitor_snapshot(
        self, monitor_id: int, last_balance: str, seen_tx_ids: list[str],
        alert_state: str = "", error: str = "", mark_checked: bool = True,
        checked_at: str = "",
        monitor_state: str | None = None,
        cursor_tx_id: str | None = None,
        cursor_block: int | None = None,
        cursor_timestamp_ms: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE tron_monitors SET last_balance=?, seen_tx_ids=?,
                   alert_state=?,
                   monitor_state=COALESCE(?, monitor_state),
                   cursor_tx_id=COALESCE(?, cursor_tx_id),
                   cursor_block=COALESCE(?, cursor_block),
                   cursor_timestamp_ms=COALESCE(?, cursor_timestamp_ms),
                   last_checked_at=CASE WHEN ? THEN COALESCE(NULLIF(?, ''), CURRENT_TIMESTAMP)
                                        ELSE last_checked_at END,
                   last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    last_balance[:120],
                    json.dumps(seen_tx_ids[:1000], ensure_ascii=True),
                    alert_state[:80], monitor_state,
                    cursor_tx_id[:128] if cursor_tx_id is not None else None,
                    max(0, int(cursor_block)) if cursor_block is not None else None,
                    max(0, int(cursor_timestamp_ms)) if cursor_timestamp_ms is not None else None,
                    int(mark_checked), checked_at[:19],
                    error[:500], monitor_id,
                ),
            )

    def disable_tron_monitor(self, owner_id: int, monitor_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE tron_monitors SET is_enabled=0,
                   updated_at=CURRENT_TIMESTAMP WHERE owner_id=? AND id=?""",
                (owner_id, monitor_id),
            )
            return cursor.rowcount > 0

    def schedule_message_deletion(
        self, chat_id: int, message_id: int, delay_seconds: int
    ) -> None:
        if delay_seconds <= 0:
            return
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO scheduled_message_deletions
                   (chat_id, message_id, delete_at)
                   VALUES (?, ?, DATETIME('now', '+' || ? || ' seconds'))
                   ON CONFLICT(chat_id, message_id) DO UPDATE SET
                     delete_at=excluded.delete_at, attempts=0, last_error=''""",
                (chat_id, message_id, delay_seconds),
            )

    def due_message_deletions(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM scheduled_message_deletions
                   WHERE delete_at<=CURRENT_TIMESTAMP ORDER BY id LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()

    def finish_message_deletion(self, deletion_id: int, error: str = "") -> None:
        with self.connect() as conn:
            if error:
                conn.execute(
                    """UPDATE scheduled_message_deletions SET attempts=attempts+1,
                       last_error=?, delete_at=DATETIME('now','+1 hour') WHERE id=?""",
                    (error[:500], deletion_id),
                )
            else:
                conn.execute(
                    "DELETE FROM scheduled_message_deletions WHERE id=?",
                    (deletion_id,),
                )

    def quick_post(self, chat_id: int, post_id: int | None = None) -> sqlite3.Row:
        with self.connect() as conn:
            if post_id is None:
                row = conn.execute(
                    "SELECT * FROM quick_posts WHERE chat_id=? ORDER BY id LIMIT 1",
                    (chat_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM quick_posts WHERE chat_id=? AND id=?",
                    (chat_id, post_id),
                ).fetchone()
            if row:
                return row
            if post_id is not None:
                raise ValueError("快捷发布消息不存在")
            share_code = f"inline{secrets.token_hex(6)}"
            cursor = conn.execute(
                """INSERT INTO quick_posts (chat_id, share_code)
                   VALUES (?, ?)""",
                (chat_id, share_code),
            )
            return conn.execute(
                "SELECT * FROM quick_posts WHERE id=?", (int(cursor.lastrowid),)
            ).fetchone()

    def quick_posts(self, chat_id: int) -> list[sqlite3.Row]:
        self.quick_post(chat_id)
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM quick_posts WHERE chat_id=? ORDER BY id", (chat_id,)
            ).fetchall()

    def create_quick_post(self, chat_id: int, name: str, updated_by: int) -> sqlite3.Row:
        title = name.strip()[:40]
        if not title:
            raise ValueError("消息名称不能为空")
        with self.connect() as conn:
            count = int(conn.execute(
                "SELECT COUNT(*) FROM quick_posts WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])
            if count >= 20:
                raise ValueError("每个群最多保存20条快捷发布消息")
            cursor = conn.execute(
                """INSERT INTO quick_posts (chat_id, name, share_code, updated_by)
                   VALUES (?, ?, ?, ?)""",
                (chat_id, title, f"inline{secrets.token_hex(6)}", updated_by),
            )
            return conn.execute(
                "SELECT * FROM quick_posts WHERE id=?", (int(cursor.lastrowid),)
            ).fetchone()

    def delete_quick_post(self, chat_id: int, post_id: int) -> bool:
        with self.connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM quick_posts WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])
            if total <= 1:
                raise ValueError("至少保留一条快捷发布消息，可使用清空内容")
            cursor = conn.execute(
                "DELETE FROM quick_posts WHERE chat_id=? AND id=?", (chat_id, post_id)
            )
            return cursor.rowcount > 0

    def quick_post_by_code(self, share_code: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM quick_posts WHERE share_code=?", (share_code.strip(),)
            ).fetchone()

    def update_quick_post(
        self, chat_id: int, updated_by: int, post_id: int | None = None, **values: str
    ) -> None:
        allowed = {
            "name", "text", "file_id", "file_type", "file_name",
            "button_text", "button_url", "entities_json",
        }
        assignments: list[str] = []
        params: list[object] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            assignments.append(f"{key}=?")
            params.append(str(value))
        if not assignments:
            return
        row = self.quick_post(chat_id, post_id)
        params.extend((updated_by, int(row["id"])))
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE quick_posts SET {', '.join(assignments)}, updated_by=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                params,
            )

    def quick_post_buttons(self, post_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM quick_post_buttons WHERE post_id=?
                   ORDER BY position, id""", (post_id,),
            ).fetchall()
            if rows:
                return rows
            post = conn.execute(
                "SELECT button_text, button_url FROM quick_posts WHERE id=?", (post_id,)
            ).fetchone()
            if post and post["button_text"] and post["button_url"]:
                conn.execute(
                    """INSERT OR IGNORE INTO quick_post_buttons
                       (post_id, position, text, url) VALUES (?, 0, ?, ?)""",
                    (post_id, post["button_text"], post["button_url"]),
                )
                return conn.execute(
                    """SELECT * FROM quick_post_buttons WHERE post_id=?
                       ORDER BY position, id""", (post_id,),
                ).fetchall()
            return []

    def add_quick_post_button(
        self, post_id: int, text: str, url: str, color: str = "default",
        width: str = "long", custom_emoji_id: str = "",
    ) -> int:
        color = color.casefold()
        if color not in {"default", "primary", "success", "danger"}:
            raise ValueError("按钮颜色无效")
        width = width.casefold()
        if width not in {"long", "short"}:
            raise ValueError("按钮类型无效")
        with self.connect() as conn:
            count = int(conn.execute(
                "SELECT COUNT(*) FROM quick_post_buttons WHERE post_id=?", (post_id,)
            ).fetchone()[0])
            if count >= 12:
                raise ValueError("每条消息最多添加12个按钮")
            cursor = conn.execute(
                """INSERT INTO quick_post_buttons
                   (post_id, position, text, url, color, width, custom_emoji_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (post_id, count, text, url.strip(), color, width,
                 custom_emoji_id.strip()[:100]),
            )
            return int(cursor.lastrowid)

    def delete_quick_post_button(self, post_id: int, button_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM quick_post_buttons WHERE post_id=? AND id=?",
                (post_id, button_id),
            )
            return cursor.rowcount > 0

    def update_quick_post_button(
        self, post_id: int, button_id: int, text: str, url: str,
        color: str, width: str, custom_emoji_id: str = "",
    ) -> bool:
        if color not in {"default", "primary", "success", "danger"}:
            raise ValueError("按钮颜色无效")
        if width not in {"long", "short"}:
            raise ValueError("按钮类型无效")
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE quick_post_buttons SET text=?,url=?,color=?,width=?,custom_emoji_id=?
                   WHERE post_id=? AND id=?""",
                (text, url.strip(), color, width,
                 custom_emoji_id.strip()[:100], post_id, button_id),
            )
            return cursor.rowcount > 0

    def schedule_quick_post(
        self, chat_id: int, post_id: int, run_at: str, created_by: int
    ) -> int:
        self.quick_post(chat_id, post_id)
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO scheduled_quick_posts
                   (chat_id, post_id, run_at, created_by) VALUES (?, ?, ?, ?)""",
                (chat_id, post_id, run_at, created_by),
            )
            return int(cursor.lastrowid)

    def pending_quick_post_schedules(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT s.*, p.name FROM scheduled_quick_posts s
                   JOIN quick_posts p ON p.id=s.post_id
                   WHERE s.chat_id=? AND s.status='pending'
                   ORDER BY s.run_at, s.id""", (chat_id,),
            ).fetchall()

    def due_quick_posts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT s.*, p.name, p.text, p.file_id, p.file_type, p.file_name,
                          p.entities_json, p.button_text, p.button_url, p.share_code
                   FROM scheduled_quick_posts s JOIN quick_posts p ON p.id=s.post_id
                   WHERE s.status='pending' AND s.run_at<=CURRENT_TIMESTAMP
                   ORDER BY s.run_at, s.id LIMIT ?""", (max(1, min(limit, 100)),),
            ).fetchall()

    def finish_quick_post_schedule(self, schedule_id: int, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE scheduled_quick_posts SET status=?, sent_at=CURRENT_TIMESTAMP,
                   last_error=? WHERE id=? AND status='pending'""",
                ("failed" if error else "sent", error[:500], schedule_id),
            )

    def cancel_quick_post_schedule(self, chat_id: int, schedule_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE scheduled_quick_posts SET status='cancelled'
                   WHERE id=? AND chat_id=? AND status='pending'""",
                (schedule_id, chat_id),
            )
            return cursor.rowcount > 0

    def channel_groups(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT g.*, COUNT(c.id) AS channel_count FROM channel_groups g
                   LEFT JOIN broadcast_channels c ON c.group_id=g.id AND c.is_enabled=1
                   GROUP BY g.id ORDER BY g.id"""
            ).fetchall()

    def add_channel_group(self, name: str, created_by: int) -> int:
        name = " ".join(name.strip().split())[:60]
        if not name:
            raise ValueError("分组名称不能为空")
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO channel_groups (name, created_by) VALUES (?, ?)",
                    (name, created_by),
                )
            except sqlite3.IntegrityError:
                raise ValueError("这个频道分组已经存在") from None
            return int(cursor.lastrowid)

    def delete_channel_group(self, group_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM channel_groups WHERE id=?", (group_id,))
            return cursor.rowcount > 0

    def broadcast_channels(self, group_id: int | None = None) -> list[sqlite3.Row]:
        where, params = "", []
        if group_id is not None:
            where, params = " WHERE c.group_id=?", [group_id]
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT c.*, COALESCE(g.name, '') AS group_name
                    FROM broadcast_channels c LEFT JOIN channel_groups g ON g.id=c.group_id
                    {where} ORDER BY c.id""", params,
            ).fetchall()

    def save_broadcast_channel(
        self, chat_id: int, title: str, username: str, added_by: int
    ) -> int:
        if chat_id >= 0:
            raise ValueError("请输入频道，不支持私聊账号")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO broadcast_channels
                   (chat_id, title, username, added_by) VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                     username=excluded.username, is_enabled=1,
                     updated_at=CURRENT_TIMESTAMP""",
                (chat_id, title[:160], username[:80], added_by),
            )
            return int(conn.execute(
                "SELECT id FROM broadcast_channels WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])

    def delete_broadcast_channel(self, channel_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM broadcast_channels WHERE id=?", (channel_id,))
            return cursor.rowcount > 0

    def assign_broadcast_channel(self, channel_id: int, group_id: int | None) -> bool:
        with self.connect() as conn:
            if group_id is not None and not conn.execute(
                "SELECT id FROM channel_groups WHERE id=?", (group_id,)
            ).fetchone():
                raise ValueError("频道分组不存在")
            cursor = conn.execute(
                """UPDATE broadcast_channels SET group_id=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""", (group_id, channel_id),
            )
            return cursor.rowcount > 0

    def channel_messages(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM channel_messages ORDER BY id").fetchall()

    def channel_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM channel_messages WHERE id=?", (message_id,)
            ).fetchone()

    def save_channel_message(
        self, name: str, text: str, file_id: str, file_type: str,
        file_name: str, entities_json: str, created_by: int,
        message_id: int | None = None,
    ) -> int:
        name = " ".join(name.strip().split())[:60]
        if not name:
            raise ValueError("消息名称不能为空")
        with self.connect() as conn:
            if message_id is None:
                cursor = conn.execute(
                    """INSERT INTO channel_messages
                       (name,text,file_id,file_type,file_name,entities_json,created_by)
                       VALUES (?,?,?,?,?,?,?)""",
                    (name, text, file_id, file_type, file_name, entities_json, created_by),
                )
                return int(cursor.lastrowid)
            cursor = conn.execute(
                """UPDATE channel_messages SET name=?, text=?, file_id=?, file_type=?,
                   file_name=?, entities_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (name, text, file_id, file_type, file_name, entities_json, message_id),
            )
            if not cursor.rowcount:
                raise ValueError("频道消息不存在")
            return message_id

    def delete_channel_message(self, message_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM channel_messages WHERE id=?", (message_id,))
            return cursor.rowcount > 0

    def channel_targets(self, target_type: str, target_id: int = 0) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if target_type == "all":
                return conn.execute(
                    "SELECT * FROM broadcast_channels WHERE is_enabled=1 ORDER BY id"
                ).fetchall()
            if target_type == "group":
                return conn.execute(
                    """SELECT * FROM broadcast_channels WHERE is_enabled=1
                       AND group_id=? ORDER BY id""", (target_id,),
                ).fetchall()
            if target_type == "channel":
                return conn.execute(
                    "SELECT * FROM broadcast_channels WHERE is_enabled=1 AND id=?",
                    (target_id,),
                ).fetchall()
            raise ValueError("发送目标无效")

    def schedule_channel_message(
        self, message_id: int, target_type: str, target_id: int,
        run_at: str, created_by: int,
    ) -> int:
        if not self.channel_message(message_id):
            raise ValueError("频道消息不存在")
        if not self.channel_targets(target_type, target_id):
            raise ValueError("发送目标没有可用频道")
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO channel_schedules
                   (message_id,target_type,target_id,run_at,created_by)
                   VALUES (?,?,?,?,?)""",
                (message_id, target_type, target_id, run_at, created_by),
            )
            return int(cursor.lastrowid)

    def channel_schedules(self, pending_only: bool = True) -> list[sqlite3.Row]:
        where = "WHERE s.status='pending'" if pending_only else ""
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT s.*, m.name FROM channel_schedules s
                    JOIN channel_messages m ON m.id=s.message_id {where}
                    ORDER BY s.run_at, s.id"""
            ).fetchall()

    def due_channel_schedules(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM channel_schedules WHERE status='pending'
                   AND run_at<=CURRENT_TIMESTAMP ORDER BY run_at,id LIMIT ?""",
                (max(1, min(limit, 50)),),
            ).fetchall()

    def finish_channel_schedule(
        self, schedule_id: int, sent_count: int, failed_count: int, error: str = ""
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE channel_schedules SET status='finished', sent_at=CURRENT_TIMESTAMP,
                   sent_count=?, failed_count=?, last_error=? WHERE id=?""",
                (sent_count, failed_count, error[:500], schedule_id),
            )

    def invite_config(self, chat_id: int) -> sqlite3.Row:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO group_invite_config (chat_id) VALUES (?)",
                (chat_id,),
            )
            return conn.execute(
                "SELECT * FROM group_invite_config WHERE chat_id=?", (chat_id,)
            ).fetchone()

    def update_invite_config(
        self, chat_id: int, updated_by: int, enabled: bool | None = None,
        expire_seconds: int | None = None, member_limit: int | None = None,
        points_per_invite=None,
    ) -> None:
        self.invite_config(chat_id)
        fields: list[str] = []
        values: list[object] = []
        if enabled is not None:
            fields.append("is_enabled=?")
            values.append(int(enabled))
        if expire_seconds is not None:
            if expire_seconds < 0 or expire_seconds > 31536000:
                raise ValueError("链接过期时间范围无效")
            fields.append("expire_seconds=?")
            values.append(expire_seconds)
        if member_limit is not None:
            if member_limit < 0 or member_limit > 99999:
                raise ValueError("最大邀请人数范围为0-99999")
            fields.append("member_limit=?")
            values.append(member_limit)
        if points_per_invite is not None:
            points_per_invite = normalize_points(points_per_invite)
            if points_per_invite < 0 or points_per_invite > 1000000:
                raise ValueError("每人邀请积分范围为0-1000000")
            fields.append("points_per_invite=?")
            values.append(points_to_db(points_per_invite))
        if not fields:
            return
        values.extend((updated_by, chat_id))
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE group_invite_config SET {', '.join(fields)},
                    updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                values,
            )

    def active_invite_link(self, chat_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE chat_id=? AND user_id=? AND is_active=1
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, user_id),
            ).fetchone()

    def save_invite_link(
        self, chat_id: int, user_id: int, invite_link: str, invite_name: str = "",
        username: str = "", display_name: str = "",
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_invite_links SET is_active=0,
                   revoked_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND user_id=? AND is_active=1""",
                (chat_id, user_id),
            )
            cursor = conn.execute(
                """INSERT INTO group_invite_links
                   (chat_id, user_id, invite_link, invite_name, username, display_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, user_id, invite_link, invite_name[:80],
                    username[:80], display_name[:160],
                ),
            )
            return int(cursor.lastrowid)

    def invite_link_by_url(self, chat_id: int, invite_link: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE chat_id=? AND invite_link=? ORDER BY id DESC LIMIT 1""",
                (chat_id, invite_link),
            ).fetchone()

    def invite_link_by_url_any(self, invite_link: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE invite_link=? ORDER BY is_active DESC, id DESC LIMIT 1""",
                (invite_link,),
            ).fetchone()

    def invite_link_by_id(self, chat_id: int, link_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM group_invite_links WHERE chat_id=? AND id=?",
                (chat_id, link_id),
            ).fetchone()

    def invite_link_by_owner_query(
        self, chat_id: int, value: str,
    ) -> sqlite3.Row | None:
        links = self.invite_links_by_owner_query(chat_id, value)
        return links[0] if links else None

    def invite_links_by_owner_id(
        self, chat_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE chat_id=? AND user_id=?
                     AND created_at>=DATETIME('now','-6 months')
                   ORDER BY is_active DESC, id DESC""",
                (chat_id, user_id),
            ).fetchall()

    def invite_links_by_owner_query(
        self, chat_id: int, value: str,
    ) -> list[sqlite3.Row]:
        target = value.strip().removeprefix("@").casefold()
        if not target:
            return []
        with self.connect() as conn:
            if target.isdigit():
                owner_id = int(target)
            else:
                direct = conn.execute(
                    """SELECT user_id FROM group_invite_links
                       WHERE chat_id=? AND username=? COLLATE NOCASE
                       ORDER BY is_active DESC, id DESC LIMIT 1""",
                    (chat_id, target),
                ).fetchone()
                owner = direct or conn.execute(
                    """SELECT user_id FROM point_accounts
                       WHERE chat_id=? AND username=? COLLATE NOCASE
                       UNION ALL
                       SELECT user_id FROM group_activity_users
                       WHERE chat_id=? AND username=? COLLATE NOCASE
                       UNION ALL
                       SELECT user_id FROM users WHERE username=? COLLATE NOCASE
                       LIMIT 1""",
                    (chat_id, target, chat_id, target, target),
                ).fetchone()
                if not owner:
                    return []
                owner_id = int(owner["user_id"])
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE chat_id=? AND user_id=?
                     AND created_at>=DATETIME('now','-6 months')
                   ORDER BY is_active DESC, id DESC""",
                (chat_id, owner_id),
            ).fetchall()

    def record_invite_join(
        self, chat_id: int, user_id: int, inviter_id: int, link_id: int,
        points_awarded=0, username: str = "", display_name: str = "",
        *, credit_points: bool = False, inviter_username: str = "",
        inviter_name: str = "",
    ) -> bool:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM group_invite_joins WHERE joined_at<DATETIME('now','-6 months')"
            )
            conn.execute(
                """DELETE FROM group_invite_links
                   WHERE is_active=0 AND created_at<DATETIME('now','-6 months')"""
            )
            cursor = conn.execute(
                """INSERT OR IGNORE INTO group_invite_joins
                   (chat_id, user_id, inviter_id, link_id, points_awarded,
                    username, display_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, user_id, inviter_id, link_id,
                    points_to_db(max(Decimal("0"), normalize_points(points_awarded))),
                    username[:80], display_name[:160],
                ),
            )
            added = cursor.rowcount > 0
            if added and credit_points and normalize_points(points_awarded) > 0:
                # Join deduplication and its reward must commit or roll back together.
                self._adjust_points_conn(
                    conn, chat_id, inviter_id, points_awarded,
                    f"邀请成员 {user_id} 首次进群", 0,
                    inviter_username, inviter_name, allow_negative=True,
                )
            return added

    def record_invite_leave(self, chat_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM group_invite_joins
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE group_invite_joins SET left_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                (chat_id, user_id),
            )
            return row

    def invite_stats(self, chat_id: int, user_id: int | None = None) -> dict[str, int]:
        with self.connect() as conn:
            if user_id is None:
                return {
                    "links": int(conn.execute(
                        """SELECT COUNT(*) FROM group_invite_links WHERE chat_id=?
                           AND created_at>=DATETIME('now','-6 months')""",
                        (chat_id,),
                    ).fetchone()[0]),
                    "invites": int(conn.execute(
                        """SELECT COUNT(*) FROM group_invite_joins WHERE chat_id=?
                           AND joined_at>=DATETIME('now','-6 months')""",
                        (chat_id,),
                    ).fetchone()[0]),
                    "exits": int(conn.execute(
                        """SELECT COUNT(*) FROM group_invite_joins
                           WHERE chat_id=? AND left_at IS NOT NULL
                             AND joined_at>=DATETIME('now','-6 months')""",
                        (chat_id,),
                    ).fetchone()[0]),
                }
            return {
                "links": int(conn.execute(
                    """SELECT COUNT(*) FROM group_invite_links
                       WHERE chat_id=? AND user_id=?
                         AND created_at>=DATETIME('now','-6 months')""",
                    (chat_id, user_id),
                ).fetchone()[0]),
                "invites": int(conn.execute(
                    """SELECT COUNT(*) FROM group_invite_joins
                       WHERE chat_id=? AND inviter_id=?
                         AND joined_at>=DATETIME('now','-6 months')""",
                    (chat_id, user_id),
                ).fetchone()[0]),
                "exits": int(conn.execute(
                    """SELECT COUNT(*) FROM group_invite_joins
                       WHERE chat_id=? AND inviter_id=? AND left_at IS NOT NULL
                         AND joined_at>=DATETIME('now','-6 months')""",
                    (chat_id, user_id),
                ).fetchone()[0]),
            }

    def invite_link_stats(self, chat_id: int, link_id: int) -> dict[str, int]:
        return self.invite_links_stats(chat_id, [link_id])

    def invite_links_stats(
        self, chat_id: int, link_ids: list[int],
    ) -> dict[str, int]:
        if not link_ids:
            return {"invites": 0, "exits": 0, "remaining": 0}
        placeholders = ",".join("?" for _ in link_ids)
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS invites,
                           SUM(CASE WHEN left_at IS NOT NULL THEN 1 ELSE 0 END) AS exits
                    FROM group_invite_joins WHERE chat_id=?
                      AND link_id IN ({placeholders})
                      AND joined_at>=DATETIME('now','-6 months')""",
                (chat_id, *link_ids),
            ).fetchone()
        invites = int(row["invites"] or 0)
        exits = int(row["exits"] or 0)
        return {"invites": invites, "exits": exits, "remaining": invites - exits}

    def count_invite_link_members(
        self, chat_id: int, link_id: int, status: str = "joined",
    ) -> int:
        return self.count_invite_links_members(chat_id, [link_id], status)

    def count_invite_links_members(
        self, chat_id: int, link_ids: list[int], status: str = "joined",
    ) -> int:
        condition = {
            "joined": "",
            "exited": " AND j.left_at IS NOT NULL",
            "remaining": " AND j.left_at IS NULL",
            "renamed": " AND j.name_changed_at IS NOT NULL",
            "remaining_unspoken": (
                " AND j.left_at IS NULL AND j.has_spoken=0"
                " AND NOT EXISTS (SELECT 1 FROM group_activity_users a"
                " WHERE a.chat_id=j.chat_id AND a.user_id=j.user_id"
                " AND a.messages>0)"
            ),
            "remaining_spoken": (
                " AND j.left_at IS NULL AND (j.has_spoken=1"
                " OR EXISTS (SELECT 1 FROM group_activity_users a"
                " WHERE a.chat_id=j.chat_id AND a.user_id=j.user_id"
                " AND a.messages>0))"
            ),
        }.get(status)
        if condition is None:
            raise ValueError("邀请人员筛选无效")
        if not link_ids:
            return 0
        placeholders = ",".join("?" for _ in link_ids)
        with self.connect() as conn:
            return int(conn.execute(
                f"""SELECT COUNT(*) FROM group_invite_joins j
                    WHERE j.chat_id=? AND j.link_id IN ({placeholders})
                      AND j.joined_at>=DATETIME('now','-6 months'){condition}""",
                (chat_id, *link_ids),
            ).fetchone()[0])

    def invite_link_members(
        self, chat_id: int, link_id: int, limit: int = 10, offset: int = 0,
        status: str = "joined",
    ) -> list[sqlite3.Row]:
        return self.invite_links_members(
            chat_id, [link_id], limit, offset, status
        )

    def invite_links_members(
        self, chat_id: int, link_ids: list[int], limit: int = 10, offset: int = 0,
        status: str = "joined",
    ) -> list[sqlite3.Row]:
        condition = {
            "joined": "",
            "exited": " AND j.left_at IS NOT NULL",
            "remaining": " AND j.left_at IS NULL",
            "renamed": " AND j.name_changed_at IS NOT NULL",
            "remaining_unspoken": (
                " AND j.left_at IS NULL AND j.has_spoken=0"
                " AND COALESCE(a.messages, 0)=0"
            ),
            "remaining_spoken": (
                " AND j.left_at IS NULL"
                " AND (j.has_spoken=1 OR COALESCE(a.messages, 0)>0)"
            ),
        }.get(status)
        if condition is None:
            raise ValueError("邀请人员筛选无效")
        if not link_ids:
            return []
        placeholders = ",".join("?" for _ in link_ids)
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT j.*,
                          COALESCE(NULLIF(j.username, ''), NULLIF(u.username, ''), '') AS shown_username,
                          COALESCE(NULLIF(j.current_display_name, ''),
                                   NULLIF(j.display_name, ''),
                                   NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                                   CAST(j.user_id AS TEXT)) AS shown_name,
                          CASE WHEN j.has_spoken=1 OR COALESCE(a.messages, 0)>0
                               THEN 1 ELSE 0 END AS spoken,
                          COALESCE(j.last_spoken_at, a.last_spoken_at) AS shown_last_spoken_at,
                          MAX(j.spoken_messages, COALESCE(a.messages, 0)) AS shown_message_count
                   FROM group_invite_joins j
                   LEFT JOIN users u ON u.user_id=j.user_id
                   LEFT JOIN (
                     SELECT chat_id, user_id, SUM(messages) AS messages,
                            MAX(CASE WHEN messages>0 THEN last_seen_at END) AS last_spoken_at
                     FROM group_activity_users GROUP BY chat_id, user_id
                   ) a ON a.chat_id=j.chat_id AND a.user_id=j.user_id
                   WHERE j.chat_id=? AND j.link_id IN ({placeholders})
                     AND j.joined_at>=DATETIME('now','-6 months')
                     {condition}
                   ORDER BY j.joined_at DESC LIMIT ? OFFSET ?""",
                (
                    chat_id, *link_ids,
                    max(1, min(limit, 100)), max(0, offset),
                ),
            ).fetchall()

    def invite_owner_stats(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT l.user_id,
                          COALESCE(NULLIF(MAX(l.username), ''), NULLIF(a.username, ''),
                                   NULLIF(u.username, ''), '') AS username,
                          COALESCE(NULLIF(MAX(l.display_name), ''), NULLIF(a.display_name, ''),
                                   NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                                   CAST(l.user_id AS TEXT)) AS display_name,
                          COUNT(DISTINCT l.id) AS links,
                          COUNT(j.user_id) AS invites,
                          SUM(CASE WHEN j.left_at IS NOT NULL THEN 1 ELSE 0 END) AS exits
                   FROM group_invite_links l
                   LEFT JOIN group_invite_joins j
                     ON j.chat_id=l.chat_id AND j.link_id=l.id
                    AND j.joined_at>=DATETIME('now','-6 months')
                   LEFT JOIN point_accounts a
                     ON a.chat_id=l.chat_id AND a.user_id=l.user_id
                   LEFT JOIN users u ON u.user_id=l.user_id
                   WHERE l.chat_id=?
                     AND l.created_at>=DATETIME('now','-6 months')
                   GROUP BY l.user_id
                   ORDER BY invites DESC, links DESC, l.user_id""",
                (chat_id,),
            ).fetchall()

    def active_invite_links(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_invite_links
                   WHERE chat_id=? AND is_active=1 ORDER BY id""",
                (chat_id,),
            ).fetchall()

    def revoke_invite_links(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_invite_links SET is_active=0,
                   revoked_at=CURRENT_TIMESTAMP WHERE chat_id=? AND is_active=1""",
                (chat_id,),
            )

    def group_recent_audits(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        target = str(chat_id)
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM audit_log
                   WHERE target=? OR target LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (target, f"{target}:%", max(1, min(limit, 100))),
            ).fetchall()

    def record_group_operation(
        self, chat_id: int, action: str, user_id: int = 0,
        username: str = "", display_name: str = "", detail: str = "",
        actor_user_id: int = 0, actor_username: str = "", actor_name: str = "",
    ) -> int:
        action = action.strip().casefold()
        if not action or len(action) > 40:
            raise ValueError("未知群组操作类型")
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM group_operations WHERE created_at<DATETIME('now','-7 days')"
            )
            if action in {"join", "leave", "ban"}:
                duplicate = conn.execute(
                    """SELECT id FROM group_operations
                       WHERE chat_id=? AND action=? AND user_id=?
                         AND created_at>=DATETIME('now','-15 seconds')
                       ORDER BY id DESC LIMIT 1""",
                    (chat_id, action, user_id),
                ).fetchone()
                if duplicate:
                    return int(duplicate["id"])
            cursor = conn.execute(
                """INSERT INTO group_operations
                   (chat_id, action, user_id, username, display_name, detail,
                    actor_user_id, actor_username, actor_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, action, user_id, username[:80],
                    display_name[:160], detail[:500], actor_user_id,
                    actor_username[:80], actor_name[:160],
                ),
            )
            return int(cursor.lastrowid)

    def group_operations(
        self, chat_id: int, limit: int = 20, offset: int = 0,
        action: str = "", actions: tuple[str, ...] = (),
    ) -> list[sqlite3.Row]:
        if action and actions:
            raise ValueError("action and actions cannot be combined")
        where = " AND action=?" if action else ""
        params: list[object] = [chat_id]
        if action:
            params.append(action)
        elif actions:
            where = " AND action IN (" + ",".join("?" for _ in actions) + ")"
            params.extend(actions)
        params.extend((max(1, min(limit, 100)), max(0, offset)))
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT * FROM group_operations WHERE chat_id=?
                    AND created_at>=DATETIME('now','-7 days'){where}
                    ORDER BY id DESC LIMIT ? OFFSET ?""",
                params,
            ).fetchall()

    def count_group_operations(
        self, chat_id: int, action: str = "", actions: tuple[str, ...] = (),
    ) -> int:
        if action and actions:
            raise ValueError("action and actions cannot be combined")
        where = " AND action=?" if action else ""
        params: list[object] = [chat_id]
        if action:
            params.append(action)
        elif actions:
            where = " AND action IN (" + ",".join("?" for _ in actions) + ")"
            params.extend(actions)
        with self.connect() as conn:
            return int(conn.execute(
                f"""SELECT COUNT(*) FROM group_operations WHERE chat_id=?
                    AND created_at>=DATETIME('now','-7 days'){where}""",
                params,
            ).fetchone()[0])

    def group_operation_totals(self, chat_id: int, days: int = 1) -> dict[str, int]:
        modifier = f"-{max(1, min(days, 7)) - 1} days"
        date_filter = (
            " AND created_at>=DATETIME('now','-7 days')" if days <= 0 else
            " AND DATE(created_at,'+8 hours')>=DATE('now','+8 hours',?)"
        )
        params: tuple[object, ...] = (chat_id,) if days <= 0 else (chat_id, modifier)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT action, COUNT(*) AS total FROM group_operations
                    WHERE chat_id=?{date_filter} GROUP BY action""",
                params,
            ).fetchall()
        totals = {"join": 0, "leave": 0, "intercept": 0, "ban": 0}
        totals.update({str(row["action"]): int(row["total"]) for row in rows})
        return totals

    def search_exact_title(self, title: str, limit: int = 10) -> list[Entry]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entries WHERE status='approved'
                   AND title=? COLLATE NOCASE ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (" ".join(title.strip().split()), max(1, min(limit, 20))),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def create_auto_reply(
        self, keyword: str, reply_text: str, match_mode: str = "contains", scope: str = "private"
    ) -> int:
        keyword = keyword.strip().casefold()
        reply_text = reply_text.strip()
        if not keyword or not reply_text:
            raise ValueError("关键词和回复内容不能为空")
        if match_mode not in {"contains", "exact"}:
            raise ValueError("无效匹配方式")
        if scope not in {"private", "group", "both"}:
            raise ValueError("无效作用范围")
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO auto_replies (keyword, reply_text, match_mode, scope)
                       VALUES (?, ?, ?, ?)""",
                    (keyword[:200], reply_text[:4000], match_mode, scope),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("相同关键词和匹配方式已存在") from exc
            return int(cursor.lastrowid)

    def list_auto_replies(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM auto_replies
                   ORDER BY is_enabled DESC, hits DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def match_auto_reply(self, text: str, scope: str = "private") -> sqlite3.Row | None:
        folded = text.strip().casefold()
        if not folded:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM auto_replies
                   WHERE is_enabled=1 AND (scope=? OR scope='both')
                   ORDER BY CASE match_mode WHEN 'exact' THEN 0 ELSE 1 END,
                            LENGTH(keyword) DESC, id ASC""",
                (scope,),
            ).fetchall()
            for row in rows:
                keyword = str(row["keyword"])
                matched = folded == keyword if row["match_mode"] == "exact" else keyword in folded
                if matched:
                    conn.execute(
                        "UPDATE auto_replies SET hits=hits+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (int(row["id"]),),
                    )
                    return row
        return None

    def set_auto_reply_enabled(self, reply_id: int, enabled: bool) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE auto_replies SET is_enabled=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (int(enabled), reply_id),
            )
            return cursor.rowcount > 0

    def delete_auto_reply(self, reply_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM auto_replies WHERE id=?", (reply_id,))
            return cursor.rowcount > 0

    def prune_private_notes(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM private_notes WHERE expires_at < CURRENT_TIMESTAMP")

    PERMANENT_NOTE_EXPIRES_AT = "9999-12-31 23:59:59"

    def add_private_note(
        self,
        keyword: str,
        body: str,
        created_by: int,
        file_id: str = "",
        file_type: str = "",
        file_name: str = "",
        permanent: bool = False,
    ) -> int:
        keyword = keyword.strip().casefold()
        if not keyword:
            raise ValueError("关键词不能为空")
        if not body.strip() and not file_id.strip():
            raise ValueError("内容或文件不能为空")
        with self.connect() as conn:
            conn.execute("DELETE FROM private_notes WHERE expires_at < CURRENT_TIMESTAMP")
            if permanent:
                cursor = conn.execute(
                    """INSERT INTO private_notes
                       (keyword, body, file_id, file_type, file_name, created_by,
                        expires_at, is_permanent)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        keyword[:120], body.strip()[:4000], file_id.strip()[:512],
                        file_type.strip()[:40], file_name.strip()[:240], created_by,
                        self.PERMANENT_NOTE_EXPIRES_AT,
                    ),
                )
            else:
                cursor = conn.execute(
                    """INSERT INTO private_notes
                       (keyword, body, file_id, file_type, file_name, created_by, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, DATETIME('now', '+9 months'))""",
                    (
                        keyword[:120], body.strip()[:4000], file_id.strip()[:512],
                        file_type.strip()[:40], file_name.strip()[:240], created_by,
                    ),
                )
            # 永久笔记不参与 99 条上限裁剪，只裁剪普通（9个月）笔记。
            conn.execute(
                """DELETE FROM private_notes
                   WHERE keyword=? AND COALESCE(is_permanent,0)=0 AND id NOT IN (
                     SELECT id FROM private_notes
                     WHERE keyword=? AND COALESCE(is_permanent,0)=0
                     ORDER BY created_at DESC, id DESC
                     LIMIT 99
                   )""",
                (keyword[:120], keyword[:120]),
            )
            return int(cursor.lastrowid)

    def latest_private_note(self, keyword: str) -> sqlite3.Row | None:
        """Most recent (non-expired) note under keyword, or None."""
        rows = self.private_notes(keyword, limit=1)
        return rows[0] if rows else None

    def private_notes(
        self, keyword: str, limit: int = 10, offset: int = 0,
    ) -> list[sqlite3.Row]:
        keyword = keyword.strip().casefold()
        limit = max(1, min(int(limit), 99))
        offset = max(0, int(offset))
        with self.connect() as conn:
            conn.execute("DELETE FROM private_notes WHERE expires_at < CURRENT_TIMESTAMP")
            return conn.execute(
                """SELECT * FROM private_notes WHERE keyword=?
                   ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (keyword[:120], limit, offset),
            ).fetchall()

    def count_private_notes(self, keyword: str) -> int:
        keyword = keyword.strip().casefold()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM private_notes WHERE keyword=? AND expires_at >= CURRENT_TIMESTAMP",
                (keyword[:120],),
            ).fetchone()
        return int(row["count"])

    def save_lottery_result(self, result) -> tuple[bool, bool]:
        with self.connect() as conn:
            previous = conn.execute(
                """SELECT issue FROM lottery_results WHERE game_code=?
                   ORDER BY issue DESC, id DESC LIMIT 1""",
                (result.game_code,),
            ).fetchone()
            existing = conn.execute(
                "SELECT id FROM lottery_results WHERE game_code=? AND issue=?",
                (result.game_code, result.issue),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE lottery_results SET game_name=?, draw_time=?, primary_numbers=?,
                       secondary_numbers=?, detail_url=?, fetched_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        result.game_name, result.draw_time, " ".join(result.primary),
                        " ".join(result.secondary), result.detail_url, int(existing["id"]),
                    ),
                )
                return False, bool(previous)
            conn.execute(
                """INSERT INTO lottery_results
                   (source, game_code, game_name, issue, draw_time, primary_numbers,
                    secondary_numbers, detail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.source, result.game_code, result.game_name, result.issue,
                    result.draw_time, " ".join(result.primary), " ".join(result.secondary),
                    result.detail_url,
                ),
            )
            return True, bool(previous)

    def latest_lottery_results(self, game_code: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if game_code:
                return conn.execute(
                    """SELECT * FROM lottery_results WHERE game_code=?
                       ORDER BY issue DESC, id DESC LIMIT 20""",
                    (game_code,),
                ).fetchall()
            return conn.execute(
                """SELECT r.* FROM lottery_results r
                   WHERE r.issue=(SELECT MAX(x.issue) FROM lottery_results x WHERE x.game_code=r.game_code)
                   ORDER BY CASE r.source WHEN 'cwl' THEN 0 ELSE 1 END, r.game_code"""
            ).fetchall()

    def lottery_history(self, game_code: str, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM lottery_results WHERE game_code=?
                   ORDER BY issue DESC, id DESC LIMIT ?""",
                (game_code, max(1, min(limit, 100))),
            ).fetchall()

    def add_lottery_subscription(self, chat_id: int, selector: str, created_by: int = 0) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO lottery_subscriptions (chat_id, selector, created_by)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id, selector) DO UPDATE SET
                     created_by=excluded.created_by, is_enabled=1""",
                (chat_id, selector[:30], created_by),
            )

    def remove_lottery_subscription(self, chat_id: int, selector: str | None = None) -> int:
        with self.connect() as conn:
            if selector is None:
                cursor = conn.execute("DELETE FROM lottery_subscriptions WHERE chat_id=?", (chat_id,))
            else:
                cursor = conn.execute(
                    "DELETE FROM lottery_subscriptions WHERE chat_id=? AND selector=?",
                    (chat_id, selector),
                )
            return cursor.rowcount

    def lottery_subscriptions(self, chat_id: int | None = None, limit: int = 300) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if chat_id is not None:
                return conn.execute(
                    """SELECT s.*, g.title AS group_title FROM lottery_subscriptions s
                       LEFT JOIN groups g ON g.chat_id=s.chat_id WHERE s.chat_id=?
                       ORDER BY s.selector""",
                    (chat_id,),
                ).fetchall()
            return conn.execute(
                """SELECT s.*, g.title AS group_title FROM lottery_subscriptions s
                   LEFT JOIN groups g ON g.chat_id=s.chat_id
                   ORDER BY s.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def lottery_subscriber_chat_ids(self, game_code: str, source: str) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT chat_id FROM lottery_subscriptions
                   WHERE is_enabled=1 AND selector IN ('all', ?, ?, ?)""",
                (source, game_code, "marksix" if game_code in {
                    "hklhc", "macau_lhc", "new_macau_lhc"
                } else ""),
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def set_lottery_source_status(self, source: str, success: bool, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO lottery_source_status
                   (source, last_success_at, last_error) VALUES (?, CASE WHEN ? THEN CURRENT_TIMESTAMP END, ?)
                   ON CONFLICT(source) DO UPDATE SET
                     last_checked_at=CURRENT_TIMESTAMP,
                     last_success_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE lottery_source_status.last_success_at END,
                     last_error=excluded.last_error""",
                (source, int(success), error[:1000], int(success)),
            )

    def lottery_source_status(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT *, CAST((JULIANDAY('now')-JULIANDAY(last_checked_at))*86400 AS INTEGER)
                   AS age_seconds FROM lottery_source_status ORDER BY source"""
            ).fetchall()

    def audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM audit_log WHERE created_at<DATETIME('now','-7 days')"
            )
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail) VALUES (?, ?, ?, ?)",
                (actor[:80], action[:120], target[:200], detail[:1000]),
            )

    def list_audit(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM audit_log
                   WHERE created_at>=DATETIME('now','-7 days')
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def add_chain_query(
        self, user_id: int, address: str, query_type: str, result_summary: str, success: bool = True
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO chain_queries (user_id, address, query_type, result_summary, success)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, address[:80], query_type[:40], result_summary[:500], int(success)),
            )

    def list_chain_queries(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM chain_queries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def chain_query_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM chain_queries"
            ).fetchone()[0])

    def record_group_activity(
        self,
        chat_id: int,
        title: str,
        username: str,
        chat_type: str,
        user_id: int = 0,
        user_username: str = "",
        display_name: str = "",
        messages: int = 0,
        joins: int = 0,
        leaves: int = 0,
        blocked: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO groups
                   (chat_id, title, username, chat_type, message_count, joins_count, leaves_count, blocked_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     title=excluded.title, username=excluded.username, chat_type=excluded.chat_type,
                     message_count=groups.message_count+excluded.message_count,
                     joins_count=groups.joins_count+excluded.joins_count,
                     leaves_count=groups.leaves_count+excluded.leaves_count,
                     blocked_count=groups.blocked_count+excluded.blocked_count,
                     last_seen_at=CURRENT_TIMESTAMP""",
                (chat_id, title[:200], username[:80], chat_type[:30], messages, joins, leaves, blocked),
            )
            conn.execute(
                """INSERT INTO group_daily_stats (chat_id, day, messages, joins, leaves, blocked)
                   VALUES (?, DATE('now','+8 hours'), ?, ?, ?, ?)
                   ON CONFLICT(chat_id, day) DO UPDATE SET
                     messages=group_daily_stats.messages+excluded.messages,
                     joins=group_daily_stats.joins+excluded.joins,
                     leaves=group_daily_stats.leaves+excluded.leaves,
                     blocked=group_daily_stats.blocked+excluded.blocked""",
                (chat_id, messages, joins, leaves, blocked),
            )
            if user_id and messages:
                conn.execute(
                    """INSERT INTO group_activity_users
                       (chat_id, user_id, day, username, display_name, messages)
                       VALUES (?, ?, DATE('now','+8 hours'), ?, ?, ?)
                       ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
                         username=excluded.username,
                         display_name=excluded.display_name,
                         messages=group_activity_users.messages+excluded.messages,
                         last_seen_at=CURRENT_TIMESTAMP""",
                    (chat_id, user_id, user_username[:80], display_name[:160], messages),
                )
                conn.executemany(
                    """INSERT INTO group_message_events
                       (chat_id, user_id, username, display_name)
                       VALUES (?, ?, ?, ?)""",
                    (
                        (chat_id, user_id, user_username[:80], display_name[:160])
                        for _ in range(messages)
                    ),
                )
                conn.execute(
                    """UPDATE group_invite_joins
                       SET has_spoken=1,
                           last_spoken_at=CURRENT_TIMESTAMP,
                           first_spoken_name=CASE
                             WHEN first_spoken_name='' AND ?<>'' THEN ?
                             ELSE first_spoken_name END,
                           name_changed_at=CASE
                             WHEN first_spoken_name<>''
                              AND current_display_name<>''
                              AND ?<>'' AND current_display_name<>?
                             THEN CURRENT_TIMESTAMP ELSE name_changed_at END,
                           current_display_name=CASE
                             WHEN ?<>'' THEN ? ELSE current_display_name END,
                           spoken_messages=spoken_messages+?
                       WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                    (
                        display_name[:160], display_name[:160],
                        display_name[:160], display_name[:160],
                        display_name[:160], display_name[:160],
                        messages, chat_id, user_id,
                    ),
                )
            conn.execute(
                "DELETE FROM group_activity_users WHERE day < DATE('now','+8 hours','-30 days')"
            )
            conn.execute(
                "DELETE FROM group_daily_stats WHERE day < DATE('now','+8 hours','-30 days')"
            )
            conn.execute(
                "DELETE FROM group_message_events WHERE created_at < DATETIME('now','-31 days')"
            )

    def group_join_config(self, chat_id: int) -> sqlite3.Row:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO group_join_config (chat_id) VALUES (?)", (chat_id,)
            )
            return conn.execute(
                "SELECT * FROM group_join_config WHERE chat_id=?", (chat_id,)
            ).fetchone()

    def set_group_join_config(
        self, chat_id: int, updated_by: int, welcome_enabled: bool | None = None,
        welcome_text: str | None = None, verification_enabled: bool | None = None,
    ) -> None:
        self.group_join_config(chat_id)
        updates = ["updated_by=?", "updated_at=CURRENT_TIMESTAMP"]
        values: list[object] = [updated_by]
        if welcome_enabled is not None:
            updates.append("welcome_enabled=?")
            values.append(int(welcome_enabled))
        if welcome_text is not None:
            text = welcome_text.strip()
            if not text or len(text) > 1000:
                raise ValueError("欢迎语需要1-1000个字符")
            updates.append("welcome_text=?")
            values.append(text)
        if verification_enabled is not None:
            updates.append("verification_enabled=?")
            values.append(int(verification_enabled))
        values.append(chat_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE group_join_config SET {', '.join(updates)} WHERE chat_id=?",
                values,
            )

    def list_groups(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT g.*,
                   COALESCE((SELECT messages FROM group_daily_stats d
                     WHERE d.chat_id=g.chat_id AND d.day=DATE('now','+8 hours')), 0) AS today_messages,
                   COALESCE((SELECT COUNT(*) FROM group_activity_users a
                     WHERE a.chat_id=g.chat_id AND a.day=DATE('now','+8 hours')), 0) AS today_active,
                   COALESCE((SELECT COUNT(*) FROM raffles r
                     WHERE r.chat_id=g.chat_id AND r.status='active'), 0) AS active_raffles
                   FROM groups g ORDER BY g.last_seen_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def group_daily(self, chat_id: int, days: int = 30) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT d.*,
                   COALESCE((SELECT COUNT(*) FROM group_activity_users a
                     WHERE a.chat_id=d.chat_id AND a.day=d.day), 0) AS active_users
                   FROM group_daily_stats d WHERE d.chat_id=?
                   ORDER BY d.day DESC LIMIT ?""",
                (chat_id, days),
            ).fetchall()

    def group_stats(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT g.*,
                   COALESCE((SELECT messages FROM group_daily_stats d
                     WHERE d.chat_id=g.chat_id AND d.day=DATE('now','+8 hours')), 0) AS today_messages,
                   COALESCE((SELECT COUNT(*) FROM group_activity_users a
                     WHERE a.chat_id=g.chat_id AND a.day=DATE('now','+8 hours')), 0) AS today_active,
                   COALESCE((SELECT joins FROM group_daily_stats d
                     WHERE d.chat_id=g.chat_id AND d.day=DATE('now','+8 hours')), 0) AS today_joins,
                   COALESCE((SELECT leaves FROM group_daily_stats d
                     WHERE d.chat_id=g.chat_id AND d.day=DATE('now','+8 hours')), 0) AS today_leaves,
                   COALESCE((SELECT blocked FROM group_daily_stats d
                     WHERE d.chat_id=g.chat_id AND d.day=DATE('now','+8 hours')), 0) AS today_blocked
                   FROM groups g WHERE g.chat_id=?""",
                (chat_id,),
            ).fetchone()

    def count_group_speakers(self, chat_id: int, window_days: int = 31) -> int:
        window_days = max(1, min(window_days, 31))
        start_modifier = f"-{window_days - 1} days"
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT user_id)
                   FROM group_activity_users
                   WHERE chat_id=? AND day>=DATE('now','+8 hours',?)""",
                (chat_id, start_modifier),
            ).fetchone()
        return int(row[0])

    def group_speaker_stats(
        self, chat_id: int, limit: int | None = None, offset: int = 0,
        window_days: int = 31,
    ) -> list[sqlite3.Row]:
        window_days = max(1, min(window_days, 31))
        start_modifier = f"-{window_days - 1} days"
        limit_sql = ""
        args: list[object] = [chat_id, start_modifier]
        if limit is not None:
            limit_sql = " LIMIT ? OFFSET ?"
            args.extend((max(1, min(limit, 1000)), max(0, offset)))
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT a.user_id,
                          COALESCE(NULLIF(MAX(a.username), ''), u.username, '') AS username,
                          COALESCE(
                            NULLIF(MAX(a.display_name), ''),
                            NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''),
                            CAST(a.user_id AS TEXT)
                          ) AS display_name,
                          SUM(CASE WHEN a.day=DATE('now','+8 hours')
                                   THEN a.messages ELSE 0 END) AS today_messages,
                          SUM(CASE WHEN a.day>=DATE('now','+8 hours','-6 days')
                                   THEN a.messages ELSE 0 END) AS week_messages,
                          SUM(a.messages) AS month_messages,
                          MAX(a.last_seen_at) AS last_seen_at
                   FROM group_activity_users a
                   LEFT JOIN users u ON u.user_id=a.user_id
                   WHERE a.chat_id=? AND a.day>=DATE('now','+8 hours',?)
                   GROUP BY a.user_id
                   ORDER BY month_messages DESC, week_messages DESC,
                            today_messages DESC, last_seen_at DESC, a.user_id{limit_sql}""",
                args,
            ).fetchall()

    def group_speaker_ranking(
        self, chat_id: int, window_days: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        window_days = max(1, min(window_days, 31))
        start_modifier = f"-{window_days - 1} days"
        with self.connect() as conn:
            return conn.execute(
                """SELECT a.user_id,
                          COALESCE(NULLIF(MAX(a.username), ''), u.username, '') AS username,
                          COALESCE(
                            NULLIF(MAX(a.display_name), ''),
                            NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''),
                            CAST(a.user_id AS TEXT)
                          ) AS display_name,
                          SUM(a.messages) AS period_messages,
                          MAX(a.last_seen_at) AS last_seen_at
                   FROM group_activity_users a
                   LEFT JOIN users u ON u.user_id=a.user_id
                   WHERE a.chat_id=? AND a.day>=DATE('now','+8 hours',?)
                   GROUP BY a.user_id
                   ORDER BY period_messages DESC, last_seen_at DESC, a.user_id
                   LIMIT ? OFFSET ?""",
                (
                    chat_id, start_modifier, max(1, min(limit, 1000)), max(0, offset),
                ),
            ).fetchall()

    def group_top_speakers(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self.group_speaker_stats(chat_id, limit)

    def update_group_member_identity(
        self, chat_id: int, user_id: int, username: str, display_name: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE group_activity_users SET username=?, display_name=?
                   WHERE chat_id=? AND user_id=?""",
                (username[:80], display_name[:160], chat_id, user_id),
            )
            conn.execute(
                """UPDATE point_accounts SET username=?, display_name=?
                   WHERE chat_id=? AND user_id=?""",
                (username[:80], display_name[:160], chat_id, user_id),
            )

    def add_group_violation(
        self, chat_id: int, user_id: int, username: str,
        display_name: str, reason: str,
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO group_violations
                   (chat_id, user_id, username, display_name, violations, last_reason)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     violations=group_violations.violations+1,
                     last_reason=excluded.last_reason,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    chat_id, user_id, username[:80], display_name[:160], reason[:200],
                ),
            )
            row = conn.execute(
                """SELECT violations FROM group_violations
                   WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()
        return int(row["violations"])

    def add_moderation_keyword(self, keyword: str, added_by: int = 0) -> int:
        keyword = " ".join(keyword.strip().split()).casefold()
        if not keyword:
            raise ValueError("违规关键词不能为空")
        if len(keyword) > 100:
            raise ValueError("违规关键词最多100个字符")
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO moderation_keywords (keyword, added_by) VALUES (?, ?)",
                    (keyword, added_by),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该违规关键词已存在") from exc
            return int(cursor.lastrowid)

    def remove_moderation_keyword(self, keyword_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM moderation_keywords WHERE id=?", (keyword_id,)
            )
            return cursor.rowcount > 0

    def remove_moderation_keyword_value(self, keyword: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM moderation_keywords WHERE keyword=? COLLATE NOCASE",
                (" ".join(keyword.strip().split()).casefold(),),
            )
            return cursor.rowcount > 0

    def list_moderation_keywords(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM moderation_keywords ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()

    def moderation_keyword_values(self) -> tuple[str, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT keyword FROM moderation_keywords ORDER BY LENGTH(keyword) DESC, id"
            ).fetchall()
        return tuple(str(row["keyword"]) for row in rows)

    def record_search(
        self, query: str, user_id: int, username: str, display_name: str,
        chat_id: int, chat_type: str, source: str, result_count: int,
    ) -> None:
        query = " ".join(query.strip().split())[:200]
        if not query:
            return
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO search_events
                   (query, normalized_query, user_id, username, display_name,
                    chat_id, chat_type, source, result_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    query, query.casefold(), user_id, username[:80],
                    display_name[:160], chat_id, chat_type[:30], source[:40],
                    max(0, result_count),
                ),
            )

    def count_search_keywords(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(DISTINCT normalized_query) FROM search_events"
            ).fetchone()[0])

    def search_keyword_rankings(
        self, limit: int = 10, offset: int = 0
    ) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT normalized_query, MAX(query) AS query,
                          COUNT(*) AS searches,
                          COUNT(DISTINCT user_id) AS unique_users,
                          MAX(created_at) AS last_searched_at
                   FROM search_events
                   GROUP BY normalized_query
                   ORDER BY searches DESC, last_searched_at DESC, normalized_query
                   LIMIT ? OFFSET ?""",
                (max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
            results: list[dict[str, object]] = []
            for row in rows:
                searchers = conn.execute(
                    """SELECT user_id, MAX(username) AS username,
                              MAX(display_name) AS display_name,
                              COUNT(*) AS searches
                       FROM search_events WHERE normalized_query=?
                       GROUP BY user_id
                       ORDER BY searches DESC, MAX(id) DESC""",
                    (row["normalized_query"],),
                ).fetchall()
                item = dict(row)
                item["searchers"] = searchers
                results.append(item)
            return results

    def count_search_events(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM search_events").fetchone()[0])

    def list_search_events(self, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM search_events
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def create_raffle(
        self, chat_id: int, creator_id: int, prize: str, winner_count: int,
        ends_at: str, raffle_type: str = "universal", activity_start_at: str = "",
        activity_min_messages: int = 0, *,
        title: str = "", rules_json: str = "[]", conditions_json: str = "{}",
        how_to_join: str = "", join_keyword: str = "", channel_ref: str = "",
        min_messages: int = 0, min_boosts: int = 0, recur_daily: int = 0,
        stats_start_mode: str = "immediate", stats_start_at: str = "",
        template_json: str = "", min_participants: int = 0,
    ) -> int:
        if raffle_type not in {"universal", "activity_random", "activity_rank"}:
            raise ValueError("未知抽奖类型")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raffle_id = self._smallest_available_id(conn, "raffles")
            conn.execute(
                """INSERT INTO raffles
                   (id, chat_id, creator_id, prize, winner_count, ends_at, raffle_type,
                    activity_start_at, activity_min_messages, title, rules_json,
                    conditions_json, how_to_join, join_keyword, channel_ref,
                    min_messages, min_boosts, recur_daily, stats_start_mode,
                    stats_start_at, template_json, min_participants)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    raffle_id, chat_id, creator_id, prize[:500], winner_count, ends_at,
                    raffle_type, activity_start_at, max(0, activity_min_messages),
                    str(title or "")[:200], str(rules_json or "[]")[:4000],
                    str(conditions_json or "{}")[:2000],
                    str(how_to_join or "")[:2000], str(join_keyword or "")[:80],
                    str(channel_ref or "")[:120], max(0, int(min_messages or 0)),
                    max(0, int(min_boosts or 0)), 1 if int(recur_daily or 0) else 0,
                    str(stats_start_mode or "immediate")[:20],
                    str(stats_start_at or "")[:40],
                    str(template_json or "")[:8000],
                    max(0, int(min_participants or 0)),
                ),
            )
            return raffle_id

    def set_raffle_message(self, raffle_id: int, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE raffles SET message_id=? WHERE id=?", (message_id, raffle_id))

    def update_raffle(
        self, raffle_id: int, chat_id: int, prize: str, winner_count: int,
        ends_at: str, *,
        title: str = "", rules_json: str = "[]", conditions_json: str = "{}",
        how_to_join: str = "", join_keyword: str = "", channel_ref: str = "",
        min_messages: int = 0, min_boosts: int = 0, recur_daily: int = 0,
        stats_start_mode: str = "immediate", stats_start_at: str = "",
        template_json: str = "", min_participants: int = 0,
    ) -> bool:
        """Update an active raffle owned by chat_id; entries/winners untouched."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id FROM raffles
                   WHERE id=? AND chat_id=? AND status='active'""",
                (raffle_id, chat_id),
            ).fetchone()
            if not row:
                return False
            cursor = conn.execute(
                """UPDATE raffles SET
                    prize=?, winner_count=?, ends_at=?,
                    title=?, rules_json=?, conditions_json=?, how_to_join=?,
                    join_keyword=?, channel_ref=?, min_messages=?, min_boosts=?,
                    recur_daily=?, stats_start_mode=?, stats_start_at=?,
                    template_json=?, min_participants=?
                   WHERE id=? AND chat_id=? AND status='active'""",
                (
                    prize[:500], winner_count, ends_at,
                    str(title or "")[:200], str(rules_json or "[]")[:4000],
                    str(conditions_json or "{}")[:2000],
                    str(how_to_join or "")[:2000], str(join_keyword or "")[:80],
                    str(channel_ref or "")[:120], max(0, int(min_messages or 0)),
                    max(0, int(min_boosts or 0)), 1 if int(recur_daily or 0) else 0,
                    str(stats_start_mode or "immediate")[:20],
                    str(stats_start_at or "")[:40],
                    str(template_json or "")[:8000],
                    max(0, int(min_participants or 0)),
                    raffle_id, chat_id,
                ),
            )
            return cursor.rowcount > 0

    def get_raffle(self, raffle_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT r.*, COUNT(e.user_id) AS entries FROM raffles r
                   LEFT JOIN raffle_entries e ON e.raffle_id=r.id
                   WHERE r.id=? GROUP BY r.id""",
                (raffle_id,),
            ).fetchone()

    def join_raffle(
        self, raffle_id: int, user_id: int, username: str, display_name: str,
        via_keyword: bool = False,
    ) -> tuple[bool, int]:
        with self.connect() as conn:
            raffle = conn.execute(
                "SELECT status, ends_at FROM raffles WHERE id=?", (raffle_id,)
            ).fetchone()
            if not raffle or raffle["status"] != "active" or raffle["ends_at"] <= conn.execute(
                "SELECT CURRENT_TIMESTAMP"
            ).fetchone()[0]:
                return False, 0
            try:
                conn.execute(
                    """INSERT INTO raffle_entries
                       (raffle_id, user_id, username, display_name, via_keyword)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        raffle_id, user_id, username[:80], display_name[:160],
                        1 if via_keyword else 0,
                    ),
                )
                joined = True
            except sqlite3.IntegrityError:
                joined = False
                if via_keyword:
                    conn.execute(
                        """UPDATE raffle_entries SET via_keyword=1
                           WHERE raffle_id=? AND user_id=?""",
                        (raffle_id, user_id),
                    )
            count = int(
                conn.execute("SELECT COUNT(*) FROM raffle_entries WHERE raffle_id=?", (raffle_id,)).fetchone()[0]
            )
            return joined, count

    def raffle_entries(self, raffle_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raffle_entries WHERE raffle_id=? ORDER BY joined_at, user_id", (raffle_id,)
            ).fetchall()

    def active_raffle_candidates(self, raffle_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            raffle = conn.execute(
                """SELECT chat_id, raffle_type, activity_start_at, ends_at,
                          activity_min_messages
                   FROM raffles WHERE id=?""",
                (raffle_id,),
            ).fetchone()
            if not raffle or raffle["raffle_type"] not in {
                "activity_random", "activity_rank",
            }:
                return []
            minimum = (
                max(1, int(raffle["activity_min_messages"]))
                if raffle["raffle_type"] == "activity_random" else 1
            )
            return conn.execute(
                """SELECT user_id, MAX(username) AS username,
                          MAX(display_name) AS display_name, COUNT(*) AS messages
                   FROM group_message_events
                   WHERE chat_id=? AND created_at>=? AND created_at<=?
                   GROUP BY user_id HAVING COUNT(*)>=?
                   ORDER BY messages DESC, MAX(created_at), user_id""",
                (
                    int(raffle["chat_id"]), str(raffle["activity_start_at"]),
                    str(raffle["ends_at"]), minimum,
                ),
            ).fetchall()

    def sync_raffle_entries(self, raffle_id: int, rows: list[sqlite3.Row]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO raffle_entries
                   (raffle_id, user_id, username, display_name)
                   VALUES (?, ?, ?, ?)""",
                (
                    (
                        raffle_id, int(row["user_id"]), str(row["username"] or "")[:80],
                        str(row["display_name"] or "")[:160],
                    )
                    for row in rows
                ),
            )

    @staticmethod
    def _raffle_field_str(raffle, key: str, default: str = "") -> str:
        try:
            keys = raffle.keys()
        except Exception:
            keys = ()
        if key not in keys:
            return default
        try:
            return str(raffle[key] or default)
        except (KeyError, IndexError, TypeError):
            return default

    @staticmethod
    def _parse_utc_text(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        raw = raw.replace("T", " ").replace("Z", "")
        if "." in raw:
            raw = raw.split(".", 1)[0]
        raw = raw[:19]
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _raffle_stats_start(raffle: sqlite3.Row) -> str:
        for key in ("activity_start_at", "stats_start_at"):
            try:
                value = str(raffle[key] or "").strip()
            except (KeyError, IndexError):
                value = ""
            if value:
                return value
        try:
            return str(raffle["created_at"] or "")
        except (KeyError, IndexError):
            return ""

    @classmethod
    def day_stats_start_utc(
        cls,
        ends_at: str,
        stats_start_mode: str = "immediate",
        stats_start_at: str = "",
        created_at: str = "",
    ) -> str:
        """Beijing draw-day stats start as UTC ``YYYY-MM-DD HH:MM:SS``.

        For ``immediate``: draw_day 00:00:00 (max with created_at if later same day).
        For ``datetime``: apply HH:MM:SS from ``stats_start_at`` (Beijing wall) to draw_day.
        """
        end_dt = cls._parse_utc_text(ends_at)
        if end_dt is None:
            return str(stats_start_at or created_at or "").strip()
        draw_day = end_dt.astimezone(BEIJING_TZ).date()
        mode = str(stats_start_mode or "immediate").strip().lower()
        if mode == "datetime":
            clock = (0, 0, 0)
            stats_dt = cls._parse_utc_text(stats_start_at)
            if stats_dt is not None:
                bj = stats_dt.astimezone(BEIJING_TZ)
                clock = (bj.hour, bj.minute, bj.second)
            day_start_bj = datetime(
                draw_day.year, draw_day.month, draw_day.day,
                clock[0], clock[1], clock[2], tzinfo=BEIJING_TZ,
            )
        else:
            day_start_bj = datetime(
                draw_day.year, draw_day.month, draw_day.day,
                0, 0, 0, tzinfo=BEIJING_TZ,
            )
            created_dt = cls._parse_utc_text(created_at)
            if created_dt is not None:
                created_bj = created_dt.astimezone(BEIJING_TZ)
                if created_bj.date() == draw_day and created_bj > day_start_bj:
                    day_start_bj = created_bj
        return day_start_bj.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _raffle_message_window(cls, raffle: sqlite3.Row) -> tuple[str, str]:
        """Return ``(start_utc_text, end_utc_text)`` for speech-to-qualify counts.

        Recurring daily raffles only count messages on the Beijing draw day of
        ``ends_at``, from that day's configured stats start clock. Non-recur
        raffles keep the stored absolute ``stats_start_at``..``ends_at`` window.
        """
        ends_at = cls._raffle_field_str(raffle, "ends_at", "")
        try:
            recur = int(cls._raffle_field_str(raffle, "recur_daily", "0") or 0)
        except (TypeError, ValueError):
            recur = 0
        if not recur:
            return cls._raffle_stats_start(raffle), ends_at
        mode = cls._raffle_field_str(raffle, "stats_start_mode", "immediate") or "immediate"
        start = cls.day_stats_start_utc(
            ends_at,
            stats_start_mode=mode,
            stats_start_at=cls._raffle_field_str(raffle, "stats_start_at", ""),
            created_at=cls._raffle_field_str(raffle, "created_at", ""),
        )
        return start, ends_at

    def _universal_auto_min_messages(self, raffle: sqlite3.Row) -> int:
        """Message threshold for auto-join (no click) on universal/sample raffles."""
        if str(raffle["raffle_type"] or "") != "universal":
            return 0
        minimum = int(raffle["min_messages"] or 0) if "min_messages" in raffle.keys() else 0
        if minimum > 0:
            return minimum
        raw = str(raffle["conditions_json"] or "") if "conditions_json" in raffle.keys() else ""
        if not raw:
            return 0
        try:
            import json
            data = json.loads(raw)
        except Exception:
            return 0
        if not isinstance(data, dict):
            return 0
        return max(0, int(data.get("messages") or 0))

    def universal_message_candidates(self, raffle_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            raffle = conn.execute("SELECT * FROM raffles WHERE id=?", (raffle_id,)).fetchone()
            if not raffle:
                return []
            minimum = self._universal_auto_min_messages(raffle)
            if minimum <= 0:
                return []
            start, end = self._raffle_message_window(raffle)
            if not start:
                return []
            return conn.execute(
                """SELECT user_id, MAX(username) AS username,
                          MAX(display_name) AS display_name, COUNT(*) AS messages
                   FROM group_message_events
                   WHERE chat_id=? AND created_at>=? AND created_at<=?
                   GROUP BY user_id HAVING COUNT(*)>=?
                   ORDER BY messages DESC, MAX(created_at), user_id""",
                (
                    int(raffle["chat_id"]), start, end, minimum,
                ),
            ).fetchall()

    def qualify_activity_raffles(
        self, chat_id: int, user_id: int, username: str, display_name: str,
    ) -> list[sqlite3.Row]:
        qualified: list[sqlite3.Row] = []
        with self.connect() as conn:
            raffles = conn.execute(
                """SELECT * FROM raffles
                   WHERE chat_id=? AND status='active'
                     AND ends_at>CURRENT_TIMESTAMP
                     AND (
                       (
                         raffle_type IN ('activity_random','activity_rank')
                         AND activity_start_at<=CURRENT_TIMESTAMP
                       )
                       OR (raffle_type='universal' AND (
                         COALESCE(min_messages,0)>0
                         OR COALESCE(conditions_json,'') LIKE '%"messages"%'
                       ))
                     )
                   ORDER BY id""",
                (chat_id,),
            ).fetchall()
            for raffle in raffles:
                raffle_type = str(raffle["raffle_type"] or "")
                if raffle_type.startswith("activity_"):
                    start = str(raffle["activity_start_at"] or "")
                    if not start:
                        continue
                    minimum = (
                        max(1, int(raffle["activity_min_messages"]))
                        if raffle_type == "activity_random" else 1
                    )
                else:
                    minimum = self._universal_auto_min_messages(raffle)
                    if minimum <= 0:
                        continue
                    start, end = self._raffle_message_window(raffle)
                    if not start:
                        continue
                if raffle_type.startswith("activity_"):
                    end = str(raffle["ends_at"] or "")
                messages = int(conn.execute(
                    """SELECT COUNT(*) FROM group_message_events
                       WHERE chat_id=? AND user_id=?
                         AND created_at>=? AND created_at<=?""",
                    (
                        chat_id, user_id, start, end,
                    ),
                ).fetchone()[0])
                if messages < minimum:
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO raffle_entries
                       (raffle_id, user_id, username, display_name)
                       VALUES (?, ?, ?, ?)""",
                    (
                        int(raffle["id"]), user_id, username[:80],
                        display_name[:160],
                    ),
                )
                if not cursor.rowcount:
                    continue
                entries = int(conn.execute(
                    "SELECT COUNT(*) FROM raffle_entries WHERE raffle_id=?",
                    (int(raffle["id"]),),
                ).fetchone()[0])
                qualified.append(conn.execute(
                    """SELECT r.*, ? AS entries FROM raffles r WHERE r.id=?""",
                    (entries, int(raffle["id"])),
                ).fetchone())
        return qualified

    def postpone_raffle(self, raffle_id: int, new_ends_at: str) -> bool:
        """Move an active raffle's draw time; entries are kept untouched."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE raffles SET ends_at=? WHERE id=? AND status='active'",
                (str(new_ends_at)[:40], raffle_id),
            )
            return cursor.rowcount > 0

    def due_raffles(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM raffles WHERE status='active' AND ends_at<=CURRENT_TIMESTAMP
                   ORDER BY ends_at LIMIT ?""",
                (limit,),
            ).fetchall()

    def complete_raffle(
        self, raffle_id: int, winner_ids: list[int],
        winner_notes: dict[int, str] | None = None,
    ) -> bool:
        notes = winner_notes or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE raffles SET status='drawn', drawn_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='active'""",
                (raffle_id,),
            )
            if not cursor.rowcount:
                return False
            conn.executemany(
                """INSERT INTO raffle_winners (raffle_id, user_id, position, note)
                   VALUES (?, ?, ?, ?)""",
                (
                    (
                        raffle_id, user_id, position,
                        str(notes.get(int(user_id), "") or "")[:500],
                    )
                    for position, user_id in enumerate(winner_ids, 1)
                ),
            )
            return True

    def cancel_raffle(self, raffle_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE raffles SET status='cancelled' WHERE id=? AND status='active'", (raffle_id,)
            )
            return cursor.rowcount > 0

    def delete_raffle(self, chat_id: int, raffle_id: int) -> bool:
        with self.connect() as conn:
            raffle = conn.execute(
                "SELECT id FROM raffles WHERE chat_id=? AND id=?",
                (chat_id, raffle_id),
            ).fetchone()
            if not raffle:
                return False
            conn.execute("DELETE FROM raffle_winners WHERE raffle_id=?", (raffle_id,))
            conn.execute("DELETE FROM raffle_entries WHERE raffle_id=?", (raffle_id,))
            conn.execute(
                "DELETE FROM raffles WHERE chat_id=? AND id=?", (chat_id, raffle_id)
            )
            return True

    def expire_raffle(self, raffle_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE raffles SET ends_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'", (raffle_id,)
            )
            return cursor.rowcount > 0

    def raffle_count(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM raffles WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])

    def list_raffles(
        self, chat_id: int | None = None, limit: int = 100, offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if chat_id is not None:
                return conn.execute(
                    """SELECT r.*, g.title AS group_title, COUNT(e.user_id) AS entries
                       FROM raffles r LEFT JOIN groups g ON g.chat_id=r.chat_id
                       LEFT JOIN raffle_entries e ON e.raffle_id=r.id WHERE r.chat_id=?
                       GROUP BY r.id ORDER BY r.id DESC LIMIT ? OFFSET ?""",
                    (chat_id, limit, max(0, offset)),
                ).fetchall()
            return conn.execute(
                """SELECT r.*, g.title AS group_title, COUNT(e.user_id) AS entries
                   FROM raffles r LEFT JOIN groups g ON g.chat_id=r.chat_id
                   LEFT JOIN raffle_entries e ON e.raffle_id=r.id
                   GROUP BY r.id ORDER BY r.id DESC LIMIT ? OFFSET ?""",
                (limit, max(0, offset)),
            ).fetchall()

    def active_raffles(self, chat_id: int, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT r.*, COUNT(e.user_id) AS entries
                   FROM raffles r
                   LEFT JOIN raffle_entries e ON e.raffle_id=r.id
                   WHERE r.chat_id=? AND r.status='active'
                     AND r.ends_at>CURRENT_TIMESTAMP
                   GROUP BY r.id ORDER BY r.ends_at, r.id LIMIT ?""",
                (chat_id, max(1, min(limit, 100))),
            ).fetchall()

    def raffle_winners(self, raffle_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT w.*, e.username, e.display_name FROM raffle_winners w
                   JOIN raffle_entries e ON e.raffle_id=w.raffle_id AND e.user_id=w.user_id
                   WHERE w.raffle_id=? ORDER BY w.position""",
                (raffle_id,),
            ).fetchall()

    def raffle_history(self, chat_id: int, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT r.*, COUNT(DISTINCT e.user_id) AS entries,
                          GROUP_CONCAT(
                            CASE WHEN w.user_id IS NOT NULL THEN
                              COALESCE(NULLIF(e.display_name,''),NULLIF(e.username,''),CAST(e.user_id AS TEXT))
                            END, '、'
                          ) AS winner_names
                   FROM raffles r
                   LEFT JOIN raffle_entries e ON e.raffle_id=r.id
                   LEFT JOIN raffle_winners w
                     ON w.raffle_id=r.id AND w.user_id=e.user_id
                   WHERE r.chat_id=? AND r.status IN ('drawn','cancelled')
                   GROUP BY r.id ORDER BY r.id DESC LIMIT ? OFFSET ?""",
                (chat_id, max(1, min(limit, 20)), max(0, offset)),
            ).fetchall()

    def raffle_history_count(self, chat_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM raffles WHERE chat_id=? AND status IN ('drawn','cancelled')",
                (chat_id,),
            ).fetchone()[0])


    def active_raffles_by_keyword(self, chat_id: int, keyword: str) -> list[sqlite3.Row]:
        key = str(keyword or "").strip()
        if not key:
            return []
        with self.connect() as conn:
            return conn.execute(
                """SELECT r.*, COUNT(e.user_id) AS entries
                   FROM raffles r
                   LEFT JOIN raffle_entries e ON e.raffle_id=r.id
                   WHERE r.chat_id=? AND r.status='active'
                     AND r.ends_at>CURRENT_TIMESTAMP
                     AND r.join_keyword<>'' AND lower(r.join_keyword)=lower(?)
                   GROUP BY r.id ORDER BY r.ends_at, r.id""",
                (chat_id, key),
            ).fetchall()

    def count_user_messages_since(
        self, chat_id: int, user_id: int, since_at: str = "",
        until_at: str = "",
    ) -> int:
        clauses = ["chat_id=?", "user_id=?"]
        params: list = [chat_id, user_id]
        if since_at:
            clauses.append("created_at>=?")
            params.append(since_at)
        if until_at:
            clauses.append("created_at<=?")
            params.append(until_at)
        with self.connect() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM group_message_events WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()[0])

    def raffle_entry(self, raffle_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raffle_entries WHERE raffle_id=? AND user_id=?",
                (raffle_id, user_id),
            ).fetchone()

    def due_daily_recur_templates(self, limit: int = 20) -> list[sqlite3.Row]:
        """Drawn universal raffles marked recur_daily that still need next-day clone."""
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM raffles
                   WHERE status='drawn' AND recur_daily=1
                     AND raffle_type='universal'
                     AND COALESCE(template_json,'')<>''
                     AND drawn_at IS NOT NULL
                     AND drawn_at>=DATETIME('now','-2 days')
                   ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 50)),),
            ).fetchall()

    def mark_raffle_recur_spawned(self, raffle_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raffles SET recur_daily=0 WHERE id=?", (raffle_id,)
            )

    def sight_group_member_profile(
        self, chat_id: int, user_id: int, *,
        first_name: str = "", last_name: str = "",
        display_name: str = "", username: str = "",
    ) -> bool:
        """Record baseline or append rename history. Returns True if a change was logged."""
        first_name = str(first_name or "")[:80]
        last_name = str(last_name or "")[:80]
        display_name = str(display_name or "").strip()[:160]
        if not display_name:
            display_name = (f"{first_name} {last_name}").strip()[:160]
        username = str(username or "")[:80]
        with self.connect() as conn:
            row = conn.execute(
                """SELECT first_name, last_name, display_name, username
                   FROM group_member_profiles WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO group_member_profiles
                       (chat_id, user_id, first_name, last_name, display_name, username)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat_id, user_id, first_name, last_name, display_name, username),
                )
                return False
            old_first = str(row["first_name"] or "")
            old_last = str(row["last_name"] or "")
            old_display = str(row["display_name"] or "")
            changed = (
                old_first != first_name
                or old_last != last_name
                or old_display != display_name
            )
            if not changed:
                if username and username != str(row["username"] or ""):
                    conn.execute(
                        """UPDATE group_member_profiles
                           SET username=?, updated_at=CURRENT_TIMESTAMP
                           WHERE chat_id=? AND user_id=?""",
                        (username, chat_id, user_id),
                    )
                return False
            conn.execute(
                """INSERT INTO group_name_history
                   (chat_id, user_id, old_first, old_last, old_display,
                    new_first, new_last, new_display)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, user_id, old_first, old_last, old_display,
                    first_name, last_name, display_name,
                ),
            )
            conn.execute(
                """UPDATE group_member_profiles
                   SET first_name=?, last_name=?, display_name=?, username=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND user_id=?""",
                (first_name, last_name, display_name, username, chat_id, user_id),
            )
            return True

    def user_has_name_change_history(
        self, chat_id: int, user_id: int,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        def _check(c: sqlite3.Connection) -> bool:
            row = c.execute(
                """SELECT 1 FROM group_name_history
                   WHERE chat_id=? AND user_id=? LIMIT 1""",
                (chat_id, user_id),
            ).fetchone()
            return row is not None

        if conn is not None:
            return _check(conn)
        with self.connect() as own:
            return _check(own)

    def user_has_name_change_on_beijing_day(
        self, chat_id: int, user_id: int, day: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """True if any rename happened on the given Beijing calendar day (UTC+8)."""
        from datetime import datetime, timezone, timedelta
        beijing = timezone(timedelta(hours=8))
        if not day:
            day = datetime.now(beijing).strftime("%Y-%m-%d")
        # changed_at stored as UTC CURRENT_TIMESTAMP / ISO; compare Beijing date
        start_utc = datetime.fromisoformat(f"{day}T00:00:00+08:00").astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=1)
        start_s = start_utc.strftime("%Y-%m-%d %H:%M:%S")
        end_s = end_utc.strftime("%Y-%m-%d %H:%M:%S")

        def _check(c: sqlite3.Connection) -> bool:
            row = c.execute(
                """SELECT 1 FROM group_name_history
                   WHERE chat_id=? AND user_id=?
                     AND REPLACE(REPLACE(changed_at,'T',' '),'Z','') >= ?
                     AND REPLACE(REPLACE(changed_at,'T',' '),'Z','') < ?
                   LIMIT 1""",
                (chat_id, user_id, start_s, end_s),
            ).fetchone()
            return row is not None

        if conn is not None:
            return _check(conn)
        with self.connect() as own:
            return _check(own)

    def group_name_history(
        self, chat_id: int, user_id: int, limit: int = 50, offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_name_history
                   WHERE chat_id=? AND user_id=?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (chat_id, user_id, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()

    def count_group_name_history(self, chat_id: int, user_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM group_name_history
                   WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()[0])

    def get_group_member_profile(
        self, chat_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM group_member_profiles
                   WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()

    def heartbeat(self, service: str, detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runtime_status (service, detail) VALUES (?, ?)
                   ON CONFLICT(service) DO UPDATE SET
                     last_seen_at=CURRENT_TIMESTAMP, detail=excluded.detail""",
                (service[:40], detail[:500]),
            )

    def runtime_status(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT service, last_seen_at, detail,
                   CAST((JULIANDAY('now')-JULIANDAY(last_seen_at))*86400 AS INTEGER) AS age_seconds
                   FROM runtime_status ORDER BY service"""
            ).fetchall()

    def monitoring_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "groups": int(conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]),
                "active_raffles": int(conn.execute("SELECT COUNT(*) FROM raffles WHERE status='active'").fetchone()[0]),
                "today_messages": int(conn.execute(
                    "SELECT COALESCE(SUM(messages),0) FROM group_daily_stats WHERE day=DATE('now','+8 hours')"
                ).fetchone()[0]),
                "today_active": int(conn.execute(
                    "SELECT COUNT(*) FROM group_activity_users WHERE day=DATE('now','+8 hours')"
                ).fetchone()[0]),
                "today_blocked": int(conn.execute(
                    "SELECT COALESCE(SUM(blocked),0) FROM group_daily_stats WHERE day=DATE('now','+8 hours')"
                ).fetchone()[0]),
                "failed_outbox": int(conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE status='failed'"
                ).fetchone()[0]),
                "failed_chain_24h": int(conn.execute(
                    "SELECT COUNT(*) FROM chain_queries WHERE success=0 AND created_at>=DATETIME('now','-1 day')"
                ).fetchone()[0]),
            }

    def dashboard_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "users": int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
                "pending": int(conn.execute("SELECT COUNT(*) FROM entries WHERE status = 'pending'").fetchone()[0]),
                "approved": int(conn.execute("SELECT COUNT(*) FROM entries WHERE status = 'approved'").fetchone()[0]),
                "open_reports": int(conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'open'").fetchone()[0]),
                "unread_support": int(conn.execute("SELECT COUNT(*) FROM support_messages WHERE direction = 'incoming' AND status = 'open'").fetchone()[0]),
                "queued_messages": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]),
                "groups": int(conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]),
                "active_raffles": int(conn.execute("SELECT COUNT(*) FROM raffles WHERE status = 'active'").fetchone()[0]),
            }

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM entries GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _entry(row: sqlite3.Row) -> Entry:
        return Entry(
            id=int(row["id"]), url=str(row["url"]), title=str(row["title"]), category=str(row["category"]),
            description=str(row["description"]), status=str(row["status"]), user_id=int(row["user_id"]),
            username=str(row["username"]), reason=str(row["reason"]), created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]), reports_count=int(row["reports_count"]),
            content_text=str(row["content_text"]), media_file_id=str(row["media_file_id"]),
            media_type=str(row["media_type"]), media_name=str(row["media_name"]),
            source_chat_id=int(row["source_chat_id"]),
            source_message_id=int(row["source_message_id"]),
            **DirectoryStore._entry_rich_fields(row),
        )

    @staticmethod
    def _entry_rich_fields(row: sqlite3.Row) -> dict:
        keys = set(row.keys())
        def value(name, default):
            return row[name] if name in keys and row[name] is not None else default
        return {
            "entities_json": str(value("entities_json", "[]") or "[]"),
            "buttons_json": str(value("buttons_json", "[]") or "[]"),
            "copy_chat_id": int(value("copy_chat_id", 0) or 0),
            "copy_message_id": int(value("copy_message_id", 0) or 0),
            "keyword_key": str(value("keyword_key", "") or ""),
        }
