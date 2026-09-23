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
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip(),
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
        message_auto_delete_seconds=message_auto_delete_seconds,
        is_clone=is_clone,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
    )
