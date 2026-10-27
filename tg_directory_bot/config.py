from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

DEFAULT_CATEGORIES = ("tools", "docs", "community", "market", "service", "other")
DEFAULT_BLOCKED_KEYWORDS = ("赌博", "博彩", "菠菜", "赌场", "彩票", "casino", "betting", "sportsbook")


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    super_admin_ids: set[int]
    db_path: Path
    categories: tuple[str, ...]
    blocked_keywords: tuple[str, ...]
    developer_ids: set[int] = field(default_factory=set)
    auto_approve: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""
    backups_dir: Path = Path("backups")
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_api_key: str = ""
    trongrid_api_keys: tuple[str, ...] = ()
    tron_max_qps: float = 0.0
    tronscan_max_qps: float = 3.0
    tron_cache_seconds: float = 5.0
    tron_fallback_nodes: tuple[str, ...] | None = None
    tron_block_scan: bool = True
    tron_draw_stats: bool = True
    tron_draw_confirmations: int = 0
    tron_draw_block_offset: int = 0
    tron_draw_initial_backfill: int = 500
    tron_draw_max_backfill: int = 20000
    tron_draw_backfill_per_step: int = 3
    # AI reference (OpenAI-compatible relay). The key stays server side: it is never
    # part of repr() / API output / logs. Feature is off while ai_api_key is empty.
    ai_api_key: str = field(default="", repr=False)
    ai_base_url: str = "https://api.mixroute.ai/v1"
    ai_model: str = "gpt-4.1-mini"
    tron_ai_predict: bool = True
    ai_timeout_seconds: float = 20.0
    tron_ai_max_per_hour: int = 120

    @property
    def tron_ai_enabled(self) -> bool:
        return bool(self.ai_api_key.strip()) and self.tron_ai_predict
    tron_scan_lag_blocks: int = 1
    tron_scan_max_catchup_blocks: int = 1200
    tron_reconcile_seconds: int = 300
    tron_fallback_poll_seconds: int = 30
    tron_reconcile_qps: float = 0.5
    tron_fallback_qps: float = 2.0
    tronscan_api_url: str = "https://apilist.tronscanapi.com"
    tronscan_api_key: str = ""
    oklink_api_key: str = ""
    tokenview_api_key: str = ""
    coingecko_url: str = "https://api.coingecko.com/api/v3"
    coingecko_api_key: str = ""
    okx_p2p_url: str = "https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin"
    cwl_lottery_url: str = "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
    sporttery_lottery_url: str = "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
    lottery_realtime_url: str = "https://api.api16868.com"
    lottery_public_data_base_url: str = (
        "https://raw.githubusercontent.com/wenjinliuu/lottery-data-repo/main/public_data"
    )
    lottery_poll_seconds: int = 60
    message_auto_delete_seconds: int = 180
    is_clone: bool = False
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    clone_id: int = 0
    mother_db_path: str = ""
    mother_bot_id: int = 0
    clone_cipher_key: str = ""
    storage_quota_bytes: int = 0
    price_alert_max_per_chat: int = 50
    price_alert_poll_seconds: int = 30
    support_relay_delete_seconds: int = 169200


def _csv(value: str | None, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not value:
        return fallback
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _admin_ids(value: str | None) -> set[int]:
    ids: set[int] = set()
    for raw in _csv(value):
        try:
            ids.add(int(raw))
        except ValueError as exc:
            raise ValueError(f"ADMIN_IDS contains a non-numeric id: {raw}") from exc
    return ids


def _first_admin_id(value: str | None) -> int | None:
    for raw in _csv(value):
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _float_env(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return float(default)
    try:
        return max(float(minimum), float(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def load_config(env_file: str | Path | None = ".env", require_bot_token: bool = True) -> Config:
    if load_dotenv and env_file:
        load_dotenv(env_file)
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_bot_token and not token:
        raise RuntimeError("BOT_TOKEN is required. Create .env from .env.example first.")
    try:
        web_port = int(os.getenv("WEB_PORT", "8080"))
    except ValueError as exc:
        raise ValueError("WEB_PORT must be a number") from exc
    try:
        lottery_poll_seconds = max(60, int(os.getenv("LOTTERY_POLL_SECONDS", "60")))
    except ValueError as exc:
        raise ValueError("LOTTERY_POLL_SECONDS must be a number") from exc
    try:
        message_auto_delete_seconds = max(0, int(os.getenv("MESSAGE_AUTO_DELETE_SECONDS", "180")))
    except ValueError as exc:
        raise ValueError("MESSAGE_AUTO_DELETE_SECONDS must be a number") from exc
    try:
        telegram_api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be a number") from exc
    tron_keys = tuple(dict.fromkeys(
        item for item in (
            *_csv((os.getenv("TRONGRID_API_KEYS") or "").replace(";", ",")),
            os.getenv("TRONGRID_API_KEY", "").strip(),
        ) if item
    ))
    categories = _csv(os.getenv("CATEGORIES"), DEFAULT_CATEGORIES)
    blocked = _csv(os.getenv("BLOCKED_KEYWORDS"), DEFAULT_BLOCKED_KEYWORDS)
    raw_admin_ids = os.getenv("ADMIN_IDS")
    admin_ids = _admin_ids(raw_admin_ids)
    legacy_super_ids = _admin_ids(os.getenv("SUPER_ADMIN_IDS"))
    is_clone = os.getenv("IS_CLONE", "0").strip().casefold() in {"1", "true", "yes", "on"}
    developer_ids = _admin_ids(os.getenv("DEVELOPER_IDS"))
    if not developer_ids and not is_clone:
        developer_ids = set(legacy_super_ids)
        if not developer_ids:
            first_admin = _first_admin_id(raw_admin_ids)
            developer_ids = {first_admin} if first_admin is not None else set()
    super_admin_ids = (admin_ids | legacy_super_ids) - developer_ids
    try:
        clone_id = int(os.getenv("CLONE_ID", "0") or "0")
        mother_bot_id = int(os.getenv("MOTHER_BOT_ID", "0") or "0")
        storage_quota_bytes = max(0, int(os.getenv("STORAGE_QUOTA_BYTES", "0") or "0"))
    except ValueError as exc:
        raise ValueError("CLONE_ID / MOTHER_BOT_ID / STORAGE_QUOTA_BYTES must be numbers") from exc
    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        super_admin_ids=super_admin_ids,
        developer_ids=developer_ids,
        db_path=Path(os.getenv("DB_PATH", "data/directory.sqlite3")),
        categories=tuple(item.casefold() for item in categories),
        blocked_keywords=tuple(item.casefold() for item in blocked),
        auto_approve=os.getenv("AUTO_APPROVE", "0").strip().casefold() in {"1", "true", "yes", "on"},
        web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
        web_port=web_port,
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip(),
        admin_password=os.getenv("ADMIN_PASSWORD", "").strip(),
        session_secret=os.getenv("SESSION_SECRET", "").strip(),
        backups_dir=Path(os.getenv("BACKUPS_DIR", "backups")),
        trongrid_url=os.getenv("TRONGRID_URL", "https://api.trongrid.io").strip().rstrip("/"),
        trongrid_api_key=(tron_keys[0] if tron_keys else ""),
        trongrid_api_keys=tron_keys,
        tron_max_qps=_float_env("TRON_MAX_QPS", 0.0, 0.0),
        tronscan_max_qps=_float_env("TRONSCAN_MAX_QPS", 3.0, 0.2),
        tron_cache_seconds=_float_env("TRON_CACHE_SECONDS", 5.0, 0.0),
        tron_fallback_nodes=(
            None if not (os.getenv("TRON_FALLBACK_NODES") or "").strip()
            else tuple(
                item.rstrip("/") for item in _csv(os.getenv("TRON_FALLBACK_NODES"))
                if item.casefold() not in {"0", "off", "none", "false"}
            )
        ),
        tron_block_scan=os.getenv("TRON_BLOCK_SCAN", "1").strip().casefold()
        not in {"0", "false", "no", "off"},
        tron_draw_stats=os.getenv("TRON_DRAW_STATS", "1").strip().casefold()
        not in {"0", "false", "no", "off"},
        tron_draw_confirmations=int(_float_env("TRON_DRAW_CONFIRMATIONS", 0, 0)),
        tron_draw_block_offset=int(_float_env("TRON_DRAW_BLOCK_OFFSET", 0, 0)) % 20,
        tron_draw_initial_backfill=int(_float_env("TRON_DRAW_INITIAL_BACKFILL", 500, 1)),
        tron_draw_max_backfill=int(_float_env("TRON_DRAW_MAX_BACKFILL", 20000, 1)),
        tron_draw_backfill_per_step=int(_float_env("TRON_DRAW_BACKFILL_PER_STEP", 3, 1)),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=(os.getenv("AI_BASE_URL", "").strip() or "https://api.mixroute.ai/v1").rstrip("/"),
        ai_model=os.getenv("AI_MODEL", "").strip() or "gpt-4.1-mini",
        tron_ai_predict=os.getenv("TRON_AI_PREDICT", "1").strip().casefold()
        not in {"0", "false", "no", "off"},
        ai_timeout_seconds=_float_env("AI_TIMEOUT_SECONDS", 20, 3),
        tron_ai_max_per_hour=int(_float_env("TRON_AI_MAX_PER_HOUR", 120, 1)),
        tron_scan_lag_blocks=int(_float_env("TRON_SCAN_LAG_BLOCKS", 1, 0)),
        tron_scan_max_catchup_blocks=int(_float_env("TRON_SCAN_MAX_CATCHUP_BLOCKS", 1200, 20)),
        tron_reconcile_seconds=int(_float_env("TRON_RECONCILE_SECONDS", 300, 30)),
        tron_fallback_poll_seconds=int(_float_env("TRON_FALLBACK_POLL_SECONDS", 30, 3)),
        tron_reconcile_qps=_float_env("TRON_RECONCILE_QPS", 0.5, 0.01),
        tron_fallback_qps=_float_env("TRON_FALLBACK_QPS", 2.0, 0.01),
        tronscan_api_url=os.getenv(
            "TRONSCAN_API_URL", "https://apilist.tronscanapi.com"
        ).strip().rstrip("/"),
        tronscan_api_key=os.getenv("TRONSCAN_API_KEY", "").strip(),
        oklink_api_key=os.getenv("OKLINK_API_KEY", "").strip(),
        tokenview_api_key=os.getenv("TOKENVIEW_API_KEY", "").strip(),
        coingecko_url=os.getenv("COINGECKO_URL", "https://api.coingecko.com/api/v3").strip().rstrip("/"),
        coingecko_api_key=os.getenv("COINGECKO_API_KEY", "").strip(),
        okx_p2p_url=os.getenv(
            "OKX_P2P_URL", "https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin"
        ).strip(),
        cwl_lottery_url=os.getenv(
            "CWL_LOTTERY_URL",
            "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice",
        ).strip(),
        sporttery_lottery_url=os.getenv(
            "SPORTTERY_LOTTERY_URL",
            "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry",
        ).strip(),
        lottery_realtime_url=os.getenv(
            "LOTTERY_REALTIME_URL", "https://api.api16868.com"
        ).strip().rstrip("/"),
        lottery_public_data_base_url=os.getenv(
            "LOTTERY_PUBLIC_DATA_BASE_URL",
            "https://raw.githubusercontent.com/wenjinliuu/lottery-data-repo/main/public_data",
        ).strip().rstrip("/"),
        lottery_poll_seconds=lottery_poll_seconds,
        price_alert_max_per_chat=int(_float_env("PRICE_ALERT_MAX_PER_CHAT", 50, 0)),
        price_alert_poll_seconds=int(_float_env("PRICE_ALERT_POLL_SECONDS", 30, 10)),
        # 双向客服消息自动撤回（秒），0 关闭。注意 Telegram 仅允许删除 48 小时内的消息
        support_relay_delete_seconds=int(_float_env("SUPPORT_RELAY_DELETE_SECONDS", 169200, 0)),
        message_auto_delete_seconds=message_auto_delete_seconds,
        is_clone=is_clone,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
        clone_id=clone_id if is_clone else 0,
        mother_db_path=os.getenv("MOTHER_DB_PATH", "").strip() if is_clone else "",
        mother_bot_id=mother_bot_id if is_clone else 0,
        clone_cipher_key=os.getenv("CLONE_CIPHER_KEY", "").strip(),
        storage_quota_bytes=storage_quota_bytes if is_clone else 0,
    )
