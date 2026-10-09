from __future__ import annotations

import asyncio
import time
import html
import json
import logging
import math
import re
import random
import secrets
import sqlite3
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from telegram import (
    BotCommand, ChatPermissions, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument, InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto, InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice, InlineQueryResultCachedSticker,
    InputMediaPhoto, InputMediaVideo,
    InputTextMessageContent, KeyboardButton, MessageEntity, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest as TelegramBadRequest, Forbidden, TelegramError
import httpx
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatBoostHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
)

from .auto_delete import AutoDeleteBot, advertisement_message, is_persistent_message, persistent_message
from .config import Config
from .clones import CloneManager
from .chain import (
    ChainQueryError, ChainService, MIN_DISPLAY_TRANSFER, MIN_HISTORY_TRANSFER,
    TronBalance, TronTransaction,
    validate_tron_address,
)
from .lottery import (
    LOTTERY_GAMES,
    MARK_SIX_CODES,
    LotteryResult,
    LotteryService,
    format_lottery_result,
    format_mark_six_numbers,
    is_valid_lottery_result,
    normalize_lottery_result,
    resolve_lottery_code,
    resolve_lottery_history_keyword,
)
from .storage import (
    EFFECTIVE_RULE_TEXT, DirectoryStore, Entry, format_points, is_effective_text,
    normalize_points,
)
from .rich_content import button_content, buttons_markup, capture_buttons, capture_buttons_resolving, capture_content, content_entities, forward_channel_source, send_content, validate_content
from . import crypto_alert, crypto_price, life_guide, raffle_fair, raffle_parse, settings_wizard, sticker_clone
from .tron_net import BUSY_MESSAGE, is_busy_error, strip_urls
from .tron_scanner import ScanDB, TronBlockScanner, row_transaction, shared_scan_db_path
from .time_utils import (
    beijing_now, beijing_now_text,
    format_beijing_time,
    format_beijing_timestamp,
    format_beijing_timestamp_ms,
    beijing_datetime_to_utc_text,
    utc_after_minutes_text,
)
from .validation import (
    Submission,
    normalize_url,
    parse_keyword_address_payload,
    parse_submission_payload,
)

URL, TITLE, CATEGORY, DESCRIPTION = range(4)

# run.py 用于母机器人和所有子机器人：成员变动、助推/取消助推都需要显式订阅。
ALLOWED_UPDATES = [
    "message", "callback_query", "inline_query", "chat_member",
    "chat_boost", "removed_chat_boost",
]

GROUP_PERMISSIONS = {
    "stats", "raffles", "lottery", "polls", "ads", "points", "welcome",
    "quickpost", "invite", "recent", "moderation", "diceodds", "renamehist",
}
GROUP_PERMISSION_LABELS = {
    "stats": "统计与活跃排行",
    "raffles": "抽奖",
    "lottery": "开奖订阅",
    "polls": "群投票",
    "ads": "广告",
    "points": "积分",
    "welcome": "欢迎与验证",
    "quickpost": "快捷发布",
    "invite": "邀请链接",
    "recent": "近期操作",
    "moderation": "删除禁言踢群",
    "diceodds": "骰子赔率",
    "renamehist": "改名记录",
}
GROUP_PERMISSION_ALIASES = {
    "统计": "stats", "统计与活跃排行": "stats", "活跃排行": "stats",
    "抽奖": "raffles", "开奖": "lottery", "开奖订阅": "lottery",
    "投票": "polls", "群投票": "polls",
    "广告": "ads", "积分": "points", "欢迎": "welcome",
    "欢迎与验证": "welcome", "进群欢迎": "welcome",
    "快捷发布": "quickpost", "邀请": "invite", "邀请链接": "invite",
    "近期操作": "recent", "管理处罚": "moderation",
    "删除禁言踢群": "moderation",
    "骰子赔率": "diceodds", "赔率": "diceodds",
    "改名记录": "renamehist", "改名历史": "renamehist", "renamehist": "renamehist",
}


def parse_group_permissions(value: str) -> set[str]:
    raw_items = [
        item.strip() for item in re.split(r"[,，、]", value) if item.strip()
    ]
    if any(item.casefold() in {"all", "全部", "所有"} for item in raw_items):
        return set(GROUP_PERMISSIONS)
    permissions = {
        GROUP_PERMISSION_ALIASES.get(item, item.casefold()) for item in raw_items
    }
    invalid = permissions - GROUP_PERMISSIONS
    if not permissions:
        raise ValueError("至少选择一项权限")
    if invalid:
        raise ValueError("未知权限：" + "、".join(sorted(invalid)))
    return permissions

HELP_TEXT = """📖 使用帮助

🏠 基础
• /start 打开分类主菜单；/help 查看帮助；/cancel 退出当前操作
• 操作页面3分钟无点击会自动撤回

🔎 搜索收录
• 发送关键词即可查看内容，如 XX 或 XX地址
• /search 关键词：模糊查找，最多列出10个
• 提交：关键词 搜录 内容（可附图片、视频、文件）
• 原样收录：回复一条消息，发送“关键词 搜录”
• 超级管理员审核后公开；同一关键词以最新一条为准
• /my：我的提交

⛓ 波场查询
• 发送 T 开头地址或 /balance 地址：余额、资源、授权
• 主菜单“💰 波场/地址监控”：交易记录、地址监控

⭐ 积分
• 签到、我的积分、积分排行、积分礼品、积分账单
• 中奖记录、兑换记录、游戏记录、积分抽奖
• 兑换 礼品编号；群内骰子：大3 / 小5 / 单10 / 双2
• 有效发言：1 分钟内最多算 2 条，少于 3 个字不算
• 活跃阶梯奖励、邀请奖励在“⭐ 积分”分类里设置

🎁 全部抽奖
• 抽奖：本群进行中；抽奖历史：往期
• /raffle 分钟 人数 奖品；/raffles 记录；/draw 编号 立即开奖
• 私聊粘贴或转发抽奖公告，或发送“识别抽奖”：自动识别后确认创建
• “用上次模板”“复制为新抽奖”：沿用以前的抽奖设置

🎟 彩票
• 开奖：本群已开启的彩种
• /lottery 彩种：最新；/lotteryhistory 彩种：历史

👥 群组管理
• 主菜单按分类进入：🎁 抽奖、⭐ 积分、🎲 骰子、📢 广告、⚙️ 群设置
• 在私聊选择群组后设置统计、抽奖、广告、积分、欢迎验证、邀请链接、快捷发布
• /link：生成个人邀请链接

🧰 其他
• z0 或 /rate：OKX 商户报价
• /price btc、“币价 btc”或直接发 BTC：查币价
• 币价涨跌监控：查询结果下点“🔔 监控此币涨跌”，或 /pricealert btc 5 2（日涨跌5%、10分钟2%）；/pricealerts 查看
• /userinfo @用户名：查询账户资料
• /jx：复制贴纸包并改标题（私聊里相关消息10分钟后自动撤回）
• 主菜单“😊 表情包复制更改标题”：固定模式保存标题和频道后，发链接即自动生成并发到频道
• 链接后加序号可去掉部分贴图，例如：链接 3|5|12
• /life 或发送“人生指南”：📖 人生指南

🛡 权限
• 超级管理员：管理本机器人、管理员，审核收录
• 群管理员默认无功能权限，由超级管理员分配

🤖 克隆机器人
• 主菜单点“克隆机器人”，发送 Bot Token，审核通过后自动启动
• 提交 Token 的人是新机器人的超级管理员
• 每个机器人的搜索收录数据独立，克隆机器人存储上限 2GB"""


def is_admin(config: Config, user_id: int | None) -> bool:
    return bool(
        user_id is not None
        and user_id in (config.admin_ids | config.super_admin_ids | config.developer_ids)
    )


def has_admin_access(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    if user_id is None:
        return False
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    return is_admin(config, user_id) or store.is_bot_admin(user_id)


def has_super_admin_access(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    if user_id is None:
        return False
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    return bool(
        user_id in config.super_admin_ids
        or user_id in config.developer_ids
        or store.is_super_admin(user_id)
    )


def is_developer_user(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    """Raw developer membership (works on mother and child bots)."""
    if user_id is None:
        return False
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    return bool(user_id in config.developer_ids or store.is_developer(user_id))


def has_developer_access(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    """Developer-only features: they exist only on the mother bot."""
    config: Config = context.application.bot_data["config"]
    if config.is_clone:
        return False
    return is_developer_user(context, user_id)


def developer_only_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.application.bot_data.get("config")
    if getattr(config, "is_clone", False):
        return "该功能不可用。"
    return "仅开发者可用。"


def has_review_access(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    """搜索收录审核：每个机器人的超级管理员（及开发者）审核本机器人的收录。"""
    return has_super_admin_access(context, user_id)


def entry_reviewer_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    database = {
        int(row["user_id"]): str(row["role"]) for row in store.list_bot_admins()
        if row["role"] in {"super", "developer"}
    }
    ids = set(config.super_admin_ids) | {
        user_id for user_id, role in database.items() if role == "super"
    }
    if not config.is_clone:
        ids |= set(all_developer_ids(context))
    else:
        # 子机器人：开发者不接收审核通知，只有本机超级管理员（克隆提供者等）收到
        ids |= set(config.admin_ids)
        ids -= set(config.developer_ids) - set(config.admin_ids)
    return sorted(ids)


def has_permission(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None, permission: str
) -> bool:
    if user_id is None:
        return False
    if has_super_admin_access(context, user_id):
        return True
    store: DirectoryStore = context.application.bot_data["store"]
    return permission in store.bot_admin_permissions(user_id)


def can_manage_bot_admin(
    context: ContextTypes.DEFAULT_TYPE, viewer_id: int, target_id: int
) -> bool:
    if is_developer_user(context, viewer_id):
        return True
    store: DirectoryStore = context.application.bot_data["store"]
    return target_id in {
        int(row["user_id"]) for row in store.list_bot_admins(
            viewer_id=viewer_id, include_all=False
        )
    }


def all_admin_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    return sorted(
        set(config.admin_ids) | set(config.super_admin_ids)
        | set(config.developer_ids) | set(store.all_admin_ids())
    )


def all_super_admin_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    database_ids = {
        int(row["user_id"]) for row in store.list_bot_admins()
        if row["role"] in {"super", "developer"}
    }
    return sorted(set(config.super_admin_ids) | set(config.developer_ids) | database_ids)


def all_developer_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    database_ids = {
        int(row["user_id"]) for row in store.list_bot_admins()
        if row["role"] == "developer"
    }
    result = set(config.developer_ids) | database_ids
    if not result and not config.is_clone:
        result.update(config.super_admin_ids)
    return sorted(result)


def main_keyboard(
    is_admin: bool = False, is_super: bool = False, show_clone: bool = True,
    is_developer: bool = False,
) -> InlineKeyboardMarkup:
    """Main menu grouped by category; every older entry stays reachable inside."""
    rows = [
        [
            InlineKeyboardButton("🔎 搜索收录", callback_data="nav:search"),
            InlineKeyboardButton("📮 提交搜录", callback_data="submit:start"),
        ],
        [
            InlineKeyboardButton("🎁 抽奖", callback_data="cat:raffle"),
            InlineKeyboardButton("⭐ 积分", callback_data="cat:points"),
        ],
        [
            InlineKeyboardButton("🎲 骰子", callback_data="cat:dice"),
            InlineKeyboardButton("📢 广告", callback_data="cat:ads"),
        ],
        [
            InlineKeyboardButton("💰 波场/地址监控", callback_data="cat:tron"),
            InlineKeyboardButton("💹 币价", callback_data="price:menu"),
        ],
        [InlineKeyboardButton(sticker_clone.MENU_BUTTON_TEXT, callback_data="stk:menu")],
        [InlineKeyboardButton(life_guide.MENU_BUTTON_TEXT, callback_data="life:home")],
        [InlineKeyboardButton("⚙️ 群设置", callback_data="nav:group")],
    ]
    if is_admin:
        rows[-1].append(InlineKeyboardButton("🛡 管理员", callback_data="nav:admin"))
    if is_developer:
        rows.append([
            InlineKeyboardButton("📝 私人笔记", callback_data="admin:notes"),
            InlineKeyboardButton("🌳 子机器人", callback_data="cat:clone"),
        ])
    rows.append([
        InlineKeyboardButton("👤 联系开发者", url="https://t.me/xinyuan188"),
        InlineKeyboardButton("📖 帮助教程", callback_data="menu:help"),
    ])
    if show_clone:
        rows.append([InlineKeyboardButton("🤖 克隆机器人", callback_data="clone:start")])
    return InlineKeyboardMarkup(rows)


def clone_available(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """母机器人和每个子机器人都能继续克隆（审核统一在母机器人）。"""
    return context.application.bot_data.get("clone_manager") is not None


def main_keyboard_for(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> InlineKeyboardMarkup:
    return main_keyboard(
        has_admin_access(context, user_id), has_super_admin_access(context, user_id),
        clone_available(context), has_developer_access(context, user_id),
    )


def custom_reply_keyboard(store: DirectoryStore) -> ReplyKeyboardMarkup:
    buttons = store.custom_buttons(enabled_only=True)
    rows = [
        [KeyboardButton(str(item["label"])) for item in buttons[index:index + 2]]
        for index in range(0, len(buttons), 2)
    ]
    rows.append([KeyboardButton("打开分类菜单")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def custom_buttons_admin_view(store: DirectoryStore) -> tuple[str, InlineKeyboardMarkup]:
    rows = store.custom_buttons()
    lines = ["🧩 自定义私聊按钮", ""]
    if rows:
        for row in rows:
            kind = "双向联系" if row["kind"] == "support" else "自定义按钮"
            target = (
                f" → {row['contact_name'] or row['contact_username'] or row['target_user_id']}"
                if row["kind"] == "support" else ""
            )
            lines.append(f"#{row['id']} · {kind} · {row['label']}{target}")
    else:
        lines.append("尚未添加自定义按钮。")
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ 双向联系", callback_data="admin:button:addsupport"),
            InlineKeyboardButton("➕ 自定义按钮", callback_data="admin:button:addmenu"),
        ],
        [InlineKeyboardButton("🗑 删除按钮", callback_data="admin:button:delete")],
        [InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")],
    ])
    return "\n".join(lines), keyboard


def clone_records_view(store: DirectoryStore) -> tuple[str, InlineKeyboardMarkup]:
    rows = store.list_bot_clones()
    labels = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝"}
    lines = ["🤖 克隆申请记录", ""]
    keyboard_rows = []
    for row in rows:
        owner = row["owner_first_name"] or row["owner_username"] or row["owner_id"]
        bot_label = (
            f"@{row['bot_username']}" if row["bot_username"]
            else f"ID {row['bot_id']}"
        )
        owner = row["owner_name"] or owner
        parent_id = int(row["parent_clone_id"] or 0)
        lines.append(
            f"#{row['id']} · {labels.get(str(row['status']), row['status'])} · "
            f"{bot_label} · 申请人 {owner} ({row['owner_id']})"
            + (f" · 来自子机器人 #{parent_id}" if parent_id else " · 来自母机器人")
            + (f" · 错误 {row['last_error']}" if row["last_error"] else "")
        )
        if row["status"] == "pending" and len(keyboard_rows) < 10:
            keyboard_rows.append([
                InlineKeyboardButton("✅ 通过", callback_data=f"clone:approve:{row['id']}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"clone:reject:{row['id']}"),
            ])
    if not rows:
        lines.append("暂无克隆申请。")
    keyboard_rows.append([InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


CLONE_TREE_PAGE_SIZE = 8
CLONE_STATUS_LABELS = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝"}


def clone_result_notice(clone_id: int, username: str, approved: bool) -> str:
    if approved:
        return (
            f"✅ 你的克隆申请 #{clone_id} 已通过，@{username} 已启动。\n"
            "你是该机器人的超级管理员，它拥有独立的搜索收录数据。"
        )
    return f"❌ 你的克隆申请 #{clone_id} 未通过审核。"


def clone_tree_flat(rows) -> list[tuple[object, int]]:
    """Depth-first (row, depth) list; depth 1 = cloned directly from the mother."""
    ids = {int(row["id"]) for row in rows}
    children: dict[int, list] = {}
    for row in sorted(rows, key=lambda item: int(item["id"])):
        parent = int(row["parent_clone_id"] or 0)
        children.setdefault(parent if parent in ids else 0, []).append(row)
    flat: list[tuple[object, int]] = []
    seen: set[int] = set()

    def walk(parent: int, depth: int) -> None:
        for row in children.get(parent, []):
            row_id = int(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            flat.append((row, depth))
            walk(row_id, depth + 1)

    walk(0, 1)
    return flat


def clone_bot_label(row) -> str:
    return f"@{row['bot_username']}" if row["bot_username"] else f"ID {row['bot_id']}"


def clone_tree_view(
    context: ContextTypes.DEFAULT_TYPE, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    store: DirectoryStore = context.application.bot_data["store"]
    manager: CloneManager | None = context.application.bot_data.get("clone_manager")
    rows = store.list_bot_clones(limit=5000)
    by_id = {int(row["id"]): row for row in rows}
    flat = clone_tree_flat(rows)
    pages = max(1, math.ceil(len(flat) / CLONE_TREE_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    mother = context.application.bot_data.get("bot_username") or "母机器人"
    lines = [
        "🌳 子机器人管理",
        f"母机器人 @{mother} · 子机器人共 {len(flat)} 个 · 第 {page + 1}/{pages} 页",
        "",
    ]
    buttons: list[InlineKeyboardButton] = []
    for row, depth in flat[page * CLONE_TREE_PAGE_SIZE:(page + 1) * CLONE_TREE_PAGE_SIZE]:
        clone_id = int(row["id"])
        pad = "　" * (depth - 1)
        status = CLONE_STATUS_LABELS.get(str(row["status"]), str(row["status"]))
        if row["status"] == "approved" and manager:
            status = "运行中" if manager.is_running(clone_id) else "未运行"
        parent_id = int(row["parent_clone_id"] or 0)
        parent_row = by_id.get(parent_id)
        parent = (
            f"#{parent_id} {clone_bot_label(parent_row)}" if parent_row else "母机器人"
        )
        owner = (
            row["owner_name"] or row["owner_first_name"] or row["owner_username"]
            or row["owner_id"]
        )
        usage = format_storage_bytes(manager.storage_bytes(clone_id)) if manager else "-"
        lines.append(f"{pad}{'└ ' if depth > 1 else ''}#{clone_id} {clone_bot_label(row)}（{row['bot_id']}）· {status}")
        lines.append(f"{pad}　超管：{owner}（{row['owner_id']}）· 上级：{parent} · 第{depth}层")
        lines.append(
            f"{pad}　创建：{format_beijing_time(row['created_at'])} · 存储：{usage}"
            + (f" · 错误：{row['last_error']}" if row["last_error"] else "")
        )
        buttons.append(InlineKeyboardButton(
            f"🗑 删除 #{clone_id}", callback_data=f"clonedel:ask:{clone_id}:{page}"
        ))
    if not flat:
        lines.append("暂无子机器人。")
    keyboard = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"clonetree:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"clonetree:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(keyboard)


async def handle_clone_tree_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str,
) -> None:
    """母机器人：子机器人列表 / 删除（级联删除全部下级，需二次确认）。"""
    query = update.callback_query
    user_id = query.from_user.id if query and query.from_user else None
    manager: CloneManager | None = context.application.bot_data.get("clone_manager")
    if not has_developer_access(context, user_id) or not manager or not manager.manage_processes:
        await query.answer("仅母机器人开发者可用。", show_alert=True)
        return
    store: DirectoryStore = context.application.bot_data["store"]
    parts = data.split(":")
    if parts[0] == "clonetree":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        text, keyboard = clone_tree_view(context, page)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if len(parts) < 3 or not parts[2].isdigit():
        await query.answer("编号无效。", show_alert=True)
        return
    action, clone_id = parts[1], int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    row = store.bot_clone(clone_id)
    if not row:
        await query.answer("该子机器人不存在或已删除。", show_alert=True)
        text, keyboard = clone_tree_view(context, page)
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    descendants = store.bot_clone_descendants(clone_id)
    if action == "ask":
        lines = [
            "⚠️ 确认删除子机器人",
            "",
            f"#{clone_id} {clone_bot_label(row)}（{row['bot_id']}）",
            f"下级子机器人：{len(descendants)} 个（将一并删除）",
        ]
        for item in descendants[:20]:
            lines.append(f"　· #{item['id']} {clone_bot_label(item)}")
        if len(descendants) > 20:
            lines.append(f"　· …… 另有 {len(descendants) - 20} 个")
        lines += [
            "",
            "删除后：停止运行、移除记录；数据库移入 data/clones/deleted 归档。",
            "如需恢复，只能重新提交 Token 克隆。",
        ]
        await query.answer()
        await query.edit_message_text(
            "\n".join(lines)[:4000],
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ 确认删除（共 {len(descendants) + 1} 个）",
                    callback_data=f"clonedel:do:{clone_id}:{page}",
                )],
                [InlineKeyboardButton("⬅️ 取消", callback_data=f"clonetree:{page}")],
            ]),
        )
        return
    if action != "do":
        await query.answer("未知操作。", show_alert=True)
        return
    try:
        removed = manager.delete_tree(clone_id)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    store.audit(
        f"tg:{user_id}", "clone.delete", str(clone_id),
        ",".join(str(item) for item in removed),
    )
    await query.answer(f"已删除 {len(removed)} 个子机器人。", show_alert=True)
    text, keyboard = clone_tree_view(context, page)
    await query.edit_message_text(
        f"✅ 已删除 #{clone_id} 及其下级，共 {len(removed)} 个。\n\n" + text,
        reply_markup=keyboard,
    )


async def sync_clone_requests(context: ContextTypes.DEFAULT_TYPE) -> None:
    """母机器人：把子机器人上提交的克隆申请推送给开发者审核。"""
    manager: CloneManager | None = context.application.bot_data.get("clone_manager")
    if not manager or not manager.manage_processes:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    for row in manager.sync_requests():
        parent = store.bot_clone(int(row["parent_clone_id"] or 0))
        source = f"#{parent['id']} {clone_bot_label(parent)}" if parent else "子机器人"
        for developer_id in all_developer_ids(context):
            try:
                with persistent_message():
                    await context.bot.send_message(
                        developer_id,
                        f"🤖 新克隆申请 #{row['id']}（来自 {source}）\n"
                        f"申请人：{row['owner_name'] or row['owner_id']} ({row['owner_id']})\n"
                        f"机器人：{clone_bot_label(row)}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ 通过", callback_data=f"clone:approve:{row['id']}"),
                            InlineKeyboardButton("❌ 拒绝", callback_data=f"clone:reject:{row['id']}"),
                        ]]),
                    )
            except TelegramError:
                logging.exception("Failed to send clone approval request")


async def notify_clone_results(context: ContextTypes.DEFAULT_TYPE) -> None:
    """子机器人：把母机器人的审核结果告诉在本机器人提交申请的人。"""
    manager: CloneManager | None = context.application.bot_data.get("clone_manager")
    config: Config = context.application.bot_data["config"]
    if not manager or manager.manage_processes or not config.clone_id:
        return
    for row in manager.store.bot_clone_results_for_parent(config.clone_id):
        approved = row["status"] == "approved"
        try:
            with persistent_message():
                await context.bot.send_message(
                    int(row["owner_id"]),
                    clone_result_notice(
                        int(row["id"]), str(row["bot_username"] or row["bot_id"]), approved
                    ),
                )
        except TelegramError:
            logging.warning("Could not notify clone owner %s", row["owner_id"])
        manager.store.mark_bot_clone_result_notified(int(row["id"]))


def search_menu_keyboard(is_developer: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔎 搜索收录", callback_data="search:prompt"),
            InlineKeyboardButton("🆕 最新收录", callback_data="menu:list"),
        ],
        [
            InlineKeyboardButton("📋 我的提交", callback_data="menu:my"),
            InlineKeyboardButton("⛓ 波场查询", callback_data="tron:prompt"),
        ],
        [InlineKeyboardButton("⏰ 波场地址监控", callback_data="tronmonitor:menu")],
        [InlineKeyboardButton("👤 查看账户信息", callback_data="account:prompt")],
    ]
    if is_developer:
        rows.append([InlineKeyboardButton("📈 搜索统计", callback_data="searchstats:keywords:0")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")])
    return InlineKeyboardMarkup(rows)


def group_menu_keyboard(
    is_admin: bool = False, is_super: bool = False,
    permissions: set[str] | None = None,
) -> InlineKeyboardMarkup:
    allowed = GROUP_PERMISSIONS if is_super else (permissions or set())
    rows = []
    if "stats" in allowed:
        rows.append([
            InlineKeyboardButton("📊 群统计", callback_data="group:stats"),
            InlineKeyboardButton("🔥 活跃排行", callback_data="group:active"),
        ])
    if "raffles" in allowed or "lottery" in allowed:
        row = []
        if "raffles" in allowed:
            row.append(InlineKeyboardButton("🎁 抽奖方案", callback_data="group:raffles"))
        if "lottery" in allowed:
            row.append(InlineKeyboardButton("🎟 开奖订阅", callback_data="group:lottery"))
        rows.append(row)
    if "polls" in allowed:
        rows.append([InlineKeyboardButton("🗳 群投票", callback_data="group:polls")])
    if "ads" in allowed:
        rows.append([
            InlineKeyboardButton("⏱ 定时广告", callback_data="group:ads"),
            InlineKeyboardButton("↔️ 消息前后广告", callback_data="group:ads"),
        ])
    if "points" in allowed:
        rows.append([InlineKeyboardButton("⭐ 积分·签到·礼品", callback_data="group:points")])
    if "welcome" in allowed:
        rows.append([InlineKeyboardButton("👋 进群欢迎·验证", callback_data="group:joincfg")])
    quick_row = []
    if "quickpost" in allowed:
        quick_row.append(InlineKeyboardButton("✏️ 快捷发布", callback_data="quickpost:menu"))
    if "invite" in allowed:
        quick_row.append(InlineKeyboardButton("🔗 邀请链接", callback_data="invite:menu"))
    if quick_row:
        rows.append(quick_row)
    if "recent" in allowed:
        rows.append([
            InlineKeyboardButton("🤖 机器人近期操作", callback_data="group:recent:bot:0"),
            InlineKeyboardButton("🕘 群组近期操作", callback_data="group:recent:group:0"),
        ])
    if "moderation" in allowed:
        rows.append([InlineKeyboardButton("🚫 删除·禁言·踢群", callback_data="group:moderation")])
    if "renamehist" in allowed:
        rows.append([InlineKeyboardButton("📝 改名记录", callback_data="group:renamehist")])
    rows.append([InlineKeyboardButton("💹 币价回复开关", callback_data="price:group")])
    if is_super:
        rows.append([InlineKeyboardButton("👮 群管理员权限", callback_data="group:permissions")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")])
    return InlineKeyboardMarkup(rows)


def group_permissions_view(
    store: DirectoryStore, chat_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    rows = store.list_group_admin_permissions(chat_id)
    lines = ["👮 群管理员权限", "", "群管理员默认没有机器人权限。"]
    for row in rows:
        name = row["first_name"] or row["username"] or row["user_id"]
        labels = [GROUP_PERMISSION_LABELS.get(item, item) for item in str(row["permissions"] or "").split(",") if item]
        lines.append(f"{name} ({row['user_id']})：{'、'.join(labels) if labels else '无权限'}")
    if not rows:
        lines.append("尚未分配群管理员权限。")
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔐 分配权限", callback_data="groupperm:set"),
            InlineKeyboardButton("♻️ 重置权限", callback_data="groupperm:reset"),
        ],
        [InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:groupmenu")],
    ])
    return "\n".join(lines), keyboard


async def group_admin_selector_view(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str,
) -> tuple[str, InlineKeyboardMarkup]:
    try:
        members = await context.bot.get_chat_administrators(chat_id)
    except TelegramError as exc:
        raise ValueError(f"无法读取群管理员：{exc}") from exc
    buttons = []
    for member in members:
        user = member.user
        if user.is_bot:
            continue
        name = user.full_name or user.username or str(user.id)
        buttons.append([InlineKeyboardButton(
            name[:40], callback_data=f"groupperm:choose:{action}:{user.id}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ 返回权限管理", callback_data="group:permissions")])
    title = "分配权限" if action == "set" else "重置权限"
    return (
        f"👮 群管理员{title}\n\n请直接选择这个群的群主或群管理员。",
        InlineKeyboardMarkup(buttons),
    )


def group_permission_editor_view(
    store: DirectoryStore, chat_id: int, target_id: int, target_name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    current = store.group_admin_permissions(chat_id, target_id)
    lines = [
        f"🔐 正在分配：{target_name} ({target_id})", "",
        "点击下方中文权限即时开启或关闭。",
    ]
    buttons = []
    for key in sorted(GROUP_PERMISSIONS, key=lambda item: GROUP_PERMISSION_LABELS[item]):
        enabled = key in current
        buttons.append([InlineKeyboardButton(
            f"{'✅' if enabled else '❌'} {GROUP_PERMISSION_LABELS[key]}",
            callback_data=f"groupperm:toggle:{target_id}:{key}",
        )])
    buttons.extend([
        [InlineKeyboardButton("✅ 完成", callback_data="group:permissions")],
        [InlineKeyboardButton("⬅️ 重新选择群管理员", callback_data="groupperm:set")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def admin_menu_keyboard(
    is_super: bool = False, is_developer: bool = False
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🚫 违规词管理", callback_data="admin:badwords")]]
    if is_super:
        rows.extend([
            [
                InlineKeyboardButton("👥 管理员管理", callback_data="admin:admins"),
                InlineKeyboardButton("🧩 自定义按钮", callback_data="admin:buttons"),
            ],
            [
                InlineKeyboardButton("📊 后台统计", callback_data="admin:stats"),
            ],
        ])
        rows.append([InlineKeyboardButton("📣 频道群发", callback_data="channelbroadcast:menu")])
    if is_super or is_developer:
        rows.insert(1, [InlineKeyboardButton("📥 待审核收录", callback_data="admin:pending")])
    if is_developer:
        # 以下为开发者功能，只在母机器人显示（子机器人 has_developer_access 恒为 False）
        rows.insert(2, [InlineKeyboardButton("🗒 私人笔记", callback_data="admin:notes")])
        rows.insert(3, [
            InlineKeyboardButton("🤖 克隆审核记录", callback_data="admin:clones"),
            InlineKeyboardButton("🌳 子机器人管理", callback_data="clonetree:0"),
        ])
        rows.insert(4, [
            InlineKeyboardButton("👤 机器人使用人员", callback_data="admin:usage:0")
        ])
        rows.insert(5, [
            InlineKeyboardButton("⏰ 波场监控统计", callback_data="admin:tronmonitors:0")
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")])
    return InlineKeyboardMarkup(rows)


def moderation_menu_keyboard(
    is_super: bool = False, back_callback: str = "nav:admin"
) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("➕ 添加违规词", callback_data="badword:add"),
        InlineKeyboardButton("➖ 删除违规词", callback_data="badword:delete"),
    ]]
    if is_super:
        rows.append([
            InlineKeyboardButton("✅ 开启处罚", callback_data="moderation:on"),
            InlineKeyboardButton("⛔ 关闭处罚", callback_data="moderation:off"),
        ])
    back_label = "⬅️ 返回群组管理" if back_callback == "nav:group" else "⬅️ 返回管理员"
    rows.append([InlineKeyboardButton(back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def raffle_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏳ 按分钟开奖", callback_data="raffleplan:minutes"),
            InlineKeyboardButton("🕘 固定时间开奖", callback_data="raffleplan:at"),
        ],
        [
            InlineKeyboardButton("🏆 多档奖品", callback_data="raffleplan:tiers"),
            InlineKeyboardButton("⚡ 快速10分钟", callback_data="raffleplan:quick"),
        ],
        [InlineKeyboardButton("🧾 样板通用抽奖", callback_data="raffleplan:pro")],
        [InlineKeyboardButton("📥 识别抽奖", callback_data="rparse:start")],
        [
            InlineKeyboardButton("🔁 用上次模板", callback_data="raffleplan:last"),
            InlineKeyboardButton("📋 复制为新抽奖", callback_data="rafflecopy:menu:0"),
        ],
        [
            InlineKeyboardButton("📋 最近抽奖", callback_data="raffleplan:list"),
            InlineKeyboardButton("🗑 删除群抽奖", callback_data="raffleplan:delete"),
        ],
        [InlineKeyboardButton("✏️ 修改抽奖", callback_data="raffleedit:menu:0")],
        [InlineKeyboardButton("⬅️ 返回抽奖类型", callback_data="group:raffles")],
    ])


def raffle_type_keyboard(show_count: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 通用抽奖", callback_data="raffletype:universal"),
            InlineKeyboardButton("Ⓜ️ 积分抽奖", callback_data="raffletype:points"),
        ],
        [InlineKeyboardButton("🥰 群活跃抽奖", callback_data="raffletype:active")],
        [InlineKeyboardButton(
            f"👥 参与人数：{'显示' if show_count else '隐藏'}",
            callback_data=f"raffle:count:{'off' if show_count else 'on'}",
        )],
        [
            InlineKeyboardButton("✏️ 修改抽奖", callback_data="raffleedit:menu:0"),
            InlineKeyboardButton("🗑 删除抽奖", callback_data="raffledelete:menu:0"),
        ],
        [InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")],
    ])


def group_ad_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ 设置定时广告", callback_data="ad:set:interval")],
        [
            InlineKeyboardButton("⬆️ 消息前广告", callback_data="ad:set:prefix"),
            InlineKeyboardButton("⬇️ 消息后广告", callback_data="ad:set:suffix"),
        ],
        [
            InlineKeyboardButton("📋 查看状态", callback_data="ad:status"),
            InlineKeyboardButton("⛔ 全部关闭", callback_data="ad:disable:all"),
        ],
        [
            InlineKeyboardButton("关闭定时", callback_data="ad:disable:interval"),
            InlineKeyboardButton("关闭前置", callback_data="ad:disable:prefix"),
            InlineKeyboardButton("关闭后置", callback_data="ad:disable:suffix"),
        ],
        [InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")],
    ])


def group_ad_status_text(store: DirectoryStore, chat_id: int) -> str:
    rows = {str(row["position"]): row for row in store.list_group_ads(chat_id)}
    labels = {"interval": "定时广告", "prefix": "消息前广告", "suffix": "消息后广告"}
    lines = ["📣 群广告管理", ""]
    for position, label in labels.items():
        row = rows.get(position)
        if not row or not row["is_enabled"]:
            lines.append(f"❌ {label}：关闭")
            continue
        detail = "开启"
        if position == "interval":
            detail += f"，每 {max(1, int(row['interval_seconds']) // 60)} 分钟"
        kinds = []
        if row["text"]:
            kinds.append("文字")
        if row["file_id"]:
            kinds.append(str(row["file_type"] or "媒体"))
        lines.append(f"✅ {label}：{detail}（{' + '.join(kinds)}）")
    lines.extend(["", "前后文字广告合并进同一条机器人消息（原文/按钮都保留，广告加在前后）；广告自带按钮会一并挂上；仅含图片/视频等媒体的广告仍单独发送。"])
    return "\n".join(lines)


def points_menu_keyboard(
    can_manage: bool = False, enabled: bool = False, can_manage_draw: bool = False,
    dice_enabled: bool = True, can_manage_dice_odds: bool = False,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📅 签到", callback_data="points:checkin"),
            InlineKeyboardButton("⭐ 我的积分", callback_data="points:balance"),
        ],
        [
            InlineKeyboardButton("🏆 积分排行", callback_data="points:rank"),
            InlineKeyboardButton("🎁 积分礼品", callback_data="points:gifts"),
        ],
        [
            InlineKeyboardButton("🎰 积分抽奖", callback_data="points:draw"),
            InlineKeyboardButton("🧾 积分账单", callback_data="points:ledger:0"),
        ],
        [
            InlineKeyboardButton("🏅 中奖记录", callback_data="points:wins:0"),
            InlineKeyboardButton("📦 兑换记录", callback_data="points:redeems:0"),
        ],
        [
            InlineKeyboardButton("🎮 游戏记录", callback_data="points:games:0"),
        ],
    ]
    if can_manage:
        rows.extend([
            [
                InlineKeyboardButton(
                    "⛔ 关闭积分" if enabled else "✅ 开启积分",
                    callback_data="points:disable" if enabled else "points:enable",
                ),
                InlineKeyboardButton("⚙️ 签到设置", callback_data="points:set:checkin"),
            ],
            [InlineKeyboardButton("🔥 活跃设置", callback_data="points:set:activitymenu")],
            [
                InlineKeyboardButton("➕ 添加礼品", callback_data="points:set:giftadd"),
                InlineKeyboardButton("➖ 删除礼品", callback_data="points:set:giftdel"),
            ],
            [InlineKeyboardButton("📝 兑换最低当日活跃", callback_data="points:set:redeemmsgmin")],
            [InlineKeyboardButton("⛔ 关闭活跃奖励", callback_data="points:activityoff")],
            [
                InlineKeyboardButton("➕➖ 增减积分", callback_data="points:set:adjust"),
                InlineKeyboardButton("🧹 清零积分", callback_data="points:set:clear"),
            ],
            [InlineKeyboardButton("👥 查询群员积分账单", callback_data="points:set:memberledger")],
            [InlineKeyboardButton("🎮 查询群员游戏记录", callback_data="points:set:membergames")],
        ])
    if can_manage_draw:
        rows.append([
            InlineKeyboardButton("⚙️ 积分抽奖设置", callback_data="points:set:drawconfig")
        ])
    if can_manage_dice_odds:
        rows.append([InlineKeyboardButton("🎲 骰子设置", callback_data="points:dice:menu")])
    rows.append([InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")])
    return InlineKeyboardMarkup(rows)


def activity_settings_view(store: DirectoryStore, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    config = store.points_config(chat_id)
    lines = ["🔥 活跃设置", ""]
    if config["activity_enabled"]:
        lines.append(
            f"随机活跃奖励：开启，每日随机目标 {config['activity_messages_min']}-"
            f"{config['activity_messages_max']} 条有效发言，奖励 "
            f"{format_points(config['activity_points_min'])}-"
            f"{format_points(config['activity_points_max'])} 积分"
        )
    else:
        lines.append("随机活跃奖励：关闭")
    tiers = store.activity_tiers(chat_id)
    lines.append("")
    lines.append("🏅 阶梯奖励（每档每天一次，与随机奖励同时生效）：")
    if tiers:
        for tier in tiers:
            lines.append(
                f"#{tier['id']} · 今日有效发言 {tier['messages']} 条 +{format_points(tier['points'])} 积分"
            )
    else:
        lines.append("暂未设置。例如：10条+5、50条+20、100条+50")
    lines.extend(["", f"有效发言：{EFFECTIVE_RULE_TEXT}。"])
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 随机活跃奖励", callback_data="points:set:activity")],
        [
            InlineKeyboardButton("➕ 添加/修改阶梯", callback_data="points:set:tieradd"),
            InlineKeyboardButton("➖ 删除阶梯", callback_data="points:set:tierdel"),
        ],
        [InlineKeyboardButton("⛔ 关闭随机活跃奖励", callback_data="points:activityoff")],
        [InlineKeyboardButton("⬅️ 返回积分功能", callback_data="group:points")],
    ])
    return "\n".join(lines), keyboard


def dice_max_bet_label(config) -> str:
    maximum = normalize_points(config["dice_max_bet"] or 0)
    return f"{format_points(maximum)} 积分" if maximum > 0 else "不限"


def dice_min_activity_label(config) -> str:
    value = int(config["dice_min_activity"] or 0)
    return f"今日有效发言满 {value} 条才能玩" if value > 0 else "不限"


def redeem_min_activity_label(config) -> str:
    value = int(config["redeem_min_activity"] or 0)
    return f"今日有效发言满 {value} 条才能兑换" if value > 0 else "不限"


def dice_free_activity_label(config) -> str:
    value = int(config["dice_free_activity"] or 0)
    return f"今日有效发言满 {value} 条不受定时限制" if value > 0 else "关闭"


def dice_settings_view(config) -> tuple[str, InlineKeyboardMarkup]:
    enabled = bool(config["dice_enabled"])
    scheduled = bool(config["dice_schedule_enabled"])
    text = (
        "🎲 骰子设置\n\n"
        f"游戏状态：{'开启' if enabled else '关闭'}\n"
        f"最低参与积分：{format_points(config['dice_min_bet'])}\n"
        f"单注上限：{dice_max_bet_label(config)}\n"
        f"最低当日活跃：{dice_min_activity_label(config)}\n"
        f"免定时活跃：{dice_free_activity_label(config)}\n"
        "（有效发言：1 分钟内最多算 2 条，少于 3 个字不算）\n"
        f"每日定时：{'开启' if scheduled else '关闭'}"
    )
    if scheduled:
        text += f"（{config['dice_open_time']} - {config['dice_close_time']}）"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⛔ 关闭骰子" if enabled else "✅ 开启骰子",
            callback_data="points:diceoff" if enabled else "points:diceon",
        )],
        [
            InlineKeyboardButton("⚙️ 骰子赔率", callback_data="points:set:diceodds"),
            InlineKeyboardButton("最低参与积分", callback_data="points:set:dicemin"),
        ],
        [
            InlineKeyboardButton("单注上限", callback_data="points:set:dicemax"),
            InlineKeyboardButton("每日定时开关", callback_data="points:set:diceschedule"),
        ],
        [
            InlineKeyboardButton("最低当日活跃", callback_data="points:set:dicemsgmin"),
            InlineKeyboardButton("免定时活跃", callback_data="points:set:dicemsgfree"),
        ],
        [InlineKeyboardButton("⬅️ 返回积分功能", callback_data="group:points")],
    ])
    return text, keyboard


def points_status_text(
    store: DirectoryStore, chat_id: int, show_admin: bool = False
) -> str:
    config = store.points_config(chat_id)
    checkin_min = format_points(config["checkin_min"])
    checkin_max = format_points(config["checkin_max"])
    checkin = (
        checkin_min
        if normalize_points(config["checkin_min"]) == normalize_points(config["checkin_max"])
        else f"随机 {checkin_min}-{checkin_max}"
    )
    activity = "关闭"
    if config["activity_enabled"]:
        activity = (
            f"每日随机目标 {config['activity_messages_min']}-"
            f"{config['activity_messages_max']} 条有效发言，奖励 "
            f"{format_points(config['activity_points_min'])}-"
            f"{format_points(config['activity_points_max'])} 积分"
        )
    draw = "关闭"
    if config["draw_enabled"]:
        draw = f"开启，每次 {format_points(config['draw_cost'])} 积分"
        if int(config["draw_min_activity"] or 0) > 0:
            draw += f"，今日有效发言满 {int(config['draw_min_activity'])} 条可参与"
    dice = "开启" if config["dice_enabled"] else "关闭"
    odds = int(config["dice_odds"] or 2000)
    odds = min(2000, max(1700, odds))
    dice_schedule = "关闭"
    if config["dice_schedule_enabled"]:
        dice_schedule = f"每日 {config['dice_open_time']}-{config['dice_close_time']}"
    return (
        f"⭐ 群积分中心\n\n"
        f"状态：{'✅ 开启' if config['is_enabled'] else '❌ 关闭'}\n"
        f"签到：{checkin} 积分\n"
        f"连续签到：第3天起每天额外 +{format_points(config['streak_bonus'])}\n"
        f"活跃奖励：{activity}\n"
        f"骰子游戏：{dice}\n"
        f"骰子赔率：{odds / 1000:.3f}（{odds}）\n\n"
        f"骰子最低参与：{format_points(config['dice_min_bet'])} 积分\n"
        f"骰子单注上限：{dice_max_bet_label(config)}\n"
        f"骰子定时：{dice_schedule}\n"
        f"骰子最低当日活跃：{dice_min_activity_label(config)}\n"
        f"骰子免定时活跃：{dice_free_activity_label(config)}\n\n"
        f"积分抽奖：{draw}\n"
        f"积分兑换最低当日活跃：{redeem_min_activity_label(config)}\n"
        "（当日活跃按有效发言计：1 分钟内最多算 2 条，少于 3 个字不算）\n\n"
        "群员可发送：签到、积分、积分排行、积分礼品、兑换 礼品编号、游戏记录；\n也可发送 大3 / 小5 / 单10 / 双2 玩骰子。"
    )


def group_join_keyboard(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⛔ 关闭欢迎" if config["welcome_enabled"] else "✅ 开启欢迎",
                callback_data="joincfg:welcome",
            ),
            InlineKeyboardButton("✏️ 设置欢迎语", callback_data="joincfg:text"),
        ],
        [InlineKeyboardButton(
            "⛔ 关闭验证" if config["verification_enabled"] else "✅ 开启验证",
            callback_data="joincfg:verify",
        )],
        [InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")],
    ])


def admin_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("通过", callback_data=f"approve:{entry_id}"),
            InlineKeyboardButton("拒绝", callback_data=f"reject:{entry_id}"),
        ]]
    )


def raffle_count_visible(store: DirectoryStore, chat_id: int) -> bool:
    return store.get_settings().get(f"group_raffle_show_count:{chat_id}", "1") == "1"


def raffle_needs_join_button(raffle) -> bool:
    """Universal raffles with 最低发言 auto-join by speaking — no click needed."""
    if str(raffle["raffle_type"] or "") != "universal":
        return False
    min_messages = 0
    try:
        min_messages = int(raffle["min_messages"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        min_messages = 0
    if min_messages > 0:
        return False
    try:
        raw = str(raffle["conditions_json"] or "")
    except (KeyError, IndexError):
        raw = ""
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and int(data.get("messages") or 0) > 0:
                return False
        except Exception:
            pass
    return True


def raffle_keyboard(
    raffle_id: int, entries: int, show_count: bool = True,
    raffle=None,
) -> InlineKeyboardMarkup | None:
    if raffle is not None and not raffle_needs_join_button(raffle):
        return None
    label = f"参与抽奖（{entries}人）" if show_count else "参与抽奖"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"raffle:join:{raffle_id}")]]
    )


def parse_raffle_prizes(
    prize: str, winner_count: int, strict: bool = False
) -> list[tuple[int, str]]:
    raw = prize.strip()
    if "|" not in raw:
        return [(winner_count, raw)]
    tiers: list[tuple[int, str]] = []
    for part in raw.split("|"):
        match = re.match(r"^\s*(\d+)\s*[*xX×]\s*(.+?)\s*$", part)
        if not match or int(match.group(1)) <= 0:
            if strict:
                raise ValueError("多档奖品格式应为：数量*奖品 | 数量*奖品")
            return [(winner_count, raw)]
        tiers.append((int(match.group(1)), match.group(2).strip()))
    if sum(quantity for quantity, _ in tiers) != winner_count:
        if strict:
            raise ValueError("多档奖品数量合计必须等于中奖人数")
        return [(winner_count, raw)]
    return tiers


def parse_numbered_id(value: str) -> int:
    match = re.fullmatch(r"\s*#?\s*(\d+)\s*", value)
    if not match:
        raise ValueError("请输入正确的 #编号")
    return int(match.group(1))


def raffle_prize_text(prize: str, winner_count: int) -> str:
    tiers = parse_raffle_prizes(prize, winner_count)
    if len(tiers) == 1:
        return html.escape(tiers[0][1])
    return "\n".join(
        f"{index}. {html.escape(label)} × {quantity}"
        for index, (quantity, label) in enumerate(tiers, 1)
    )


def raffle_prize_tree_lines(prize: str, winner_count: int) -> list[str]:
    tiers = parse_raffle_prizes(prize, winner_count)
    return [
        f"  ├ {html.escape(label)} x {quantity}"
        for quantity, label in tiers
    ]


def parse_raffle_rules(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in raw.replace("\r", "").split("\n") if line.strip()]


def parse_raffle_conditions(value: object) -> dict:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def format_raffle_draw_time(value: object) -> str:
    rendered = format_beijing_time(value)
    return f"{rendered} +0800" if rendered else ""


def telegram_user_link(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{html.escape(display_name)}</a>'


RAFFLE_RULES_HEADING = "📜 规则："


def raffle_rules_heading(rules: list[str]) -> str:
    """Single 「规则」 heading line, unless the user's own text already starts with 规则."""
    if not rules:
        return ""
    first = str(rules[0]).strip().lstrip("📜📋📌📝*#【[「<《 ").strip()
    if first.startswith("规则"):
        return ""
    return RAFFLE_RULES_HEADING


def raffle_min_participants(raffle) -> int:
    try:
        keys = raffle.keys()
    except Exception:
        return 0
    if "min_participants" not in keys:
        return 0
    try:
        return max(0, int(raffle["min_participants"] or 0))
    except (TypeError, ValueError):
        return 0


def raffle_text(raffle, show_count: bool = True) -> str:
    raffle_type = str(raffle["raffle_type"] or "universal")
    if raffle_type.startswith("activity_"):
        rule = (
            f"发言达到 {raffle['activity_min_messages']} 条后随机抽取"
            if raffle_type == "activity_random" else "按发言次数排行取前列"
        )
        count_line = f"👥 参与人数：{raffle['entries']} 人\n" if show_count else ""
        return (
            f"🎁 <b>群活跃抽奖</b>\n\n"
            f"🎁 奖品：\n{raffle_prize_text(str(raffle['prize']), int(raffle['winner_count']))}\n"
            f"🏆 中奖名额：{raffle['winner_count']} 人\n"
            f"📊 规则：{rule}\n"
            f"🗓 发言统计开始：{format_beijing_time(raffle['activity_start_at'])}\n"
            f"⏰ 开奖时间：{format_beijing_time(raffle['ends_at'])}\n\n"
            f"{count_line}"
            "系统将在开奖时自动统计符合条件的成员。"
        )
    rules = parse_raffle_rules(raffle["rules_json"] if "rules_json" in raffle.keys() else "")
    conditions = parse_raffle_conditions(
        raffle["conditions_json"] if "conditions_json" in raffle.keys() else ""
    )
    title = str(raffle["title"] if "title" in raffle.keys() else "" or "").strip()
    if not title:
        title = "通用抽奖"
    keyword = str(raffle["join_keyword"] if "join_keyword" in raffle.keys() else "" or "").strip()
    how_to = str(raffle["how_to_join"] if "how_to_join" in raffle.keys() else "" or "").strip()
    channel = str(raffle["channel_ref"] if "channel_ref" in raffle.keys() else "" or "").strip()
    min_messages = int(raffle["min_messages"] if "min_messages" in raffle.keys() else 0 or 0)
    need = min_messages or int(conditions.get("messages") or 0)
    try:
        recur_daily = int(raffle["recur_daily"] if "recur_daily" in raffle.keys() else 0 or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        recur_daily = 0
    if need > 0:
        if recur_daily:
            auto_how = f"当天发言达到 {need} 条即自动参与，无需点击按钮。"
        else:
            auto_how = f"活动期间群内发言达到 {need} 条即自动参与，无需点击按钮。"
        if keyword:
            auto_how += f"也可发送关键词：{keyword}"
        if (
            not how_to
            or "按钮" in how_to
            or (recur_daily and "活动期间" in how_to)
        ):
            how_to = auto_how
    elif not how_to:
        how_to = "点击下方按钮参与抽奖。" + (f"也可发送关键词：{keyword}" if keyword else "")
    lines: list[str] = []
    if rules:
        heading = raffle_rules_heading(rules)
        if heading:
            lines.append(heading)
        lines.extend(html.escape(rule) for rule in rules)
        lines.append("")
    lines.append(html.escape(title))
    lines.append("├活动类型: 通用抽奖")
    lines.append(f"├定时开奖: {html.escape(format_raffle_draw_time(raffle['ends_at']))}")
    if keyword:
        lines.append(f"├参与关键词: {html.escape(keyword)}")
    min_boosts = int(raffle["min_boosts"] if "min_boosts" in raffle.keys() else 0 or 0)
    if conditions.get("channel") or channel:
        lines.append(f"├关注频道: {html.escape(channel or str(conditions.get('channel') or ''))}")
    if conditions.get("messages") or min_messages:
        lines.append(f"├最低发言: {min_messages or int(conditions.get('messages') or 0)} 条")
    if conditions.get("boosts") or min_boosts:
        lines.append(f"├最低助推: {min_boosts or int(conditions.get('boosts') or 0)}")
    min_participants = raffle_min_participants(raffle)
    if min_participants > 0:
        lines.append(f"├最少参与: {min_participants} 人（不足自动顺延一天）")
    if show_count:
        lines.append(f"├已参与: {raffle['entries']} 人")
    lines.append("├奖品列表:")
    lines.extend(raffle_prize_tree_lines(str(raffle["prize"]), int(raffle["winner_count"])))
    lines.append("[如何参与？]")
    lines.append(html.escape(how_to))
    return "\n".join(lines)


def should_block_group_content(
    content: str, blocked_keywords: tuple[str, ...], block_all_links: bool
) -> bool:
    text = content.casefold()
    keyword_hit = any(keyword and keyword in text for keyword in blocked_keywords)
    link_hit = block_all_links and bool(re.search(r"(?:https?://|www\.|t\.me/)", text))
    return keyword_hit or link_hit


def group_violation_reason(
    content: str, blocked_keywords: tuple[str, ...], has_link_entity: bool = False
) -> str | None:
    text = content.casefold()
    link_hit = has_link_entity or bool(re.search(
        r"(?:https?://|www\.|t\.me/|telegram\.me/|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/|\b))",
        text,
    ))
    if link_hit:
        return "发送链接"
    for keyword in blocked_keywords:
        if keyword and keyword in text:
            return f"命中违规关键词：{keyword}"
    return None


def record_directory_search(
    update: Update, store: DirectoryStore, query: str, result_count: int, source: str
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    store.record_search(
        query=query,
        user_id=user.id,
        username=user.username or "",
        display_name=user.full_name or str(user.id),
        chat_id=chat.id,
        chat_type=chat.type,
        source=source,
        result_count=result_count,
    )
    store.record_bot_usage(
        user.id, user.username or "", user.full_name or str(user.id)
    )


def record_selected_bot_usage(update: Update, store: DirectoryStore) -> None:
    """Count only searches/queries, completed settings and channel sends."""
    user = update.effective_user
    if user:
        username = getattr(user, "username", "") or ""
        display_name = getattr(user, "full_name", "") or str(user.id)
        store.record_bot_usage(
            user.id, username, display_name
        )


def schedule_group_trigger_cleanup(
    context: ContextTypes.DEFAULT_TYPE, message, content: str
) -> None:
    bot = context.bot
    if not isinstance(bot, AutoDeleteBot):
        return
    store: DirectoryStore = context.application.bot_data["store"]
    settings = store.get_settings()
    if settings.get("group_moderation_enabled") == "1":
        blocked_keywords = store.moderation_keyword_values()
        entities = tuple(message.entities or ()) + tuple(message.caption_entities or ())
        has_link_entity = any(entity.type in {"url", "text_link"} for entity in entities)
        if group_violation_reason(content, blocked_keywords, has_link_entity):
            return
    bot.schedule_delete(message.chat_id, message.message_id)


SETTING_MESSAGE_DELETE_SECONDS = 600  # 设置面板 10 分钟无操作再撤回


def schedule_setting_cleanup(
    context: ContextTypes.DEFAULT_TYPE, message, seconds: int = SETTING_MESSAGE_DELETE_SECONDS
) -> None:
    if not message:
        return
    bot = context.bot
    if isinstance(bot, AutoDeleteBot):
        bot.schedule_delete_after(message.chat_id, message.message_id, seconds)
    context.application.bot_data["store"].schedule_message_deletion(
        message.chat_id, message.message_id, seconds
    )


async def refresh_callback_cleanup(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Restart an operation panel's inactivity timer on every button click."""
    query = update.callback_query
    if not query or not query.message:
        return
    data = query.data or ""
    if data.startswith("raffle:join:") or data.startswith("verify:"):
        return
    schedule_setting_cleanup(context, query.message)


async def reject_foreign_panel_click(
    query, context: ContextTypes.DEFAULT_TYPE, data: str
) -> bool:
    message = query.message
    if (
        not message
        or message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or data.startswith("raffle:join:")
        or data.startswith("verify:")
    ):
        return False
    replied = message.reply_to_message
    owner = replied.from_user if replied else None
    if not owner or owner.is_bot or owner.id == query.from_user.id:
        return False
    await query.answer("该操作页只能由打开者使用。", show_alert=True)
    return True


async def menu_input_error(
    context: ContextTypes.DEFAULT_TYPE, message, mode: str, error: str,
) -> None:
    if hasattr(message, "setting_answers"):
        context.user_data["menu_mode"] = mode
        await message.reply_text(error if "|" not in error else "设置不符合要求，请返回对应步骤检查填写内容。")
        return
    key = f"menu_input_failures:{mode}"
    count = int(context.user_data.get(key, 0)) + 1
    context.user_data[key] = count
    if count >= 3:
        context.user_data.pop("menu_mode", None)
        context.user_data.pop(key, None)
        context.user_data.pop("raffle_active_start_at", None)
        await message.reply_text(
            f"{error}\n\n连续 3 次输入不符合当前问题，本次设置已自动关闭。"
        )
        return
    context.user_data["menu_mode"] = mode
    await message.reply_text(
        f"{error}\n\n输入不符合当前问题（{count}/3），请按提示重新输入。"
    )


def clear_menu_input_failures(
    context: ContextTypes.DEFAULT_TYPE, mode: str,
) -> None:
    context.user_data.pop(f"menu_input_failures:{mode}", None)


def parse_group_trigger(
    content: str, directory_trigger: str = "地址", rate_trigger: str = "z0"
) -> tuple[str, str] | None:
    text = content.strip()
    folded = text.casefold()
    rate_key = rate_trigger.strip().casefold()
    directory_key = directory_trigger.strip()
    if rate_key and folded == rate_key:
        return "rate", ""
    if directory_key and folded.endswith(directory_key.casefold()):
        query = text[:-len(directory_key)].strip()
        if query:
            return "directory", query
    return None


def extract_tron_address(content: str) -> str | None:
    text = content.strip()
    if len(text) != 34 or not text.startswith("T"):
        return None
    try:
        return validate_tron_address(text)
    except ValueError:
        return None


def entry_text(entry: Entry, include_owner: bool = False) -> str:
    labels = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝", "removed": "已下架"}
    rich = bool(entry.content_text or entry.media_file_id or entry.url.startswith("tgcontent://"))
    content = entry.content_text.strip()
    if len(content) > 700:
        content = content[:700] + "…"
    if rich:
        value_lines = []
        if content:
            value_lines.append(f"<b>内容：</b>{html.escape(content)}")
        if entry.media_file_id:
            media_label = {
                "photo": "图片", "video": "视频", "animation": "动图",
                "audio": "音频", "voice": "语音", "document": "文件",
            }.get(entry.media_type, "媒体")
            name = f"（{html.escape(entry.media_name)}）" if entry.media_name else ""
            value_lines.append(f"<b>附件：</b>{media_label}{name}")
        public_text = f"<b>关键词：</b>{html.escape(entry.title)}\n" + "\n".join(value_lines)
    else:
        public_text = (
            f"<b>关键词：</b>{html.escape(entry.title)}\n"
            f"<b>地址：</b>{html.escape(entry.url)}"
        )
    if not include_owner:
        return public_text
    desc = f"\n说明：{html.escape(entry.description)}" if entry.description else ""
    reason = f"\n原因：{html.escape(entry.reason)}" if entry.reason else ""
    reports = f"\n举报：{entry.reports_count}" if entry.reports_count else ""
    return (
        f"<b>待审核收录 #{entry.id}</b>\n\n"
        f"{public_text}\n"
        f"状态：{labels.get(entry.status, entry.status)}{desc}\n"
        f"提交者：<code>{entry.user_id}</code>"
        + (f" @{html.escape(entry.username)}" if entry.username else "")
        + f"{reason}{reports}"
    )


ENTRY_MEDIA_METHODS = {
    "photo": "photo", "video": "video", "animation": "animation",
    "audio": "audio", "voice": "voice", "document": "document",
    "sticker": "sticker", "video_note": "video_note",
}
DIRECTORY_NOT_FOUND_MAX_QUERY = 20


def entry_body(entry: Entry) -> tuple[str, list]:
    """The stored 收录 content exactly as submitted (text + entities)."""
    if entry.content_text:
        try:
            entities = [
                MessageEntity.de_json(item, None)
                for item in json.loads(entry.entities_json or "[]")
            ]
        except (TypeError, ValueError):
            entities = []
        return entry.content_text, entities
    if entry.media_file_id or entry.copy_message_id:
        return "", []
    # 旧版“关键词 地址”收录：原样回复地址
    return entry.url, []


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def deliver_entry(
    entry: Entry, *, message=None, bot=None, chat_id: int | None = None,
) -> None:
    """Send a 收录 exactly as it was captured, with no wrapper text.

    Order: copy_message from the original message (keeps everything),
    otherwise rebuild from stored text + entities + media + buttons.
    With ``message`` the content is sent as a reply to it.
    """
    markup = buttons_markup(entry.buttons_json)
    target_bot = bot
    if target_bot is None and message is not None and hasattr(message, "get_bot"):
        try:
            target_bot = message.get_bot()
        except RuntimeError:
            target_bot = None

    def call(kind: str, *args, **kwargs):
        if message is not None:
            name = "reply_text" if kind == "message" else f"reply_{kind}"
            return getattr(message, name)(*args, **kwargs)
        return getattr(bot, f"send_{kind}")(chat_id, *args, **kwargs)

    with advertisement_message():
        if entry.copy_chat_id and entry.copy_message_id:
            kwargs = {"reply_markup": markup} if markup else {}
            try:
                if message is not None:
                    result = await message.reply_copy(
                        entry.copy_chat_id, entry.copy_message_id, **kwargs
                    )
                    result_chat_id = getattr(message, "chat_id", None)
                else:
                    result = await bot.copy_message(
                        chat_id=chat_id, from_chat_id=entry.copy_chat_id,
                        message_id=entry.copy_message_id, **kwargs,
                    )
                    result_chat_id = chat_id
                scheduler = getattr(target_bot, "schedule_delete", None)
                if (
                    callable(scheduler) and not is_persistent_message()
                    and result_chat_id is not None and getattr(result, "message_id", None)
                ):
                    scheduler(int(result_chat_id), int(result.message_id))
                return
            except TelegramError:
                logging.info("copy_message failed for entry %s; rebuilding", entry.id)
        text, entities = entry_body(entry)
        entity_kwargs = {"entities": entities} if entities else {}
        if not entry.media_file_id:
            await call(
                "message", text or entry.title, parse_mode=None,
                reply_markup=markup, **entity_kwargs,
            )
            return
        kind = ENTRY_MEDIA_METHODS.get(entry.media_type, "document")
        if kind in {"sticker", "video_note"}:
            if text:
                await call("message", text, parse_mode=None, **entity_kwargs)
            await call(kind, entry.media_file_id, reply_markup=markup)
            return
        if text and len(text) > 1024:
            await call(kind, entry.media_file_id)
            await call("message", text, parse_mode=None, reply_markup=markup, **entity_kwargs)
            return
        caption_kwargs = {"caption_entities": entities} if entities else {}
        await call(
            kind, entry.media_file_id, caption=text or None, parse_mode=None,
            reply_markup=markup, **caption_kwargs,
        )


def entry_keyword_list_text(entries: list[Entry]) -> str:
    lines = ["🔎 找到以下关键词，发送关键词即可查看内容：", ""]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. <code>{html.escape(entry.title)}</code>")
    return "\n".join(lines)


def is_directory_not_found_query(query: str) -> bool:
    """Only short 「XX地址」 style messages get the not-found reply."""
    query = query.strip()
    return bool(query) and "\n" not in query and len(query) <= DIRECTORY_NOT_FOUND_MAX_QUERY


async def directory_search_reply(
    update: Update, store: DirectoryStore, query: str, source: str,
) -> None:
    """Explicit search (/search, 搜索菜单): exact keyword first, else a short list."""
    message = update.effective_message
    settings = store.get_settings()
    trigger = settings.get("group_directory_trigger", "地址")
    entry = store.find_keyword_entry(query, (trigger,))
    if entry:
        record_directory_search(update, store, query, 1, source)
        await deliver_entry(entry, message=message)
        return
    entries = store.search_keyword_titles(query, limit=10)
    record_directory_search(update, store, query, len(entries), source)
    if not entries:
        await message.reply_text(settings.get("not_found_text", "地址没有收录，请联系管理员。"))
        return
    await message.reply_text(entry_keyword_list_text(entries), parse_mode=ParseMode.HTML)


def format_storage_bytes(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def storage_usage_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    used = store.storage_usage_bytes()
    quota = int(getattr(config, "storage_quota_bytes", 0) or 0)
    if not quota:
        return f"💾 存储用量：{format_storage_bytes(used)}"
    percent = used * 100 / quota
    return (
        f"💾 存储用量：{format_storage_bytes(used)} / {format_storage_bytes(quota)}"
        f"（{percent:.1f}%）"
    )


def storage_quota_exceeded(context: ContextTypes.DEFAULT_TYPE) -> bool:
    config = context.application.bot_data.get("config")
    quota = int(getattr(config, "storage_quota_bytes", 0) or 0)
    if not quota:
        return False
    store: DirectoryStore = context.application.bot_data["store"]
    return store.storage_usage_bytes() >= quota


def storage_full_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return (
        "❌ 本机器人存储空间已满，暂时无法新增收录。\n"
        + storage_usage_text(context)
        + "\n请联系本机器人超级管理员清理。"
    )


def register_user(update: Update, store: DirectoryStore) -> bool:
    user = update.effective_user
    if not user:
        return False
    store.touch_user(
        user.id, user.username or "", user.first_name or "", user.last_name or "",
        getattr(user, "language_code", "") or "",
    )
    return not store.is_user_blocked(user.id)


async def jx_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jx：复制贴纸包并改标题（所有用户可用，母/子机器人通用）。"""
    if not await guard(update, context):
        return
    await sticker_clone.begin(update, context)


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    store: DirectoryStore = context.application.bot_data["store"]
    allowed = register_user(update, store)
    if allowed:
        settings = store.get_settings()
        if settings.get("maintenance_mode") == "1" and not has_admin_access(
            context, update.effective_user.id if update.effective_user else None
        ):
            await update.effective_message.reply_text("系统维护中，请稍后再试。")
            return False
        return True
    if update.effective_message:
        await update.effective_message.reply_text("你的账号暂时无法使用此机器人。")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if (
        update.effective_chat
        and update.effective_chat.type == ChatType.PRIVATE
        and context.args
    ):
        payload = context.args[0]
        if payload == "jx":
            context.args = []
            await sticker_clone.begin(update, context)
            return
        action, separator, raw_address = payload.partition("_")
        if separator and action in {"tr10", "trmon"}:
            try:
                address = validate_tron_address(raw_address)
            except ValueError:
                address = ""
            if address and action == "trmon":
                context.user_data["tron_monitor_address"] = address
                prompt = await tron_monitor_asset_prompt(context, address)
                sent = await update.effective_message.reply_text(
                    prompt,
                    parse_mode=ParseMode.HTML,
                    reply_markup=tron_monitor_prompt_keyboard(context),
                )
                schedule_setting_cleanup(context, sent)
                return
            if address and action == "tr10":
                store: DirectoryStore = context.application.bot_data["store"]
                try:
                    result = await context.application.bot_data["chain"].tron_balance(
                        address
                    )
                except (ValueError, ChainQueryError) as exc:
                    store.add_chain_query(
                        update.effective_user.id, address, "recent10", str(exc), False
                    )
                    await update.effective_message.reply_text(str(exc))
                    return
                store.add_chain_query(
                    update.effective_user.id, address, "recent10",
                    f"transactions={len(result.transactions[:10])}",
                )
                text, keyboard = tron_recent_view(
                    result,
                    context.application.bot_data.get("kkpay_emoji_ids"),
                    context.application.bot_data.get("tron_direction_emoji_ids"),
                    store.chain_query_count(),
                    str(context.application.bot_data.get("bot_username") or ""),
                )
                sent = await update.effective_message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                schedule_setting_cleanup(context, sent)
                return
    if (
        update.effective_chat
        and update.effective_chat.type == ChatType.PRIVATE
        and context.args
        and context.args[0].casefold() == "groups"
    ):
        text, keyboard = await private_group_selector(
            context, update.effective_user.id
        )
        sent = await update.effective_message.reply_text(text, reply_markup=keyboard)
        schedule_setting_cleanup(context, sent)
        return
    settings = context.application.bot_data["store"].get_settings()
    sent = await update.effective_message.reply_text(
        settings.get("welcome_text", "欢迎使用。"),
        reply_markup=main_keyboard_for(
            context, update.effective_user.id if update.effective_user else None
        ),
    )
    schedule_setting_cleanup(context, sent)
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        reply_keyboard = custom_reply_keyboard(context.application.bot_data["store"])
        if reply_keyboard:
            shortcut_message = await update.effective_message.reply_text(
                "请选择快捷按钮：", reply_markup=reply_keyboard
            )
            schedule_setting_cleanup(context, shortcut_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update, context):
        await update.effective_message.reply_text(
            HELP_TEXT,
            reply_markup=main_keyboard_for(
                context, update.effective_user.id if update.effective_user else None
            ),
        )


async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        payload = ""
    else:
        message = update.effective_message
        payload = message.text.partition(" ")[2].strip() if message and message.text else ""
    if payload:
        rich_submission = parse_rich_submission_command(payload)
        if rich_submission:
            await save_rich_submission(update, context, *rich_submission)
        else:
            await message.reply_text("格式不正确，请发送：关键词 搜录 内容")
        return ConversationHandler.END
    await message.reply_text(
        "请发送：关键词 搜录 内容\n\n"
        "例如：v8 搜录 https://example.com\n"
        "图片、视频或文件：在说明中写 v8 搜录 文字说明\n"
        "原样收录（含格式、按钮）：回复那条消息，发送 v8 搜录\n\n"
        "内容会按原格式保存和回复；同一关键词以最新审核通过的一条为准。\n"
        "关键词、搜录、内容之间必须有空格。发送 /cancel 可取消。"
    )
    return URL


async def submit_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    rich_submission = parse_rich_submission_command(text)
    if rich_submission:
        await save_rich_submission(update, context, *rich_submission)
    else:
        await update.effective_message.reply_text("格式不正确，请发送：关键词 搜录 内容")
    return ConversationHandler.END


async def submit_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.effective_message.text.strip()
    if not 1 <= len(title) <= 120:
        await update.effective_message.reply_text("标题需要 1-120 个字符，请重新发送：")
        return TITLE
    context.user_data["submission"]["title"] = title
    config: Config = context.application.bot_data["config"]
    keyboard = [
        [InlineKeyboardButton(category, callback_data=f"category:{category}")]
        for category in config.categories
    ]
    await update.effective_message.reply_text("请选择分类：", reply_markup=InlineKeyboardMarkup(keyboard))
    return CATEGORY


async def submit_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category = (query.data or "").partition(":")[2]
    config: Config = context.application.bot_data["config"]
    if category not in config.categories:
        await query.edit_message_text("分类已失效，请重新发送 /submit。")
        return ConversationHandler.END
    context.user_data["submission"]["category"] = category
    await query.edit_message_text("请发送简介（最多 500 字），不需要可发送一个减号 -")
    return DESCRIPTION


async def submit_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.effective_message.text.strip()
    description = "" if raw == "-" else raw
    if len(description) > 500:
        await update.effective_message.reply_text("简介最多 500 字，请重新发送：")
        return DESCRIPTION
    data = context.user_data.pop("submission", {})
    payload = f"{data.get('url', '')} | {data.get('title', '')} | {data.get('category', '')} | {description}"
    await save_payload(update, context, payload, update.effective_message)
    return ConversationHandler.END


async def save_payload(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str, message) -> None:
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    user = update.effective_user
    if not user:
        await message.reply_text("无法识别提交用户，请重新发送。")
        return
    if storage_quota_exceeded(context):
        await message.reply_text(storage_full_text(context))
        return
    try:
        submission = (
            parse_submission_payload(payload, config.categories)
            if "|" in payload
            else parse_keyword_address_payload(payload, config.categories)
        )
        status = "pending"
        entry_id = store.add_submission(
            submission, user.id, user.username or "", status,
            getattr(
                message, "chat_id",
                getattr(getattr(update, "effective_chat", None), "id", 0),
            ),
            getattr(message, "message_id", 0),
        )
    except ValueError as exc:
        await message.reply_text(f"提交失败：{exc}")
        return
    store.audit(f"tg:{user.id}", "entry.submit", str(entry_id), submission.url)
    await message.reply_text(
        f"提交成功，编号 #{entry_id}，正在等待超级管理员审核。",
        reply_markup=main_keyboard_for(context, user.id),
    )
    if isinstance(getattr(context, "user_data", None), dict):
        context.user_data["preserve_incoming_message_id"] = getattr(message, "message_id", 0)
    await notify_admins(context, entry_id)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("settings_draft", None)
    context.user_data.pop("submission", None)
    context.user_data.pop("support_mode", None)
    context.user_data.pop("support_target_id", None)
    context.user_data.pop("support_button_id", None)
    context.user_data.pop("menu_mode", None)
    context.user_data.pop("group_poll_question", None)
    context.user_data.pop("pending_private_note", None)
    context.user_data.pop(sticker_clone.STATE_KEY, None)
    context.user_data.pop(RAFFLE_PARSE_WAIT_KEY, None)
    context.user_data.pop(RAFFLE_PARSE_KEY, None)
    await update.effective_message.reply_text(
        "已取消。",
        reply_markup=main_keyboard_for(
            context, update.effective_user.id if update.effective_user else None
        ),
    )
    return ConversationHandler.END


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, entry_id: int) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    entry = store.get(entry_id)
    if not entry:
        return
    for admin_id in entry_reviewer_ids(context):
        try:
            with persistent_message():
                # 先发原样内容（与公开回复完全一致），再发审核卡片
                await deliver_entry(entry, bot=context.bot, chat_id=admin_id)
        except (TelegramError, ValueError):
            logging.exception("Failed to send submission content to admin %s", admin_id)
        try:
            with persistent_message():
                await context.bot.send_message(
                    admin_id,
                    "收到新的待审核提交：\n\n" + entry_text(entry, include_owner=True),
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_keyboard(entry.id),
                    disable_web_page_preview=True,
                )
        except TelegramError:
            logging.exception("Failed to notify admin %s", admin_id)


async def list_entries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    config: Config = context.application.bot_data["config"]
    category = context.args[0].casefold() if context.args else None
    if category and category not in config.categories:
        await update.effective_message.reply_text(f"未知分类，可选：{', '.join(config.categories)}")
        return
    entries = context.application.bot_data["store"].list_entries(category=category, limit=3)
    store: DirectoryStore = context.application.bot_data["store"]
    empty_text = store.get_settings().get("not_found_text", "地址没有收录，请联系管理员。")
    await send_entries(update.effective_message, entries, empty_text)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = " ".join(context.args).strip()
    if len(query) < 2:
        await update.effective_message.reply_text("用法：/search 关键词")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    await directory_search_reply(update, store, query, "search_command")


async def my_entries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    entries = context.application.bot_data["store"].my_entries(update.effective_user.id)
    if not entries:
        await update.effective_message.reply_text("你还没有提交记录。")
        return
    await update.effective_message.reply_text(my_entries_text(entries), parse_mode=ParseMode.HTML)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/report 编号 原因")
        return
    entry_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip() or "未填写原因"
    store: DirectoryStore = context.application.bot_data["store"]
    entry = store.get(entry_id)
    if not entry or entry.status != "approved":
        await update.effective_message.reply_text("没有找到这条已通过的收录。")
        return
    ok = store.add_report(entry_id, update.effective_user.id, reason)
    await update.effective_message.reply_text("举报已提交。" if ok else "你已经举报过这一条。")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("用法：/balance 以T开头的波场地址")
        return
    await send_balance_query(update, context, context.args[0].strip())


async def send_balance_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE, address: str
) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    record_selected_bot_usage(update, store)
    message = update.effective_message
    if store.get_settings().get("chain_enabled") != "1":
        await message.reply_text("链上余额查询目前已关闭。")
        return
    pending = await message.reply_text("🔎 正在查询波场地址，请稍候…")
    try:
        result = await context.application.bot_data["chain"].tron_balance(address)
    except (ValueError, ChainQueryError) as exc:
        user_id = update.effective_user.id if update.effective_user else 0
        store.add_chain_query(user_id, address, "balance", str(exc), False)
        try:
            await pending.edit_text(str(exc))
        except TelegramError:
            await message.reply_text(str(exc))
        return
    summary = f"TRX={result.trx:f}, USDT={result.usdt:f}, transactions={len(result.transactions)}"
    user_id = update.effective_user.id if update.effective_user else 0
    store.add_chain_query(user_id, result.address, "balance", summary)
    query_count = store.chain_query_count()
    text, keyboard = tron_result_view(
        result,
        emoji_ids=context.application.bot_data.get("kkpay_emoji_ids"),
        direction_emoji_ids=context.application.bot_data.get("tron_direction_emoji_ids"),
        status_emoji_ids=context.application.bot_data.get("tron_status_emoji_ids"),
        query_count=query_count,
        bot_username=str(context.application.bot_data.get("bot_username") or ""),
    )
    try:
        await pending.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError:
        fallback_text, fallback_keyboard = tron_result_view(
            result, query_count=query_count,
            bot_username=str(context.application.bot_data.get("bot_username") or ""),
        )
        try:
            await pending.edit_text(
                fallback_text, parse_mode=ParseMode.HTML,
                reply_markup=fallback_keyboard, disable_web_page_preview=True,
            )
        except TelegramError:
            await message.reply_text(
                fallback_text, parse_mode=ParseMode.HTML,
                reply_markup=fallback_keyboard, disable_web_page_preview=True,
            )


def tron_result_view(
    result: TronBalance,
    transaction: TronTransaction | None = None,
    asset_override: str = "",
    heading: str = "🔍 查询结果",
    notice: str = "",
    emoji_ids: dict[str, str] | None = None,
    direction_emoji_ids: dict[str, str] | None = None,
    status_emoji_ids: dict[str, str] | None = None,
    query_count: int = 0,
    bot_username: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    transaction = transaction or (result.transactions[0] if result.transactions else None)
    asset = (asset_override or (transaction.asset if transaction else "USDT")).upper()
    emoji_ids = emoji_ids or {}
    usdt_icon = (
        f'<tg-emoji emoji-id="{emoji_ids["USDT"]}">🟢</tg-emoji>'
        if emoji_ids.get("USDT") else "🟢"
    )
    trx_icon = (
        f'<tg-emoji emoji-id="{emoji_ids["TRX"]}">🔴</tg-emoji>'
        if emoji_ids.get("TRX") else "🔴"
    )
    asset_icon = usdt_icon if asset == "USDT" else trx_icon
    direction_emoji_ids = direction_emoji_ids or {}
    incoming_icon = (
        f'<tg-emoji emoji-id="{direction_emoji_ids["in"]}">➕</tg-emoji>'
        if direction_emoji_ids.get("in") else "➕"
    )
    outgoing_icon = (
        f'<tg-emoji emoji-id="{direction_emoji_ids["out"]}">⛔</tg-emoji>'
        if direction_emoji_ids.get("out") else "⛔"
    )
    status_emoji_ids = status_emoji_ids or {}

    def status_icon(key: str, fallback: str) -> str:
        emoji_id = status_emoji_ids.get(key, "")
        return (
            f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
            if emoji_id else fallback
        )

    def resource_icon(value: int, required: int) -> str:
        if value >= required:
            return status_icon("free_bandwidth", "🟢")
        if value > 0:
            return status_icon("bandwidth", "🟡")
        return status_icon("energy", "🔴")

    lines = [
        heading,
        "地址：",
        f"<code>{html.escape(result.address)}</code>",
        "",
        f"{usdt_icon} USDT：<b>{result.usdt:,.2f}</b>",
        f"{trx_icon} TRX：<b>{result.trx:,.2f}</b>",
        "",
        f"{resource_icon(result.energy_remaining, 65_000)} 能量剩余：{result.energy_remaining:,}",
        f"{resource_icon(result.bandwidth_remaining, 300)} 带宽剩余：{result.bandwidth_remaining:,}",
        f"{resource_icon(result.free_bandwidth_remaining, 300)} 免费带宽：{result.free_bandwidth_remaining:,}",
        "",
        (
            f"冻结状态：{status_icon('negative', '🔴')} 已冻结"
            if result.is_frozen is True else
            f"冻结状态：{status_icon('positive', '🟢')} 未冻结"
            if result.is_frozen is False else
            "冻结状态：🟡 查询失败"
        ),
        (
            "安全状态："
            + (
                f"{status_icon('negative', '🔴')} 已多签"
                if result.is_multisig is True else
                f"{status_icon('positive', '🟢')} 无多签"
                if result.is_multisig is False else "🟡 多签未知"
            )
            + "  "
            + (
                f"{status_icon('negative', '🔴')} 已授权"
                if result.has_authorization is True else
                f"{status_icon('positive', '🟢')} 无授权"
                if result.has_authorization is False else "🟡 授权未知"
            )
        ),
        "",
        f"📅 注册时间：{format_beijing_timestamp_ms(result.created_at_ms)}",
    ]
    if transaction and heading.startswith("⏰"):
        incoming = transaction.direction == "转入"
        direction = "收入" if incoming else "支出" if transaction.direction == "转出" else "相关"
        sign = "+" if incoming else "-" if transaction.direction == "转出" else ""
        direction_icon = incoming_icon if incoming else outgoing_icon
        lines.extend(["",
            f"{direction_icon} {direction}: {sign}{transaction.amount:,.6f} {transaction.asset}",
            f"{trx_icon} TRON",
            f"{'来自' if incoming else '发往'}: <code>{html.escape(transaction.counterparty or '未知')}</code>",
            f"{asset_icon} 区块: {transaction.block_number or '数据源未提供'}",
            f"🕒 时间: {format_beijing_timestamp_ms(transaction.timestamp_ms)}",
        ])
    if notice:
        lines.extend(["", notice])
    lines.extend(["", f"累计查询 {query_count:,} 次"])
    keyboard_rows = [
        [
            InlineKeyboardButton(
                "🔍 查询最近10条交易", callback_data=f"tronrecent:{result.address}"
            ),
            InlineKeyboardButton(
                "🔊 监控此地址", callback_data=f"tronmonitor:address:{result.address}"
            ),
        ],
        [
            custom_emoji_callback_button(
                "USDT记录", f"tronrecords:USDT:all:7:0:{result.address}",
                emoji_ids.get("USDT", ""), "🟢",
            ),
            custom_emoji_callback_button(
                "TRX记录", f"tronrecords:TRX:all:7:0:{result.address}",
                emoji_ids.get("TRX", ""), "🔴",
            ),
        ],
    ]
    if bot_username:
        keyboard_rows.append([InlineKeyboardButton(
            "👥 加群查询", url=f"https://t.me/{bot_username}?startgroup=true"
        )])
    keyboard = InlineKeyboardMarkup(keyboard_rows)
    return "\n".join(lines), keyboard


def custom_emoji_callback_button(
    text: str, callback_data: str, emoji_id: str = "", fallback: str = "",
) -> InlineKeyboardButton:
    if emoji_id:
        return InlineKeyboardButton(
            text, callback_data=callback_data,
            api_kwargs={"icon_custom_emoji_id": emoji_id},
        )
    return InlineKeyboardButton(
        f"{fallback} {text}".strip(), callback_data=callback_data
    )


def tron_forward_keyboard(address: str, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔍 查询最近10条交易",
                url=f"https://t.me/{bot_username}?start=tr10_{address}",
            ),
            InlineKeyboardButton(
                "🔊 监控此地址",
                url=f"https://t.me/{bot_username}?start=trmon_{address}",
            ),
        ],
        [InlineKeyboardButton(
            "👥 加群查询", url=f"https://t.me/{bot_username}?startgroup=true"
        )],
    ])


def tron_recent_view(
    result: TronBalance,
    emoji_ids: dict[str, str] | None = None,
    direction_emoji_ids: dict[str, str] | None = None,
    query_count: int = 0,
    bot_username: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    emoji_ids = emoji_ids or {}
    direction_emoji_ids = direction_emoji_ids or {}
    rows = sorted(
        result.transactions, key=lambda item: item.timestamp_ms, reverse=True
    )[:10]
    lines = ["🔍 最近10条交易", f"地址：<code>{html.escape(result.address)}</code>", ""]
    for item in rows:
        incoming = item.direction == "转入"
        direction_key = "in" if incoming else "out"
        fallback = "➕" if incoming else "⛔"
        custom_id = direction_emoji_ids.get(direction_key, "")
        icon = (
            f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'
            if custom_id else fallback
        )
        sign = "+" if incoming else "-"
        counterparty = item.counterparty or "未知"
        short_address = (
            f"{counterparty[:7]}...{counterparty[-7:]}"
            if len(counterparty) > 18 else counterparty
        )
        lines.extend([
            f"{icon} <b>{sign}{format_tron_amount(item.amount)} {item.asset}</b>",
            "<i>──</i>",
            f"<code>{'来自' if incoming else '转至'}：</code>"
            f"<a href=\"https://tronscan.org/#/transaction/{item.tx_id}\">"
            f"{html.escape(short_address)}</a>",
            f"<code>{format_beijing_timestamp_ms(item.timestamp_ms)}</code>", "",
        ])
    if not rows:
        lines.append("暂无已确认的 TRX/USDT 交易。")
    lines.append(f"累计查询 {query_count:,} 次")
    keyboard_rows = [[
        custom_emoji_callback_button(
            "USDT记录", f"tronrecords:USDT:all:7:0:{result.address}",
            emoji_ids.get("USDT", ""), "🟢",
        ),
        custom_emoji_callback_button(
            "TRX记录", f"tronrecords:TRX:all:7:0:{result.address}",
            emoji_ids.get("TRX", ""), "🔴",
        ),
    ]]
    if bot_username:
        keyboard_rows.append([InlineKeyboardButton(
            "👥 加群查询", url=f"https://t.me/{bot_username}?startgroup=true"
        )])
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回地址查询", callback_data=f"tronback:{result.address}")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def tron_records_view(
    address: str, transactions: tuple[TronTransaction, ...], asset: str,
    direction: str, days: int, page: int,
    emoji_ids: dict[str, str] | None = None,
    direction_emoji_ids: dict[str, str] | None = None,
    query_count: int = 0,
    bot_username: str = "",
    limit_notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    period_transactions = [
        item for item in transactions
        if item.asset == asset and item.amount >= MIN_HISTORY_TRANSFER
    ]
    if days:
        cutoff = now_ms - days * 86400 * 1000
        period_transactions = [
            item for item in period_transactions if item.timestamp_ms >= cutoff
        ]
    incoming_rows = [item for item in period_transactions if item.direction == "转入"]
    outgoing_rows = [item for item in period_transactions if item.direction == "转出"]
    incoming_total = sum((item.amount for item in incoming_rows), Decimal(0))
    outgoing_total = sum((item.amount for item in outgoing_rows), Decimal(0))
    net_total = incoming_total - outgoing_total
    filtered = period_transactions
    if direction == "in":
        filtered = [item for item in filtered if item.direction == "转入"]
    elif direction == "out":
        filtered = [item for item in filtered if item.direction == "转出"]
    page_size = 10
    pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    current = filtered[page * page_size:(page + 1) * page_size]
    labels = {"all": "全部", "in": "仅收入", "out": "仅支出"}
    fallback_icon = "🟢" if asset == "USDT" else "🔴"
    custom_id = (emoji_ids or {}).get(asset, "")
    asset_icon = (
        f'<tg-emoji emoji-id="{custom_id}">{fallback_icon}</tg-emoji>'
        if custom_id else fallback_icon
    )
    direction_emoji_ids = direction_emoji_ids or {}
    income_icon = (
        f'<tg-emoji emoji-id="{direction_emoji_ids["in"]}">➕</tg-emoji>'
        if direction_emoji_ids.get("in") else "➕"
    )
    expense_icon = (
        f'<tg-emoji emoji-id="{direction_emoji_ids["out"]}">⛔</tg-emoji>'
        if direction_emoji_ids.get("out") else "⛔"
    )
    lines = [
        f"{asset_icon} 💳 <b>{asset} 交易记录</b>", "",
        "🔎 <b>查询结果</b>",
        f"地址：<code>{html.escape(address)}</code>", "",
        f"🕒{'全部时间' if not days else f'近{days}天'}交易记录，下方按钮选择更长时间", "",
        f"{income_icon}收入：<b>+{format_tron_amount(incoming_total)} {asset}</b>（{len(incoming_rows)}笔）",
        f"{expense_icon}支出：<b>-{format_tron_amount(outgoing_total)} {asset}</b>（{len(outgoing_rows)}笔）",
        f"🟡净额：<b>{'+' if net_total > 0 else ''}{format_tron_amount(net_total)} {asset}</b>",
        f"筛选：{labels[direction]}", "",
    ]
    if limit_notice:
        lines.extend([f"⚠️ {html.escape(limit_notice)}", ""])
    for item in current:
        incoming = item.direction == "转入"
        sign = "+" if incoming else "-" if item.direction == "转出" else ""
        direction_id = direction_emoji_ids.get("in" if incoming else "out", "")
        direction_fallback = "➕" if incoming else "⛔"
        direction_icon = (
            f'<tg-emoji emoji-id="{direction_id}">{direction_fallback}</tg-emoji>'
            if direction_id else direction_fallback
        )
        counterparty = item.counterparty
        short_address = (
            f"{counterparty[:7]}...{counterparty[-7:]}" if len(counterparty) > 18 else counterparty
        )
        transaction_url = f"https://tronscan.org/#/transaction/{item.tx_id}"
        lines.extend([
            f"{direction_icon} <b>{sign}{format_tron_amount(item.amount)} {asset}</b>",
            "<i>──</i>",
            f"<code>{'来自' if incoming else '发往'}：</code>"
            f"<a href=\"{transaction_url}\">{html.escape(short_address or '未知')}</a>",
            f"<code>{format_beijing_timestamp_ms(item.timestamp_ms)}</code>", "",
        ])
    if not current:
        lines.append("这个筛选条件下暂无交易记录。")
    lines.extend([
        f"<i>共 {len(filtered)} 笔，第 {page + 1}/{pages} 页</i>",
        f"累计查询 {query_count:,} 次",
    ])

    def callback(new_direction: str, new_days: int, new_page: int = 0) -> str:
        return f"tronrecords:{asset}:{new_direction}:{new_days}:{new_page}:{address}"

    keyboard_rows = [
        [
            InlineKeyboardButton("✅ 全部" if direction == "all" else "全部", callback_data=callback("all", days)),
            InlineKeyboardButton("✅ 仅收入" if direction == "in" else "仅收入", callback_data=callback("in", days)),
            InlineKeyboardButton("✅ 仅支出" if direction == "out" else "仅支出", callback_data=callback("out", days)),
        ],
        [
            InlineKeyboardButton("✅ 近7天" if days == 7 else "近7天", callback_data=callback(direction, 7)),
            InlineKeyboardButton("✅ 近30天" if days == 30 else "近30天", callback_data=callback(direction, 30)),
            InlineKeyboardButton("✅ 近90天" if days == 90 else "近90天", callback_data=callback(direction, 90)),
        ],
    ]
    page_buttons = []
    if page:
        page_buttons.append(InlineKeyboardButton("上一页", callback_data=callback(direction, days, page - 1)))
    if page + 1 < pages:
        page_buttons.append(InlineKeyboardButton("下一页", callback_data=callback(direction, days, page + 1)))
    if page_buttons:
        keyboard_rows.append(page_buttons)
    if bot_username:
        keyboard_rows.append([InlineKeyboardButton(
            "👥 加群查询", url=f"https://t.me/{bot_username}?startgroup=true"
        )])
    keyboard_rows.append([InlineKeyboardButton("⬅️ 返回地址查询", callback_data=f"tronback:{address}")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def without_custom_emoji(text: str) -> str:
    return re.sub(r'<tg-emoji emoji-id="[^"]+">(.*?)</tg-emoji>', r"\1", text)


def format_tron_amount(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    integer, dot, fraction = normalized.partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    grouped = f"{int(integer or '0'):,}"
    return f"{sign}{grouped}{dot}{fraction}"


def sqlite_utc_timestamp_ms(value: object) -> int:
    try:
        parsed = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    return int(parsed.replace(tzinfo=timezone.utc).timestamp() * 1000)


TRON_BALANCE_UNAVAILABLE = "余额获取中/暂不可用"


def tron_monitor_alert_view(
    result: TronBalance | None,
    transaction: TronTransaction | None,
    monitor_id: int,
    notice: str,
    emoji_ids: dict[str, str] | None = None,
    direction_emoji_ids: dict[str, str] | None = None,
    address: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """Render a monitor alert.

    ``result`` must be a balance verified for exactly ``address``; pass None
    when it could not be verified and 「余额获取中/暂不可用」 is shown instead.
    """
    address = address or (result.address if result else "")
    if result is not None and address and result.address != address:
        result = None  # 永不显示其他地址的余额
    emoji_ids = emoji_ids or {}
    direction_emoji_ids = direction_emoji_ids or {}

    def custom_icon(asset: str, fallback: str) -> str:
        emoji_id = emoji_ids.get(asset, "")
        return (
            f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
            if emoji_id else fallback
        )

    usdt_icon = custom_icon("USDT", "🟢")
    trx_icon = custom_icon("TRX", "🔴")
    lines = [
        f"⏰ 波场监控 #{monitor_id}",
        html.escape(notice),
        "",
    ]
    if address:
        lines.append(f"📍 监控地址：<code>{html.escape(address)}</code>")
    if result is not None:
        lines.extend([
            f"{usdt_icon} USDT余额：<b>{format_tron_amount(result.usdt)}</b>",
            f"{trx_icon} TRX余额：<b>{format_tron_amount(result.trx)}</b>",
        ])
    else:
        lines.extend([
            f"{usdt_icon} USDT余额：<b>{TRON_BALANCE_UNAVAILABLE}</b>",
            f"{trx_icon} TRX余额：<b>{TRON_BALANCE_UNAVAILABLE}</b>",
        ])
    if transaction:
        incoming = transaction.direction == "转入"
        direction_id = direction_emoji_ids.get("in" if incoming else "out", "")
        direction_fallback = "➕" if incoming else "⛔"
        direction_icon = (
            f'<tg-emoji emoji-id="{direction_id}">{direction_fallback}</tg-emoji>'
            if direction_id else direction_fallback
        )
        sign = "+" if incoming else "-"
        relation = "来自" if incoming else "发往"
        asset_icon = usdt_icon if transaction.asset == "USDT" else trx_icon
        lines.extend([
            "",
            f"{direction_icon} <b>{sign}{format_tron_amount(transaction.amount)} "
            f"{transaction.asset}</b>",
            f"{relation}：<code>{html.escape(transaction.counterparty or '未知')}</code>",
            f"{asset_icon} 区块：{transaction.block_number or '确认中'}",
            f"🕒 时间：{format_beijing_timestamp_ms(transaction.timestamp_ms)}",
        ])
    keyboard = InlineKeyboardMarkup([[
        custom_emoji_callback_button(
            "USDT记录", f"tronrecords:USDT:all:7:0:{address}",
            emoji_ids.get("USDT", ""), "🟢",
        ),
        custom_emoji_callback_button(
            "TRX记录", f"tronrecords:TRX:all:7:0:{address}",
            emoji_ids.get("TRX", ""), "🔴",
        ),
    ]])
    return "\n".join(lines), keyboard


def tron_monitor_asset_keyboard(
    emoji_ids: dict[str, str] | None = None,
) -> InlineKeyboardMarkup:
    emoji_ids = emoji_ids or {}
    return InlineKeyboardMarkup([[
        custom_emoji_callback_button(
            "监控USDT", "tronmonitor:asset:usdt", emoji_ids.get("USDT", ""), "🟢"
        ),
        custom_emoji_callback_button(
            "监控TRX", "tronmonitor:asset:trx", emoji_ids.get("TRX", ""), "🔴"
        ),
    ], [
        InlineKeyboardButton(
            "🟢🔴 监控USDT+TRX交易播报",
            callback_data="tronmonitor:asset:both",
        ),
    ]])


async def tron_monitor_asset_prompt(
    context: ContextTypes.DEFAULT_TYPE, address: str,
) -> str:
    try:
        count = await asyncio.wait_for(
            context.application.bot_data["chain"].tron_transaction_count(
                address, "both", 365, 10_000
            ),
            timeout=25,
        )
        high_volume = count > 10_000
        count_text = "超过 10,000" if high_volume else f"{count:,}"
        context.user_data["tron_monitor_recent_count"] = count
    except (TimeoutError, ValueError, ChainQueryError):
        high_volume = False
        count_text = "暂时无法统计"
        context.user_data.pop("tron_monitor_recent_count", None)
    # 扫块监控：交易量大的地址（交易所/热钱包）也可以监控，不再拦截
    context.user_data["tron_monitor_volume_blocked"] = False
    extra = (
        "交易次数暂时无法统计，仍可开启监控。\n\n"
        if count_text == "暂时无法统计" else
        "该地址交易频繁（疑似交易所或热钱包），播报会比较密集。\n\n"
        if high_volume else ""
    )
    return (
        "📡 <b>选择监控币种</b>\n\n"
        f"地址：<code>{html.escape(address)}</code>\n"
        f"近一年交易次数：<b>{count_text}</b>\n\n"
        f"{extra}请选择要监控的币种："
    )


def tron_monitor_prompt_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
) -> InlineKeyboardMarkup | None:
    if context.user_data.get("tron_monitor_volume_blocked"):
        return None
    return tron_monitor_asset_keyboard(
        context.application.bot_data.get("kkpay_emoji_ids")
    )


def tron_monitor_setup_view(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, InlineKeyboardMarkup]:
    asset = str(context.user_data.get("tron_monitor_asset") or "")
    low = str(context.user_data.get("tron_monitor_low") or "")
    high = str(context.user_data.get("tron_monitor_high") or "")
    notify = bool(context.user_data.get("tron_monitor_notify", True))
    minimum = str(context.user_data.get("tron_monitor_minimum") or "0.1")
    delete_days = int(context.user_data.get("tron_monitor_delete_days", 7))
    name = "USDT+TRX" if asset == "both" else asset.upper()
    lines = [f"⏰ 设置{name}监控", ""]
    if asset != "both":
        lines.extend([
            f"小于安全余额播报：{low or '关闭'}",
            f"大于安全余额播报：{high or '关闭'}",
        ])
    lines.extend([
        f"交易变动播报：{'开启' if notify else '关闭'}",
        f"最小交易播报金额：{minimum if notify else '-'} {name}",
        f"提醒消息撤回：{delete_days}天" if delete_days else "提醒消息撤回：关闭",
    ])
    buttons: list[list[InlineKeyboardButton]] = []
    if asset != "both":
        buttons.extend([
            [InlineKeyboardButton("⬇️ 小于安全余额播报", callback_data="tronmonitor:set:low")],
            [InlineKeyboardButton("⬆️ 大于安全余额播报", callback_data="tronmonitor:set:high")],
        ])
    buttons.extend([
        [InlineKeyboardButton("🔔 交易变动播报", callback_data="tronmonitor:set:transfer")],
        [InlineKeyboardButton("🕒 提醒撤回时间", callback_data="tronmonitor:set:delete")],
        [InlineKeyboardButton("✅ 保存并开启", callback_data="tronmonitor:save")],
        [InlineKeyboardButton("⬅️ 重选监控类型", callback_data="tronmonitor:add")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def tron_monitor_menu(
    store: DirectoryStore, owner_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    rows = store.list_tron_monitors(owner_id)
    active = [row for row in rows if row["is_enabled"]]
    page_size = 10
    page_count = max(1, (len(active) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    page_rows = active[page * page_size:(page + 1) * page_size]
    lines = [
        "⏰ 波场地址监控", "",
        "监控会持续开启；提醒消息可设置若干天后自动撤回。", "",
    ]
    if active:
        lines.append(f"第 {page + 1}/{page_count} 页 · 每页10条")
        for row in page_rows:
            thresholds = []
            if row["low_balance"]:
                thresholds.append(f"低于 {row['low_balance']}")
            if row["high_balance"]:
                thresholds.append(f"高于 {row['high_balance']}")
            delete_label = (
                f"{row['notification_delete_days']}天"
                if int(row["notification_delete_days"]) else "不自动撤回"
            )
            transfer_label = "交易播报开" if row["notify_transfers"] else "交易播报关"
            if row["notify_transfers"]:
                transfer_label += f"(≥{row['min_transfer_amount']})"
            lines.append(
                f"#{row['id']} · {str(row['asset']).upper()} · "
                f"{str(row['address'])[:8]}...{str(row['address'])[-6:]} · "
                f"{transfer_label} · {' / '.join(thresholds) or '无余额阈值'} · "
                f"消息撤回 {delete_label}"
            )
    else:
        lines.append("当前没有启用的地址监控。")
    buttons = [[InlineKeyboardButton("➕ 添加监控", callback_data="tronmonitor:add")]]
    buttons.extend([
        [InlineKeyboardButton(f"⛔ 关闭 #{row['id']}", callback_data=f"tronmonitor:off:{row['id']}")]
        for row in page_rows
    ])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"tronmonitor:page:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"tronmonitor:page:{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回搜索服务", callback_data="nav:search")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def tron_monitor_stats_view(
    store: DirectoryStore, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    summary = store.tron_monitor_stats()
    _, total_users = store.tron_monitor_users(1, 0)
    page_count = max(1, (total_users + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows, _ = store.tron_monitor_users(page_size, page * page_size)
    lines = [
        "⏰ 波场监控统计", "",
        f"正在监控地址：{summary['addresses']} 个",
        f"使用人员：{summary['users']} 人", "",
        "使用详情（按地址数从多到少）：",
    ]
    for position, row in enumerate(rows, page * page_size + 1):
        user_id = int(row["owner_id"])
        name = str(row["display_name"] or user_id)
        username = f" @{html.escape(str(row['username']))}" if row["username"] else ""
        lines.append(
            f"{position}. {telegram_user_link(user_id, name)}{username} "
            f"（ID {user_id}）· {row['address_count']} 个地址"
        )
    if not rows:
        lines.append("暂无启用中的地址监控。")
    lines.append(f"\n第 {page + 1}/{page_count} 页")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"admin:tronmonitors:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"admin:tronmonitors:{page + 1}"
        ))
    buttons = [nav] if nav else []
    buttons.extend([
        [InlineKeyboardButton(
            "🔎 查询使用人员地址", callback_data="admin:tronmonitors:query"
        )],
        [InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def tron_monitor_user_view(
    store: DirectoryStore, owner,
) -> tuple[str, InlineKeyboardMarkup]:
    owner_id = int(owner["owner_id"])
    rows = [row for row in store.list_tron_monitors(owner_id) if row["is_enabled"]]
    name = str(owner["display_name"] or owner_id)
    username = f" @{html.escape(str(owner['username']))}" if owner["username"] else ""
    lines = [
        "🔎 使用人员监控地址", "",
        f"用户：{telegram_user_link(owner_id, name)}{username}",
        f"数字ID：<code>{owner_id}</code>",
        f"启用地址：{len({str(row['address']) for row in rows})} 个", "",
    ]
    for row in rows:
        lines.extend([
            f"#{row['id']} · {str(row['asset']).upper()}",
            f"<code>{html.escape(str(row['address']))}</code>",
            f"交易播报：{'开启' if row['notify_transfers'] else '关闭'}"
            + (f"（≥{row['min_transfer_amount']}）" if row["notify_transfers"] else ""),
            f"余额阈值：低于 {row['low_balance'] or '关闭'} / 高于 {row['high_balance'] or '关闭'}",
            f"最近检查：{format_beijing_time(row['last_checked_at'])}", "",
        ])
    return "\n".join(lines), InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 继续查询", callback_data="admin:tronmonitors:query")],
        [InlineKeyboardButton("⬅️ 返回监控统计", callback_data="admin:tronmonitors:0")],
    ])


def raffle_delete_view(
    store: DirectoryStore, chat_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    total = store.raffle_count(chat_id)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = store.list_raffles(chat_id, page_size, page * page_size)
    type_labels = {
        "universal": "通用抽奖", "activity_rank": "活跃排名抽奖",
        "activity_random": "活跃达标抽奖",
    }
    lines = ["🗑 删除抽奖", "", "请选择要删除的抽奖："]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        raffle_type = type_labels.get(str(row["raffle_type"]), str(row["raffle_type"]))
        lines.append(
            f"#{row['id']} · {raffle_type} · {row['prize']} · {row['status']}"
        )
        buttons.append([InlineKeyboardButton(
            f"删除 #{row['id']} · {raffle_type}",
            callback_data=f"raffledelete:item:{row['id']}:{page}",
        )])
    if not rows:
        lines.append("当前群还没有抽奖记录。")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"raffledelete:menu:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"raffledelete:menu:{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回抽奖类型", callback_data="group:raffles")])
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 个")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def raffle_edit_view(
    store: DirectoryStore, chat_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """List active universal raffles for the edit wizard."""
    page_size = 10
    rows = [
        row for row in store.active_raffles(chat_id, limit=100)
        if str(row["raffle_type"] or "") == "universal"
    ]
    total = len(rows)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    page_rows = rows[page * page_size:(page + 1) * page_size]
    lines = ["✏️ 修改抽奖", "", "请选择要修改的进行中抽奖（样板/通用）："]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in page_rows:
        title = str(row["title"] or "").strip() or str(row["prize"] or "")
        title = title[:28]
        lines.append(
            f"#{row['id']} · {title} · {row['status']} · {row['entries']}人"
        )
        buttons.append([InlineKeyboardButton(
            f"修改 #{row['id']} · {title}",
            callback_data=f"raffleedit:item:{row['id']}:{page}",
        )])
    if not page_rows:
        lines.append("当前没有进行中的通用抽奖可修改。")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"raffleedit:menu:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"raffleedit:menu:{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回抽奖类型", callback_data="group:raffles")])
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 个")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


DICE_BET_SIDES = ("大", "小", "单", "双")
DICE_BET_MAX_AMOUNT = 100000


def is_dice_command(points_config, message) -> bool:
    """本群骰子可用（积分+骰子都开启）且消息是骰子口令。"""
    return bool(
        points_config is not None
        and points_config["is_enabled"] and points_config["dice_enabled"]
        and parse_dice_bet(getattr(message, "text", None) or "")
    )


def parse_dice_bet(text: str) -> tuple[str, Decimal] | None:
    """Parse pure group dice bets like 大3 / 小 5 / 单：10 / 双-2 / 大1.5."""
    raw = (text or "").strip()
    match = re.fullmatch(
        r"([大小单双])\s*[：:\-－]?\s*(\d+(?:\.\d{1,2})?)", raw
    )
    if not match:
        return None
    try:
        amount = normalize_points(match.group(2))
    except ValueError:
        return None
    if amount < normalize_points("0.01") or amount > DICE_BET_MAX_AMOUNT:
        return None
    return match.group(1), amount


def parse_dice_toggle_keyword(text: str) -> bool | None:
    """Return True/False for 开启骰子/关闭骰子 keywords, else None."""
    normalized = " ".join((text or "").strip().split())
    if normalized == "开启骰子":
        return True
    if normalized == "关闭骰子":
        return False
    return None


def can_toggle_group_dice(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
) -> bool:
    """Same gate as the points-menu dice on/off buttons: points permission or super."""
    return has_group_permission(context, chat_id, user_id, "points")


def invite_member_overview(
    store: DirectoryStore, chat_id: int, user_id: int,
) -> str:
    """Personal invite link + stats text for keyword / admin queries."""
    owner = store.find_group_user(chat_id, str(user_id))
    username = str(owner["username"] or "") if owner else ""
    display = (
        str(owner["display_name"] or username or user_id) if owner else str(user_id)
    )
    label = f"@{username}" if username else f"ID {user_id}"
    row = store.active_invite_link(chat_id, user_id)
    stats = store.invite_stats(chat_id, user_id)
    invites = int(stats["invites"])
    exits = int(stats["exits"])
    remaining = invites - exits
    account = store.point_account(chat_id, user_id)
    points = format_points(account["balance"]) if account else "0"
    if row:
        link_line = str(row["invite_link"])
    else:
        link_line = "尚未生成专属邀请链接（可让对方在群内发送 /link）"
    return (
        f"🔗 邀请链接查询 · {display}（{label}）\n"
        f"{link_line}\n\n"
        f"有效邀请人数：{invites}\n"
        f"已退出人数：{exits}\n"
        f"仍在群内：{remaining}\n"
        f"当前积分：{points}\n"
        f"近6个月链接数：{int(stats['links'])}\n"
        "只有成员第一次进群会计数。"
    )


async def apply_group_dice_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool,
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply_text("请在群内开启或关闭骰子。")
        return
    if not can_toggle_group_dice(context, chat.id, user.id):
        await message.reply_text("你没有开启/关闭骰子的权限。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    store.set_dice_enabled(chat.id, enabled, user.id)
    store.audit(
        f"tg:{user.id}",
        "points.diceon" if enabled else "points.diceoff",
        str(chat.id),
        "keyword",
    )
    store.record_group_operation(
        chat.id, "setting", user.id, user.username or "",
        user.full_name or "", "开启骰子游戏" if enabled else "关闭骰子游戏",
        user.id, user.username or "", user.full_name or "",
    )
    await message.reply_text("骰子游戏已开启。" if enabled else "骰子游戏已关闭。")


async def group_voice_dice_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if (
        not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not user or not message or not message.voice
    ):
        return
    if not can_toggle_group_dice(context, chat.id, user.id):
        return
    await message.reply_text(
        "语音快捷：请选择开启或关闭本群骰子游戏。",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 开启骰子", callback_data="groupdice:on"),
            InlineKeyboardButton("⛔ 关闭骰子", callback_data="groupdice:off"),
        ]]),
    )


async def handle_invite_member_query_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
) -> bool:
    """Resolve invite_member_query follow-up; True if consumed."""
    if context.user_data.get("menu_mode") != "invite_member_query":
        return False
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message:
        return False
    store: DirectoryStore = context.application.bot_data["store"]
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat_id = chat.id
    else:
        chat_id = callback_group_id(context, chat)
        if chat_id is None:
            context.user_data.pop("menu_mode", None)
            await message.reply_text("请先在群组管理中选择群组，或到群内查询。")
            return True
    config = store.invite_config(chat_id)
    if not config["is_enabled"]:
        context.user_data.pop("menu_mode", None)
        await message.reply_text("本群尚未开启个人邀请链接功能。")
        return True
    try:
        target_id, target_username, target_name = resolve_group_user_target(
            store, chat_id, message, text
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return True
    can_query_others = has_group_permission(context, chat_id, user.id, "invite")
    if target_id != user.id and not can_query_others:
        context.user_data.pop("menu_mode", None)
        await message.reply_text("只能查询自己的邀请记录。")
        return True
    context.user_data.pop("menu_mode", None)
    overview = invite_member_overview(store, chat_id, target_id)
    keyboard = None
    row = store.active_invite_link(chat_id, target_id)
    if row:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "复制链接", copy_text=CopyTextButton(str(row["invite_link"]))
            )
        ]])
    await message.reply_text(
        overview, disable_web_page_preview=True, reply_markup=keyboard,
    )
    return True


def parse_points_amount(text: str, *, allow_zero: bool = False) -> Decimal:
    """Parse a non-negative points amount (optionally 0)."""
    amount = normalize_points((text or "").strip())
    if amount < 0:
        raise ValueError("积分不能为负数")
    if amount == 0 and not allow_zero:
        raise ValueError("积分必须大于0")
    return amount


def parse_points_delta(text: str) -> Decimal:
    """Parse signed points delta like +1.5 / -0.5 / 2."""
    raw = (text or "").strip().replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", raw):
        raise ValueError("积分数量格式无效")
    delta = normalize_points(raw)
    if delta == 0:
        raise ValueError("积分增减不能为0")
    return delta


def dice_side_matched(side: str, value: int) -> bool:
    if side == "大":
        return value in (4, 5, 6)
    if side == "小":
        return value in (1, 2, 3)
    if side == "单":
        return value in (1, 3, 5)
    if side == "双":
        return value in (2, 4, 6)
    return False


def dice_value_tags(value: int) -> str:
    size = "大" if value >= 4 else "小"
    parity = "单" if value % 2 else "双"
    return f"{size}{parity}"


def dice_schedule_is_open(config, current_time: str | None = None) -> bool:
    if not config["dice_schedule_enabled"]:
        return True
    current = current_time or beijing_now().strftime("%H:%M")
    opens = str(config["dice_open_time"] or "00:00")
    closes = str(config["dice_close_time"] or "23:59")
    if opens == closes:
        return True
    if opens < closes:
        return opens <= current < closes
    return current >= opens or current < closes


def parse_dice_odds_input(text: str) -> int:
    """Parse dice odds as thousandths: 1950 or 1.95 / 1,95 → 1950."""
    raw = (text or "").strip().replace(",", ".")
    prompt = "请发送骰子赔率（1.7-2.0，也可写 1700-2000；例如 1.95，押1000中奖反1950）"
    if not raw:
        raise ValueError(prompt)
    if re.fullmatch(r"\d+", raw):
        odds = int(raw)
    else:
        try:
            odds = int(round(float(raw) * 1000))
        except ValueError as exc:
            raise ValueError(prompt) from exc
    if not 1700 <= odds <= 2000:
        raise ValueError("骰子赔率范围为1.7-2.0（也可写 1700-2000；例如 1.95）")
    return odds


def parse_group_poll_options(text: str) -> list[str]:
    options = [item.strip() for item in text.splitlines() if item.strip()]
    unique: list[str] = []
    for item in options:
        if item not in unique:
            unique.append(item)
    if not 2 <= len(unique) <= 10:
        raise ValueError("请发送 2-10 个不同选项，每行一个")
    if any(len(item) > 100 for item in unique):
        raise ValueError("每个选项最多 100 个字符")
    return unique


def group_poll_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 发起投票", callback_data="grouppoll:create")],
        [InlineKeyboardButton("🗑 删除群投票", callback_data="grouppoll:delete:menu:0")],
        [InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:groupmenu")],
    ])


def group_poll_delete_view(
    store: DirectoryStore, chat_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    total = store.group_poll_count(chat_id)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = store.list_group_polls(chat_id, page_size, page * page_size)
    lines = ["🗑 删除群投票", "", "请选择要删除的投票："]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        question = str(row["question"] or "")
        if len(question) > 28:
            question = question[:28] + "…"
        lines.append(f"#{row['id']} · {question}")
        buttons.append([InlineKeyboardButton(
            f"删除 #{row['id']} · {question}",
            callback_data=f"grouppoll:delete:item:{row['id']}:{page}",
        )])
    if not rows:
        lines.append("当前群还没有机器人发起的投票。")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"grouppoll:delete:menu:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"grouppoll:delete:menu:{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回群投票", callback_data="group:polls")])
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 个")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _tron_monitor_lock(context: ContextTypes.DEFAULT_TYPE, monitor_id: int) -> asyncio.Lock:
    locks = context.application.bot_data.setdefault("tron_monitor_locks", {})
    lock = locks.get(monitor_id)
    if lock is None:
        lock = locks[monitor_id] = asyncio.Lock()
    return lock


async def poll_one_tron_monitor(
    context: ContextTypes.DEFAULT_TYPE, monitor, semaphore: asyncio.Semaphore,
    scanned: list[TronTransaction] | None = None, scanned_block: int = 0,
    scanner_active: bool = False,
) -> None:
    """Process one monitor.

    ``scanned`` = transactions delivered by the block scanner (instant path):
    no transaction-history request, one fresh verified balance. Without it
    this is the slow reconciliation pass (balance + incremental history).
    """
    store: DirectoryStore = context.application.bot_data["store"]
    monitor_id = int(monitor["id"])
    async with semaphore, _tron_monitor_lock(context, monitor_id):
        reloaded = getattr(store, "tron_monitor_by_id", None)
        if callable(reloaded):
            try:
                latest_row = reloaded(monitor_id)
            except Exception:
                latest_row = None
            if latest_row is not None and not isinstance(latest_row, (bool, int, str)):
                if not int(latest_row["is_enabled"] or 0):
                    return
                monitor = latest_row
        await _poll_one_tron_monitor_locked(
            context, monitor, scanned, scanned_block, scanner_active,
        )


async def _poll_one_tron_monitor_locked(
    context: ContextTypes.DEFAULT_TYPE, monitor,
    scanned: list[TronTransaction] | None, scanned_block: int,
    scanner_active: bool = False,
) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    chain: ChainService = context.application.bot_data["chain"]
    if True:
        monitor_id = int(monitor["id"])
        poll_started = datetime.now(timezone.utc)
        checked_at = poll_started.strftime("%Y-%m-%d %H:%M:%S")
        try:
            previous_seen = list(json.loads(str(monitor["seen_tx_ids"] or "[]")))
        except json.JSONDecodeError:
            previous_seen = []
        try:
            address = str(monitor["address"])
            asset = str(monitor["asset"])
            monitored_assets = ("usdt", "trx") if asset == "both" else (asset,)
            scan_cursor_ms = sqlite_utc_timestamp_ms(
                monitor["last_checked_at"] or monitor["started_at"] or monitor["created_at"]
            )
            started_ms = sqlite_utc_timestamp_ms(
                monitor["started_at"] or monitor["created_at"]
            )
            cursor_timestamp_ms = int(monitor["cursor_timestamp_ms"] or 0)
            watermark_ms = cursor_timestamp_ms or scan_cursor_ms
            scan_from_ms = max(started_ms, watermark_ms - 5 * 60 * 1000)
            cursor_block = int(monitor["cursor_block"] or 0)
            monitor_state = str(monitor["monitor_state"] or "bootstrapping")
            pre_verified: TronBalance | None = None
            if scanned is not None:
                transaction_rows = tuple(scanned)
                try:
                    result = await chain.tron_verified_balance(
                        address, min_block=scanned_block,
                    )
                    if result.address == address:
                        pre_verified = result
                except ChainQueryError:
                    result = None
                if pre_verified is None:
                    try:
                        result = await chain.tron_monitor_balance(address)
                    except ChainQueryError:
                        result = None
            else:
                result, transaction_rows = await asyncio.gather(
                    chain.tron_monitor_balance(address),
                    chain.tron_monitor_transactions(address, monitored_assets, scan_from_ms),
                )
            seen = set(previous_seen)
            matching = [
                transaction for transaction in transaction_rows
                if transaction.asset.casefold() in monitored_assets and transaction.tx_id
            ]
            fresh = [
                transaction for transaction in matching
                if transaction.tx_id not in seen
                and transaction.timestamp_ms >= scan_from_ms
            ]
            low = Decimal(str(monitor["low_balance"])) if monitor["low_balance"] else None
            high = Decimal(str(monitor["high_balance"])) if monitor["high_balance"] else None
            previous_state = str(monitor["alert_state"] or "")

            def evaluate(balance: TronBalance | None) -> tuple[dict, str, list[str]]:
                if balance is None:
                    return {}, previous_state, []
                values = {"usdt": balance.usdt, "trx": balance.trx}
                item_states = {
                    item: (
                        "low" if low is not None and values[item] < low else
                        "high" if high is not None and values[item] > high else
                        "normal"
                    )
                    for item in monitored_assets
                }
                alerts = []
                for item in monitored_assets:
                    item_state = item_states[item]
                    if (
                        monitor_state == "live" and item_state != "normal"
                        and f"{item}:{item_state}" not in previous_state
                    ):
                        relation = "低于" if item_state == "low" else "高于"
                        threshold = low if item_state == "low" else high
                        alerts.append(
                            f"余额 {format_tron_amount(values[item])} {item.upper()}，"
                            f"已{relation} {threshold}"
                        )
                return (
                    values,
                    ",".join(f"{item}:{item_states[item]}" for item in monitored_assets),
                    alerts,
                )

            balances, state, threshold_alerts = evaluate(result)
            minimum_transfer = Decimal(str(monitor["min_transfer_amount"] or "0.1"))
            transfer_items: list[TronTransaction] = []
            if scanned is None and scanner_active and len(matching) >= 150:
                # 高频地址：实时播报由扫块负责；对账页数据不完整，只确认不重复播报
                fresh = []
            if monitor_state == "live" and fresh and bool(monitor["notify_transfers"]):
                transfer_items = [
                    transaction
                    for transaction in sorted(fresh, key=lambda item: item.timestamp_ms)
                    if transaction.amount >= minimum_transfer
                ]
            # 播报前：补全区块号，并从权威节点重新核实“本监控地址”的实时余额。
            # 绝不使用缓存或其他地址的余额；核实失败时显示「余额获取中/暂不可用」。
            alert_balance: TronBalance | None = None
            balance_verified = result is not None
            if scanned is not None:
                # 扫块即时路径：余额已在本轮从节点新鲜核实（不再重复请求）
                if pre_verified is not None:
                    alert_balance = pre_verified
                elif transfer_items or threshold_alerts:
                    balance_verified = False
                    threshold_alerts = []
                    state = previous_state
            elif transfer_items or threshold_alerts:
                enriched: list[TronTransaction] = []
                for transaction in transfer_items:
                    try:
                        transaction = await chain.transaction_with_block(transaction)
                    except ChainQueryError:
                        pass
                    enriched.append(transaction)
                transfer_items = enriched
                min_block = max(
                    (transaction.block_number for transaction in transfer_items), default=0
                )
                try:
                    verified = await chain.tron_verified_balance(
                        address, min_block=min_block
                    )
                except ChainQueryError:
                    verified = None
                if verified is not None and verified.address != address:
                    verified = None
                if verified is None:
                    balance_verified = False
                    # 余额未核实：不发阈值提醒，保留旧状态，下一轮重新判断。
                    threshold_alerts = []
                    state = previous_state
                else:
                    alert_balance = verified
                    result = verified
                    balances, state, threshold_alerts = evaluate(verified)
            notifications: list[tuple[list[str], TronTransaction | None]] = [
                ([
                    f"检测到新的{transaction.asset}"
                    f"{'支出' if transaction.direction == '转出' else '收入' if transaction.direction == '转入' else '交易'}"
                ], transaction)
                for transaction in transfer_items
            ]
            if threshold_alerts:
                if notifications:
                    notifications[-1][0].extend(threshold_alerts)
                else:
                    notifications.append((threshold_alerts, None))
            if balance_verified:
                last_balance = (
                    f"USDT={result.usdt};TRX={result.trx}"
                    if asset == "both" else str(balances[asset])
                )
            else:
                last_balance = str(monitor["last_balance"] or "")
            cursor_candidates = [
                transaction for transaction in matching
                if transaction.tx_id and (
                    transaction.timestamp_ms >= cursor_timestamp_ms
                    or transaction.block_number >= cursor_block
                )
            ]
            latest = max(
                cursor_candidates,
                key=lambda item: (item.block_number, item.timestamp_ms, item.tx_id),
                default=None,
            )
            if latest and not (
                latest.block_number > cursor_block
                or latest.timestamp_ms > cursor_timestamp_ms
            ):
                latest = None
            notify_tx_ids = {
                transaction.tx_id
                for _, transaction in notifications
                if transaction and transaction.tx_id
            }
            send_error = ""
            sent_tx_ids: list[str] = []
            for notice_lines, transaction in notifications:
                notice = "\n".join(notice_lines)
                alert_text, alert_keyboard = tron_monitor_alert_view(
                    alert_balance, transaction, monitor_id, notice,
                    context.application.bot_data.get("kkpay_emoji_ids"),
                    context.application.bot_data.get("tron_direction_emoji_ids"),
                    address=address,
                )
                sent = None
                with persistent_message():
                    try:
                        try:
                            sent = await context.bot.send_message(
                                int(monitor["owner_id"]), alert_text,
                                parse_mode=ParseMode.HTML, reply_markup=alert_keyboard,
                                disable_web_page_preview=True,
                            )
                        except TelegramError:
                            fallback_text, fallback_keyboard = tron_monitor_alert_view(
                                alert_balance, transaction, monitor_id, notice,
                                address=address,
                            )
                            sent = await context.bot.send_message(
                                int(monitor["owner_id"]), fallback_text,
                                parse_mode=ParseMode.HTML, reply_markup=fallback_keyboard,
                                disable_web_page_preview=True,
                            )
                    except TelegramError as exc:
                        send_error = str(exc)[:500]
                        continue
                if transaction and transaction.tx_id:
                    sent_tx_ids.append(transaction.tx_id)
                delete_days = int(monitor["notification_delete_days"] or 0)
                if delete_days and sent is not None:
                    store.schedule_message_deletion(
                        sent.chat_id, sent.message_id, delete_days * 86400
                    )
            acked_ids = [
                transaction.tx_id for transaction in matching
                if transaction.tx_id and transaction.tx_id not in notify_tx_ids
            ]
            ordered_seen = list(dict.fromkeys(
                sent_tx_ids + acked_ids + previous_seen
            ))[:1000]
            store.update_tron_monitor_snapshot(
                monitor_id, last_balance, ordered_seen, state,
                error=send_error,
                checked_at=checked_at,
                monitor_state="live",
                cursor_tx_id=(latest.tx_id if latest else str(monitor["cursor_tx_id"] or "")),
                cursor_block=(latest.block_number if latest else cursor_block),
                cursor_timestamp_ms=(
                    latest.timestamp_ms if latest else cursor_timestamp_ms
                ),
            )
        except (ValueError, ChainQueryError, InvalidOperation, json.JSONDecodeError, TelegramError) as exc:
            # 监控游标不前进：限流结束后从旧水位补扫，不会漏单
            error_text = BUSY_MESSAGE if is_busy_error(exc) else strip_urls(str(exc))
            store.update_tron_monitor_snapshot(
                monitor_id, str(monitor["last_balance"] or ""), previous_seen,
                str(monitor["alert_state"] or ""), error_text, mark_checked=False,
            )


TRON_HOT_ADDRESS_SECONDS = 6.0


def _tron_scan_db(context: ContextTypes.DEFAULT_TYPE) -> ScanDB | None:
    return context.application.bot_data.get("tron_scan_db")


TRON_RECONCILE_REQUESTS_PER_MONITOR = 7  # 1 balance + 2 TronGrid history + 4 TronScan


def tron_adaptive_interval(
    monitor_count: int, base_seconds: float, budget_qps: float,
) -> float:
    """Per-monitor interval so the whole per-address pass stays within budget.

    interval = max(base_seconds, monitors × 7 requests / budget_qps)
    e.g. budget 0.5 QPS: 20 monitors -> 300 s, 100 -> 1400 s, 1000 -> 14000 s.
    """
    budget = max(0.01, float(budget_qps or 0.5))
    return max(
        float(base_seconds),
        max(0, int(monitor_count)) * TRON_RECONCILE_REQUESTS_PER_MONITOR / budget,
    )


def _tron_poll_interval(
    context: ContextTypes.DEFAULT_TYPE, monitor_count: int = 0,
) -> tuple[float, bool]:
    """(per-monitor interval, scanner healthy) of the per-address pass.

    Scanner healthy -> reconciliation: max(TRON_RECONCILE_SECONDS, N×7/TRON_RECONCILE_QPS)
    Scanner stale/disabled -> fallback: max(TRON_FALLBACK_POLL_SECONDS, N×7/TRON_FALLBACK_QPS)
    N = monitors of this process + addresses watched by the other bot
    processes (the rate budget is shared by mother + clones).
    """
    config = context.application.bot_data.get("config")
    reconcile = float(getattr(config, "tron_reconcile_seconds", 300) or 300)
    fallback = float(getattr(config, "tron_fallback_poll_seconds", 30) or 30)
    reconcile_qps = float(getattr(config, "tron_reconcile_qps", 0.5) or 0.5)
    fallback_qps = float(getattr(config, "tron_fallback_qps", 2.0) or 2.0)
    db = _tron_scan_db(context)
    total = int(monitor_count)
    healthy = False
    if db is not None and getattr(config, "tron_block_scan", True):
        try:
            healthy = db.scanner_healthy()
            total += db.watch_count(exclude=_tron_process_key(config))
        except Exception:
            healthy = False
    if healthy:
        return tron_adaptive_interval(total, reconcile, reconcile_qps), True
    return tron_adaptive_interval(total, fallback, fallback_qps), False


def _tron_process_key(config) -> str:
    if getattr(config, "is_clone", False):
        return f"clone-{int(getattr(config, 'clone_id', 0) or 0)}"
    return "mother"


async def poll_tron_monitors(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-address pass: bootstrap new monitors + slow reconciliation.

    Instant alerts come from the block scanner (``sync_tron_scan``); this pass
    only catches up anything the scanner could not see (outage, skipped
    blocks), with exponential backoff while the API is busy.
    """
    store: DirectoryStore = context.application.bot_data["store"]
    monitors = store.active_tron_monitors()
    if not monitors:
        return
    bot_data = context.application.bot_data
    schedule: dict[int, float] = bot_data.setdefault("tron_poll_schedule", {})
    failures: dict[int, int] = bot_data.setdefault("tron_poll_failures", {})
    interval, scanner_mode = _tron_poll_interval(context, len(monitors))
    bot_data["tron_poll_interval"] = interval
    now = time.monotonic()
    db = _tron_scan_db(context)
    if db is not None:
        try:
            requested = float(db.get("reconcile_requested", "0") or 0)
        except (ValueError, sqlite3.Error):
            requested = 0.0
        if requested and requested > bot_data.get("tron_reconcile_seen", 0.0):
            bot_data["tron_reconcile_seen"] = requested
            schedule.clear()
    due = []
    for monitor in monitors:
        monitor_id = int(monitor["id"])
        live = str(monitor["monitor_state"] or "") == "live"
        if monitor_id not in schedule:
            # spread the first reconciliation of live monitors over the interval
            schedule[monitor_id] = (
                now + random.uniform(0, interval) if live and scanner_mode else now
            )
        if now >= schedule[monitor_id]:
            due.append(monitor)
    if not due:
        return
    semaphore = asyncio.Semaphore(5)
    results = await asyncio.gather(*(
        poll_one_tron_monitor(context, monitor, semaphore, scanner_active=scanner_mode)
        for monitor in due
    ), return_exceptions=True)
    for monitor, _result in zip(due, results):
        monitor_id = int(monitor["id"])
        row = None
        try:
            row = store.tron_monitor_by_id(monitor_id)
        except Exception:
            row = None
        error = str(row["last_error"] or "") if row is not None and "last_error" in row.keys() else ""
        if error and is_busy_error(error):
            failures[monitor_id] = failures.get(monitor_id, 0) + 1
            delay = min(600.0, max(interval, 15.0 * (2 ** failures[monitor_id])))
        else:
            failures.pop(monitor_id, None)
            delay = interval
        schedule[monitor_id] = time.monotonic() + delay * random.uniform(0.9, 1.1)


async def scan_tron_blocks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mother process only: follow the chain head and record matches."""
    scanner: TronBlockScanner | None = context.application.bot_data.get("tron_scanner")
    if scanner is None:
        return
    try:
        await scanner.step()
    except ChainQueryError as exc:
        logging.info("TRON block scanner waiting: %s", exc)
    except Exception:
        logging.exception("TRON block scanner step failed")


async def sync_tron_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every process: publish monitored addresses, consume scanner matches."""
    db = _tron_scan_db(context)
    if db is None:
        return
    bot_data = context.application.bot_data
    store: DirectoryStore = bot_data["store"]
    config = bot_data.get("config")
    process_key = _tron_process_key(config)
    monitors = store.active_tron_monitors()
    now = time.monotonic()
    watch: dict[str, str] = {}
    for monitor in monitors:
        address = str(monitor["address"])
        asset = str(monitor["asset"] or "both")
        previous = watch.get(address)
        watch[address] = asset if previous in (None, asset) else "both"
    if watch != bot_data.get("tron_watch_published") or now - bot_data.get(
        "tron_watch_published_at", 0.0
    ) >= 30:
        try:
            db.publish_watch(process_key, watch)
            bot_data["tron_watch_published"] = dict(watch)
            bot_data["tron_watch_published_at"] = now
        except Exception:
            logging.exception("Could not publish TRON watch list")
    cursor = bot_data.get("tron_match_cursor")
    try:
        if cursor is None:
            saved = store.get_settings().get("tron_scan_match_cursor", "")
            cursor = int(saved) if saved.isdigit() else db.max_match_id()
            if cursor > db.max_match_id():
                cursor = db.max_match_id()
        rows = db.matches_after(cursor, 1000)
    except Exception:
        logging.exception("Could not read TRON scanner matches")
        return
    pending: dict[str, list[TronTransaction]] = bot_data.setdefault("tron_pending_matches", {})
    pending_activity: dict[str, int] = bot_data.setdefault("tron_pending_activity", {})
    for row in rows:
        transaction = row_transaction(row)
        address = str(row["address"])
        if transaction.asset == "ACTIVITY":
            pending_activity[address] = max(
                pending_activity.get(address, 0), transaction.block_number,
            )
        else:
            pending.setdefault(address, []).append(transaction)
    if rows:
        cursor = max(int(row["id"]) for row in rows)
        store.set_setting("tron_scan_match_cursor", str(cursor))
    bot_data["tron_match_cursor"] = cursor
    if not pending and not pending_activity:
        return
    monitors_by_address: dict[str, list] = {}
    for monitor in monitors:
        monitors_by_address.setdefault(str(monitor["address"]), []).append(monitor)
    last_dispatch: dict[str, float] = bot_data.setdefault("tron_address_dispatch", {})
    activity_seen: dict[str, float] = bot_data.setdefault("tron_activity_dispatch", {})
    semaphore = asyncio.Semaphore(5)
    jobs = []
    for address in set(pending) | set(pending_activity):
        if address not in monitors_by_address:
            pending.pop(address, None)
            pending_activity.pop(address, None)
            continue
        transfers = pending.get(address) or []
        if transfers:
            # 同一地址最多每 TRON_HOT_ADDRESS_SECONDS 秒核实一次余额：高频地址的
            # 多笔转账合并为一批播报，避免为每个区块都请求余额
            if now - last_dispatch.get(address, -1e9) < TRON_HOT_ADDRESS_SECONDS:
                continue
            transactions = pending.pop(address)
            pending_activity.pop(address, None)
            last_dispatch[address] = now
        else:
            # 仅手续费类活动（无转账）：最多每 60 秒复核一次余额阈值
            if now - activity_seen.get(address, -1e9) < 60:
                continue
            transactions = []
            activity_seen[address] = now
        block = max(
            [item.block_number for item in transactions]
            + [pending_activity.pop(address, 0)]
        )
        for monitor in monitors_by_address[address]:
            if str(monitor["monitor_state"] or "") != "live":
                continue  # the reconciliation pass bootstraps it first
            jobs.append(poll_one_tron_monitor(
                context, monitor, semaphore, scanned=list(transactions), scanned_block=block,
            ))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    if random.random() < 0.01:
        try:
            db.prune()
        except Exception:
            pass


async def process_scheduled_deletions(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for row in store.due_message_deletions():
        try:
            await context.bot.delete_message(int(row["chat_id"]), int(row["message_id"]))
        except TelegramError as exc:
            if int(row["attempts"]) >= 2:
                store.finish_message_deletion(int(row["id"]))
            else:
                store.finish_message_deletion(int(row["id"]), str(exc))
        else:
            store.finish_message_deletion(int(row["id"]))


async def rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    store: DirectoryStore = context.application.bot_data["store"]
    record_selected_bot_usage(update, store)
    if store.get_settings().get("rate_enabled") != "1":
        await update.effective_message.reply_text("汇率查询目前已关闭。")
        return
    progress = await update.effective_message.reply_text("⏳ 正在读取 OKX 商户报价…")
    context.application.create_task(complete_rate_query(
        context, progress.chat_id, progress.message_id,
        update.effective_user.id, "buy", "bank",
    ))


async def complete_rate_query(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int,
    user_id: int, direction: str, payment: str,
) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        text, keyboard = await rate_result_view(context, direction, payment)
    except ChainQueryError as exc:
        store.add_chain_query(user_id, "", "rate", str(exc), False)
        text = f"汇率查询失败：{html.escape(str(exc))}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "重试", callback_data=f"rate:{direction}:{payment}"
            )
        ]])
    else:
        store.add_chain_query(user_id, "", "rate", f"{direction}:{payment}")
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    except TelegramError:
        logging.exception("Failed to update OKX rate result")


async def rate_result_view(
    context: ContextTypes.DEFAULT_TYPE, direction: str, payment: str,
) -> tuple[str, InlineKeyboardMarkup]:
    chain: ChainService = context.application.bot_data["chain"]
    try:
        snapshot, captured_at = await chain.okx_p2p_snapshot()
        quotes = snapshot.get((direction, payment))
        if not quotes:
            raise ChainQueryError("当前支付方式暂时没有可显示的商户广告")
    except ChainQueryError as exc:
        raise exc
    updated = datetime.fromtimestamp(
        captured_at, timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    payment_labels = {"bank": "银行卡", "wechat": "微信支付", "alipay": "支付宝"}
    action_label = "买 USDT · 商家卖单" if direction == "buy" else "卖 USDT · 商家买单"
    lines = [
        f"🏦 <b>OKX USDT/CNY · {payment_labels[payment]}</b>",
        f"{'🟢' if direction == 'buy' else '🔴'} <b>{action_label} TOP 10</b>", "",
    ]
    shown_quotes = quotes[:10]
    prices = [f"{quote.price:,.2f}" for quote in shown_quotes]
    price_width = max((len(price) for price in prices), default=0)
    for quote, price in zip(shown_quotes, prices):
        padding = "&#160;" * (price_width - len(price))
        lines.append(
            f"<b>{padding}{price} CNY</b>　{html.escape(str(quote.merchant))}"
        )
    lines.extend([
        "", f"抓取时间：{updated}",
        "提醒：以上为 OKX 商户广告价，会实时变化，成交前请再次核对。",
    ])
    direction_ids = context.application.bot_data.get("tron_direction_emoji_ids") or {}
    payment_row = [
        InlineKeyboardButton(
            ("✅ " if payment == key else "") + label,
            callback_data=f"rate:{direction}:{key}",
        )
        for key, label in (("bank", "银行卡"), ("wechat", "微信"), ("alipay", "支付宝"))
    ]
    direction_row = [
        custom_emoji_callback_button(
            ("✅ " if direction == "buy" else "") + "买 USDT",
            f"rate:buy:{payment}", direction_ids.get("in", ""), "🟢",
        ),
        custom_emoji_callback_button(
            ("✅ " if direction == "sell" else "") + "卖 USDT",
            f"rate:sell:{payment}", direction_ids.get("out", ""), "🔴",
        ),
    ]
    keyboard_rows = [payment_row, direction_row]
    bot_username = str(context.application.bot_data.get("bot_username") or "")
    if bot_username:
        keyboard_rows.append([InlineKeyboardButton(
            "👥 加群查询", url=f"https://t.me/{bot_username}?startgroup=true"
        )])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def format_quotes(title: str, quotes, updated: str) -> str:
    lines = [f"OKX P2P · {title}前 10 商户报价", ""]
    for quote in quotes:
        methods = "/".join(quote.payment_methods) or "未标注"
        lines.append(
            f"{quote.rank}. {quote.merchant} · ¥{quote.price:,.2f}\n"
            f"   可用 {quote.available_usdt:,.2f} U · 限额 ¥{quote.min_cny:,.0f}-{quote.max_cny:,.0f}\n"
            f"   {methods} · 成交率 {quote.completion_rate:.2f}% · {quote.avg_seconds}s"
        )
    lines.extend(["", f"抓取时间：{updated}", "市场广告报价会随时变化，仅供参考。"])
    return "\n".join(lines)


def format_compact_quotes(buy_quotes, sell_quotes, updated: str) -> str:
    def merchant_lines(quotes) -> list[str]:
        return [f"{quote.price:,.2f}  {quote.merchant}" for quote in quotes[:10]]

    lines = [
        "OKX P2P · USDT/CNY 商户报价",
        "",
        "购买价格",
        *merchant_lines(buy_quotes),
        "",
        "出售价格",
        *merchant_lines(sell_quotes),
        "",
        f"抓取时间：{updated}",
        "提醒：以上为 OKX 商户广告价，会实时变化，成交前请再次核对。",
    ]
    return "\n".join(lines)


def lottery_codes_for_selector(selector: str) -> tuple[str, ...]:
    if selector == "all":
        return tuple(LOTTERY_GAMES)
    if selector in {"cwl", "sport"}:
        return tuple(code for code, game in LOTTERY_GAMES.items() if game.source == selector)
    if selector == "marksix":
        return ("hklhc", "macau_lhc", "new_macau_lhc")
    return (selector,) if selector in LOTTERY_GAMES else ()


def lottery_selector_label(selector: str) -> str:
    if selector == "all":
        return "全部彩种"
    if selector == "cwl":
        return "全部福彩"
    if selector == "sport":
        return "全部体彩"
    if selector == "marksix":
        return "全部六合彩"
    game = LOTTERY_GAMES.get(selector)
    return game.name if game else selector


LOTTERY_HISTORY_PAGE_SIZE = 10
GROUP_STATS_PAGE_SIZE = 10
SEARCH_STATS_PAGE_SIZE = 10


def lottery_subscription_keyboard(
    store: DirectoryStore, chat_id: int
) -> InlineKeyboardMarkup:
    selectors = {str(row["selector"]) for row in store.lottery_subscriptions(chat_id)}
    subscribed_codes: set[str] = set()
    for selector in selectors:
        subscribed_codes.update(lottery_codes_for_selector(selector))
    rows = [[InlineKeyboardButton(
        "⛔ 关闭全部播报" if subscribed_codes else "✅ 开启全部播报",
        callback_data="lotterysub:all",
    )]]
    game_buttons = [
        InlineKeyboardButton(
            f"{'✅' if code in subscribed_codes else '❌'} {game.name}",
            callback_data=f"lotterysub:toggle:{code}",
        )
        for code, game in LOTTERY_GAMES.items()
    ]
    rows.extend([game_buttons[index:index + 2] for index in range(0, len(game_buttons), 2)])
    rows.append([InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")])
    return InlineKeyboardMarkup(rows)


def lottery_subscription_text(store: DirectoryStore, chat_id: int) -> str:
    selectors = {str(row["selector"]) for row in store.lottery_subscriptions(chat_id)}
    enabled = set()
    for selector in selectors:
        enabled.update(lottery_codes_for_selector(selector))
    return (
        "🎟 群开奖播报\n\n请选择需要订阅的彩种：\n"
        "【❌ 未订阅 · ✅ 已订阅】\n\n"
        f"当前已订阅 {len(enabled)}/{len(LOTTERY_GAMES)} 个彩种。\n"
        "香港六合彩使用香港赛马会官方数据；澳门与新澳为第三方数据，非澳门官方。"
    )


def lottery_history_page(store: DirectoryStore, game_code: str, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    game = LOTTERY_GAMES[game_code]
    rows = store.lottery_history(game_code, 100)
    page_count = max(1, (len(rows) + LOTTERY_HISTORY_PAGE_SIZE - 1) // LOTTERY_HISTORY_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * LOTTERY_HISTORY_PAGE_SIZE
    lines = [f"{game.name} 历史开奖（第 {page + 1}/{page_count} 页，共{len(rows)}期）", ""]
    for row in rows[start:start + LOTTERY_HISTORY_PAGE_SIZE]:
        numbers = str(row["primary_numbers"])
        if row["secondary_numbers"]:
            numbers += " + " + str(row["secondary_numbers"])
        draw_time = str(row["draw_time"] or "第三方历史页未提供时间")
        if game_code in MARK_SIX_CODES:
            numbers = format_mark_six_numbers(
                str(row["issue"]), draw_time,
                tuple(str(row["primary_numbers"]).split()),
                tuple(str(row["secondary_numbers"]).split()),
            )
        lines.append(f"第{row['issue']}期 · {draw_time}\n{numbers}")
    if not rows:
        lines.append("暂无历史开奖缓存。")
    keyboard = None
    if page_count > 1:
        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton("上一页", callback_data=f"lotteryhist:{game_code}:{page - 1}"))
        if page + 1 < page_count:
            buttons.append(InlineKeyboardButton("下一页", callback_data=f"lotteryhist:{game_code}:{page + 1}"))
        keyboard = InlineKeyboardMarkup([buttons])
    return "\n\n".join(lines), keyboard


async def send_lottery_history(update: Update, context: ContextTypes.DEFAULT_TYPE, game_code: str) -> None:
    if not await guard(update, context):
        return
    store: DirectoryStore = context.application.bot_data["store"]
    if store.get_settings().get("lottery_enabled") != "1":
        await update.effective_message.reply_text("开奖结果查询目前已关闭。")
        return
    service: LotteryService = context.application.bot_data["lottery"]
    error = ""
    try:
        results = await service.history(game_code, 100)
        for result in results:
            if not is_valid_lottery_result(result):
                continue
            try:
                result = normalize_lottery_result(result)
            except ValueError:
                continue
            store.save_lottery_result(result)
        store.set_lottery_source_status(LOTTERY_GAMES[game_code].source, True)
    except Exception as exc:
        error = str(exc)
        store.set_lottery_source_status(LOTTERY_GAMES[game_code].source, False, error)
    rows = store.lottery_history(game_code, 100)
    if not rows:
        await update.effective_message.reply_text(f"暂时无法取得历史开奖：{error or '数据源尚未初始化'}")
        return
    text, keyboard = lottery_history_page(store, game_code, 0)
    if error:
        text += "\n\n当前数据源暂不可用，以上为最近一次成功缓存。"
    store.audit(
        f"tg:{update.effective_user.id}", "lottery.history", str(update.effective_chat.id), game_code
    )
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def lottery_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args).strip() if context.args else ""
    game_code = resolve_lottery_code(raw)
    if game_code not in LOTTERY_GAMES:
        await update.effective_message.reply_text(
            "用法：/lotteryhistory 彩种（支持快乐8、香港六合彩、澳门六合彩、新澳六合彩等）"
        )
        return
    await send_lottery_history(update, context, game_code)


def format_stored_lottery(row) -> str:
    result = LotteryResult(
        source=str(row["source"]), game_code=str(row["game_code"]),
        game_name=str(row["game_name"]), issue=str(row["issue"]),
        draw_time=str(row["draw_time"]),
        primary=tuple(str(row["primary_numbers"]).split()),
        secondary=tuple(str(row["secondary_numbers"]).split()),
        detail_url=str(row["detail_url"]),
    )
    return format_lottery_result(result)


async def refresh_lottery_results(
    context: ContextTypes.DEFAULT_TYPE,
    game_codes: tuple[str, ...] | None = None,
    broadcast: bool = True,
    broadcast_initial: bool = False,
) -> dict[str, str]:
    store: DirectoryStore = context.application.bot_data["store"]
    service: LotteryService = context.application.bot_data["lottery"]
    known_issues = {
        str(row["game_code"]): str(row["issue"])
        for row in store.latest_lottery_results()
    }
    results, errors = await service.latest_all(game_codes, known_issues)
    requested = game_codes or tuple(LOTTERY_GAMES)
    for source in sorted({LOTTERY_GAMES[code].source for code in requested}):
        source_codes = [code for code in requested if LOTTERY_GAMES[code].source == source]
        source_errors = [errors[code] for code in source_codes if code in errors]
        if source_codes:
            store.set_lottery_source_status(source, not source_errors, "；".join(source_errors))
    for result in results:
        if not is_valid_lottery_result(result):
            # 拒绝不完整/非法号码，避免覆盖已有正确结果
            continue
        try:
            result = normalize_lottery_result(result)
        except ValueError:
            continue
        inserted, had_previous = store.save_lottery_result(result)
        if (
            not inserted or not broadcast
            or (not had_previous and not broadcast_initial)
        ):
            continue
        formatted = format_lottery_result(result)
        body = formatted if result.game_code == "ssq" else ("彩票开奖播报\n\n" + formatted)
        for chat_id in store.lottery_subscriber_chat_ids(result.game_code, result.source):
            store.queue_message(chat_id, body, kind="lottery")
        store.audit("lottery", "result.new", f"{result.game_code}:{result.issue}")
    store.heartbeat("lottery", f"ok={len(results)} errors={len(errors)}")
    return errors


async def lottery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    store: DirectoryStore = context.application.bot_data["store"]
    if store.get_settings().get("lottery_enabled") != "1":
        await update.effective_message.reply_text("开奖结果查询目前已关闭。")
        return
    raw = " ".join(context.args).strip() if context.args else "全部"
    selector = resolve_lottery_code(raw)
    if not selector:
        await update.effective_message.reply_text(
            "未知彩种。可选现有福彩、体彩、香港六合彩、澳门六合彩和新澳六合彩。"
        )
        return
    codes = lottery_codes_for_selector(selector)
    errors = await refresh_lottery_results(context, codes, broadcast=True)
    rows = store.latest_lottery_results()
    selected = [row for row in rows if str(row["game_code"]) in codes]
    if not selected:
        detail = next(iter(errors.values()), "数据源尚未初始化")
        await update.effective_message.reply_text(f"暂时无法取得开奖结果：{detail}")
        return
    text = "\n\n".join(format_stored_lottery(row) for row in selected)
    if errors:
        text += f"\n\n部分数据源暂不可用（{len(errors)} 个彩种），已显示数据库内最近结果。"
    await update.effective_message.reply_text(text)


async def lottery_subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("开奖播报只能订阅到群组。")
        return
    if not user or not await is_group_admin(update, context, user.id, "lottery"):
        await update.effective_message.reply_text("只有群管理员可以修改开奖订阅。")
        return
    raw = " ".join(context.args).strip() if context.args else "全部"
    selector = resolve_lottery_code(raw)
    if not selector:
        await update.effective_message.reply_text("用法：/lotterysub 全部、福彩、体彩、六合彩或彩种名称")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    store.add_lottery_subscription(chat.id, selector, user.id)
    store.audit(f"tg:{user.id}", "lottery.subscribe", str(chat.id), selector)
    await update.effective_message.reply_text(f"已订阅：{lottery_selector_label(selector)}。新期开奖后将自动播报。")


async def lottery_unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    if not user or not await is_group_admin(update, context, user.id, "lottery"):
        await update.effective_message.reply_text("只有群管理员可以修改开奖订阅。")
        return
    raw = " ".join(context.args).strip() if context.args else "全部"
    selector = resolve_lottery_code(raw)
    if not selector:
        await update.effective_message.reply_text("用法：/lotteryunsub 全部、福彩、体彩或彩种名称")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    removed = store.remove_lottery_subscription(chat.id, None if selector == "all" else selector)
    store.audit(f"tg:{user.id}", "lottery.unsubscribe", str(chat.id), selector)
    await update.effective_message.reply_text(f"已取消 {removed} 条开奖订阅。")


async def lottery_subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    rows = context.application.bot_data["store"].lottery_subscriptions(chat.id)
    if not rows:
        await update.effective_message.reply_text("当前群没有开奖播报订阅。")
        return
    await update.effective_message.reply_text(
        "当前开奖订阅：\n" + "\n".join(f"- {lottery_selector_label(str(row['selector']))}" for row in rows)
    )


async def poll_lottery_results(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fast-poll near draw time; slow safety poll otherwise (daily games)."""
    store: DirectoryStore = context.application.bot_data["store"]
    settings = store.get_settings()
    if settings.get("lottery_enabled") != "1":
        return
    subscribed_codes = tuple(
        code for code, game in LOTTERY_GAMES.items()
        if store.lottery_subscriber_chat_ids(code, game.source)
    )
    if not subscribed_codes:
        return
    now = datetime.now(timezone(timedelta(hours=8)))
    completed: dict[str, str] = context.application.bot_data.setdefault(
        "lottery_completed_draws", {}
    )
    expected_by_code = {
        code: lottery_poll_window(code, now) for code in subscribed_codes
    }
    fast_codes = tuple(
        code for code, expected in expected_by_code.items() if expected is not None
    )
    last_slow = float(context.application.bot_data.get("lottery_last_slow_poll") or 0)
    do_slow = (time.monotonic() - last_slow) >= 300
    if fast_codes:
        poll_codes = fast_codes
    elif do_slow:
        context.application.bot_data["lottery_last_slow_poll"] = time.monotonic()
        poll_codes = subscribed_codes
    else:
        return
    before = {
        str(row["game_code"]): str(row["issue"])
        for row in store.latest_lottery_results()
    }
    await refresh_lottery_results(
        context, poll_codes,
        broadcast=settings.get("lottery_broadcast_enabled") == "1",
        broadcast_initial=True,
    )
    after_rows = {
        str(row["game_code"]): row for row in store.latest_lottery_results()
    }
    after = {code: str(row["issue"]) for code, row in after_rows.items()}
    for code in poll_codes:
        expected = expected_by_code.get(code) or lottery_poll_window(code, now)
        if expected is None:
            # Slow-path poll without an active window: no completed-draw mark.
            continue
        changed = bool(after.get(code) and after.get(code) != before.get(code))
        result_row = after_rows.get(code)
        result_time_text = str(result_row["draw_time"] or "") if result_row else ""
        result_time = parse_lottery_draw_time(result_time_text)
        result_is_current = bool(
            expected and result_time and (
                expected - timedelta(minutes=5) <= result_time <= expected + timedelta(hours=6)
                or (
                    result_time.date() == expected.date()
                    and result_time >= expected - timedelta(minutes=5)
                )
            )
        )
        if changed or result_is_current:
            completed[code] = expected.isoformat()


LOTTERY_DRAW_SCHEDULES: dict[str, tuple[set[int], int, int]] = {
    "ssq": ({1, 3, 6}, 21, 15),
    "fc3d": (set(range(7)), 21, 15),
    "qlc": ({0, 2, 4}, 21, 15),
    "kl8": (set(range(7)), 21, 30),
    "dlt": ({0, 2, 5}, 21, 25),
    "pl3": (set(range(7)), 21, 25),
    "pl5": (set(range(7)), 21, 25),
    "qxc": ({1, 4, 6}, 21, 25),
    "hklhc": ({1, 3, 5}, 21, 30),
    "macau_lhc": (set(range(7)), 21, 32),
    "new_macau_lhc": (set(range(7)), 21, 32),
}


def parse_lottery_draw_time(value: str) -> datetime | None:
    cleaned = value.strip().replace("T", " ").removesuffix("Z")
    if not cleaned:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned[:19], pattern)
            return parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue
    return None


def next_lottery_draw(game_code: str, after: datetime) -> datetime:
    weekdays, hour, minute = LOTTERY_DRAW_SCHEDULES[game_code]
    for day_offset in range(9):
        candidate_day = after + timedelta(days=day_offset)
        candidate = candidate_day.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate.weekday() in weekdays and candidate > after:
            return candidate
    return after + timedelta(days=1)


def previous_lottery_draw(game_code: str, at: datetime) -> datetime | None:
    weekdays, hour, minute = LOTTERY_DRAW_SCHEDULES[game_code]
    for day_offset in range(8):
        candidate_day = at - timedelta(days=day_offset)
        candidate = candidate_day.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate.weekday() in weekdays and candidate <= at:
            return candidate
    return None


def lottery_poll_window(game_code: str, at: datetime) -> datetime | None:
    """Active fast-poll window: 2 minutes before draw through 45 minutes after."""
    if game_code not in LOTTERY_DRAW_SCHEDULES:
        return None
    upcoming = next_lottery_draw(game_code, at)
    if at < upcoming <= at + timedelta(minutes=2):
        return upcoming
    expected = previous_lottery_draw(game_code, at)
    if expected and expected <= at <= expected + timedelta(minutes=45):
        return expected
    return None


async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("客服消息请私聊机器人发送。")
        return
    if not await guard(update, context):
        return
    text = " ".join(context.args).strip()
    if not text:
        context.user_data["support_mode"] = True
        context.user_data["preserve_incoming_message_id"] = update.effective_message.message_id
        with persistent_message():
            await update.effective_message.reply_text(
                "请直接发送要咨询的内容，管理员可以在后台回复你。发送 /cancel 结束客服。"
            )
        return
    await save_support(update, context, text)


async def save_support(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    if store.get_settings().get("support_enabled") != "1":
        await update.effective_message.reply_text("客服功能目前已关闭。")
        return
    user = update.effective_user
    target_admin_id = int(context.user_data.get("support_target_id") or 0)
    support_button_id = int(context.user_data.get("support_button_id") or 0)
    store.add_support_message(
        user.id, "incoming", text,
        telegram_message_id=update.effective_message.message_id,
        target_admin_id=target_admin_id,
        support_button_id=support_button_id,
    )
    store.audit(f"tg:{user.id}", "support.incoming", str(user.id))
    context.user_data["support_mode"] = True
    context.user_data["preserve_incoming_message_id"] = update.effective_message.message_id
    acknowledgement = store.get_settings().get(
        "support_ack_text", "消息已转给管理员，回复会直接发到这里。"
    )
    with persistent_message():
        await update.effective_message.reply_text(
            acknowledgement,
            reply_markup=main_keyboard_for(context, user.id if user else None),
        )
    recipients = [target_admin_id] if target_admin_id else all_super_admin_ids(context)
    for admin_id in recipients:
        try:
            with persistent_message():
                await context.bot.send_message(
                    admin_id,
                    f"新客服消息\n用户：<code>{user.id}</code>"
                    + (f" @{html.escape(user.username)}" if user.username else "")
                    + f"\n\n{html.escape(text)}\n\n"
                    + f"回复：<code>/reply {user.id} 内容</code>",
                    parse_mode=ParseMode.HTML,
                )
        except TelegramError:
            logging.exception("Failed to send support notification")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = update.effective_message.text.strip()
    await handle_plain_text(update, context, text)


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    if text == "打开分类菜单":
        settings = store.get_settings()
        await update.effective_message.reply_text(
            settings.get("welcome_text", "欢迎使用。"),
            reply_markup=main_keyboard_for(
                context, update.effective_user.id if update.effective_user else None
            ),
        )
        return
    custom_button = store.custom_button_by_label(text)
    if custom_button:
        if custom_button["kind"] == "support":
            context.user_data["support_mode"] = True
            context.user_data["support_target_id"] = int(custom_button["target_user_id"] or 0)
            context.user_data["support_button_id"] = int(custom_button["id"])
            await update.effective_message.reply_text(
                f"已进入双向联系：{custom_button['contact_name'] or custom_button['label']}\n"
                "请发送消息，发送 /cancel 结束。"
            )
        elif custom_button["button_url"]:
            await update.effective_message.reply_text(
                str(custom_button["response_text"] or custom_button["label"]),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("打开", url=str(custom_button["button_url"]))
                ]]),
            )
        else:
            await update.effective_message.reply_text(
                str(custom_button["response_text"] or custom_button["label"])
            )
        return
    if context.user_data.get("support_mode"):
        await save_support(update, context, text)
        return
    settings = store.get_settings()
    if settings.get("auto_reply_enabled") == "1":
        rule = store.match_auto_reply(text)
        if rule:
            await update.effective_message.reply_text(
                str(rule["reply_text"]),
                reply_markup=main_keyboard_for(
                    context, update.effective_user.id if update.effective_user else None
                ),
            )
            store.audit(f"tg:{update.effective_user.id}", "auto_reply.hit", str(rule["id"]), text[:200])
            return
    await update.effective_message.reply_text(
        "请选择菜单中的功能。需要联系开发者可点击“联系开发者”。",
        reply_markup=main_keyboard_for(
            context, update.effective_user.id if update.effective_user else None
        ),
    )


async def private_keyword_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
) -> bool:
    """Run group keyword features in private chat against the selected group."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return False
    store: DirectoryStore = context.application.bot_data["store"]
    settings = store.get_settings()
    normalized = " ".join(text.split())
    group_actions = {
        "签到", "积分", "我的积分", "积分排行", "积分礼品", "积分商城",
        "积分兑换", "积分抽奖", "积分账单", "中奖记录", "兑换记录", "游戏记录",
        "抽奖", "抽奖历史", "统计", "活跃排行", "开奖",
    }
    redeem_match = re.fullmatch(r"兑换\s*#?\d+", normalized)
    if normalized in group_actions or redeem_match:
        permission = (
            "points" if normalized in {
                "签到", "积分", "我的积分", "积分排行", "积分礼品", "积分商城",
                "积分兑换", "积分抽奖", "积分账单", "中奖记录", "兑换记录", "游戏记录",
            } or redeem_match else
            "raffles" if normalized in {"抽奖", "抽奖历史"} else
            "stats" if normalized in {"统计", "活跃排行"} else "lottery"
        )
        chat_id = callback_group_id(context, update.effective_chat)
        if chat_id is None:
            selector_text, selector_keyboard = await private_group_selector(context, user.id)
            await message.reply_text(
                "请先选择要使用该关键词的群组。\n\n" + selector_text,
                reply_markup=selector_keyboard,
            )
            return True
        if not has_group_permission(context, chat_id, user.id, permission):
            await message.reply_text("你没有该群这个功能的权限。")
            return True
        if normalized == "开奖":
            enabled_codes: set[str] = set()
            for row in store.lottery_subscriptions(chat_id):
                enabled_codes.update(lottery_codes_for_selector(str(row["selector"])))
            if not enabled_codes:
                await message.reply_text("所选群组尚未开启任何彩种的开奖播报。")
                return True
            codes = tuple(code for code in LOTTERY_GAMES if code in enabled_codes)
            errors = await refresh_lottery_results(context, codes, broadcast=False)
            rows = [
                row for row in store.latest_lottery_results()
                if str(row["game_code"]) in enabled_codes
            ]
            if not rows:
                await message.reply_text(
                    "暂时无法取得已开启彩种的开奖结果："
                    + next(iter(errors.values()), "数据源尚未初始化")
                )
                return True
            for index, row in enumerate(rows):
                formatted = format_stored_lottery(row)
                if str(row["game_code"]) == "ssq":
                    prefix = ""
                else:
                    prefix = "彩票开奖播报\n\n" if index == 0 else ""
                await message.reply_text(prefix + formatted)
            return True
        if normalized == "签到":
            await point_checkin_reply(update, context, chat_id)
        elif normalized in {"积分", "我的积分"}:
            await message.reply_text(point_balance_text(store, chat_id, user.id))
        elif normalized == "积分排行":
            body, keyboard = point_ranking_page(store, chat_id, 0)
            await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif normalized in {"积分礼品", "积分商城", "积分兑换"}:
            await message.reply_text(point_gifts_text(store, chat_id))
        elif normalized == "积分抽奖":
            reset_point_draw_spend(context, chat_id)
            body, keyboard = point_draw_view(
                store, chat_id,
                has_group_permission(context, chat_id, user.id, "points"), None
            )
            await message.reply_text(body, reply_markup=keyboard)
        elif normalized in {"积分账单", "中奖记录", "兑换记录"}:
            kind = {"积分账单": "ledger", "中奖记录": "wins", "兑换记录": "redeems"}[normalized]
            body, keyboard = point_records_page(store, chat_id, user.id, kind, 0)
            await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif normalized == "游戏记录":
            body, keyboard = point_game_records_page(store, chat_id, user.id)
            with persistent_message():
                await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif redeem_match:
            await point_redeem_reply(
                update, context, int(re.search(r"\d+", normalized).group()), chat_id
            )
        elif normalized == "抽奖":
            active = store.active_raffles(chat_id, 100)
            show_count = raffle_count_visible(store, chat_id)
            if not active:
                await message.reply_text("所选群组当前没有进行中的抽奖。")
            else:
                await message.reply_text(f"🎁 所选群组共有 {len(active)} 个进行中的抽奖：")
                for raffle in active:
                    await message.reply_text(
                        f"<b>抽奖 #{raffle['id']}</b>\n\n" + raffle_text(raffle, show_count),
                        parse_mode=ParseMode.HTML,
                        reply_markup=(
                            raffle_keyboard(
                                int(raffle["id"]), int(raffle["entries"]), show_count,
                                raffle=raffle,
                            )
                            if str(raffle["raffle_type"] or "universal") == "universal" else None
                        ),
                    )
        elif normalized == "抽奖历史":
            body, keyboard = raffle_history_view(store, chat_id, 0)
            await message.reply_text(body, reply_markup=keyboard)
        elif normalized == "统计":
            await hydrate_group_speaker_names(context, chat_id, 1, 0)
            body, keyboard = group_stats_page(
                store, chat_id, 0, 1, has_super_admin_access(context, user.id)
            )
            await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await hydrate_group_speaker_names(context, chat_id, 1, 0)
            body, keyboard = group_active_page(
                store, chat_id, 0, 1, has_super_admin_access(context, user.id)
            )
            await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return True

    if settings.get("group_keyword_enabled") != "1":
        return False
    history_game = resolve_lottery_history_keyword(text)
    if history_game:
        await send_lottery_history(update, context, history_game)
        return True
    exact_entry = store.find_keyword_entry(
        text, (settings.get("group_directory_trigger", "地址"),)
    )
    if exact_entry:
        record_directory_search(update, store, text, 1, "private_exact_keyword")
        await deliver_entry(exact_entry, message=message)
        return True
    trigger = parse_group_trigger(
        text,
        settings.get("group_directory_trigger", "地址"),
        settings.get("group_rate_trigger", "z0"),
    )
    if trigger and trigger[0] == "rate":
        await rate(update, context)
        return True
    if trigger and is_directory_not_found_query(trigger[1]):
        _, search_query = trigger
        record_directory_search(update, store, search_query, 0, "private_group_keyword")
        await message.reply_text(
            settings.get("not_found_text", "地址没有收录，请联系管理员。")
        )
        return True
    return False


async def group_keyword_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    settings = store.get_settings()
    text = message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else 0
    points_config = store.points_config(chat_id) if chat_id else None
    if context.user_data.get("consumed_group_message") == message.message_id:
        context.user_data.pop("consumed_group_message", None)
        return
    if await handle_invite_member_query_input(update, context, text):
        schedule_group_trigger_cleanup(context, message, text)
        return
    dice_toggle = parse_dice_toggle_keyword(text)
    if dice_toggle is not None:
        # Admin toggle only; no-op text if caller has no points permission.
        if not can_toggle_group_dice(context, chat_id, update.effective_user.id):
            return
        schedule_group_trigger_cleanup(context, message, text)
        await apply_group_dice_toggle(update, context, dice_toggle)
        return
    if await quick_text_features(update, context, text, False):
        return
    if " ".join(text.split()) == "改名记录":
        if not has_group_permission(
            context, chat_id, update.effective_user.id, "renamehist"
        ):
            return
        schedule_group_trigger_cleanup(context, message, text)
        context.user_data["menu_mode"] = "renamehist_query"
        context.user_data["selected_group_id"] = chat_id
        await message.reply_text("请发送 @用户名或数字ID（也可回复对方消息）。")
        return
    if " ".join(text.split()) == "邀请链接查询":
        config = store.invite_config(chat_id)
        if not config["is_enabled"]:
            return
        schedule_group_trigger_cleanup(context, message, text)
        context.user_data["menu_mode"] = "invite_member_query"
        context.user_data["selected_group_id"] = chat_id
        await message.reply_text(
            "请发送 @用户名或数字ID（也可回复对方消息）。"
        )
        return
    if getattr(getattr(message, "forward_origin", None), "sender_user", None):
        await user_info_command(update, context)
        return
    if await send_personal_invite_query(update, context, text):
        schedule_group_trigger_cleanup(context, message, text)
        return
    rich_submission = parse_rich_submission_command(text)
    if rich_submission:
        if await guard(update, context):
            await save_rich_submission(update, context, *rich_submission)
        return
    tron_address = extract_tron_address(text)
    if tron_address:
        schedule_group_trigger_cleanup(context, message, text)
        if await guard(update, context):
            await send_balance_query(update, context, tron_address)
        return
    normalized = " ".join(text.split())
    if normalized == "开奖":
        enabled_codes: set[str] = set()
        for row in store.lottery_subscriptions(chat_id):
            enabled_codes.update(lottery_codes_for_selector(str(row["selector"])))
        if not enabled_codes:
            return
        if settings.get("lottery_enabled") != "1":
            return
        schedule_group_trigger_cleanup(context, message, text)
        if not await guard(update, context):
            return
        codes = tuple(code for code in LOTTERY_GAMES if code in enabled_codes)
        errors = await refresh_lottery_results(context, codes, broadcast=False)
        rows = [
            row for row in store.latest_lottery_results()
            if str(row["game_code"]) in enabled_codes
        ]
        if not rows:
            await message.reply_text(
                "暂时无法取得已开启彩种的开奖结果："
                + next(iter(errors.values()), "数据源尚未初始化")
            )
            return
        for index, row in enumerate(rows):
            formatted = format_stored_lottery(row)
            if str(row["game_code"]) == "ssq":
                prefix = ""
            else:
                prefix = "彩票开奖播报\n\n" if index == 0 else ""
            await message.reply_text(prefix + formatted)
        return
    points_keywords = {
        "签到", "积分", "我的积分", "积分排行", "积分礼品", "积分商城",
        "积分兑换", "积分抽奖", "积分账单", "中奖记录", "兑换记录", "游戏记录",
    }
    raffle_stat_keywords = {"抽奖", "抽奖历史", "统计", "活跃排行"}
    is_redeem = bool(re.fullmatch(r"兑换\s*#?\d+", normalized))
    points_on = bool(points_config and points_config["is_enabled"])
    if (
        (normalized in points_keywords or is_redeem) and not points_on
    ):
        # 积分未开启：不回应、不删消息
        return
    if (
        normalized in points_keywords
        or normalized in raffle_stat_keywords
        or is_redeem
    ):
        schedule_group_trigger_cleanup(context, message, text)
        if not await guard(update, context):
            return
        if normalized == "签到":
            await point_checkin_reply(update, context)
        elif normalized in {"积分", "我的积分"}:
            await message.reply_text(
                point_balance_text(store, update.effective_chat.id, update.effective_user.id)
            )
        elif normalized == "积分排行":
            ranking_text, ranking_keyboard = point_ranking_page(
                store, update.effective_chat.id, 0
            )
            await message.reply_text(
                ranking_text, parse_mode=ParseMode.HTML,
                reply_markup=ranking_keyboard,
            )
        elif normalized in {"积分礼品", "积分商城", "积分兑换"}:
            await message.reply_text(point_gifts_text(store, update.effective_chat.id))
        elif normalized == "积分抽奖":
            if not points_config or not points_config["draw_enabled"]:
                return
            reset_point_draw_spend(context, update.effective_chat.id)
            draw_text, draw_keyboard = point_draw_view(
                store, update.effective_chat.id,
                has_group_permission(
                    context, update.effective_chat.id,
                    update.effective_user.id, "points",
                ),
                None,
            )
            await message.reply_text(draw_text, reply_markup=draw_keyboard)
        elif normalized in {"积分账单", "中奖记录", "兑换记录"}:
            record_kind = {
                "积分账单": "ledger", "中奖记录": "wins", "兑换记录": "redeems"
            }[normalized]
            record_text, record_keyboard = point_records_page(
                store, update.effective_chat.id, update.effective_user.id,
                record_kind, 0,
            )
            await message.reply_text(
                record_text, parse_mode=ParseMode.HTML,
                reply_markup=record_keyboard,
            )
        elif normalized == "游戏记录":
            record_text, record_keyboard = point_game_records_page(
                store, update.effective_chat.id, update.effective_user.id,
            )
            with persistent_message():
                await message.reply_text(
                    record_text, parse_mode=ParseMode.HTML,
                    reply_markup=record_keyboard,
                )
        elif normalized.startswith("兑换"):
            gift_id = int(re.search(r"\d+", normalized).group())
            await point_redeem_reply(update, context, gift_id)
        elif normalized == "抽奖":
            active_raffles = store.active_raffles(update.effective_chat.id, 100)
            show_count = raffle_count_visible(store, update.effective_chat.id)
            if active_raffles:
                await message.reply_text(
                    f"🎁 本群共有 {len(active_raffles)} 个进行中的抽奖："
                )
            for raffle in active_raffles:
                await message.reply_text(
                    f"<b>抽奖 #{raffle['id']}</b>\n\n"
                    + raffle_text(raffle, show_count),
                    parse_mode=ParseMode.HTML,
                    reply_markup=(
                        raffle_keyboard(
                            int(raffle["id"]), int(raffle["entries"]), show_count,
                            raffle=raffle,
                        )
                        if str(raffle["raffle_type"] or "universal") == "universal"
                        else None
                    ),
                )
            if not active_raffles:
                await message.reply_text("本群当前没有进行中的抽奖。")
        elif normalized == "抽奖历史":
            history_text, history_keyboard = raffle_history_view(
                store, update.effective_chat.id, 0
            )
            await message.reply_text(history_text, reply_markup=history_keyboard)
        elif normalized == "统计":
            await group_stats_command(update, context)
        else:
            await hydrate_group_speaker_names(context, update.effective_chat.id, 1, 0)
            active_text, active_keyboard = group_active_page(
                store, update.effective_chat.id, 0, 1,
                has_super_admin_access(context, update.effective_user.id),
            )
            await message.reply_text(
                active_text, parse_mode=ParseMode.HTML, reply_markup=active_keyboard
            )
        return
    dice_bet = parse_dice_bet(text)
    if dice_bet:
        # Avoid clashing with admin menu_mode numeric / short inputs.
        if context.user_data.get("menu_mode"):
            return
        # 未开积分/骰子：当普通聊天，不回应
        if not points_config or not points_config["is_enabled"] or not points_config["dice_enabled"]:
            return
        # 骰子口令与结果均不自动撤回
        if not await guard(update, context):
            return
        await point_dice_bet_reply(update, context, dice_bet[0], dice_bet[1])
        return
    if settings.get("group_keyword_enabled") != "1":
        return
    history_game = resolve_lottery_history_keyword(text)
    if history_game:
        if settings.get("lottery_enabled") != "1":
            return
        schedule_group_trigger_cleanup(context, message, text)
        await send_lottery_history(update, context, history_game)
        return
    exact_entry = store.find_keyword_entry(
        text, (settings.get("group_directory_trigger", "地址"),)
    )
    if exact_entry:
        schedule_group_trigger_cleanup(context, message, text)
        if not await guard(update, context):
            return
        record_directory_search(update, store, text, 1, "group_exact_keyword")
        await deliver_entry(exact_entry, message=message)
        return
    trigger = parse_group_trigger(
        text,
        settings.get("group_directory_trigger", "地址"),
        settings.get("group_rate_trigger", "z0"),
    )
    if trigger and trigger[0] == "directory" and not is_directory_not_found_query(trigger[1]):
        # 长句子只是碰巧以“地址”结尾：不当作搜索
        trigger = None
    rule = None
    if not trigger and settings.get("auto_reply_enabled") == "1":
        rule = store.match_auto_reply(text, scope="group")
    if not trigger and not rule:
        return
    schedule_group_trigger_cleanup(context, message, text)
    if trigger and trigger[0] == "rate":
        if settings.get("rate_enabled") != "1":
            return
        store.audit(f"tg:{update.effective_user.id}", "group_trigger.rate", str(chat_id))
        await rate(update, context)
        return
    if not await guard(update, context):
        return
    if rule:
        await message.reply_text(str(rule["reply_text"]))
        store.audit(f"tg:{update.effective_user.id}", "auto_reply.group_hit", str(rule["id"]), text[:200])
        return
    action, query = trigger
    # 精确匹配已在上面处理；走到这里说明没有该关键词
    record_directory_search(update, store, query, 0, "group_keyword")
    empty_text = settings.get("not_found_text", "地址没有收录，请联系管理员。")
    store.audit(f"tg:{update.effective_user.id}", "group_trigger.directory", str(update.effective_chat.id), query)
    await message.reply_text(empty_text)


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_permission(context, update.effective_user.id if update.effective_user else None, "support"):
        await update.effective_message.reply_text("没有双向客服回复权限。")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/reply 用户ID 回复内容")
        return
    user_id = int(context.args[0])
    body = " ".join(context.args[1:]).strip()
    try:
        with persistent_message():
            await context.bot.send_message(user_id, "管理员回复：\n\n" + body)
    except TelegramError as exc:
        await update.effective_message.reply_text(f"发送失败：{exc}")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    store.add_support_message(user_id, "outgoing", body, admin_name=f"tg:{update.effective_user.id}")
    store.audit(f"tg:{update.effective_user.id}", "support.reply", str(user_id))
    await update.effective_message.reply_text("回复已发送。")


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_super_admin_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可用。")
        return
    viewer_id = update.effective_user.id
    rows = context.application.bot_data["store"].list_bot_admins(
        viewer_id=viewer_id,
        include_all=is_developer_user(context, viewer_id),
    )
    if not rows:
        await update.effective_message.reply_text("暂无管理员。")
        return
    lines = ["机器人管理员："]
    for row in rows:
        label = {
            "developer": "开发者", "super": "超级管理员", "admin": "管理员",
        }.get(str(row["role"]), str(row["role"]))
        username = f" @{row['username']}" if row["username"] else ""
        permissions = f" · 权限 {row['permissions']}" if row["permissions"] else ""
        lines.append(f"{row['user_id']} · {label}{username}{permissions}")
    await update.effective_message.reply_text("\n".join(lines))


async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_super_admin_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可用。")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/addadmin Telegram数字ID")
        return
    user_id = int(context.args[0])
    store: DirectoryStore = context.application.bot_data["store"]
    store.add_bot_admin(user_id, "admin", update.effective_user.id)
    store.audit(f"tg:{update.effective_user.id}", "admin.add", str(user_id))
    await update.effective_message.reply_text(f"已添加管理员：{user_id}")


async def delete_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_super_admin_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可用。")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/deladmin Telegram数字ID")
        return
    user_id = int(context.args[0])
    if not can_manage_bot_admin(context, update.effective_user.id, user_id):
        await update.effective_message.reply_text("不能管理与自己无关的管理员。")
        return
    config: Config = context.application.bot_data["config"]
    if user_id in config.developer_ids:
        await update.effective_message.reply_text("不能删除开发者。")
        return
    if user_id in config.super_admin_ids:
        await update.effective_message.reply_text("不能删除主超级管理员。")
        return
    if user_id in config.admin_ids:
        await update.effective_message.reply_text("这个管理员写在 .env 的 ADMIN_IDS 里，需先从 .env 删除后重启。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    if not store.remove_bot_admin(user_id):
        await update.effective_message.reply_text("没有找到这个管理员。")
        return
    store.audit(f"tg:{update.effective_user.id}", "admin.delete", str(user_id))
    await update.effective_message.reply_text(f"已删除管理员：{user_id}")


ADMIN_PERMISSIONS = {
    "moderation", "group_manage", "support", "stats",
}


async def set_permissions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not has_super_admin_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可分配权限。")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "用法：/setperm 用户ID moderation,group_manage,support,stats"
        )
        return
    target_id = int(context.args[0])
    if not can_manage_bot_admin(context, update.effective_user.id, target_id):
        await update.effective_message.reply_text("不能管理与自己无关的管理员。")
        return
    permissions = {
        item.strip().casefold() for item in " ".join(context.args[1:]).split(",")
        if item.strip()
    }
    invalid = permissions - ADMIN_PERMISSIONS
    if invalid:
        await update.effective_message.reply_text("未知权限：" + ", ".join(sorted(invalid)))
        return
    store: DirectoryStore = context.application.bot_data["store"]
    if not store.set_bot_admin_permissions(target_id, permissions):
        await update.effective_message.reply_text("只可给普通管理员分配权限。")
        return
    store.audit(f"tg:{update.effective_user.id}", "admin.permissions", str(target_id), ",".join(sorted(permissions)))
    await update.effective_message.reply_text("管理员权限已更新。")


async def reset_permissions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not has_super_admin_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可重置权限。")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/resetperm 用户ID")
        return
    target_id = int(context.args[0])
    if not can_manage_bot_admin(context, update.effective_user.id, target_id):
        await update.effective_message.reply_text("不能管理与自己无关的管理员。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    if not store.set_bot_admin_permissions(target_id, set()):
        await update.effective_message.reply_text("只可重置普通管理员权限。")
        return
    await update.effective_message.reply_text("管理员权限已重置。")


def private_note_attachment(message) -> tuple[str, str, str]:
    if message.animation:
        return message.animation.file_id, "animation", message.animation.file_name or ""
    if message.document:
        return message.document.file_id, "document", message.document.file_name or ""
    if message.photo:
        return message.photo[-1].file_id, "photo", ""
    if message.video:
        return message.video.file_id, "video", message.video.file_name or ""
    if message.audio:
        return message.audio.file_id, "audio", message.audio.file_name or ""
    if message.voice:
        return message.voice.file_id, "voice", ""
    return "", "", ""


def publishing_attachment(message) -> tuple[str, str, str]:
    if getattr(message, "sticker", None):
        return message.sticker.file_id, "sticker", ""
    if getattr(message, "video_note", None):
        return message.video_note.file_id, "video_note", ""
    return private_note_attachment(message)


RICH_SUBMISSION_PATTERN = re.compile(r"\s*(.{1,120}?)\s+搜录(?:\s+([\s\S]*?))?\s*")
RICH_SUBMISSION_MARK = re.compile(r"\s+搜录(?:\s+|$)")


def parse_rich_submission_command(text: str) -> tuple[str, str] | None:
    """「关键词 搜录 内容」 or 「关键词 搜录」 (then reply to the content message)."""
    match = RICH_SUBMISSION_PATTERN.fullmatch(text or "")
    if not match:
        return None
    keyword = " ".join(match.group(1).split())
    content = (match.group(2) or "").strip()
    return (keyword, content) if keyword else None


def slice_submission_content(message) -> tuple[str, str]:
    """Text after 「搜录」 with its formatting entities (UTF-16 offsets shifted)."""
    full_text, raw_entities = capture_content(message)
    match = RICH_SUBMISSION_MARK.search(full_text or "")
    if not match:
        return "", "[]"
    start = match.end()
    end = len(full_text.rstrip())
    if end <= start:
        return "", "[]"
    content = full_text[start:end]
    start16 = _utf16_len(full_text[:start])
    end16 = start16 + _utf16_len(content)
    entities = []
    for item in json.loads(raw_entities or "[]"):
        offset = int(item.get("offset", 0))
        length = int(item.get("length", 0))
        left, right = max(offset, start16), min(offset + length, end16)
        if right <= left:
            continue
        shifted = dict(item)
        shifted["offset"] = left - start16
        shifted["length"] = right - left
        entities.append(shifted)
    return content, json.dumps(entities, ensure_ascii=False)


async def save_rich_submission(
    update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str, content: str
) -> None:
    """Store a 收录 exactly as given (same mechanism as ads).

    * 「关键词 搜录 内容」(text, or media caption): content after 搜录 with
      its entities; attached media kept.
    * Reply 「关键词 搜录」 to any message (incl. channel forwards with
      buttons): that message is stored verbatim and later copied back.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if storage_quota_exceeded(context):
        await message.reply_text(storage_full_text(context))
        return
    file_id, file_type, file_name = publishing_attachment(message)
    target = getattr(message, "reply_to_message", None)
    if target is not None and getattr(target, "forum_topic_created", None):
        target = None  # 话题群里的普通消息默认“回复”话题首条，不算回复
    entities_json = "[]"
    buttons_json = "[]"
    copy_chat_id = copy_message_id = 0
    if not content and not file_id and target is not None:
        content, entities_json = capture_content(target)
        file_id, file_type, file_name = publishing_attachment(target)
        scratch = chat.id if chat.type == ChatType.PRIVATE else 0
        try:
            buttons_json = (
                await capture_buttons_resolving(context.bot, target, scratch)
                if scratch else capture_buttons(target)
            )
        except Exception:
            buttons_json = "[]"
        source = forward_channel_source(target)
        copy_chat_id, copy_message_id = source or (chat.id, target.message_id)
    elif content or file_id:
        if content:
            content, entities_json = slice_submission_content(message)
    else:
        await message.reply_text(
            "请在“关键词 搜录”后写上内容；\n"
            "或回复要收录的消息，再发送“关键词 搜录”。"
        )
        return
    unique_url = f"tgcontent://{chat.id}/{message.message_id}"
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        entry_id = store.add_rich_submission(
            keyword, unique_url, content, user.id, user.username or "",
            file_id=file_id, file_type=file_type, file_name=file_name,
            source_chat_id=chat.id, source_message_id=message.message_id,
            entities_json=entities_json, buttons_json=buttons_json,
            copy_chat_id=copy_chat_id, copy_message_id=copy_message_id,
        )
    except ValueError as exc:
        await message.reply_text(f"提交失败：{exc}")
        return
    store.audit(f"tg:{user.id}", "entry.rich_submit", str(entry_id), file_type or content[:100])
    await message.reply_text(
        f"收录提交成功，编号 #{entry_id}，关键词：{keyword}\n"
        "等待超级管理员审核；同一关键词审核通过后以最新内容为准。"
    )
    if isinstance(getattr(context, "user_data", None), dict):
        context.user_data["preserve_incoming_message_id"] = message.message_id
    await notify_admins(context, entry_id)


async def rich_submission_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    message = update.effective_message
    if message and context.user_data.get("consumed_group_message") == message.message_id:
        context.user_data.pop("consumed_group_message", None)
        return None
    text = (message.caption or "").strip() if message else ""
    parsed = parse_rich_submission_command(text)
    if not parsed or not await guard(update, context):
        return None
    await save_rich_submission(update, context, *parsed)
    return ConversationHandler.END


def parse_private_note_command(text: str) -> tuple[str, str, str] | None:
    stripped = text.strip()
    if stripped in {"1", "2"}:
        return stripped, "", ""
    match = re.match(r"^\s*([12])(?:\s+|[,，:：]\s*)(\S+)(?:\s+([\s\S]*))?\s*$", text)
    if not match:
        return None
    return match.group(1), match.group(2), (match.group(3) or "").strip()


PRIVATE_NOTE_FILE_LABELS = {
    "photo": "图片", "video": "视频", "audio": "音频",
    "voice": "语音", "animation": "动图", "sticker": "贴纸",
    "video_note": "视频消息",
}
PRIVATE_NOTE_PREVIEW_LIMIT = 500
PENDING_NOTE_TIMEOUT_SECONDS = 600
PENDING_NOTE_MAX_ITEMS = 50
PENDING_NOTE_SAVE_WORDS = {"保存", "保存笔记", "确认保存"}
PENDING_NOTE_CANCEL_WORDS = {"取消", "取消保存"}


def private_note_file_label(row, below: bool = False) -> str:
    file_type = str(row["file_type"] or "")
    label = PRIVATE_NOTE_FILE_LABELS.get(file_type, "文件")
    name = str(row["file_name"] or "").strip()
    extra = f"（{html.escape(name)}）" if name else ""
    hint = "（见下方）" if below else ""
    return f"📎 附件：{label}{extra}{hint}"


PRIVATE_NOTE_CAPTION_LIMIT = 1024
PRIVATE_NOTE_GROUPABLE = {"photo", "video"}
PRIVATE_NOTE_NO_CAPTION = {"sticker", "video_note"}


def private_note_caption_parts(header: str, body: str) -> tuple[str, str]:
    """Return (caption_html, overflow_html). Plain header/body in; HTML out.

    Caption stays within Telegram's 1024-char limit; long bodies go to a
    separate text message instead of being cut.
    """
    header = str(header or "").strip()
    body = str(body or "").strip()
    plain = f"{header}\n{body}" if header and body else (header or body)
    if len(plain) <= PRIVATE_NOTE_CAPTION_LIMIT:
        return html.escape(plain), ""
    caption = html.escape(f"{header}\n（说明较长，见下一条消息）" if header else "（说明较长，见下一条消息）")
    return caption, html.escape(body)


def _split_text_chunks(text: str, limit: int = 4000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


async def _send_private_note_single(bot, chat_id: int, row, caption: str):
    file_id = str(row["file_id"] or "")
    file_type = str(row["file_type"] or "")
    kwargs = {"parse_mode": ParseMode.HTML}
    if caption and file_type not in PRIVATE_NOTE_NO_CAPTION:
        kwargs["caption"] = caption
    else:
        kwargs = {}
    if file_type == "photo":
        return await bot.send_photo(chat_id, file_id, **kwargs)
    if file_type == "video":
        return await bot.send_video(chat_id, file_id, **kwargs)
    if file_type == "animation":
        return await bot.send_animation(chat_id, file_id, **kwargs)
    if file_type == "audio":
        return await bot.send_audio(chat_id, file_id, **kwargs)
    if file_type == "voice":
        return await bot.send_voice(chat_id, file_id, **kwargs)
    if file_type == "sticker":
        return await bot.send_sticker(chat_id, file_id)
    if file_type == "video_note":
        return await bot.send_video_note(chat_id, file_id)
    return await bot.send_document(chat_id, file_id, **kwargs)


async def send_private_note_media(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, items: list,
) -> list:
    """Send the original saved media for notes, in order.

    ``items`` is a list of ``(header_plain_text, row)``. Consecutive photos /
    videos are sent as media groups (max 10); everything else individually
    (documents as the original file, videos as the original video…). Invalid
    file_ids produce a short fallback line; never raises. All sent messages
    get the same 10-minute private cleanup as note replies.
    """
    bot = context.bot
    sent: list = []

    def track(result) -> None:
        if result is None:
            return
        messages = list(result) if isinstance(result, (list, tuple)) else [result]
        for msg in messages:
            sent.append(msg)
            try:
                schedule_setting_cleanup(context, msg)
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass

    async def send_text(text: str) -> None:
        for chunk in _split_text_chunks(text):
            try:
                track(await bot.send_message(
                    chat_id, chunk, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ))
            except Exception as exc:  # noqa: BLE001
                logging.info("Could not send private note text: %s", exc)

    async def send_one(header: str, row) -> None:
        caption, overflow = private_note_caption_parts(header, str(row["body"] or ""))
        file_type = str(row["file_type"] or "")
        try:
            track(await _send_private_note_single(bot, chat_id, row, caption))
        except Exception as exc:  # noqa: BLE001 - invalid/expired file_id etc.
            logging.info("Could not send private note media: %s", exc)
            label = PRIVATE_NOTE_FILE_LABELS.get(file_type, "文件")
            await send_text(
                f"⚠️ {html.escape(header)} {label}发送失败（文件可能已失效）"
                + (f"\n{html.escape(str(row['body'] or '').strip())}" if str(row["body"] or "").strip() else "")
            )
            return
        if file_type in PRIVATE_NOTE_NO_CAPTION and caption:
            await send_text(caption)
        if overflow:
            await send_text(overflow)

    async def send_group(group: list) -> None:
        if len(group) == 1:
            await send_one(*group[0])
            return
        media = []
        overflows: list[str] = []
        for header, row in group:
            caption, overflow = private_note_caption_parts(header, str(row["body"] or ""))
            cls = InputMediaPhoto if str(row["file_type"]) == "photo" else InputMediaVideo
            media.append(cls(
                media=str(row["file_id"]), caption=caption or None,
                parse_mode=ParseMode.HTML,
            ))
            if overflow:
                overflows.append(overflow)
        try:
            track(await bot.send_media_group(chat_id, media))
        except Exception as exc:  # noqa: BLE001 - fall back to one by one
            logging.info("Private note media group failed, sending singly: %s", exc)
            for header, row in group:
                await send_one(header, row)
            return
        for overflow in overflows:
            await send_text(overflow)

    pending_group: list = []
    for header, row in items:
        if not str(row["file_id"] or ""):
            continue
        if str(row["file_type"] or "") in PRIVATE_NOTE_GROUPABLE:
            pending_group.append((header, row))
            if len(pending_group) == 10:
                await send_group(pending_group)
                pending_group = []
            continue
        if pending_group:
            await send_group(pending_group)
            pending_group = []
        await send_one(header, row)
    if pending_group:
        await send_group(pending_group)
    return sent


def private_note_preview_html(row, limit: int = PRIVATE_NOTE_PREVIEW_LIMIT) -> str:
    """HTML preview of one stored note: time, escaped/truncated text, media label."""
    lines: list[str] = []
    saved_at = format_beijing_time(row["created_at"]) if row["created_at"] else ""
    if saved_at:
        lines.append(f"🕒 {html.escape(saved_at)}")
    body = str(row["body"] or "").strip()
    if body:
        if len(body) > limit:
            body = body[:limit].rstrip() + "…"
        lines.append(html.escape(body))
    if str(row["file_id"] or ""):
        lines.append(private_note_file_label(row, below=True))
    return "\n".join(lines)


def private_note_saved_text(
    keyword: str, total: int, previous=None, *,
    permanent: bool = False, saved_count: int = 1,
) -> str:
    """Save confirmation (HTML). Appends the previous note under the keyword if any."""
    safe_keyword = html.escape(keyword)
    if permanent:
        lines = [f"已永久保存私密笔记：{safe_keyword}"]
        if saved_count > 1:
            lines.append(f"本次保存：{saved_count} 条")
        lines.append(f"当前共有：{total} 条")
        lines.append("保存时间：永久（不自动删除）")
    else:
        lines = [
            f"已保存私密笔记：{safe_keyword}",
            f"当前保留：{total}/99" if total <= 99 else f"当前保留：{total} 条",
            "保存时间：9个月",
        ]
    text = "\n".join(lines)
    if previous is not None:
        preview = private_note_preview_html(previous)
        if preview:
            text += "\n\n上一条保存的信息：\n" + preview
    return text


async def reply_private_note_saved(
    context: ContextTypes.DEFAULT_TYPE, message, text: str, previous=None,
) -> None:
    """Send the save confirmation, then the previous note's original media (if any)."""
    await message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
    if previous is not None and str(previous["file_id"] or ""):
        saved_at = format_beijing_time(previous["created_at"]) if previous["created_at"] else ""
        header = "上一条保存的信息" + (f" · {saved_at}" if saved_at else "")
        await send_private_note_media(context, message.chat_id, [(header, previous)])


async def save_private_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str, body: str,
    permanent: bool = False,
) -> None:
    message = update.effective_message
    if not keyword:
        await message.reply_text("用法：1 关键词 内容。带文件时把这句话写在文件说明里。")
        return
    file_id, file_type, file_name = (
        publishing_attachment(message) if permanent else private_note_attachment(message)
    )
    store: DirectoryStore = context.application.bot_data["store"]
    previous = store.latest_private_note(keyword)
    try:
        note_id = store.add_private_note(
            keyword, body, update.effective_user.id, file_id=file_id, file_type=file_type,
            file_name=file_name, permanent=permanent,
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    total = store.count_private_notes(keyword)
    store.audit(
        f"tg:{update.effective_user.id}",
        "private_note.add_permanent" if permanent else "private_note.add",
        keyword, f"id={note_id}",
    )
    await reply_private_note_saved(
        context, message,
        private_note_saved_text(keyword, total, previous, permanent=permanent),
        previous,
    )


def pending_note_item(message) -> dict | None:
    """Capture one collected message (text/caption + media file_id)."""
    body = str(getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    file_id, file_type, file_name = publishing_attachment(message)
    if not body and not file_id:
        return None
    return {
        "body": body, "file_id": file_id, "file_type": file_type,
        "file_name": file_name, "message_id": getattr(message, "message_id", 0),
    }


async def start_pending_private_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str,
) -> None:
    message = update.effective_message
    previous_mode = str(context.user_data.pop("menu_mode", "") or "")
    if previous_mode and previous_mode != "private_note_query":
        clear_menu_input_failures(context, previous_mode)
    context.user_data["pending_private_note"] = {
        "keyword": keyword, "items": [], "touched": time.time(),
        "media_groups": [],
    }
    store: DirectoryStore = context.application.bot_data["store"]
    existing = store.count_private_notes(keyword)
    existing_line = (
        f"该关键词已有 {existing} 条笔记（查询请直接发送关键词）。\n" if existing else ""
    )
    await message.reply_text(
        f"📥 开始收集私密笔记：{keyword}\n{existing_line}"
        "请发送或转发要保存的消息（文字、图片、视频、文件等都可以，可多条）。\n"
        "全部发完后发送「保存」（或回复任意一条消息「保存」）永久保存；发送「取消」放弃。\n"
        "10 分钟内没有操作将自动取消。"
    )


async def handle_pending_private_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Return True if the message was consumed by the 2-关键词 pending-save mode."""
    pending = context.user_data.get("pending_private_note")
    if not isinstance(pending, dict):
        return False
    message = update.effective_message
    if not message:
        return False
    if context.user_data.get("settings_draft") or context.user_data.get("menu_mode") not in (
        None, "", "private_note_query",
    ):
        return False
    keyword = str(pending.get("keyword") or "")
    items = pending.setdefault("items", [])
    if time.time() - float(pending.get("touched") or 0) > PENDING_NOTE_TIMEOUT_SECONDS:
        context.user_data.pop("pending_private_note", None)
        await message.reply_text(
            f"「{keyword}」的待保存已超时（10分钟），"
            + (f"收集的 {len(items)} 条未保存。" if items else "未保存任何内容。")
        )
        return False
    user = update.effective_user
    if not has_developer_access(context, user.id if user else None):
        context.user_data.pop("pending_private_note", None)
        return False
    text = str(message.text or "").strip() if getattr(message, "text", None) else ""
    has_media = bool(publishing_attachment(message)[0])
    is_forward = bool(getattr(message, "forward_origin", None))
    if text and not has_media and not is_forward:
        if text in PENDING_NOTE_CANCEL_WORDS:
            context.user_data.pop("pending_private_note", None)
            await message.reply_text(f"已取消保存「{keyword}」，收集的 {len(items)} 条未保存。")
            return True
        if text in PENDING_NOTE_SAVE_WORDS:
            if not items:
                pending["touched"] = time.time()
                await message.reply_text(
                    f"还没有收集到任何消息。请先发送或转发要保存到「{keyword}」的内容，"
                    "或发送「取消」退出。"
                )
                return True
            store: DirectoryStore = context.application.bot_data["store"]
            previous = store.latest_private_note(keyword)
            saved = 0
            for item in items:
                try:
                    store.add_private_note(
                        keyword, str(item.get("body") or ""), user.id,
                        file_id=str(item.get("file_id") or ""),
                        file_type=str(item.get("file_type") or ""),
                        file_name=str(item.get("file_name") or ""),
                        permanent=True,
                    )
                    saved += 1
                except ValueError:
                    continue
            context.user_data.pop("pending_private_note", None)
            total = store.count_private_notes(keyword)
            store.audit(
                f"tg:{user.id}", "private_note.add_permanent", keyword, f"batch={saved}",
            )
            await reply_private_note_saved(
                context, message,
                private_note_saved_text(
                    keyword, total, previous, permanent=True, saved_count=saved,
                ),
                previous,
            )
            return True
        parsed = parse_private_note_command(text)
        if parsed and parsed[0] == "2" and parsed[1] and not parsed[2]:
            pending["touched"] = time.time()
            await message.reply_text(
                f"正在收集「{keyword}」（已收集 {len(items)} 条）。"
                "请先发送「保存」或「取消」，再开始新的关键词。"
            )
            return True
    item = pending_note_item(message)
    if item is None:
        pending["touched"] = time.time()
        await message.reply_text("这类消息暂不支持保存，请发送文字、图片、视频、文件等。")
        return True
    if len(items) >= PENDING_NOTE_MAX_ITEMS:
        await message.reply_text(
            f"单次最多收集 {PENDING_NOTE_MAX_ITEMS} 条，请先发送「保存」。"
        )
        return True
    items.append(item)
    pending["touched"] = time.time()
    context.user_data["preserve_incoming_message_id"] = message.message_id
    media_group = str(getattr(message, "media_group_id", "") or "")
    groups = pending.setdefault("media_groups", [])
    if media_group and media_group in groups:
        return True
    if media_group:
        groups.append(media_group)
    await message.reply_text(
        f"已收集第 {len(items)} 条（{keyword}）。继续发送，或发送「保存」完成、「取消」放弃。"
    )
    return True


def private_notes_page_view(store: DirectoryStore, keyword: str, page: int = 0):
    """Build one list page (10 notes). Returns None if the keyword has no notes.

    Returns ``(text_html, markup, media_items, page, rows, total)`` where
    ``media_items`` are ``(header, row)`` for the media notes on this page.
    """
    page_size = 10
    page = max(0, int(page))
    total = store.count_private_notes(keyword)
    if total <= 0:
        return None
    rows = store.private_notes(keyword, limit=page_size, offset=page * page_size)
    if not rows:
        page = max(0, (total - 1) // page_size)
        rows = store.private_notes(keyword, limit=page_size, offset=page * page_size)
    if not rows:
        return None
    start_no = page * page_size + 1
    lines = [
        f"🗒 <b>{html.escape(keyword)}</b> 私密笔记",
        f"第 {start_no}-{start_no + len(rows) - 1} 条 / 共 {total} 条",
        "",
    ]
    media_items: list = []
    for index, row in enumerate(rows, start_no):
        saved_at = format_beijing_time(row["created_at"])
        lines.append(f"<b>#{index}</b> · {html.escape(saved_at)}")
        body = str(row["body"] or "").strip()
        file_id = str(row["file_id"] or "")
        if body:
            shown = body
            if file_id and len(shown) > PRIVATE_NOTE_PREVIEW_LIMIT:
                shown = shown[:PRIVATE_NOTE_PREVIEW_LIMIT].rstrip() + "…"
            lines.append(html.escape(shown))
        if file_id:
            lines.append(private_note_file_label(row, below=True))
            media_items.append((f"#{index} · {saved_at}", row))
        lines.append("")
    nav: list = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "⬅️ 上一页", callback_data=f"privnote:{page - 1}",
        ))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(
            "下一页 ➡️", callback_data=f"privnote:{page + 1}",
        ))
    markup = InlineKeyboardMarkup([nav]) if nav else None
    return chr(10).join(lines).rstrip(), markup, media_items, page, rows, total


async def send_private_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str,
    page: int = 0,
) -> None:
    """Reply with one page (10 notes) instead of one message per note."""
    message = update.effective_message
    context.user_data["preserve_incoming_message_id"] = message.message_id
    schedule_setting_cleanup(context, message)
    if not keyword:
        sent = await message.reply_text("请直接发送私人笔记关键词。")
        schedule_setting_cleanup(context, sent)
        return
    store: DirectoryStore = context.application.bot_data["store"]
    view = private_notes_page_view(store, keyword, page)
    if view is None:
        sent = await message.reply_text(f"没有找到私密笔记：{keyword}")
        schedule_setting_cleanup(context, sent)
        return
    text, markup, media_items, page, rows, total = view
    context.user_data["privnote_keyword"] = keyword
    sent = await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup,
        disable_web_page_preview=True,
    )
    schedule_setting_cleanup(context, sent)
    if media_items:
        await send_private_note_media(context, message.chat_id, media_items)
    store.audit(
        f"tg:{update.effective_user.id}", "private_note.query", keyword,
        f"page={page}; count={len(rows)}; total={total}",
    )


async def note_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("私人笔记只能私聊机器人使用。")
        return
    if not has_developer_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(developer_only_text(context))
        return
    keyword = context.args[0] if context.args else ""
    body = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    await save_private_note(update, context, keyword, body)


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("私人笔记只能私聊机器人使用。")
        return
    if not has_developer_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(developer_only_text(context))
        return
    await send_private_notes(update, context, context.args[0] if context.args else "")


async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    message = update.effective_message
    if message and context.user_data.get("consumed_private_message") == message.message_id:
        context.user_data.pop("consumed_private_message", None)
        return
    if await handle_pending_private_note(update, context):
        return
    text = (message.text or message.caption or "").strip() if message else ""
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    if (
        context.application.bot_data.get("clone_manager") is not None
        and re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", text)
    ):
        context.user_data["menu_mode"] = "clone_token"
        await process_menu_input(update, context, text)
        return
    if context.user_data.get("menu_mode") == "invite_query":
        await process_menu_input(update, context, text)
        return
    if context.user_data.get("menu_mode") == "renamehist_query":
        await process_menu_input(update, context, text)
        return
    if context.user_data.get("menu_mode") == "invite_member_query":
        await handle_invite_member_query_input(update, context, text)
        return
    if " ".join(text.split()) == "改名记录":
        store_local: DirectoryStore = context.application.bot_data["store"]
        chat_id = callback_group_id(context, update.effective_chat)
        if chat_id is None:
            await message.reply_text("请先在群组管理中选择群组，或到群内发送「改名记录」。")
            return
        if not has_group_permission(context, chat_id, update.effective_user.id, "renamehist"):
            await message.reply_text("你没有改名记录查询权限。")
            return
        context.user_data["menu_mode"] = "renamehist_query"
        context.user_data["selected_group_id"] = chat_id
        await message.reply_text("请发送 @用户名或数字ID（也可回复对方消息）。")
        return
    if " ".join(text.split()) == "邀请链接查询":
        store_local: DirectoryStore = context.application.bot_data["store"]
        chat_id = callback_group_id(context, update.effective_chat)
        if chat_id is None:
            await message.reply_text("请先在群组管理中选择群组，或到群内发送「邀请链接查询」。")
            return
        config = store_local.invite_config(chat_id)
        if not config["is_enabled"]:
            await message.reply_text("该群尚未开启个人邀请链接功能。")
            return
        context.user_data["menu_mode"] = "invite_member_query"
        context.user_data["selected_group_id"] = chat_id
        await message.reply_text(
            "请发送 @用户名或数字ID（也可回复对方消息）。"
        )
        return
    if await send_personal_invite_query(update, context, text):
        return
    if await quick_text_features(update, context, text, True):
        return
    account_match = re.fullmatch(r"1\s*@([A-Za-z0-9_]{5,32})", text)
    username_match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})", text)
    if account_match or username_match:
        previous_mode = str(context.user_data.pop("menu_mode", "") or "")
        if previous_mode:
            clear_menu_input_failures(context, previous_mode)
        matched = account_match or username_match
        await user_info_command(update, context, "@" + matched.group(1))
        return
    if getattr(getattr(message, "forward_origin", None), "sender_user", None):
        previous_mode = str(context.user_data.pop("menu_mode", "") or "")
        if previous_mode:
            clear_menu_input_failures(context, previous_mode)
        await user_info_command(update, context)
        return
    rich_submission = parse_rich_submission_command(text)
    if rich_submission:
        await save_rich_submission(update, context, *rich_submission)
        return
    mode_before = str(context.user_data.get("menu_mode") or "")
    if await process_menu_input(update, context, text):
        if mode_before and context.user_data.get("menu_mode") != mode_before:
            clear_menu_input_failures(context, mode_before)
        return
    note_command = None if config.is_clone else parse_private_note_command(text)
    if note_command:
        if not has_developer_access(context, update.effective_user.id if update.effective_user else None):
            await message.reply_text(developer_only_text(context))
            return
        action, keyword, body = note_command
        if action == "1":
            await save_private_note(update, context, keyword, body)
        elif keyword and (body or publishing_attachment(message)[0]):
            # 2 关键词 内容：立即永久保存
            await save_private_note(update, context, keyword, body, permanent=True)
        elif keyword:
            # 2 关键词：进入待保存模式，收集后续转发/发送的消息
            await start_pending_private_note(update, context, keyword)
        else:
            await message.reply_text(
                "用法：\n2 关键词 内容 —— 立即永久保存\n"
                "2 关键词 —— 之后发送/转发多条消息，再发「保存」一起永久保存\n"
                "查询笔记请直接发送关键词。"
            )
        return
    normalized_note_keyword = " ".join(text.split())
    if (
        normalized_note_keyword
        and has_developer_access(context, update.effective_user.id)
        and store.private_notes(normalized_note_keyword, limit=1)
    ):
        await send_private_notes(update, context, normalized_note_keyword)
        return
    tron_address = extract_tron_address(text)
    if tron_address:
        await send_balance_query(update, context, tron_address)
        return
    if text and await private_keyword_reply(update, context, text):
        return
    if message and message.text:
        await handle_plain_text(update, context, message.text.strip())
        return
    await message.reply_text("文件笔记请用：1 关键词 说明（写在文件说明里）。")


async def process_menu_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    mode = context.user_data.get("menu_mode")
    if not mode:
        return False
    message = update.effective_message
    user = update.effective_user
    store: DirectoryStore = context.application.bot_data["store"]
    if not text:
        await menu_input_error(
            context, message, mode, "请输入文字内容，或发送 /cancel 取消。"
        )
        return True
    if mode == "private_note_query":
        if not has_developer_access(context, user.id if user else None):
            context.user_data.pop("menu_mode", None)
            await message.reply_text(developer_only_text(context))
            return True
        parsed_note = parse_private_note_command(text)
        if parsed_note and parsed_note[1]:
            # 「1 关键词 内容」/「2 关键词 …」交给私人笔记命令处理
            return False
        keyword = " ".join(text.split())
        record_selected_bot_usage(update, store)
        await send_private_notes(update, context, keyword)
        context.user_data["menu_mode"] = mode
        return True
    context.user_data.pop("menu_mode", None)
    if mode.startswith("channel_"):
        if not has_super_admin_access(context, user.id if user else None):
            await message.reply_text("仅超级管理员可管理频道群发。")
            return True
        parts = settings_wizard.input_parts(message, text)
        file_id, file_type, file_name = publishing_attachment(message)
        try:
            if mode == "channel_add":
                original = getattr(message, "original_message", message)
                origin_chat = getattr(getattr(original, "forward_origin", None), "chat", None)
                chat = origin_chat or await context.bot.get_chat(parts[0].strip())
                if chat.type != ChatType.CHANNEL:
                    raise ValueError("只支持添加频道；请转发频道消息或发送频道用户名/ID")
                store.save_broadcast_channel(chat.id, chat.title or str(chat.id), chat.username or "", user.id)
                result, markup = channel_broadcast_view(store, "channels")
            elif mode == "channel_delete":
                if not store.delete_broadcast_channel(int(parts[0].lstrip("#"))):
                    raise ValueError("没有找到这个频道编号")
                result, markup = channel_broadcast_view(store, "channels")
            elif mode == "channel_groupadd":
                store.add_channel_group(parts[0], user.id)
                result, markup = channel_broadcast_view(store, "groups")
            elif mode == "channel_groupdel":
                if not store.delete_channel_group(int(parts[0].lstrip("#"))):
                    raise ValueError("没有找到这个分组编号")
                result, markup = channel_broadcast_view(store, "groups")
            elif mode == "channel_assign":
                group_id = int(parts[1].lstrip("#"))
                if not store.assign_broadcast_channel(int(parts[0].lstrip("#")), group_id or None):
                    raise ValueError("没有找到这个频道编号")
                result, markup = channel_broadcast_view(store, "channels")
            elif mode in {"channel_messageadd", "channel_messageedit"}:
                content_message = getattr(message, "original_message", message)
                raw_text, entities_json = capture_content(content_message)
                file_id, file_type, file_name = publishing_attachment(content_message)
                validate_content(raw_text, file_id, file_type)
                if mode.endswith("add"):
                    message_id = store.save_channel_message(
                        parts[0], raw_text, file_id, file_type, file_name,
                        entities_json, user.id,
                    )
                else:
                    message_id = store.save_channel_message(
                        parts[1], raw_text, file_id, file_type, file_name,
                        entities_json, user.id, int(parts[0].lstrip("#")),
                    )
                context.user_data["channel_message_selected"] = message_id
                result, markup = channel_broadcast_view(store, "messages", message_id)
            elif mode == "channel_messagedel":
                if not store.delete_channel_message(int(parts[0].lstrip("#"))):
                    raise ValueError("没有找到这个消息编号")
                context.user_data.pop("channel_message_selected", None)
                result, markup = channel_broadcast_view(store, "messages")
            elif mode == "channel_schedule":
                target = parts[1].strip()
                if target == "全部频道":
                    target_type, target_id = "all", 0
                elif re.fullmatch(r"频道分组\s*#?\d+", target):
                    target_type, target_id = "group", int(re.search(r"\d+", target).group())
                elif re.fullmatch(r"频道\s*#?\d+", target):
                    target_type, target_id = "channel", int(re.search(r"\d+", target).group())
                else:
                    raise ValueError("发送目标请填写：全部频道、频道分组 #1 或频道 #2")
                store.schedule_channel_message(
                    int(parts[0].lstrip("#")), target_type, target_id,
                    beijing_datetime_to_utc_text(parts[2]), user.id,
                )
                result, markup = channel_broadcast_view(
                    store, "send", int(parts[0].lstrip("#"))
                )
            else:
                raise ValueError("未知频道群发设置")
        except (TypeError, ValueError, TelegramError) as exc:
            await menu_input_error(context, message, mode, str(exc))
            return True
        store.audit(f"tg:{user.id}", mode.replace("_", "."), "channel-broadcast")
        await message.reply_text(result, reply_markup=markup)
        return True
    if mode == "clone_token":
        manager: CloneManager | None = context.application.bot_data.get("clone_manager")
        if not manager:
            await message.reply_text("当前机器人不支持继续克隆。")
            return True
        try:
            clone_id, username = await manager.request(
                user.id, text, owner_name=user.full_name or ""
            )
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return True
        context.user_data.pop("menu_mode", None)
        try:
            await message.delete()
        except TelegramError:
            pass
        with persistent_message():
            await context.bot.send_message(
                user.id,
                f"克隆申请已提交：@{username}\n编号：#{clone_id}\n"
                "审核通过后自动启动，你将是该机器人的超级管理员；审核结果会发送给你。",
            )
        store.audit(f"tg:{user.id}", "clone.request", str(clone_id), username)
        if not manager.manage_processes:
            # 子机器人上的申请：由母机器人推送审核
            return True
        for developer_id in all_developer_ids(context):
            try:
                with persistent_message():
                    await context.bot.send_message(
                        developer_id,
                        f"🤖 新克隆申请 #{clone_id}\n"
                        f"申请人：{user.full_name} ({user.id})\n"
                        f"机器人：@{username}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ 通过", callback_data=f"clone:approve:{clone_id}"),
                            InlineKeyboardButton("❌ 拒绝", callback_data=f"clone:reject:{clone_id}"),
                        ]]),
                    )
            except TelegramError:
                logging.exception("Failed to send clone approval request")
        return True
    if mode in {
        "admin_manage_add", "admin_manage_delete",
        "admin_manage_permissions", "admin_manage_reset",
    }:
        if not has_super_admin_access(context, user.id if user else None):
            await message.reply_text("仅超级管理员可管理管理员。")
            return True
        context.user_data["preserve_incoming_message_id"] = message.message_id
        can_manage_all = is_developer_user(context, user.id)
        visible_admin_ids = {
            int(row["user_id"]) for row in store.list_bot_admins(
                viewer_id=user.id, include_all=can_manage_all
            )
        }
        try:
            if mode == "admin_manage_permissions":
                parts = settings_wizard.input_parts(message, text, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    raise ValueError("格式：管理员数字ID | 权限1,权限2")
                if not can_manage_all and int(parts[0]) not in visible_admin_ids:
                    raise ValueError("不能管理与自己无关的管理员")
                permissions = {
                    item.strip().casefold() for item in parts[1].split(",") if item.strip()
                }
                invalid = permissions - ADMIN_PERMISSIONS
                if invalid:
                    raise ValueError("未知权限：" + ", ".join(sorted(invalid)))
                if not store.set_bot_admin_permissions(int(parts[0]), permissions):
                    raise ValueError("只可给普通管理员分配权限")
                result = "管理员权限已更新。"
            else:
                raw_id = text.strip()
                if not raw_id.isdigit():
                    raise ValueError("请输入 Telegram 数字ID")
                target_id = int(raw_id)
                if (
                    mode != "admin_manage_add" and not can_manage_all
                    and target_id not in visible_admin_ids
                ):
                    raise ValueError("不能管理与自己无关的管理员")
                if mode == "admin_manage_add":
                    store.add_bot_admin(target_id, "admin", user.id)
                    result = f"管理员 {target_id} 已添加。"
                elif mode == "admin_manage_reset":
                    if not store.set_bot_admin_permissions(target_id, set()):
                        raise ValueError("只可重置普通管理员权限")
                    result = f"管理员 {target_id} 权限已重置。"
                else:
                    config: Config = context.application.bot_data["config"]
                    if target_id in config.developer_ids:
                        raise ValueError("不能删除开发者")
                    if target_id in config.admin_ids | config.super_admin_ids:
                        raise ValueError("该账号写在 .env 中，需先修改配置并重启")
                    if not store.remove_bot_admin(target_id):
                        raise ValueError("没有找到这个管理员")
                    result = f"管理员 {target_id} 已删除。"
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return True
        sent = await message.reply_text(result, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ 返回管理员管理", callback_data="admin:admins")
        ]]))
        schedule_setting_cleanup(context, message)
        schedule_setting_cleanup(context, sent)
        context.user_data.pop("preserve_incoming_message_id", None)
        return True
    if mode in {"custom_button_support", "custom_button_menu", "custom_button_delete"}:
        if not has_super_admin_access(context, user.id if user else None):
            await message.reply_text("仅超级管理员可设置自定义按钮。")
            return True
        context.user_data["preserve_incoming_message_id"] = message.message_id
        try:
            if mode == "custom_button_delete":
                raw_id = text.lstrip("#")
                if not raw_id.isdigit() or not store.delete_custom_button(int(raw_id)):
                    raise ValueError("没有找到这个按钮编号")
                result = f"按钮 #{raw_id} 已删除。"
            elif mode == "custom_button_support":
                parts = settings_wizard.input_parts(message, text)
                if len(parts) != 3:
                    raise ValueError("格式：按钮文字 | 联系姓名 | @用户名或数字ID")
                target_value = parts[2].lstrip("@")
                known = store.find_known_user(target_value)
                if target_value.isdigit():
                    target_id = int(target_value)
                    username = ""
                elif known:
                    target_id = int(known["user_id"])
                    username = str(known["username"] or target_value)
                else:
                    raise ValueError("该用户名需要先私聊机器人一次，才能接收双向消息")
                button_id = store.add_custom_button(
                    "support", parts[0], user.id, parts[1], username, target_id
                )
                result = f"双向联系按钮 #{button_id} 已添加。"
            else:
                parts = settings_wizard.input_parts(message, text, 1)
                if len(parts) != 2:
                    raise ValueError("格式：按钮文字 | 回复内容或https://链接")
                is_url = bool(re.match(r"^https?://", parts[1], re.I))
                button_id = store.add_custom_button(
                    "menu", parts[0], user.id,
                    response_text="" if is_url else parts[1],
                    button_url=parts[1] if is_url else "",
                )
                result = f"自定义按钮 #{button_id} 已添加。"
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return True
        view_text, view_keyboard = custom_buttons_admin_view(store)
        sent = await message.reply_text(
            result + "\n\n" + view_text, reply_markup=view_keyboard
        )
        schedule_setting_cleanup(context, message)
        schedule_setting_cleanup(context, sent)
        context.user_data.pop("preserve_incoming_message_id", None)
        return True
    if mode == "tron_monitor_user_query":
        if not has_developer_access(context, user.id if user else None):
            await message.reply_text(developer_only_text(context))
            return True
        owner = store.tron_monitor_owner_by_query(text)
        if not owner:
            await menu_input_error(
                context, message, mode,
                "没有找到该用户启用中的监控，请发送 @用户名或数字ID。",
            )
            return True
        result, keyboard = tron_monitor_user_view(store, owner)
        await message.reply_text(
            result, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return True
    if mode == "tron_monitor_address":
        try:
            address = validate_tron_address(text)
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return True
        context.user_data["tron_monitor_address"] = address
        prompt = await tron_monitor_asset_prompt(context, address)
        await message.reply_text(
            prompt,
            parse_mode=ParseMode.HTML,
            reply_markup=tron_monitor_prompt_keyboard(context),
        )
        return True
    if mode in {
        "tron_monitor_low", "tron_monitor_high",
        "tron_monitor_transfer", "tron_monitor_delete",
    }:
        try:
            if mode in {"tron_monitor_low", "tron_monitor_high"}:
                key = "tron_monitor_low" if mode.endswith("low") else "tron_monitor_high"
                if text in {"关闭", "关", "-", "无", "0"}:
                    candidate = ""
                else:
                    value = Decimal(text)
                    if not value.is_finite() or value <= 0:
                        raise ValueError
                    candidate = str(value)
                low = candidate if key == "tron_monitor_low" else str(context.user_data.get("tron_monitor_low") or "")
                high = candidate if key == "tron_monitor_high" else str(context.user_data.get("tron_monitor_high") or "")
                if low and high and Decimal(low) >= Decimal(high):
                    raise ValueError("小于阈值必须低于大于阈值")
                context.user_data[key] = candidate
            elif mode == "tron_monitor_transfer":
                if text in {"关闭", "关", "-", "无", "0"}:
                    context.user_data["tron_monitor_notify"] = False
                else:
                    value = Decimal(text)
                    if value < Decimal("0.1"):
                        raise ValueError("最小播报金额不能小于0.1")
                    context.user_data["tron_monitor_notify"] = True
                    context.user_data["tron_monitor_minimum"] = str(value)
            else:
                if not text.isdigit() or not 0 <= int(text) <= 30:
                    raise ValueError("撤回天数范围为0-30，0表示不自动撤回")
                context.user_data["tron_monitor_delete_days"] = int(text)
        except (InvalidOperation, ValueError) as exc:
            await menu_input_error(
                context, message, mode,
                str(exc) if str(exc) else "请输入有效数字，或发送“关闭”",
            )
            return True
        context.user_data.pop("menu_mode", None)
        setup_text, setup_keyboard = tron_monitor_setup_view(context)
        await message.reply_text(setup_text, reply_markup=setup_keyboard)
        return True
    if mode == "directory_search":
        query = " ".join(text.split())
        await directory_search_reply(update, store, query, "menu_search")
        return True
    if mode == "tron_search":
        await send_balance_query(update, context, text)
        return True
    if mode == "account_info":
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", text.strip()):
            await menu_input_error(
                context, message, mode, "请只发送 @用户名，例如：@jiuye"
            )
            return True
        record_selected_bot_usage(update, store)
        await user_info_command(update, context, text)
        return True
    if mode not in {"badword_add", "badword_delete"}:
        return False
    if not has_permission(context, user.id if user else None, "moderation"):
        await message.reply_text("仅管理员可维护违规关键词。")
        return True
    try:
        if mode == "badword_add":
            keyword_id = store.add_moderation_keyword(text, user.id)
            store.audit(f"tg:{user.id}", "moderation_keyword.add", str(keyword_id), text)
            result = f"已添加违规关键词：{text}"
        else:
            removed = store.remove_moderation_keyword_value(text)
            if not removed:
                raise ValueError("没有找到这个违规关键词")
            store.audit(f"tg:{user.id}", "moderation_keyword.delete", text)
            result = f"已删除违规关键词：{text}"
    except ValueError as exc:
        await menu_input_error(context, message, mode, str(exc))
        return True
    await message.reply_text(
        result,
        reply_markup=moderation_menu_keyboard(has_super_admin_access(context, user.id)),
    )
    return True


async def send_stored_media(bot, chat_id: int, row) -> None:
    with advertisement_message(), persistent_message():
        source_chat_id = 0
        source_message_id = 0
        try:
            keys = row.keys() if hasattr(row, "keys") else ()
            if "source_chat_id" in keys:
                source_chat_id = int(row["source_chat_id"] or 0)
            if "source_message_id" in keys:
                source_message_id = int(row["source_message_id"] or 0)
        except (TypeError, ValueError):
            source_chat_id = source_message_id = 0
        if source_chat_id and source_message_id:
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                return
            except TelegramError:
                pass
        await send_content(bot, chat_id, row, buttons_markup(row))


QUICK_BUTTON_COLORS = {
    "default": "默认", "primary": "蓝色", "success": "绿色", "danger": "红色",
}


def quick_post_markup(store: DirectoryStore | None, row) -> InlineKeyboardMarkup | None:
    buttons = store.quick_post_buttons(int(row["id"])) if store else []
    if not buttons and not store and row["button_text"] and row["button_url"]:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(str(row["button_text"]), url=str(row["button_url"]))
        ]])
    if not buttons:
        return None
    rows, short_row = [], []
    for button in buttons:
        kwargs = {}
        color = str(button["color"] or "default")
        api_kwargs = {}
        if color != "default":
            api_kwargs["style"] = color
        custom_emoji_id = str(button["custom_emoji_id"] or "")
        if custom_emoji_id:
            api_kwargs["icon_custom_emoji_id"] = custom_emoji_id
        if api_kwargs:
            # PTB forwards current Bot API button fields through api_kwargs.
            kwargs["api_kwargs"] = api_kwargs
        rendered = InlineKeyboardButton(
            str(button["text"]), url=str(button["url"]), **kwargs,
        )
        if str(button["width"] or "long") == "short":
            short_row.append(rendered)
            if len(short_row) == 2:
                rows.append(short_row)
                short_row = []
            continue
        if short_row:
            rows.append(short_row)
            short_row = []
        rows.append([rendered])
    if short_row:
        rows.append(short_row)
    return InlineKeyboardMarkup(rows)


def quick_post_menu_text(
    store: DirectoryStore, row, bot_username: str = ""
) -> str:
    inline_name = f"@{bot_username} {row['share_code']}" if bot_username else str(row["share_code"])
    posts = store.quick_posts(int(row["chat_id"]))
    buttons = store.quick_post_buttons(int(row["id"]))
    schedules = store.pending_quick_post_schedules(int(row["chat_id"]))
    button_lines = [
        f"  #{button['id']} {button['text']}（{QUICK_BUTTON_COLORS.get(str(button['color']), '默认')}，"
        f"{'短按钮' if str(button['width']) == 'short' else '长按钮'}）"
        for button in buttons
    ] or ["  暂无"]
    schedule_lines = [
        f"  #{item['id']} {item['name']} · {format_beijing_time(item['run_at'])}"
        for item in schedules[:10]
    ] or ["  暂无"]
    return (
        "✏️ 快捷发布\n"
        f"已保存消息：{len(posts)} 条\n"
        f"当前消息：#{row['id']} {row['name']}\n\n"
        f"├媒体图片: {'✅' if row['file_id'] else '❌'}\n"
        f"├链接按钮: {len(buttons)} 个\n"
        f"├文本内容: {'✅' if row['text'] else '❌'}\n"
        f"└内联分享: {inline_name}\n\n"
        "按钮：\n" + "\n".join(button_lines) + "\n\n"
        "待发布：\n" + "\n".join(schedule_lines)
    )


def quick_post_menu_keyboard(
    store: DirectoryStore, row, section: str = "main"
) -> InlineKeyboardMarkup:
    posts = store.quick_posts(int(row["chat_id"]))
    rows = []
    if section == "messages":
        for index in range(0, len(posts), 2):
            rows.append([
                InlineKeyboardButton(
                    ("✅ " if int(post["id"]) == int(row["id"]) else "")
                    + f"#{post['id']} {post['name']}",
                    callback_data=f"quickpost:select:{post['id']}",
                ) for post in posts[index:index + 2]
            ])
        rows.extend([[
            InlineKeyboardButton("➕ 添加消息", callback_data="quickpost:set:add"),
            InlineKeyboardButton("🗑 删除消息", callback_data="quickpost:delete"),
        ], [
            InlineKeyboardButton("输入内容", callback_data="quickpost:set:text"),
            InlineKeyboardButton("🖼 媒体内容", callback_data="quickpost:set:media"),
        ], [InlineKeyboardButton("🧹 清空当前", callback_data="quickpost:clear")]])
    elif section == "buttons":
        rows.extend([[
            InlineKeyboardButton("➕ 添加按钮", callback_data="quickpost:set:button"),
            InlineKeyboardButton("✏️ 修改按钮", callback_data="quickpost:set:buttonedit"),
        ], [InlineKeyboardButton("➖ 删除按钮", callback_data="quickpost:set:buttondel")]])
    elif section == "publish":
        rows.extend([[
            InlineKeyboardButton("📤 立即发布", callback_data="quickpost:publish"),
            InlineKeyboardButton("⏰ 定时发布", callback_data="quickpost:set:schedule"),
        ], [
            InlineKeyboardButton("取消定时发布", callback_data="quickpost:set:schedulecancel"),
            InlineKeyboardButton("↗️ 分享", switch_inline_query=str(row["share_code"])),
        ]])
    else:
        rows.extend([[
            InlineKeyboardButton("🗂 消息管理", callback_data="quickpost:view:messages"),
            InlineKeyboardButton("🔘 按钮管理", callback_data="quickpost:view:buttons"),
        ], [InlineKeyboardButton("📤 发布管理", callback_data="quickpost:view:publish")]])
    if section != "main":
        rows.append([InlineKeyboardButton("⬅️ 返回快捷发布", callback_data="quickpost:menu")])
    else:
        rows.append([InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")])
    return InlineKeyboardMarkup(rows)


async def send_quick_post(
    bot, chat_id: int, row, store: DirectoryStore | None = None
) -> None:
    with persistent_message(), advertisement_message():
        await send_content(bot, chat_id, row, quick_post_markup(store, row))


async def quick_post_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    if not query:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    raw_query = query.query.strip()
    tron_address = extract_tron_address(raw_query)
    if tron_address:
        try:
            result = await context.application.bot_data["chain"].tron_balance(
                tron_address
            )
        except (ValueError, ChainQueryError):
            await query.answer([], cache_time=1, is_personal=True)
            return
        count_cache = context.application.bot_data.setdefault(
            "tron_inline_count_cache", {}
        )
        cache_key = (query.from_user.id, tron_address)
        now_value = datetime.now(timezone.utc).timestamp()
        if now_value - float(count_cache.get(cache_key, 0)) > 30:
            store.add_chain_query(
                query.from_user.id, tron_address, "inline_share",
                f"TRX={result.trx:f}, USDT={result.usdt:f}",
            )
            count_cache[cache_key] = now_value
        text, _ = tron_result_view(
            result,
            emoji_ids=context.application.bot_data.get("kkpay_emoji_ids"),
            direction_emoji_ids=context.application.bot_data.get(
                "tron_direction_emoji_ids"
            ),
            status_emoji_ids=context.application.bot_data.get(
                "tron_status_emoji_ids"
            ),
            query_count=store.chain_query_count(),
        )
        bot_username = str(context.application.bot_data.get("bot_username") or "")
        inline_result = InlineQueryResultArticle(
            f"tron-{tron_address}", "波场地址查询",
            InputTextMessageContent(text, parse_mode=ParseMode.HTML),
            reply_markup=tron_forward_keyboard(tron_address, bot_username),
            description=f"{tron_address[:8]}...{tron_address[-6:]}",
        )
        await query.answer([inline_result], cache_time=1, is_personal=True)
        return
    row = store.quick_post_by_code(raw_query)
    if not row or (not row["text"] and not row["file_id"]):
        await query.answer([], cache_time=1, is_personal=True)
        return
    result_id = f"quick-{row['id']}"
    title = str(row["name"] or "快捷发布")
    caption = str(row["text"] or "") or None
    caption_options = dict(caption=caption, caption_entities=content_entities(row), parse_mode=None)
    markup = quick_post_markup(store, row)
    file_type = str(row["file_type"] or "")
    file_id = str(row["file_id"] or "")
    if file_type == "photo":
        result = InlineQueryResultCachedPhoto(result_id, file_id, title=title, **caption_options, reply_markup=markup)
    elif file_type == "video":
        result = InlineQueryResultCachedVideo(result_id, file_id, title, **caption_options, reply_markup=markup)
    elif file_type == "animation":
        result = InlineQueryResultCachedMpeg4Gif(result_id, file_id, title=title, **caption_options, reply_markup=markup)
    elif file_type == "audio":
        result = InlineQueryResultCachedAudio(result_id, file_id, **caption_options, reply_markup=markup)
    elif file_type == "voice":
        result = InlineQueryResultCachedVoice(result_id, file_id, title, **caption_options, reply_markup=markup)
    elif file_type == "sticker":
        if caption:
            await query.answer([], cache_time=1, is_personal=True)
            return
        result = InlineQueryResultCachedSticker(result_id, file_id, reply_markup=markup)
    elif file_type == "video_note":
        await query.answer([], cache_time=1, is_personal=True)
        return
    elif file_id:
        result = InlineQueryResultCachedDocument(result_id, title, file_id, **caption_options, reply_markup=markup)
    else:
        result = InlineQueryResultArticle(
            result_id, title, InputTextMessageContent(str(row["text"]), entities=content_entities(row), parse_mode=None),
            reply_markup=markup, description=str(row["text"] or "")[:100],
        )
    await query.answer([result], cache_time=1, is_personal=True)


async def group_ad_text(store: DirectoryStore, chat_id, position: str) -> str:
    """Plain text for prefix/suffix merge. Media (file_id) stays out of the text merge."""
    if not isinstance(chat_id, int) or chat_id >= 0:
        return ""
    row = store.group_ad(chat_id, position)
    if not row or row["file_id"]:
        return ""
    return str(row["text"] or "")


async def send_due_group_ads(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for row in store.due_group_ads():
        try:
            await send_stored_media(context.bot, int(row["chat_id"]), row)
        except TelegramError as exc:
            store.audit("group-ad", "send.failed", str(row["id"]), str(exc))


async def send_due_quick_posts(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for schedule in store.due_quick_posts():
        try:
            post = store.quick_post(int(schedule["chat_id"]), int(schedule["post_id"]))
            await send_quick_post(context.bot, int(schedule["chat_id"]), post, store)
        except (ValueError, TelegramError) as exc:
            store.finish_quick_post_schedule(int(schedule["id"]), str(exc))
            store.audit("quick-post", "scheduled.failed", str(schedule["id"]), str(exc))
        else:
            store.finish_quick_post_schedule(int(schedule["id"]))
            store.audit("quick-post", "scheduled.sent", str(schedule["id"]))


def channel_broadcast_view(
    store: DirectoryStore, section: str = "main", selected_id: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    channels = store.broadcast_channels()
    groups = store.channel_groups()
    messages = store.channel_messages()
    schedules = store.channel_schedules()
    if section == "channels":
        lines = ["📺 频道管理", ""] + [
            f"#{row['id']} {row['title']}"
            f"（{row['group_name'] or '未分组'}，{row['chat_id']}）"
            for row in channels
        ]
        if not channels:
            lines.append("暂无频道。请先把机器人设为频道管理员。")
        rows = [[
            InlineKeyboardButton("➕ 添加频道", callback_data="channelbroadcast:set:add"),
            InlineKeyboardButton("➖ 删除频道", callback_data="channelbroadcast:set:delete"),
        ], [InlineKeyboardButton("🗂 设置分组", callback_data="channelbroadcast:set:assign")]]
    elif section == "groups":
        lines = ["🗂 频道分组", ""] + [
            f"#{row['id']} {row['name']}（{row['channel_count']} 个频道）" for row in groups
        ]
        if not groups:
            lines.append("暂无分组。")
        rows = [[
            InlineKeyboardButton("➕ 添加分组", callback_data="channelbroadcast:set:groupadd"),
            InlineKeyboardButton("➖ 删除分组", callback_data="channelbroadcast:set:groupdel"),
        ]]
    elif section == "messages":
        lines = ["📝 频道消息", ""] + [
            f"#{row['id']} {row['name']}（{'媒体' if row['file_id'] else '文字'}）"
            for row in messages
        ]
        if not messages:
            lines.append("暂无已保存消息。")
        rows = []
        for start in range(0, len(messages), 2):
            rows.append([InlineKeyboardButton(
                ("✅ " if int(item["id"]) == selected_id else "")
                + f"#{item['id']} {item['name']}",
                callback_data=f"channelbroadcast:message:select:{item['id']}",
            ) for item in messages[start:start + 2]])
        rows.extend([[
            InlineKeyboardButton("➕ 添加消息", callback_data="channelbroadcast:set:messageadd"),
            InlineKeyboardButton("✏️ 修改消息", callback_data="channelbroadcast:set:messageedit"),
        ], [InlineKeyboardButton("➖ 删除消息", callback_data="channelbroadcast:set:messagedel")]])
    elif section == "send":
        message = store.channel_message(selected_id) if selected_id else None
        lines = ["📤 频道发送", "", f"当前消息：{('#' + str(message['id']) + ' ' + message['name']) if message else '未选择'}"]
        lines.extend(["", "待发送："] + [
            f"#{row['id']} {row['name']} · {format_beijing_time(row['run_at'])}"
            for row in schedules[:10]
        ])
        if not schedules:
            lines.append("暂无定时任务。")
        rows = [[InlineKeyboardButton("全部频道", callback_data="channelbroadcast:send:all:0")]]
        rows.extend([[InlineKeyboardButton(
            f"分组：{row['name']}", callback_data=f"channelbroadcast:send:group:{row['id']}"
        )] for row in groups])
        rows.extend([[InlineKeyboardButton(
            f"频道：{row['title']}", callback_data=f"channelbroadcast:send:channel:{row['id']}"
        )] for row in channels])
        rows.append([InlineKeyboardButton("⏰ 定时发送", callback_data="channelbroadcast:set:schedule")])
    else:
        text = (
            "📣 频道群发\n\n"
            f"频道：{len(channels)} 个　分组：{len(groups)} 个\n"
            f"消息：{len(messages)} 条　待发送：{len(schedules)} 条\n\n"
            "请按分类管理。"
        )
        return text, InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📺 频道管理", callback_data="channelbroadcast:view:channels"),
                InlineKeyboardButton("🗂 分组管理", callback_data="channelbroadcast:view:groups"),
            ],
            [
                InlineKeyboardButton("📝 消息管理", callback_data="channelbroadcast:view:messages"),
                InlineKeyboardButton("📤 发送管理", callback_data="channelbroadcast:view:send"),
            ],
            [InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")],
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回频道群发", callback_data="channelbroadcast:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def send_channel_broadcast(
    context: ContextTypes.DEFAULT_TYPE, message_row, target_type: str, target_id: int
) -> tuple[int, int, str]:
    store: DirectoryStore = context.application.bot_data["store"]
    sent = failed = 0
    errors = []
    for channel in store.channel_targets(target_type, target_id):
        try:
            with persistent_message(), advertisement_message():
                await send_content(context.bot, int(channel["chat_id"]), message_row)
            sent += 1
        except (TelegramError, ValueError) as exc:
            failed += 1
            errors.append(f"{channel['chat_id']}: {exc}")
    return sent, failed, "; ".join(errors)[:500]


async def send_due_channel_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for schedule in store.due_channel_schedules():
        message = store.channel_message(int(schedule["message_id"]))
        if not message:
            store.finish_channel_schedule(int(schedule["id"]), 0, 1, "消息不存在")
            continue
        sent, failed, error = await send_channel_broadcast(
            context, message, str(schedule["target_type"]), int(schedule["target_id"])
        )
        store.finish_channel_schedule(int(schedule["id"]), sent, failed, error)
        store.audit("channel-broadcast", "scheduled.sent", str(schedule["id"]), f"成功{sent} 失败{failed}")


async def pin_raffle_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, raffle_id: int | str
) -> bool:
    """Pin a raffle-related group message silently; never raise (e.g. no pin rights)."""
    try:
        if not message_id or int(chat_id) >= 0:
            return False
        await context.bot.pin_chat_message(
            chat_id, message_id, disable_notification=True
        )
        return True
    except Exception as exc:  # noqa: BLE001 - pinning is best-effort
        logging.info("Could not pin raffle message %s in %s: %s", message_id, chat_id, exc)
        try:
            context.application.bot_data["store"].audit(
                "bot", "raffle.pin_failed", str(raffle_id), str(exc)[:300]
            )
        except Exception:  # noqa: BLE001
            pass
        return False



def parse_raffle_condition_text(value: str) -> dict:
    """Parse free-form condition lines into a structured dict."""
    text = str(value or "").strip()
    result = {
        "channel": "", "messages": 0, "keyword": "", "boosts": 0,
        "require_channel": False, "require_messages": False,
        "require_keyword": False, "require_boosts": False,
    }
    if not text or text in {"无", "无条件", "-", "0"}:
        return result
    chunks = re.split(r"[\n,，;；|]+", text)
    for chunk in chunks:
        item = chunk.strip()
        if not item:
            continue
        channel_match = re.match(r"^(?:频道|关注|channel)\s*(@?[A-Za-z0-9_]{4,}|-?\d+)\s*$", item, re.I)
        msg_match = re.match(r"^(?:发言|消息|messages?)\s*(\d+)\s*$", item, re.I)
        kw_match = re.match(r"^(?:关键词|关键字|keyword)\s*(.+)$", item, re.I)
        boost_match = re.match(r"^(?:助推|boosts?)\s*(\d+)\s*$", item, re.I)
        if channel_match:
            result["channel"] = channel_match.group(1).strip()
            result["require_channel"] = True
        elif msg_match:
            result["messages"] = int(msg_match.group(1))
            result["require_messages"] = True
        elif kw_match:
            result["keyword"] = kw_match.group(1).strip()
            result["require_keyword"] = True
        elif boost_match:
            result["boosts"] = int(boost_match.group(1))
            result["require_boosts"] = True
        else:
            raise ValueError(
                "条件格式示例：频道 @mychannel / 发言 10 / 关键词 抽奖 / 助推 1"
            )
    return result


def name_history_view(
    store: DirectoryStore, chat_id: int, user_id: int, display_name: str = "",
) -> str:
    profile = store.get_group_member_profile(chat_id, user_id)
    rows = store.group_name_history(chat_id, user_id, limit=50)
    if not display_name:
        if profile:
            display_name = str(profile["display_name"] or user_id)
        else:
            member = store.find_group_user(chat_id, str(user_id))
            display_name = (
                str(member["display_name"] or member["username"] or user_id)
                if member else str(user_id)
            )
    lines = [
        f"📝 {html.escape(display_name)} 的改名记录",
        f"用户ID：{user_id}",
        "",
    ]
    if profile:
        lines.append(
            "当前："
            f"{html.escape(str(profile['first_name'] or ''))} "
            f"{html.escape(str(profile['last_name'] or ''))}".strip()
            + f" / {html.escape(str(profile['display_name'] or ''))}"
        )
        lines.append("")
    if not rows:
        lines.append("暂无改名记录。")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• {format_beijing_time(row['changed_at'])}\n"
            f"  旧：{html.escape(str(row['old_display'] or '').strip() or '(空)')}\n"
            f"  新：{html.escape(str(row['new_display'] or '').strip() or '(空)')}"
        )
    return "\n".join(lines)


def parse_min_participants(value: object) -> int:
    raw = str(value if value is not None else "").strip()
    if raw in {"", "无", "不限", "不限制", "-", "关闭"}:
        return 0
    if not raw.isdigit():
        raise ValueError("最少参与人数请填写整数，0 表示不限制")
    number_value = int(raw)
    if number_value > 100000:
        raise ValueError("最少参与人数不能大于 100000")
    return number_value


def build_raffle_extras_from_pro(answers: list[str]) -> tuple[str, int, str, dict]:
    """Return ends_at, winner_count, prize, extras from raffle_pro answers."""
    if len(answers) < 8:
        raise ValueError("样板抽奖信息不完整")
    title = str(answers[0] or "").strip()
    rules_raw = str(answers[1] or "").strip()
    if rules_raw in {"无", "-", "0"}:
        rules: list[str] = []
    else:
        rules = [line.strip() for line in rules_raw.replace("\r", "").split("\n") if line.strip()]
    ends_at = beijing_datetime_to_utc_text(str(answers[2] or "").strip())
    stats_raw = str(answers[3] or "").strip()
    if stats_raw in {"立即", "现在", "0"}:
        stats_start_mode = "immediate"
        stats_start_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    else:
        stats_start_mode = "datetime"
        stats_start_at = beijing_datetime_to_utc_text(stats_raw, allow_past=True)
    conditions = parse_raffle_condition_text(str(answers[4] or ""))
    how_to = str(answers[5] or "").strip()
    prize_raw = str(answers[6] or "").strip()
    prize_lines = [line.strip() for line in prize_raw.replace("\r", "").split("\n") if line.strip()]
    if len(prize_lines) > 1 or (
        prize_lines and re.match(r"^\s*\d+\s*[*xX×]\s*.+", prize_lines[0])
    ):
        prize = " | ".join(prize_lines)
        tiers = []
        for item in prize_lines:
            match = re.match(r"^\s*(\d+)\s*[*xX×]\s*(.+?)\s*$", item)
            if not match:
                raise ValueError("多档奖品每行格式：数量*奖品")
            tiers.append((int(match.group(1)), match.group(2)))
        winner_count = sum(q for q, _ in tiers)
    else:
        parts = [part.strip() for part in re.split(r"[|｜]", prize_raw) if part.strip()]
        if len(parts) < 2 or not parts[0].isdigit():
            raise ValueError("单档奖品格式：中奖人数 | 奖品")
        winner_count = int(parts[0])
        prize = " | ".join(parts[1:])
    parse_raffle_prizes(prize, winner_count, strict=True)
    recur = str(answers[7] or "").strip() in {"是", "开启", "on", "1", "yes"}
    min_participants = parse_min_participants(answers[8] if len(answers) > 8 else "")
    join_keyword = str(conditions.get("keyword") or "")
    channel_ref = str(conditions.get("channel") or "")
    min_messages = int(conditions.get("messages") or 0)
    if min_messages > 0 and (not how_to or "按钮" in how_to):
        if recur:
            how_to = f"当天发言达到 {min_messages} 条即自动参与，无需点击按钮。"
        else:
            how_to = f"活动期间群内发言达到 {min_messages} 条即自动参与，无需点击按钮。"
        if join_keyword:
            how_to += f"也可发送关键词：{join_keyword}"
    extras = {
        "title": title,
        "rules_json": json.dumps(rules, ensure_ascii=False),
        "conditions_json": json.dumps(conditions, ensure_ascii=False),
        "how_to_join": how_to,
        "join_keyword": join_keyword,
        "channel_ref": channel_ref,
        "min_messages": int(conditions.get("messages") or 0),
        "min_boosts": int(conditions.get("boosts") or 0),
        "recur_daily": 1 if recur else 0,
        "stats_start_mode": stats_start_mode,
        "stats_start_at": stats_start_at,
        "min_participants": min_participants,
    }
    extras["template_json"] = json.dumps(
        {
            "title": title,
            "rules": rules,
            "conditions": conditions,
            "how_to_join": how_to,
            "prize": prize,
            "winner_count": winner_count,
            "recur_daily": 1 if recur else 0,
            "stats_start_mode": stats_start_mode,
            "draw_clock": str(answers[2] or "").strip(),
            "min_participants": min_participants,
        },
        ensure_ascii=False,
    )
    return ends_at, winner_count, prize, extras


def _raffle_field(raffle, key: str, default=""):
    try:
        keys = set(raffle.keys())
    except Exception:
        keys = set()
    if key in keys:
        value = raffle[key]
        return default if value is None else value
    return default


async def count_user_chat_boosts(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
) -> int:
    """Telegram group boost count (⚡N beside member name) via getUserChatBoosts."""
    getter = getattr(context.bot, "get_user_chat_boosts", None)
    if not callable(getter):
        return 0
    try:
        result = await getter(chat_id=chat_id, user_id=user_id)
    except TypeError:
        try:
            result = await getter(chat_id, user_id)
        except TelegramError:
            return 0
    except TelegramError:
        return 0
    boosts = getattr(result, "boosts", None)
    if boosts is None and isinstance(result, dict):
        boosts = result.get("boosts")
    return len(boosts or [])


async def evaluate_raffle_unmet(
    context: ContextTypes.DEFAULT_TYPE, raffle, user_id: int,
) -> list[str]:
    """Return unmet condition labels for a winner; empty if all met or none required.

    Conditions never block joining — only annotate winners on draw.
    Boost count uses Telegram chat boosts (getUserChatBoosts / ⚡N), not keywords.
    """
    conditions = parse_raffle_conditions(_raffle_field(raffle, "conditions_json", "{}"))
    unmet: list[str] = []
    store: DirectoryStore = context.application.bot_data["store"]
    chat_id = int(raffle["chat_id"])
    channel = str(
        _raffle_field(raffle, "channel_ref", "") or conditions.get("channel") or ""
    ).strip()
    if conditions.get("require_channel") or channel:
        if not channel:
            unmet.append("关注频道")
        else:
            try:
                member = await context.bot.get_chat_member(channel, user_id)
                status = str(getattr(member, "status", "") or "")
                if status in {
                    ChatMemberStatus.LEFT, ChatMemberStatus.BANNED,
                    "left", "kicked",
                }:
                    unmet.append("关注频道")
            except TelegramError:
                unmet.append("关注频道")
    min_messages = int(
        _raffle_field(raffle, "min_messages", 0) or conditions.get("messages") or 0
    )
    if conditions.get("require_messages") or min_messages:
        since, until = DirectoryStore._raffle_message_window(raffle)
        if not since:
            since = str(
                _raffle_field(raffle, "stats_start_at", "")
                or _raffle_field(raffle, "activity_start_at", "")
                or _raffle_field(raffle, "created_at", "")
                or ""
            )
            until = str(raffle["ends_at"] or "")
        count = store.count_user_messages_since(
            chat_id, user_id, since, until
        )
        if count < max(1, min_messages):
            unmet.append("发言不足")
    if conditions.get("require_keyword") or conditions.get("keyword"):
        entry = store.raffle_entry(int(raffle["id"]), user_id)
        if not entry or not int(entry["via_keyword"] or 0):
            unmet.append("未发关键词")
    min_boosts = int(
        _raffle_field(raffle, "min_boosts", 0) or conditions.get("boosts") or 0
    )
    if conditions.get("require_boosts") or min_boosts:
        # ⚡N next to member name = number of chat boosts from getUserChatBoosts
        boost_count = await count_user_chat_boosts(context, chat_id, user_id)
        if boost_count < max(1, min_boosts):
            unmet.append("助推不足")
    return unmet


def sight_user_profile_from_tg(store: DirectoryStore, chat_id: int, user) -> None:
    if not user or getattr(user, "is_bot", False):
        return
    store.sight_group_member_profile(
        chat_id, int(user.id),
        first_name=getattr(user, "first_name", "") or "",
        last_name=getattr(user, "last_name", "") or "",
        display_name=user.full_name or user.username or str(user.id),
        username=user.username or "",
    )


def raffle_pro_answers_from_row(raffle) -> list[str]:
    """Rebuild the 9 raffle_pro wizard answers from a stored raffle row."""
    title = str(_raffle_field(raffle, "title", "") or "").strip() or "通用抽奖"
    rules = parse_raffle_rules(_raffle_field(raffle, "rules_json", ""))
    rules_text = "\n".join(rules) if rules else "无"
    template: dict = {}
    raw_template = str(_raffle_field(raffle, "template_json", "") or "").strip()
    if raw_template:
        try:
            data = json.loads(raw_template)
            if isinstance(data, dict):
                template = data
        except json.JSONDecodeError:
            template = {}
    draw_clock = str(template.get("draw_clock") or "").strip()
    if not draw_clock:
        draw_clock = format_beijing_time(raffle["ends_at"])
    stats_mode = str(
        _raffle_field(raffle, "stats_start_mode", "")
        or template.get("stats_start_mode")
        or "immediate"
    ).strip() or "immediate"
    if stats_mode == "immediate":
        stats_text = "立即"
    else:
        stats_text = (
            format_beijing_time(_raffle_field(raffle, "stats_start_at", ""))
            or "立即"
        )
    conditions = parse_raffle_conditions(_raffle_field(raffle, "conditions_json", "{}"))
    condition_lines: list[str] = []
    channel = str(
        conditions.get("channel") or _raffle_field(raffle, "channel_ref", "") or ""
    ).strip()
    if conditions.get("require_channel") or channel:
        condition_lines.append(f"频道 {channel}")
    messages = int(
        conditions.get("messages") or _raffle_field(raffle, "min_messages", 0) or 0
    )
    if conditions.get("require_messages") or messages:
        condition_lines.append(f"发言 {messages}")
    keyword = str(
        conditions.get("keyword") or _raffle_field(raffle, "join_keyword", "") or ""
    ).strip()
    if conditions.get("require_keyword") or keyword:
        condition_lines.append(f"关键词 {keyword}")
    boosts = int(
        conditions.get("boosts") or _raffle_field(raffle, "min_boosts", 0) or 0
    )
    if conditions.get("require_boosts") or boosts:
        condition_lines.append(f"助推 {boosts}")
    conditions_text = "\n".join(condition_lines) if condition_lines else "无"
    how_to = str(_raffle_field(raffle, "how_to_join", "") or "").strip()
    if not how_to:
        how_to = "点击下方按钮参与抽奖。"
    prize = str(raffle["prize"] or "")
    winner_count = int(raffle["winner_count"] or 1)
    tiers = parse_raffle_prizes(prize, winner_count)
    multi_parts = [part.strip() for part in prize.split("|") if part.strip()]
    looks_multi = len(tiers) > 1 or (
        len(multi_parts) > 1
        and all(re.match(r"^\s*\d+\s*[*xX×]\s*.+", part) for part in multi_parts)
    )
    if looks_multi:
        prize_text = "\n".join(f"{quantity}*{name}" for quantity, name in tiers)
    else:
        prize_name = tiers[0][1] if tiers else prize
        prize_text = f"{winner_count} | {prize_name}"
    recur = "是" if int(_raffle_field(raffle, "recur_daily", 0) or 0) else "否"
    min_participants = str(raffle_min_participants(raffle))
    return [
        title, rules_text, draw_clock, stats_text, conditions_text,
        how_to, prize_text, recur, min_participants,
    ]


async def refresh_raffle_announcement(
    context: ContextTypes.DEFAULT_TYPE, raffle_id: int, chat_id: int,
) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    raffle = store.get_raffle(raffle_id)
    if not raffle:
        return
    show_count = raffle_count_visible(store, chat_id)
    body = raffle_text(raffle, show_count)
    markup = (
        raffle_keyboard(raffle_id, int(raffle["entries"] or 0), show_count, raffle=raffle)
        if str(raffle["raffle_type"] or "") == "universal" else None
    )
    message_id = raffle["message_id"]
    if message_id:
        try:
            await context.bot.edit_message_text(
                body, chat_id=chat_id, message_id=int(message_id),
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            return
        except TelegramError:
            pass
    with persistent_message():
        sent = await context.bot.send_message(
            chat_id, body, parse_mode=ParseMode.HTML, reply_markup=markup,
        )
    store.set_raffle_message(raffle_id, sent.message_id)
    await pin_raffle_message(context, chat_id, sent.message_id, raffle_id)


async def create_raffle_from_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ends_at: str,
    winner_count: int, prize: str, raffle_type: str = "universal",
    activity_start_at: str = "", activity_min_messages: int = 0,
    chat_id_override: int | None = None, **raffle_extras,
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    raffle_chat_id = chat_id_override or chat.id
    if not 1 <= winner_count <= 50:
        raise ValueError("中奖人数范围为1-50")
    parse_raffle_prizes(prize, winner_count, strict=True)
    store: DirectoryStore = context.application.bot_data["store"]
    raffle_id = store.create_raffle(
        raffle_chat_id, user.id, prize, winner_count, ends_at, raffle_type,
        activity_start_at, activity_min_messages, **raffle_extras,
    )
    raffle = store.get_raffle(raffle_id)
    show_count = raffle_count_visible(store, raffle_chat_id)
    with persistent_message():
        sent = await context.bot.send_message(
            raffle_chat_id, raffle_text(raffle, show_count), parse_mode=ParseMode.HTML,
            reply_markup=(
                raffle_keyboard(raffle_id, 0, show_count, raffle=raffle)
                if raffle_type == "universal" else None
            ),
        )
    store.set_raffle_message(raffle_id, sent.message_id)
    await pin_raffle_message(context, raffle_chat_id, sent.message_id, raffle_id)
    store.audit(f"tg:{user.id}", "raffle.create", str(raffle_id), prize)


async def group_menu_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await settings_wizard.begin_from_input(update, context, callback_group_id(context, update.effective_chat))
    if await settings_wizard.receive(update, context):
        return
    if context.user_data.get("settings_draft"):
        return
    await commit_group_menu_input(update, context)


async def commit_group_menu_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    mode = context.user_data.get("menu_mode")
    if mode == "invite_member_query":
        message = update.effective_message
        if not message:
            return
        text = (message.text or message.caption or "").strip()
        if await handle_invite_member_query_input(update, context, text):
            context.user_data["consumed_group_message"] = message.message_id
            context.user_data["consumed_private_message"] = message.message_id
        return
    if not mode or not (
        mode.startswith("group_ad_")
        or mode.startswith("group_join_")
        or mode.startswith("raffle_")
        or mode.startswith("points_")
        or mode.startswith("quickpost_")
        or mode.startswith("invite_")
        or mode.startswith("groupperm_")
        or mode.startswith("group_poll")
        or mode.startswith("renamehist")
    ):
        return
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    effective_chat = update.effective_chat
    target_chat_id = (
        effective_chat.id
        if effective_chat and effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        else callback_group_id(context, effective_chat)
    )
    if target_chat_id is None:
        await message.reply_text("请先在群组管理中选择群组。")
        return
    required_permission = menu_mode_group_permission(mode)
    allowed = True if mode == "points_drawcost" else (
        has_super_admin_access(context, user.id)
        if mode.startswith("groupperm_") else
        await is_chat_admin(context, target_chat_id, user.id, required_permission)
    )
    if not allowed:
        await message.reply_text("你没有这个功能的管理权限。")
        return
    context.user_data["preserve_incoming_message_id"] = message.message_id
    text = (message.text or message.caption or "").strip()
    file_id, file_type, file_name = publishing_attachment(message)
    if mode == "group_poll_question":
        if not 1 <= len(text) <= 300:
            await menu_input_error(context, message, mode, "投票问题需要 1-300 个字符")
            return
        context.user_data["group_poll_question"] = text
        context.user_data["menu_mode"] = "group_poll_options"
        clear_menu_input_failures(context, mode)
        await message.reply_text(
            "请发送 2-10 个选项，每行一个。\n例如：\n同意\n反对\n弃权\n\n发送 /cancel 取消。"
        )
        return
    if mode == "group_poll_options":
        try:
            options = parse_group_poll_options(text)
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return
        question = str(context.user_data.get("group_poll_question") or "").strip()
        if not question:
            context.user_data.pop("menu_mode", None)
            await message.reply_text("投票设置已过期，请重新发起。")
            return
        try:
            with persistent_message():
                sent = await context.bot.send_poll(
                    target_chat_id, question, options,
                    is_anonymous=True, allows_multiple_answers=False,
                )
            poll_id = store.create_group_poll(
                target_chat_id, sent.message_id, user.id, question, options,
            )
        except (ValueError, TelegramError) as exc:
            await menu_input_error(context, message, mode, f"发起投票失败：{exc}")
            return
        context.user_data.pop("menu_mode", None)
        context.user_data.pop("group_poll_question", None)
        clear_menu_input_failures(context, mode)
        store.record_group_operation(
            target_chat_id, "setting", user.id, user.username or "",
            user.full_name, "发起群投票",
            user.id, user.username or "", user.full_name,
        )
        store.audit(f"tg:{user.id}", "group_poll.create", str(poll_id), question)
        await message.reply_text(f"群投票 #{poll_id} 已发到本群。")
        return
    if mode == "raffle_active_start":
        try:
            if text == "0":
                start_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            else:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                    raise ValueError("请输入 0 或日期，格式：年-月-日")
                start_at = beijing_datetime_to_utc_text(text + " 00:00", allow_past=True)
                start_dt = datetime.strptime(start_at, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                now = datetime.now(timezone.utc)
                if start_dt > now or start_dt < now - timedelta(days=31):
                    raise ValueError("发言起始时间只能选择今天至近31天")
        except ValueError as exc:
            await menu_input_error(context, message, mode, str(exc))
            return
        context.user_data["raffle_active_start_at"] = start_at
        context.user_data["menu_mode"] = "raffle_active_details"
        clear_menu_input_failures(context, mode)
        if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
            context.user_data["consumed_private_message"] = message.message_id
        context.user_data["consumed_group_message"] = message.message_id
        schedule_group_trigger_cleanup(context, message, text)
        await message.reply_text(
            "请选择活跃抽奖规则并发送：\n\n"
            "随机 | 开奖分钟 | 中奖人数 | 最低发言次数 | 奖品\n"
            "例如：随机 | 1440 | 3 | 20 | 会员礼品\n\n"
            "排名 | 开奖分钟 | 中奖人数 | 奖品\n"
            "例如：排名 | 1440 | 3 | 会员礼品\n\n"
            "发送 /cancel 取消。"
        )
        return
    try:
        if mode.startswith("groupperm_"):
            action = mode.removeprefix("groupperm_")
            parts = settings_wizard.input_parts(message, text, 1)
            target_text = parts[0]
            target_id, target_username, target_name = resolve_group_user_target(
                store, target_chat_id, message, target_text
            )
            if not await is_telegram_chat_admin(context, target_chat_id, target_id):
                raise ValueError("只能给当前群的群主或群管理员分配权限")
            if action == "set":
                if len(parts) != 2:
                    raise ValueError("格式：@用户名或数字ID | 中文权限1,中文权限2")
                permissions = parse_group_permissions(parts[1])
                store.set_group_admin_permissions(
                    target_chat_id, target_id, permissions, user.id
                )
                result = f"{target_name} 的群管理员权限已更新。"
            elif action == "reset":
                store.reset_group_admin_permissions(target_chat_id, target_id)
                result = f"{target_name} 的群管理员权限已重置为默认无权限。"
            else:
                raise ValueError("未知权限操作")
            store.record_group_operation(
                target_chat_id, "permission", target_id, target_username,
                target_name, result, user.id, user.username or "", user.full_name,
            )
            _, markup = group_permissions_view(store, target_chat_id)
        elif mode == "group_join_welcome":
            store: DirectoryStore = context.application.bot_data["store"]
            store.set_group_join_config(
                target_chat_id, user.id, welcome_text=text
            )
            config = store.group_join_config(target_chat_id)
            result = "欢迎语设置成功。可使用 {name} 和 {group} 占位符。"
            markup = group_join_keyboard(config)
        elif mode.startswith("group_ad_"):
            position = mode.removeprefix("group_ad_")
            interval_seconds = 0
            ad_text = text
            if position == "interval":
                parts = settings_wizard.input_parts(message, text, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    raise ValueError("格式：间隔分钟 | 广告文字")
                interval_seconds = int(parts[0]) * 60
                ad_text = parts[1]
                if not 1 <= int(parts[0]) <= 43200:
                    raise ValueError("广告间隔范围为1-43200分钟")
            if hasattr(message, "original_message") or position != "interval":
                ad_text = capture_content(message)[0]
            validate_content(ad_text, file_id, file_type)
            store: DirectoryStore = context.application.bot_data["store"]
            scratch_chat_id = int(getattr(message, "chat_id", 0) or getattr(getattr(message, "chat", None), "id", 0) or 0)
            buttons_json = await capture_buttons_resolving(
                context.bot, message, scratch_chat_id,
            )
            source = forward_channel_source(message)
            source_chat_id = int(source[0]) if source else 0
            source_message_id = int(source[1]) if source else 0
            ad_id = store.set_group_ad(
                target_chat_id, position, ad_text, user.id,
                interval_seconds, file_id, file_type, file_name,
                entities_json=capture_content(message)[1],
                buttons_json=buttons_json,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
            )
            store.audit(f"tg:{user.id}", "group_ad.set", str(ad_id), position)
            result = "广告设置成功。\n\n" + group_ad_status_text(store, target_chat_id)
            # Hypothesis: channel-origin copy is the only reliable Bot API way to
            # recover buttons from forwards; warn when origin/copy did not yield buttons.
            original = getattr(message, "original_message", message)
            if (
                (buttons_json in ("[]", "", None))
                and getattr(original, "forward_origin", None) is not None
            ):
                result += (
                    "\n\n注意：未检测到内联按钮。"
                    "若原帖带按钮，请转发带按钮的频道原帖，并确认机器人已在该频道内。"
                )
            markup = group_ad_keyboard()
        elif mode.startswith("quickpost_"):
            action = mode.removeprefix("quickpost_")
            store: DirectoryStore = context.application.bot_data["store"]
            chat_id = target_chat_id
            selected_id = int(context.user_data.get(f"quickpost_selected:{chat_id}") or 0)
            try:
                current = store.quick_post(chat_id, selected_id or None)
            except ValueError:
                current = store.quick_post(chat_id)
                selected_id = int(current["id"])
            parts = settings_wizard.input_parts(message, text, 1)
            if action == "add":
                new_row = store.create_quick_post(chat_id, parts[0], user.id)
                raw_text, entities_json = capture_content(message)
                validate_content(raw_text, file_id, file_type)
                store.update_quick_post(
                    chat_id, user.id, int(new_row["id"]), text=raw_text,
                    file_id=file_id, file_type=file_type, file_name=file_name,
                    entities_json=entities_json,
                )
                selected_id = int(new_row["id"])
                context.user_data[f"quickpost_selected:{chat_id}"] = selected_id
            elif action == "text":
                raw_text, entities_json = capture_content(message)
                validate_content(raw_text, file_id or current["file_id"], file_type or current["file_type"])
                values = dict(text=raw_text, entities_json=entities_json)
                if file_id:
                    values.update(file_id=file_id, file_type=file_type, file_name=file_name)
                store.update_quick_post(chat_id, user.id, int(current["id"]), **values)
            elif action == "media":
                if not file_id:
                    raise ValueError("请发送图片、视频、动画、音频或文件")
                raw_text, entities_json = capture_content(message)
                validate_content(raw_text, file_id, file_type)
                store.update_quick_post(
                    chat_id, user.id, int(current["id"]), file_id=file_id, file_type=file_type,
                    file_name=file_name, text=raw_text, entities_json=entities_json,
                )
            elif action == "button":
                parts = settings_wizard.input_parts(message, text)
                colors = {"默认": "default", "蓝色": "primary", "绿色": "success", "红色": "danger"}
                widths = {"长按钮": "long", "短按钮": "short"}
                store.add_quick_post_button(
                    int(current["id"]), parts[0], parts[1],
                    colors.get(parts[2], "default"), widths.get(parts[3], "long"),
                    str(context.user_data.pop("quickpost_button_emoji_id", "")),
                )
            elif action == "buttondel":
                button_id = int(parts[0].lstrip("#"))
                if not store.delete_quick_post_button(int(current["id"]), button_id):
                    raise ValueError("当前消息没有这个按钮编号")
            elif action == "buttonedit":
                parts = settings_wizard.input_parts(message, text)
                colors = {"默认": "default", "蓝色": "primary", "绿色": "success", "红色": "danger"}
                widths = {"长按钮": "long", "短按钮": "short"}
                if not store.update_quick_post_button(
                    int(current["id"]), int(parts[0].lstrip("#")), parts[1], parts[2],
                    colors.get(parts[3], "default"), widths.get(parts[4], "long"),
                    str(context.user_data.pop("quickpost_button_emoji_id", "")),
                ):
                    raise ValueError("当前消息没有这个按钮编号")
            elif action == "schedule":
                schedule_post_id = int(parts[0].lstrip("#"))
                run_at = beijing_datetime_to_utc_text(parts[1])
                store.schedule_quick_post(chat_id, schedule_post_id, run_at, user.id)
            elif action == "schedulecancel":
                schedule_id = int(parts[0].lstrip("#"))
                if not store.cancel_quick_post_schedule(chat_id, schedule_id):
                    raise ValueError("没有找到这个待发布任务编号")
            else:
                raise ValueError("未知快捷发布设置")
            row = store.quick_post(chat_id, selected_id or int(current["id"]))
            result = "快捷发布设置已保存。\n\n" + quick_post_menu_text(
                store, row, str(context.application.bot_data.get("bot_username") or "")
            )
            markup = quick_post_menu_keyboard(store, row)
            store.audit(f"tg:{user.id}", f"quickpost.{action}", str(chat_id))
        elif mode == "invite_query":
            store: DirectoryStore = context.application.bot_data["store"]
            chat_id = target_chat_id
            if text.startswith("https://t.me/"):
                link = store.invite_link_by_url(chat_id, text)
            else:
                link = store.invite_links_by_owner_query(chat_id, text)
            if not link:
                raise ValueError("没有找到该用户名或邀请链接的记录")
            result, markup = invite_query_view(store, chat_id, link, 0)
        elif mode.startswith("invite_"):
            action = mode.removeprefix("invite_")
            store: DirectoryStore = context.application.bot_data["store"]
            chat_id = target_chat_id
            if action in {"premium", "normal"}:
                if not has_super_admin_access(context, user.id):
                    raise ValueError("邀请积分仅超级管理员可以设置")
                parts = settings_wizard.input_parts(message, text)
                expected = 3 if action == "premium" else 2
                if len(parts) != expected or not parts[0].strip().isdigit():
                    raise ValueError(
                        "格式：有效发言条数 | 达标奖励 | 每次助推奖励" if action == "premium"
                        else "格式：有效发言条数 | 达标奖励"
                    )
                try:
                    amounts = [parse_points_amount(part, allow_zero=True) for part in parts[1:]]
                except ValueError as exc:
                    raise ValueError("奖励请输入0或正数积分（最多两位小数）") from exc
                values = {
                    f"{action}_msg_threshold": int(parts[0]),
                    f"{action}_msg_points": amounts[0],
                }
                if action == "premium":
                    values["premium_boost_points"] = amounts[1]
                store.update_invite_rewards(chat_id, user.id, **values)
            elif action == "points":
                if not has_super_admin_access(context, user.id):
                    raise ValueError("邀请积分仅超级管理员可以设置")
                try:
                    value = parse_points_amount(text, allow_zero=True)
                except ValueError as exc:
                    raise ValueError("请输入0或正数积分（最多两位小数）") from exc
                store.update_invite_config(chat_id, user.id, points_per_invite=value)
            else:
                if not text.isdigit():
                    raise ValueError("请输入0或正整数")
                value = int(text)
                if action == "expirehours":
                    store.update_invite_config(
                        chat_id, user.id, expire_seconds=value * 3600
                    )
                elif action == "maxmembers":
                    store.update_invite_config(chat_id, user.id, member_limit=value)
                else:
                    raise ValueError("未知邀请链接设置")
            config = store.invite_config(chat_id)
            result, markup = invite_menu_view(store, chat_id, config)
            store.audit(f"tg:{user.id}", f"invite.{action}", str(chat_id), text)
        elif mode.startswith("points_"):
            action = mode.removeprefix("points_")
            parts = settings_wizard.input_parts(message, text)
            store: DirectoryStore = context.application.bot_data["store"]
            chat_id = target_chat_id
            if action == "drawcost":
                try:
                    spend = parse_points_amount(text)
                except ValueError as exc:
                    raise ValueError("请输入0.01-1000000之间的积分（最多两位小数）") from exc
                if spend > 1000000:
                    raise ValueError("请输入0.01-1000000之间的积分（最多两位小数）")
                minimum = normalize_points(store.points_config(chat_id)["draw_cost"])
                if spend < minimum:
                    raise ValueError(
                        f"本群每次抽奖最少消耗 {format_points(minimum)} 积分"
                    )
                context.user_data[f"point_draw_spend:{chat_id}"] = float(spend)
                result = f"本次抽奖消耗已设为 {format_points(spend)} 积分。"
                markup = point_draw_view(
                    store, chat_id,
                    has_group_permission(context, chat_id, user.id, "points"),
                    spend,
                )[1]
            elif action == "checkin":
                if len(parts) != 3:
                    raise ValueError("格式：签到最小积分 | 签到最大积分 | 连续3天额外积分")
                try:
                    values = [parse_points_amount(part, allow_zero=True) for part in parts]
                except ValueError as exc:
                    raise ValueError(
                        "格式：签到最小积分 | 签到最大积分 | 连续3天额外积分"
                    ) from exc
                store.set_checkin_points(
                    chat_id, values[0], values[1], values[2], user.id
                )
                result = "签到积分设置成功。"
            elif action == "activity":
                if (
                    len(parts) != 4
                    or not parts[0].isdigit()
                    or not parts[1].isdigit()
                ):
                    raise ValueError("格式：消息目标最小 | 消息目标最大 | 奖励最小 | 奖励最大")
                try:
                    pmin = parse_points_amount(parts[2], allow_zero=True)
                    pmax = parse_points_amount(parts[3], allow_zero=True)
                except ValueError as exc:
                    raise ValueError(
                        "格式：消息目标最小 | 消息目标最大 | 奖励最小 | 奖励最大"
                    ) from exc
                store.set_activity_points(
                    chat_id, int(parts[0]), int(parts[1]), pmin, pmax, user.id
                )
                result = "每日活跃积分设置成功（按有效发言计：1 分钟内最多算 2 条，少于 3 个字不算）。"
            elif action == "tieradd":
                if len(parts) != 2 or not parts[0].strip().isdigit():
                    raise ValueError("格式：今日有效发言条数 | 奖励积分")
                try:
                    tier_points = parse_points_amount(parts[1])
                except ValueError as exc:
                    raise ValueError("奖励积分请输入正数（最多两位小数）") from exc
                store.set_activity_tier(chat_id, int(parts[0]), tier_points, user.id)
                result = f"阶梯奖励已保存：今日有效发言 {int(parts[0])} 条 +{format_points(tier_points)} 积分。"
            elif action == "tierdel":
                if text.strip() in {"全部", "all", "ALL"}:
                    count = store.clear_activity_tiers(chat_id)
                    result = f"已清空 {count} 档阶梯奖励。"
                else:
                    tier_id = parse_numbered_id(text)
                    if not store.delete_activity_tier(chat_id, tier_id):
                        raise ValueError("没有找到这个阶梯编号")
                    result = f"阶梯 #{tier_id} 已删除。"
            elif action == "giftadd":
                if len(parts) not in {2, 3}:
                    raise ValueError("格式：所需积分 | 礼品名称 | 库存")
                try:
                    cost = parse_points_amount(parts[0])
                except ValueError as exc:
                    raise ValueError("格式：所需积分 | 礼品名称 | 库存") from exc
                stock = -1 if len(parts) == 2 else int(parts[2])
                gift_id = store.add_point_gift(
                    chat_id, parts[1], cost, stock, user.id
                )
                result = f"礼品添加成功，编号 #{gift_id}。"
            elif action == "giftdel":
                gift_id = parse_numbered_id(text)
                if not store.disable_point_gift(chat_id, gift_id):
                    raise ValueError("没有找到这个礼品编号")
                result = f"礼品 #{gift_id} 已删除。"
            elif action == "adjust":
                if len(parts) < 2:
                    raise ValueError("格式：@用户名或数字ID | +数量或-数量 | 原因")
                try:
                    delta = parse_points_delta(parts[1])
                except ValueError as exc:
                    raise ValueError("格式：@用户名或数字ID | +数量或-数量 | 原因") from exc
                target_id, target_username, target_name = resolve_group_user_target(
                    store, chat_id, message, parts[0]
                )
                reason = " | ".join(parts[2:]) or "群主调整积分"
                balance = store.adjust_points(
                    chat_id, target_id, delta, reason, user.id,
                    target_username, target_name, allow_negative=True,
                )
                result = (
                    f"积分调整成功，{target_name} 当前积分："
                    f"{format_points(balance)}。"
                )
            elif action == "clear":
                target = text.casefold()
                target_id = None
                if target not in {"all", "全部"}:
                    target_id, _, _ = resolve_group_user_target(
                        store, chat_id, message, text
                    )
                count = store.clear_points(
                    chat_id, user.id,
                    target_id,
                )
                result = f"积分清零完成，共处理 {count} 名成员。"
            elif action == "memberledger":
                target_id, _, target_name = resolve_group_user_target(
                    store, chat_id, message, text
                )
                result, markup = point_member_ledger_view(
                    store, chat_id, target_id, target_name
                )
            elif action == "membergames":
                target_id, _, target_name = resolve_group_user_target(
                    store, chat_id, message, text
                )
                result, markup = point_game_records_page(
                    store, chat_id, target_id, target_name,
                    admin_view=True,
                )
            elif action in {"drawconfig", "drawmincost"}:
                if len(parts) != 2:
                    if action == "drawmincost":
                        try:
                            cost = parse_points_amount(text)
                        except ValueError as exc:
                            raise ValueError(
                                "请输入0.01-1000000之间的积分（最多两位小数）"
                            ) from exc
                        if cost > 1000000:
                            raise ValueError(
                                "请输入0.01-1000000之间的积分（最多两位小数）"
                            )
                        config = store.points_config(chat_id)
                        store.set_point_draw_config(
                            chat_id, bool(config["draw_enabled"]), cost,
                            float(config["draw_rate_multiplier"]), user.id,
                        )
                        result = (
                            f"积分抽奖最低消耗已设为 {format_points(cost)} 积分。"
                        )
                    else:
                        raise ValueError(
                            "请输入0.01-1000000之间的积分（最多两位小数）"
                        )
                else:
                    try:
                        cost = parse_points_amount(parts[0])
                    except ValueError as exc:
                        raise ValueError(
                            "请输入0.01-1000000之间的积分（最多两位小数）"
                        ) from exc
                    if cost > 1000000:
                        raise ValueError(
                            "请输入0.01-1000000之间的积分（最多两位小数）"
                        )
                    enabled = parts[1].casefold() not in {"关闭", "关", "off", "0"}
                    if parts[1].casefold() not in {
                        "开启", "开", "on", "1", "关闭", "关", "off", "0",
                    }:
                        raise ValueError("开关请填写：开启 或 关闭")
                    store.set_point_draw_config(
                        chat_id, enabled, cost,
                        float(store.points_config(chat_id)["draw_rate_multiplier"]),
                        user.id,
                    )
                    result = "积分抽奖设置成功。"
            elif action == "drawrate":
                try:
                    multiplier = float(text)
                except ValueError as exc:
                    raise ValueError("请输入0-5之间的中奖概率倍率") from exc
                config = store.points_config(chat_id)
                store.set_point_draw_config(
                    chat_id, bool(config["draw_enabled"]),
                    normalize_points(config["draw_cost"]), multiplier, user.id,
                )
                result = f"积分抽奖中奖倍率已设为 {multiplier:g}。"
            elif action == "diceodds":
                odds = parse_dice_odds_input(text)
                store.set_dice_odds(chat_id, odds, user.id)
                result = f"骰子赔率已设为 {odds / 1000:.3f}（{odds}）。"
            elif action == "dicemin":
                minimum = parse_points_amount(text)
                store.set_dice_min_bet(chat_id, minimum, user.id)
                result = f"骰子每次最低参与积分已设为 {format_points(minimum)}。"
            elif action == "drawmsgmin":
                try:
                    count = int(text.strip())
                except ValueError as exc:
                    raise ValueError("请发送0-100000之间的整数（0 表示不限）") from exc
                store.set_point_draw_min_activity(chat_id, count, user.id)
                result = (
                    f"积分抽奖最低当日活跃已设为 {count} 条有效发言（1 分钟内最多算 2 条，少于 3 个字不算）。"
                    if count else "积分抽奖最低当日活跃已关闭（不限）。"
                )
            elif action == "redeemmsgmin":
                try:
                    count = int(text.strip())
                except ValueError as exc:
                    raise ValueError("请发送0-100000之间的整数（0 表示不限）") from exc
                store.set_point_redeem_min_activity(chat_id, count, user.id)
                result = (
                    f"积分兑换最低当日活跃已设为 {count} 条有效发言（1 分钟内最多算 2 条，少于 3 个字不算）。"
                    if count else "积分兑换最低当日活跃已关闭（不限）。"
                )
            elif action in {"dicemsgmin", "dicemsgfree"}:
                try:
                    count = int(text.strip())
                except ValueError as exc:
                    raise ValueError("请发送0-100000之间的整数（0 表示关闭）") from exc
                store.set_dice_activity_rule(
                    chat_id, "min" if action == "dicemsgmin" else "free", count, user.id,
                )
                if action == "dicemsgmin":
                    result = (
                        f"骰子最低当日活跃已设为 {count} 条有效发言（1 分钟内最多算 2 条，少于 3 个字不算）。"
                        if count else "骰子最低当日活跃已关闭（不限）。"
                    )
                else:
                    result = (
                        f"今日有效发言满 {count} 条的成员将不受骰子定时限制（1 分钟内最多算 2 条，少于 3 个字不算）。"
                        if count else "骰子免定时活跃已关闭。"
                    )
            elif action == "dicemax":
                maximum = parse_points_amount(text, allow_zero=True)
                store.set_dice_max_bet(chat_id, maximum, user.id)
                result = (
                    f"骰子单注上限已设为 {format_points(maximum)} 积分。"
                    if maximum > 0 else "骰子单注上限已取消（不限）。"
                )
            elif action == "diceschedule":
                if len(parts) != 3:
                    raise ValueError("请完成定时开关、开放时间和关闭时间设置")
                store.set_dice_schedule(
                    chat_id, parts[0] == "开启", parts[1], parts[2], user.id,
                )
                result = (
                    f"骰子每日定时已开启：{parts[1]}-{parts[2]}。"
                    if parts[0] == "开启" else "骰子每日定时已关闭。"
                )
            else:
                raise ValueError("未知积分设置")
            if action in {"drawconfig", "drawmincost", "drawrate", "drawmsgmin"}:
                settings_text, markup = point_draw_settings_view(store, chat_id)
                result += "\n\n" + settings_text
            elif action in {"tieradd", "tierdel", "activity"}:
                settings_text, markup = activity_settings_view(store, chat_id)
                result += "\n\n" + settings_text
                store.audit(f"tg:{user.id}", f"points.{action}", str(chat_id), text[:200])
            elif action not in {"drawcost", "memberledger", "membergames"}:
                config = store.points_config(chat_id)
                show_draw = has_group_permission(context, chat_id, user.id, "points")
                can_dice_odds = has_group_permission(context, chat_id, user.id, "diceodds")
                result += "\n\n" + points_status_text(store, chat_id, show_draw)
                markup = points_menu_keyboard(
                    True, bool(config["is_enabled"]), show_draw,
                    bool(config["dice_enabled"]), can_dice_odds,
                )
                store.audit(f"tg:{user.id}", f"points.{action}", str(chat_id), text[:200])
        elif mode == "renamehist_query":
            target_id, _, target_name = resolve_group_user_target(
                store, target_chat_id, message, text
            )
            result = name_history_view(store, target_chat_id, target_id, target_name)
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")
            ]])
        else:
            plan = mode.removeprefix("raffle_")
            parts = settings_wizard.input_parts(message, text)
            answers = getattr(message, "setting_answers", None)
            if plan == "pro":
                if answers and len(answers) >= 8:
                    ends_at, winner_count, prize, extras = build_raffle_extras_from_pro(
                        list(answers)
                    )
                else:
                    raise ValueError("请通过「样板通用抽奖」向导填写完整信息")
                edit_id = context.user_data.get("edit_raffle_id")
                if isinstance(edit_id, int):
                    if not store.update_raffle(
                        edit_id, target_chat_id, prize, winner_count, ends_at, **extras,
                    ):
                        raise ValueError("抽奖不存在、不属于本群或已开奖，无法修改")
                    await refresh_raffle_announcement(context, edit_id, target_chat_id)
                    store.audit(f"tg:{user.id}", "raffle.update", str(edit_id), prize)
                    context.user_data.pop("edit_raffle_id", None)
                    result, markup = f"抽奖 #{edit_id} 已更新。", raffle_plan_keyboard()
                else:
                    await create_raffle_from_input(
                        update, context, ends_at, winner_count, prize,
                        chat_id_override=target_chat_id, **extras,
                    )
                    result, markup = "样板通用抽奖已创建。", raffle_plan_keyboard()
            elif plan == "delete":
                raffle_id = parse_numbered_id(text)
                raffle = store.get_raffle(raffle_id)
                if not raffle or int(raffle["chat_id"]) != target_chat_id:
                    raise ValueError("当前群没有这个抽奖编号")
                if not store.delete_raffle(target_chat_id, raffle_id):
                    raise ValueError("删除群抽奖失败，请重试")
                if raffle["message_id"]:
                    try:
                        await context.bot.unpin_chat_message(
                            target_chat_id, int(raffle["message_id"])
                        )
                    except TelegramError:
                        pass
                    try:
                        await context.bot.delete_message(
                            target_chat_id, int(raffle["message_id"])
                        )
                    except TelegramError:
                        pass
                store.audit(f"tg:{user.id}", "raffle.delete", str(raffle_id))
                result, markup = f"群抽奖 #{raffle_id} 已删除。", raffle_plan_keyboard()
            elif plan == "active_details":
                if len(parts) < 4 or parts[0] not in {"随机", "排名"}:
                    raise ValueError("格式：随机/排名 | 开奖分钟 | 中奖人数 | 条件 | 奖品")
                if not parts[1].isdigit() or not parts[2].isdigit():
                    raise ValueError("开奖分钟和中奖人数必须是数字")
                minutes = int(parts[1])
                if not 1 <= minutes <= 44640:
                    raise ValueError("开奖分钟范围为1-44640（最多31天）")
                winner_count = int(parts[2])
                if parts[0] == "随机":
                    if len(parts) < 5 or not parts[3].isdigit() or int(parts[3]) < 1:
                        raise ValueError("随机模式需要填写最低发言次数")
                    activity_min = int(parts[3])
                    prize = " | ".join(parts[4:])
                    raffle_type = "activity_random"
                else:
                    activity_min = 0
                    prize = " | ".join(parts[3:])
                    raffle_type = "activity_rank"
                await create_raffle_from_input(
                    update, context, utc_after_minutes_text(minutes), winner_count,
                    prize, raffle_type,
                    str(context.user_data.pop("raffle_active_start_at", "")),
                    activity_min, target_chat_id,
                )
                result, markup = "群活跃抽奖已创建。", raffle_type_keyboard(
                    raffle_count_visible(store, target_chat_id)
                )
            elif plan == "minutes":
                if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                    raise ValueError("格式：分钟 | 中奖人数 | 奖品")
                minutes = int(parts[0])
                if not 1 <= minutes <= 10080:
                    raise ValueError("开奖分钟范围为1-10080")
                ends_at = utc_after_minutes_text(minutes)
                winner_count, prize = int(parts[1]), " | ".join(parts[2:])
            elif plan == "at":
                if len(parts) < 3 or not parts[1].isdigit():
                    raise ValueError("格式：开奖时间 | 中奖人数 | 奖品")
                ends_at = beijing_datetime_to_utc_text(parts[0])
                winner_count, prize = int(parts[1]), " | ".join(parts[2:])
            elif plan == "tiers":
                if len(parts) < 2 or not parts[0].isdigit():
                    raise ValueError("格式：分钟 | 1*奖品 | 2*奖品")
                prize = " | ".join(parts[1:])
                tiers = []
                for item in parts[1:]:
                    match = re.match(r"^\s*(\d+)\s*[*xX×]\s*(.+?)\s*$", item)
                    if not match:
                        raise ValueError("每档奖品格式应为：人数*奖品")
                    tiers.append((int(match.group(1)), match.group(2)))
                winner_count = sum(quantity for quantity, _ in tiers)
                parse_raffle_prizes(prize, winner_count, strict=True)
                minutes = int(parts[0])
                if not 1 <= minutes <= 10080:
                    raise ValueError("开奖分钟范围为1-10080")
                ends_at = utc_after_minutes_text(minutes)
            elif plan == "quick":
                if len(parts) < 2 or not parts[0].isdigit():
                    raise ValueError("格式：中奖人数 | 奖品")
                ends_at = utc_after_minutes_text(10)
                winner_count, prize = int(parts[0]), " | ".join(parts[1:])
            else:
                raise ValueError("未知抽奖方案")
            if plan not in {"active_details", "delete", "pro"}:
                await create_raffle_from_input(
                    update, context, ends_at, winner_count, prize,
                    chat_id_override=target_chat_id,
                )
                result, markup = "抽奖已创建。", raffle_plan_keyboard()
    except ValueError as exc:
        if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
            context.user_data["consumed_private_message"] = message.message_id
        await menu_input_error(context, message, mode, str(exc))
        return
    group_setting_changed = (
        mode == "group_join_welcome"
        or mode.startswith("group_ad_")
        or mode.startswith("quickpost_")
        or (mode.startswith("invite_") and mode != "invite_query")
        or (
            mode.startswith("points_")
            and mode not in {"points_drawcost", "points_memberledger", "points_membergames"}
        )
        or mode.startswith("raffle_")
    )
    if group_setting_changed:
        operation_action = (
            "points" if mode.startswith("points_") else
            "raffle" if mode.startswith("raffle_") else
            "setting"
        )
        store.record_group_operation(
            target_chat_id, operation_action, user.id, user.username or "",
            user.full_name, group_mutation_label(mode),
            user.id, user.username or "", user.full_name,
        )
    clear_menu_input_failures(context, mode)
    context.user_data.pop("menu_mode", None)
    context.user_data["consumed_group_message"] = message.message_id
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        context.user_data["consumed_private_message"] = message.message_id
    persist = mode in {"points_membergames", "renamehist_query"}
    reply_kwargs = {
        "reply_markup": markup,
        "parse_mode": (
            ParseMode.HTML
            if mode in {
                "invite_query", "points_memberledger", "points_membergames",
                "renamehist_query",
            } else None
        ),
        "disable_web_page_preview": (mode == "invite_query"),
    }
    if persist:
        with persistent_message():
            sent = await message.reply_text(result, **reply_kwargs)
    else:
        sent = await message.reply_text(result, **reply_kwargs)
    schedule_setting_cleanup(context, message)
    if not persist:
        schedule_setting_cleanup(context, sent)
    context.user_data.pop("preserve_incoming_message_id", None)


async def raffle_at_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    if not user or not await is_group_admin(update, context, user.id, "raffles"):
        await update.effective_message.reply_text("只有群管理员可以发起抽奖。")
        return
    parts = [part.strip() for part in " ".join(context.args).split("|")]
    if len(parts) < 3 or not parts[1].isdigit():
        await update.effective_message.reply_text(
            "用法：/raffleat 2026-08-27 21:30 | 3 | 奖品"
        )
        return
    try:
        await create_raffle_from_input(
            update, context, beijing_datetime_to_utc_text(parts[0]),
            int(parts[1]), " | ".join(parts[2:]),
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_review_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可审核收录。")
        return
    entries = context.application.bot_data["store"].list_entries(status="pending", limit=10)
    await send_entries(update.effective_message, entries, "没有待审核提交。", True)


async def set_status_from_command(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str) -> None:
    if not has_review_access(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("仅超级管理员可审核收录。")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("请提供收录编号。")
        return
    entry_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip()
    await apply_status(context, entry_id, status, reason, f"tg:{update.effective_user.id}")
    await update.effective_message.reply_text(f"#{entry_id} 已更新为 {status}。")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_from_command(update, context, "approved")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_from_command(update, context, "rejected")


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_from_command(update, context, "removed")


async def edit_entry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not has_review_access(context, user.id if user else None):
        await update.effective_message.reply_text("仅超级管理员可审核收录。")
        return
    if len(context.args) < 3 or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/edit 编号 关键词 地址")
        return
    entry_id = int(context.args[0])
    config: Config = context.application.bot_data["config"]
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        submission = parse_keyword_address_payload(" ".join(context.args[1:]), config.categories)
        if not store.update_entry(
            entry_id, submission.title, submission.url, submission.category, submission.description
        ):
            raise ValueError("没有找到这个收录编号")
    except ValueError as exc:
        await update.effective_message.reply_text(f"修改失败：{exc}")
        return
    store.audit(f"tg:{user.id}", "entry.update", str(entry_id), submission.url)
    await update.effective_message.reply_text(
        f"#{entry_id} 已修改。\n关键词：{submission.title}\n地址：{submission.url}"
    )


async def apply_status(context: ContextTypes.DEFAULT_TYPE, entry_id: int, status: str, reason: str, actor: str) -> bool:
    store: DirectoryStore = context.application.bot_data["store"]
    entry = store.get(entry_id)
    if not entry or not store.update_status(entry_id, status, reason):
        return False
    labels = {"approved": "已通过", "rejected": "已拒绝", "removed": "已下架"}
    store.queue_message(entry.user_id, f"你的提交 #{entry_id} {labels.get(status, status)}。" + (f"\n原因：{reason}" if reason else ""))
    if entry.source_chat_id and entry.source_message_id:
        try:
            await context.bot.delete_message(entry.source_chat_id, entry.source_message_id)
        except TelegramError as exc:
            store.audit(
                "bot", "entry.source_delete_failed", str(entry_id), str(exc)
            )
    store.audit(actor, f"entry.{status}", str(entry_id), reason)
    return True


def has_group_permission(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
    permission: str = "view",
) -> bool:
    if has_super_admin_access(context, user_id):
        return True
    permissions = context.application.bot_data["store"].group_admin_permissions(
        chat_id, user_id
    )
    return bool(permissions) if permission == "view" else permission in permissions


async def is_group_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int,
    permission: str = "view",
) -> bool:
    chat = update.effective_chat
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return False
    return has_group_permission(context, chat.id, user_id, permission)


async def is_chat_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
    permission: str = "view",
) -> bool:
    return has_group_permission(context, chat_id, user_id, permission)


async def is_telegram_chat_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int
) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


def callback_group_id(
    context: ContextTypes.DEFAULT_TYPE, chat
) -> int | None:
    if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return int(chat.id)
    selected = context.user_data.get("selected_group_id")
    return int(selected) if isinstance(selected, int) and selected < 0 else None


def callback_group_permission(data: str) -> str:
    if data in {"group:stats", "group:active"} or data.startswith("groupstats:"):
        return "stats"
    if data == "group:raffles" or data.startswith((
        "raffletype:", "raffleplan:", "raffle:count:", "raffledelete:", "raffleedit:",
        "rafflecopy:",
    )):
        return "raffles"
    if data == "group:lottery" or data.startswith("lotterysub:"):
        return "lottery"
    if data == "group:polls" or data.startswith("grouppoll:"):
        return "polls"
    if data == "group:ads" or data.startswith("ad:"):
        return "ads"
    if data == "group:points" or data.startswith("points:set:"):
        return "points"
    if data == "group:joincfg" or data.startswith("joincfg:"):
        return "welcome"
    if data.startswith("quickpost:"):
        return "quickpost"
    if data.startswith("invite:"):
        return "invite"
    if data.startswith("groupdice:"):
        return "points"
    if data == "group:recent" or data.startswith("group:recent:"):
        return "recent"
    if data == "group:moderation":
        return "moderation"
    if data == "group:renamehist" or data.startswith("renamehist:"):
        return "renamehist"
    return ""


def menu_mode_group_permission(mode: str) -> str:
    if mode.startswith("group_ad_"):
        return "ads"
    if mode.startswith("quickpost_"):
        return "quickpost"
    if mode.startswith("invite_"):
        return "invite"
    if mode.startswith("points_"):
        if mode in {
            "points_diceodds", "points_dicemin", "points_dicemax", "points_diceschedule",
            "points_dicemsgmin", "points_dicemsgfree",
        }:
            return "diceodds"
        return "points"
    if mode.startswith("group_join_"):
        return "welcome"
    if mode.startswith("raffle_"):
        return "raffles"
    if mode.startswith("group_poll"):
        return "polls"
    if mode.startswith("renamehist"):
        return "renamehist"
    return ""


def callback_is_group_mutation(data: str) -> bool:
    return (
        data in {
            "quickpost:clear", "quickpost:delete", "invite:on", "invite:off", "invite:reset",
            "joincfg:welcome", "joincfg:verify", "points:enable",
            "points:disable", "points:diceon", "points:diceoff",
            "groupdice:on", "groupdice:off",
            "points:activityoff", "points:set:drawtoggle",
        }
        or data.startswith((
            "ad:disable:", "raffle:count:", "raffledelete:item:", "lotterysub:",
            "grouppoll:delete:item:",
        ))
    )


def group_mutation_label(value: str) -> str:
    exact = {
        "group_join_welcome": "修改进群欢迎语",
        "quickpost:clear": "清空快捷发布设置",
        "quickpost:delete": "删除当前快捷发布消息",
        "invite:on": "开启邀请链接",
        "invite:off": "关闭邀请链接",
        "invite:reset": "重置邀请链接",
        "joincfg:welcome": "切换进群欢迎",
        "joincfg:verify": "切换进群验证",
        "points:enable": "开启积分功能",
        "points:disable": "关闭积分功能",
        "points:diceon": "开启骰子游戏",
        "points:diceoff": "关闭骰子游戏",
        "groupdice:on": "开启骰子游戏",
        "groupdice:off": "关闭骰子游戏",
        "points:activityoff": "关闭活跃积分",
        "points:set:drawtoggle": "切换积分抽奖",
        "lotterysub:all": "修改全部开奖订阅",
    }
    if value in exact:
        return exact[value]
    prefixes = (
        ("group_ad_", "修改广告设置"),
        ("quickpost_", "修改快捷发布设置"),
        ("invite_", "修改邀请链接设置"),
        ("points_", "修改积分设置"),
        ("raffle_", "创建或修改抽奖"),
        ("ad:disable:", "关闭广告"),
        ("raffle:count:", "修改抽奖人数显示"),
        ("raffledelete:item:", "删除抽奖"),
        ("lotterysub:", "修改开奖订阅"),
        ("grouppoll:delete:item:", "删除群投票"),
    )
    for prefix, label in prefixes:
        if value.startswith(prefix):
            return label
    return "修改机器人设置"


async def private_group_selector(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    store: DirectoryStore = context.application.bot_data["store"]
    rows = []
    for group in store.list_groups(100):
        chat_id = int(group["chat_id"])
        if await is_chat_admin(context, chat_id, user_id):
            rows.append(group)
    buttons = [[
        InlineKeyboardButton(
            str(group["title"] or group["username"] or group["chat_id"])[:50],
            callback_data=f"groupselect:{group['chat_id']}",
        )
    ] for group in rows[:50]]
    bot_username = str(context.application.bot_data.get("bot_username") or "")
    if bot_username:
        buttons.append([
            InlineKeyboardButton(
                "➕ 添加群组", url=f"https://t.me/{bot_username}?startgroup=true"
            )
        ])
    buttons.append([InlineKeyboardButton("⬅️ 返回首页", callback_data="nav:main")])
    text = (
        "ℹ️ 群组管理\n\n请选择要设置的群组。"
        if rows else
        "ℹ️ 群组管理\n\n暂未找到你有管理权限的群组。请先把机器人添加到群组，"
        "授予管理消息和邀请用户权限。群管理员还需要超级管理员分配机器人权限。"
    )
    return text, InlineKeyboardMarkup(buttons)


async def is_points_manager(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    return await is_group_admin(update, context, user_id, "points")


async def hydrate_group_speaker_names(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int,
    period_days: int, page: int,
) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    rows = store.group_speaker_ranking(
        chat_id, period_days, GROUP_STATS_PAGE_SIZE,
        max(0, page) * GROUP_STATS_PAGE_SIZE,
    )
    for row in rows:
        user_id = int(row["user_id"])
        display_name = str(row["display_name"] or "")
        if display_name and display_name != str(user_id):
            continue
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
        except TelegramError:
            continue
        user = member.user
        store.update_group_member_identity(
            chat_id, user_id, user.username or "",
            user.full_name or user.username or "群成员",
        )


def point_balance_text(store: DirectoryStore, chat_id: int, user_id: int) -> str:
    account = store.point_account(chat_id, user_id)
    balance = format_points(account["balance"]) if account else "0"
    earned = format_points(account["earned_total"]) if account else "0"
    spent = format_points(account["spent_total"]) if account else "0"
    return f"⭐ 你当前有 {balance} 积分\n累计获得：{earned} · 累计使用：{spent}"


def resolve_group_user_target(store: DirectoryStore, chat_id: int, message, value: str):
    target = value.strip()
    for entity in tuple(message.entities or ()) + tuple(message.caption_entities or ()):
        if entity.type == "text_mention" and entity.user:
            return entity.user.id, entity.user.username or "", entity.user.full_name or target
    if not target:
        replied = getattr(message, "reply_to_message", None)
        replied_user = getattr(replied, "from_user", None) if replied else None
        if replied_user and not getattr(replied_user, "is_bot", False):
            return (
                int(replied_user.id),
                replied_user.username or "",
                replied_user.full_name or str(replied_user.id),
            )
        raise ValueError("没有找到该成员，请先让对方在群内发言，或使用数字ID")
    row = store.find_group_user(chat_id, target)
    if not row:
        raise ValueError("没有找到该成员，请先让对方在群内发言，或使用数字ID")
    return int(row["user_id"]), str(row["username"] or ""), str(
        row["display_name"] or target
    )


def _record_keyboard(
    prefix: str, page: int, total: int, page_size: int = 10,
    maximum: int | None = 100,
) -> InlineKeyboardMarkup:
    effective_total = min(total, maximum) if maximum is not None else total
    page_count = max(1, (effective_total + page_size - 1) // page_size)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("上一页", callback_data=f"{prefix}:{page - 1}"))
    if page + 1 < page_count:
        buttons.append(InlineKeyboardButton("下一页", callback_data=f"{prefix}:{page + 1}"))
    rows = [buttons] if buttons else []
    rows.append([InlineKeyboardButton("⬅️ 返回积分中心", callback_data="group:points")])
    return InlineKeyboardMarkup(rows)


def point_ranking_page(
    store: DirectoryStore, chat_id: int, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    total = min(store.count_point_accounts(chat_id), 100)
    page_count = max(1, (total + 9) // 10)
    page = min(max(page, 0), page_count - 1)
    rows = store.point_rankings(chat_id, 10, page * 10)
    if not rows:
        return "本群还没有积分记录。", _record_keyboard("points:rank", 0, 0)
    lines = [f"🏆 本群积分排行（第 {page + 1}/{page_count} 页，最多100名）", ""]
    for index, row in enumerate(rows, page * 10 + 1):
        display = str(row["display_name"] or row["username"] or "群成员")
        name = telegram_user_link(int(row["user_id"]), display[:30])
        lines.append(f"{index}. {name} · {format_points(row['balance'])} 积分")
    return "\n".join(lines), _record_keyboard("points:rank", page, total)


def point_ranking_text(store: DirectoryStore, chat_id: int) -> str:
    return point_ranking_page(store, chat_id, 0)[0]


def group_recent_operations_view(
    store: DirectoryStore, chat_id: int, page: int = 0, kind: str = "group",
) -> tuple[str, InlineKeyboardMarkup]:
    kind = "bot" if kind == "bot" else "group"
    actions = (
        ("setting", "permission")
        if kind == "bot" else
        ("join", "leave", "intercept", "ban")
    )
    page_size = 10
    total = store.count_group_operations(chat_id, actions=actions)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = store.group_operations(
        chat_id, page_size, page * page_size, actions=actions
    )
    totals = store.group_operation_totals(chat_id, 0)
    labels = {
        "join": "进群", "leave": "退群", "intercept": "拦截", "ban": "封禁",
        "setting": "设置", "points": "积分操作", "raffle": "抽奖操作",
        "permission": "权限操作",
    }
    lines = [
        ("🤖 机器人近期操作（只保留7天）" if kind == "bot" else
         "🕘 群组近期操作（只保留7天）"),
    ]
    if kind == "group":
        lines.extend([
            f"进群 {totals['join']} · 退群 {totals['leave']} · "
            f"拦截 {totals['intercept']} · 封禁 {totals['ban']}", "",
        ])
    else:
        lines.append("")
    for row in rows:
        actor = (
            str(row["actor_name"] or "")
            or ("@" + str(row["actor_username"]) if row["actor_username"] else "")
            or (str(row["actor_user_id"]) if int(row["actor_user_id"] or 0) else "系统")
        )
        target = str(row["display_name"] or row["username"] or row["user_id"] or "-")
        line = (
            f"{format_beijing_time(row['created_at'])} · "
            f"{labels.get(str(row['action']), row['action'])}\n"
            f"对象：{target} · 操作人：{actor}"
        )
        if row["detail"]:
            line += f" · {row['detail']}"
        lines.append(line)
    if not rows:
        lines.append("暂无机器人操作记录。" if kind == "bot" else "暂无群组操作记录。")
    lines.append(f"\n第 {page + 1}/{page_count} 页")
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"group:recent:{kind}:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"group:recent:{kind}:{page + 1}"
        ))
    keyboard_rows = [nav] if nav else []
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def point_records_page(
    store: DirectoryStore, chat_id: int, user_id: int,
    kind: str, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    if kind == "ledger":
        total = store.count_point_ledger(chat_id, user_id)
        page_size = 10
        page = min(max(page, 0), max(0, (total - 1) // page_size))
        rows = store.point_ledger_rows(
            chat_id, user_id, page_size, page * page_size
        )
        title = "🧾 我的积分账单（近6个月）"
        lines = [title, ""]
        for row in rows:
            sign = "+" if normalize_points(row["delta"]) > 0 else ""
            actor_name = " ".join(filter(None, (
                str(row["actor_first_name"] or ""),
                str(row["actor_last_name"] or ""),
            ))).strip()
            actor = (
                actor_name
                or ("@" + str(row["actor_username"]) if row["actor_username"] else "")
                or (str(row["created_by"]) if int(row["created_by"] or 0) else "系统")
            )
            lines.append(
                f"{format_beijing_time(row['created_at'])} · {sign}{format_points(row['delta'])} · 余额 {format_points(row['balance_after'])}\n"
                f"{html.escape(str(row['reason'] or '积分变动'))} · 操作人：{html.escape(actor)}"
            )
    elif kind == "wins":
        total = store.count_all_raffle_winners(chat_id)
        page = min(max(page, 0), max(0, (total - 1) // 10))
        rows = store.all_raffle_winners(chat_id, 10, page * 10)
        lines = ["🏅 中奖记录", ""]
        for row in rows:
            display = str(row["display_name"] or row["username"] or "群成员")
            position = max(1, int(row["winner_position"] or 1))
            prize = str(row["gift_name"])
            if row["record_type"] == "群抽奖":
                prize_slots = []
                for quantity, label in parse_raffle_prizes(
                    prize, int(row["raffle_winner_count"] or position)
                ):
                    prize_slots.extend([label] * quantity)
                if len(prize_slots) >= position:
                    prize = prize_slots[position - 1]
            lines.append(
                f"{row['record_type']} #{row['id']} · "
                f"第{position}名 · "
                f"{telegram_user_link(int(row['user_id']), display)} · "
                f"{html.escape(prize)}\n"
                f"中奖时间：{format_beijing_time(row['created_at'])}"
            )
    else:
        total = store.count_point_redemptions(chat_id)
        page = min(max(page, 0), max(0, (total - 1) // 10))
        rows = store.point_redemption_rows(chat_id, 10, page * 10)
        lines = ["📦 积分礼品兑换记录", ""]
        status_labels = {"pending": "已兑换", "won": "抽奖中奖"}
        for row in rows:
            display = str(row["display_name"] or row["username"] or "群成员")
            status_raw = str(row["status"] or "")
            status_text = status_labels.get(status_raw, status_raw)
            note = str(row["note"] or "").strip()
            note_part = f" · 备注：{html.escape(note)}" if note else ""
            lines.append(
                f"#{row['id']} · {telegram_user_link(int(row['user_id']), display)} · "
                f"{html.escape(str(row['gift_name']))} · {format_points(row['points_cost'])}积分 · "
                f"{status_text}{note_part}\n"
                f"兑换时间：{format_beijing_time(row['created_at'])}"
            )
    if not rows:
        lines.append("暂无记录。")
    capped = total
    page_size = 10
    maximum = None
    page_count = max(1, (capped + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    return "\n".join(lines), _record_keyboard(
        f"points:{kind}", page, capped, page_size, maximum
    )


def point_member_ledger_view(
    store: DirectoryStore, chat_id: int, user_id: int,
    display_name: str = "", page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    total = store.count_point_ledger(chat_id, user_id)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = store.point_ledger_rows(chat_id, user_id, page_size, page * page_size)
    if not display_name:
        member = store.find_group_user(chat_id, str(user_id))
        display_name = (
            str(member["display_name"] or member["username"] or user_id)
            if member else str(user_id)
        )
    lines = [
        f"🧾 {html.escape(display_name)} 的积分账单（近6个月）", "",
    ]
    for row in rows:
        sign = "+" if normalize_points(row["delta"]) > 0 else ""
        actor_name = " ".join(filter(None, (
            str(row["actor_first_name"] or ""),
            str(row["actor_last_name"] or ""),
        ))).strip()
        actor = (
            actor_name
            or ("@" + str(row["actor_username"]) if row["actor_username"] else "")
            or (str(row["created_by"]) if int(row["created_by"] or 0) else "系统")
        )
        lines.append(
            f"{format_beijing_time(row['created_at'])} · {sign}{format_points(row['delta'])} · 余额 {format_points(row['balance_after'])}\n"
            f"{html.escape(str(row['reason'] or '积分变动'))} · "
            f"操作人：{html.escape(actor)}"
        )
    if not rows:
        lines.append("暂无积分账单。")
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 条")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"pointmemberledger:{user_id}:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"pointmemberledger:{user_id}:{page + 1}"
        ))
    keyboard_rows = [nav] if nav else []
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回积分中心", callback_data="group:points")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)



def point_game_records_page(
    store: DirectoryStore, chat_id: int, user_id: int,
    display_name: str = "", page: int = 0, admin_view: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    total = store.count_point_game_records(chat_id, user_id)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = store.point_game_records(chat_id, user_id, page_size, page * page_size)
    if admin_view and not display_name:
        member = store.find_group_user(chat_id, str(user_id))
        display_name = (
            str(member["display_name"] or member["username"] or user_id)
            if member else str(user_id)
        )
    if admin_view:
        title = f"🎮 {html.escape(display_name)} 的游戏记录（近6个月）"
    else:
        title = "🎮 游戏记录（近6个月）"
    lines = [title, ""]
    for row in rows:
        raw_time = format_beijing_time(row["created_at"])
        short_time = raw_time[5:16] if len(raw_time) >= 16 else raw_time
        game_type = str(row["game_type"] or "")
        detail_raw = str(row["detail"] or "")
        detail = html.escape(detail_raw)
        stake = normalize_points(row["stake"] or 0)
        delta = normalize_points(row["delta"] or 0)
        balance = format_points(row["balance_after"] or 0)
        is_win = bool(row["is_win"])
        if game_type == "dice":
            side = str(row["side"] or "")
            dice_value = int(row["dice_value"] or 0)
            tags = dice_value_tags(dice_value) if 1 <= dice_value <= 6 else ""
            bet = f"押{side}{format_points(stake)}" if side else detail_raw
            points_part = (
                f"点数{dice_value}({tags})" if dice_value else detail_raw
            )
            outcome = (f"赢+{format_points(abs(delta))}" if is_win else f"输-{format_points(abs(delta))}")
            lines.append(
                f"{short_time} · 骰子 · {html.escape(bet)} · "
                f"{html.escape(points_part)} · {outcome} · 余额 {balance}"
            )
        else:
            mid = (
                f"中奖 {detail}" if is_win and detail_raw and detail_raw != "未中奖"
                else (detail or ("中奖" if is_win else "未中奖"))
            )
            sign = "+" if delta > 0 else ""
            lines.append(
                f"{short_time} · 积分抽奖 · {mid} · {sign}{format_points(delta)} · 余额 {balance}"
            )
    if not rows:
        lines.append("暂无游戏记录。")
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 条")
    if admin_view:
        nav = []
        if page:
            nav.append(InlineKeyboardButton(
                "上一页",
                callback_data=f"points:membergames:{user_id}:{page - 1}",
            ))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton(
                "下一页",
                callback_data=f"points:membergames:{user_id}:{page + 1}",
            ))
        keyboard_rows = [nav] if nav else []
        keyboard_rows.append([
            InlineKeyboardButton("⬅️ 返回积分中心", callback_data="group:points")
        ])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)
    return "\n".join(lines), _record_keyboard("points:games", page, total, page_size, None)


def point_gifts_text(store: DirectoryStore, chat_id: int) -> str:
    rows = store.point_gifts(chat_id)
    if not rows:
        return "🎁 本群暂未设置积分礼品。"
    lines = ["🎁 积分礼品", ""]
    for row in rows:
        stock = "不限量" if int(row["stock"]) < 0 else f"剩余 {row['stock']}"
        lines.append(f"#{row['id']} · {row['name']} · {format_points(row['points_cost'])} 积分 · {stock}")
    min_activity = int(store.points_config(chat_id)["redeem_min_activity"] or 0)
    if min_activity > 0:
        lines.extend(["", f"兑换条件：今日有效发言满 {min_activity} 条（1 分钟内最多算 2 条，少于 3 个字不算）"])
    lines.extend(["", "发送：兑换 礼品编号"])
    return "\n".join(lines)


def point_draw_view(
    store: DirectoryStore, chat_id: int, show_admin: bool = False,
    draw_cost=None,
) -> tuple[str, InlineKeyboardMarkup]:
    config = store.points_config(chat_id)
    rows = store.point_gifts(chat_id)
    lines = ["🎰 积分抽奖", ""]
    if not config["draw_enabled"]:
        lines.append("本群积分抽奖尚未开启。")
    else:
        minimum_cost = normalize_points(config["draw_cost"])
        spend = normalize_points(draw_cost if draw_cost is not None else minimum_cost)
        if spend < minimum_cost:
            spend = minimum_cost
        draw_cost = spend
        lines.append(
            f"本次消耗：{format_points(draw_cost)} 积分"
            f"（最低 {format_points(minimum_cost)}）"
        )
        if int(config["draw_min_activity"] or 0) > 0:
            lines.append(
                f"参与条件：今日有效发言满 {int(config['draw_min_activity'])} 条"
                "（1 分钟内最多算 2 条，少于 3 个字不算）"
            )
        lines.append("")
        for row in rows:
            stock = "不限量" if int(row["stock"]) < 0 else f"剩余{row['stock']}"
            lines.append(f"#{row['id']} {row['name']} · {stock}")
    buttons = []
    if config["draw_enabled"]:
        buttons.append([
            InlineKeyboardButton("✏️ 设置本次消耗积分", callback_data="points:drawcost")
        ])
        buttons.extend([
            [InlineKeyboardButton(
                f"抽 #{row['id']} {str(row['name'])[:18]}",
                callback_data=f"points:drawgift:{row['id']}",
            )]
            for row in rows
        ])
    buttons.append([InlineKeyboardButton("⬅️ 返回积分中心", callback_data="group:points")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def draw_min_activity_label(config) -> str:
    value = int(config["draw_min_activity"] or 0)
    return f"今日有效发言满 {value} 条才能参与" if value > 0 else "不限"


def point_draw_settings_view(
    store: DirectoryStore, chat_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    config = store.points_config(chat_id)
    enabled = bool(config["draw_enabled"])
    multiplier = float(config["draw_rate_multiplier"])
    text = (
        "⚙️ 积分抽奖设置\n\n"
        f"状态：{'✅ 开启' if enabled else '❌ 关闭'}\n"
        f"每次最低消耗：{format_points(config['draw_cost'])} 积分\n"
        f"中奖概率倍率：{multiplier:g}（范围 0-5）\n"
        f"最低当日活跃：{draw_min_activity_label(config)}\n"
        "（有效发言：1 分钟内最多算 2 条，少于 3 个字不算）\n\n"
        "中奖率按本次消耗积分、礼品所需积分和倍率自动计算；"
        "群员抽奖页面不显示倍率。"
    )
    rows = [
        [InlineKeyboardButton(
            "⛔ 关闭积分抽奖" if enabled else "✅ 开启积分抽奖",
            callback_data="points:set:drawtoggle",
        )],
        [InlineKeyboardButton(
            "✏️ 设置最低消耗积分", callback_data="points:set:drawmincost"
        )],
        [InlineKeyboardButton(
            f"⚙️ 设置中奖倍率 {multiplier:g}", callback_data="points:set:drawrate"
        )],
        [InlineKeyboardButton(
            "📝 设置最低当日活跃", callback_data="points:set:drawmsgmin"
        )],
        [InlineKeyboardButton("⬅️ 返回积分中心", callback_data="group:points")],
    ]
    return text, InlineKeyboardMarkup(rows)


def reset_point_draw_spend(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int,
) -> None:
    context.user_data.pop(f"point_draw_spend:{chat_id}", None)


async def point_draw_gift(
    update: Update, context: ContextTypes.DEFAULT_TYPE, gift_id: int,
    chat_id_override: int | None = None,
) -> None:
    query = update.callback_query
    chat = query.message.chat if query and query.message else None
    user = query.from_user if query else update.effective_user
    if not chat or not user:
        return
    draw_chat_id = chat_id_override or chat.id
    store: DirectoryStore = context.application.bot_data["store"]
    selected_cost = context.user_data.get(f"point_draw_spend:{draw_chat_id}")
    try:
        result = store.draw_point_gift(
            draw_chat_id, user.id, gift_id, user.username or "",
            user.full_name or user.username or "群成员",
            selected_cost,
        )
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    mention = telegram_user_link(user.id, user.full_name or user.username or "群成员")
    if result["is_winner"]:
        text = (
            f"🎉 {mention} 中奖啦！\n"
            f"🎁 礼品：{html.escape(str(result['gift_name']))}\n"
            f"兑奖编号：#{result['redemption_id']}\n"
            f"消耗：{format_points(result['points_spent'])} 积分 · 剩余：{format_points(result['balance'])} 积分"
        )
        with persistent_message():
            win_message = await context.bot.send_message(
                draw_chat_id, text, parse_mode=ParseMode.HTML
            )
        await pin_raffle_message(
            context, draw_chat_id, getattr(win_message, "message_id", 0),
            f"points:{result['redemption_id']}",
        )
        await query.answer(
            f"恭喜中奖！本次消耗 {format_points(result['points_spent'])} 积分，"
            f"剩余 {format_points(result['balance'])} 积分。",
            show_alert=True,
        )
    else:
        await query.answer(
            f"未中奖，消耗 {format_points(result['points_spent'])} 积分，剩余 {format_points(result['balance'])} 积分。",
            show_alert=True,
        )
    text, keyboard = point_draw_view(
        store, draw_chat_id,
        has_group_permission(context, draw_chat_id, user.id, "points"),
        selected_cost,
    )
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except TelegramError:
        pass


async def point_checkin_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    chat_id_override: int | None = None,
) -> None:
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if not chat or not user or not message:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        points, balance, streak, today_number = store.checkin_points(
            chat_id_override or chat.id, user.id, user.username or "", user.full_name or "群成员"
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    streak_text = f"，已连续签到 {streak} 天" if streak > 1 else ""
    with persistent_message():
        await message.reply_text(
            f"📅 今日第 {today_number} 个签到\n"
            f"签到成功 +{format_points(points)} 积分{streak_text}\n⭐ 当前积分：{format_points(balance)}"
        )



async def point_dice_bet_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    side: str, amount,
    chat_id_override: int | None = None,
) -> None:
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if not chat or not user or not message:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    chat_id = chat_id_override or chat.id
    config = store.points_config(chat_id)
    if not config["is_enabled"]:
        await message.reply_text("本群积分功能尚未开启")
        return
    if not config["dice_enabled"]:
        await message.reply_text("本群骰子游戏尚未开启")
        return
    min_activity = int(config["dice_min_activity"] or 0)
    free_activity = int(config["dice_free_activity"] or 0)
    today_messages = (
        store.user_today_active_messages(chat_id, user.id)
        if min_activity > 0 or free_activity > 0 else 0
    )
    if config["dice_schedule_enabled"]:
        opens = str(config["dice_open_time"] or "00:00")
        closes = str(config["dice_close_time"] or "23:59")
        bypass = free_activity > 0 and today_messages >= free_activity
        if not bypass and not dice_schedule_is_open(config):
            notice = f"骰子当前未开放，每日开放时间：{opens}-{closes}"
            if free_activity > 0:
                notice += (
                    f"\n今日有效发言满 {free_activity} 条可不受时间限制"
                    f"（1 分钟内最多算 2 条，少于 3 个字不算，当前 {today_messages} 条）"
                )
            await message.reply_text(notice)
            return
    if min_activity > 0 and today_messages < min_activity:
        await message.reply_text(
            f"今日活跃不足：需要当日有效发言 {min_activity} 条才能玩骰子"
            f"（1 分钟内最多算 2 条，少于 3 个字不算），你今天已有效发言 {today_messages} 条"
        )
        return
    account = store.point_account(chat_id, user.id)
    balance = normalize_points(account["balance"]) if account else Decimal("0")
    if balance <= 0:
        await message.reply_text("0分或负分不能玩骰子。")
        return
    stake = normalize_points(amount)
    minimum = normalize_points(config["dice_min_bet"])
    if stake < minimum:
        await message.reply_text(f"每次至少需要 {format_points(minimum)} 积分")
        return
    maximum = normalize_points(config["dice_max_bet"] or 0)
    if maximum > 0 and stake > maximum:
        await message.reply_text(f"单注最高 {format_points(maximum)} 积分")
        return
    if stake > balance:
        await message.reply_text(f"积分不足，当前积分：{format_points(balance)}")
        return
    try:
        with persistent_message():
            dice_message = await message.reply_dice(emoji="🎲")
    except TelegramError as exc:
        await message.reply_text(f"发送骰子失败：{exc}")
        return
    value = int(getattr(getattr(dice_message, "dice", None), "value", 0) or 0)
    if value < 1 or value > 6:
        await message.reply_text("未能取得骰子结果，请稍后再试。")
        return
    won = dice_side_matched(side, value)
    odds = int(config["dice_odds"] or 2000)
    odds = min(2000, max(1700, odds))
    if won:
        payout = normalize_points(stake * Decimal(odds) / Decimal(1000))
        delta = normalize_points(payout - stake)
    else:
        payout = Decimal("0")
        delta = normalize_points(-stake)
    reason = f"骰子{side}{'赢' if won else '输'}"
    try:
        new_balance = store.adjust_points(
            chat_id, user.id, delta, reason, user.id,
            user.username or "", user.full_name or "群成员",
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    tags = dice_value_tags(value)
    detail = (
        f"押{side}{format_points(stake)} · 点数{value}({tags}) · "
        f"赔率{odds} · 反{format_points(payout)}"
    )
    store.add_point_game_record(
        chat_id, user.id, "dice",
        side=side, dice_value=value, stake=stake, delta=delta,
        balance_after=new_balance, is_win=won,
        detail=detail,
    )
    if won:
        result = (
            f"赢 +{format_points(delta)}（赔率{odds} · "
            f"反{format_points(payout)}）"
        )
    else:
        result = f"输 -{format_points(stake)}"
    with persistent_message():
        await message.reply_text(
            f"🎲 骰子点数：{value}（{tags}）\n"
            f"押注：{side} {format_points(stake)}\n"
            f"结果：{result}\n"
            f"⭐ 当前积分：{format_points(new_balance)}"
        )


async def point_redeem_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, gift_id: int,
    chat_id_override: int | None = None,
) -> None:
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if not chat or not user or not message:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        redemption_id, gift_name, balance = store.redeem_point_gift(
            chat_id_override or chat.id, user.id, gift_id,
            user.username or "", user.full_name or "群成员"
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    mention = telegram_user_link(user.id, user.full_name or user.username or "群成员")
    redeem_text = (
        f"🎁 {mention} 兑换成功\n礼品：{html.escape(gift_name)}\n"
        f"兑换编号：#{redemption_id}\n剩余积分：{format_points(balance)}\n请联系群主领取。"
    )
    in_group = chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
    if in_group:
        with persistent_message():
            sent = await message.reply_text(redeem_text, parse_mode=ParseMode.HTML)
        await pin_raffle_message(
            context, chat.id, getattr(sent, "message_id", 0), f"redeem:{redemption_id}",
        )
    else:
        await message.reply_text(redeem_text, parse_mode=ParseMode.HTML)


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message or not message.new_chat_members:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    config = store.group_join_config(chat.id)
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        mention = telegram_user_link(
            member.id, member.full_name or member.username or "新成员"
        )
        template = str(config["welcome_text"] or "欢迎 {name} 加入本群！")
        welcome = html.escape(template).replace("{name}", mention).replace(
            "{group}", html.escape(chat.title or "本群")
        )
        verification_ok = False
        if config["verification_enabled"]:
            try:
                await context.bot.restrict_chat_member(
                    chat.id, member.id, ChatPermissions.no_permissions()
                )
                verification_ok = True
            except TelegramError as exc:
                store.audit(
                    "join-verify", "restrict.failed", f"{chat.id}:{member.id}", str(exc)
                )
        lines = []
        if config["welcome_enabled"]:
            lines.append(welcome)
        markup = None
        if verification_ok:
            lines.append("\n请点击下方按钮完成进群验证。")
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ 点击验证", callback_data=f"verify:{member.id}"
                )
            ]])
        if lines:
            with persistent_message():
                await context.bot.send_message(
                    chat.id, "\n".join(lines), parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )


async def award_effective_message_rewards(
    update, context: ContextTypes.DEFAULT_TYPE, store: DirectoryStore, chat, user, message,
) -> None:
    """Rewards triggered by one counted effective message: activity tiers and
    invite (message) rewards. Award messages are never auto-deleted."""
    try:
        tiers = store.award_activity_tiers(
            chat.id, user.id, user.username or "", user.full_name or "群成员"
        )
    except ValueError:
        tiers = []
    for need, points, balance in tiers:
        with persistent_message():
            await message.reply_text(
                f"🏅 今日有效发言达到 {need} 条，阶梯奖励 +{format_points(points)} 积分\n"
                f"⭐ 当前积分：{format_points(balance)}"
            )
    try:
        invite = store.award_invite_message_reward(chat.id, user.id)
    except ValueError:
        invite = None
    if invite:
        join, points, balance, threshold = invite
        inviter_id = int(join["inviter_id"])
        inviter = store.find_group_user(chat.id, str(inviter_id))
        inviter_name = (
            str(inviter["display_name"] or inviter["username"] or inviter_id)
            if inviter else str(inviter_id)
        )
        kind = "会员" if int(join["is_premium"] or 0) else "成员"
        with persistent_message():
            await context.bot.send_message(
                chat.id,
                f"🎉 邀请奖励：{telegram_user_link(inviter_id, inviter_name)} 邀请的{kind} "
                f"{telegram_user_link(user.id, user.full_name or user.username or str(user.id))} "
                f"有效发言已满 {threshold} 条\n"
                f"邀请人 +{format_points(points)} 积分，当前积分：{format_points(balance)}",
                parse_mode=ParseMode.HTML,
            )


async def track_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if not chat or not message or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    settings = store.get_settings()
    normal_message = int(
        user is not None
        and not user.is_bot
        and not message.new_chat_members
        and not message.left_chat_member
    )
    joins = len(message.new_chat_members or [])
    leaves = int(message.left_chat_member is not None)
    points_config = store.points_config(chat.id)
    points_tracking = bool(points_config["is_enabled"] and points_config["activity_enabled"])
    # 骰子口令（大3/小5/单10/双2，含被拒绝的下注）不算发言条数：
    # 不计入群统计、活跃奖励、骰子活跃门槛和抽奖发言条件。
    counted_message = 0 if (normal_message and is_dice_command(points_config, message)) else normal_message
    # 有效发言：去掉空白后至少 3 个字（贴纸/无说明媒体不算），滚动 1 分钟最多计 2 条。
    effective_text = is_effective_text(message.text or message.caption or "")
    effective_counted = False
    if normal_message or joins or leaves or settings.get("group_monitoring_enabled") == "1" or points_tracking:
        effective_counted = store.record_group_activity(
            chat.id,
            chat.title or "",
            chat.username or "",
            chat.type,
            user.id if user else 0,
            (user.username or "") if user else "",
            (user.full_name or user.username or "群成员") if user else "",
            messages=counted_message,
            joins=joins,
            leaves=leaves,
            effective=effective_text,
            is_premium=bool(getattr(user, "is_premium", False)) if user else False,
        ) is True
    for member in message.new_chat_members or ():
        if not member.is_bot:
            sight_user_profile_from_tg(store, chat.id, member)
            store.record_group_operation(
                chat.id, "join", member.id, member.username or "",
                member.full_name or member.username or str(member.id),
                actor_user_id=user.id if user else 0,
                actor_username=(user.username or "") if user else "",
                actor_name=(user.full_name or "") if user else "",
            )
    if message.left_chat_member and not message.left_chat_member.is_bot:
        member = message.left_chat_member
        store.record_group_operation(
            chat.id, "leave", member.id, member.username or "",
            member.full_name or member.username or str(member.id),
            actor_user_id=user.id if user else 0,
            actor_username=(user.username or "") if user else "",
            actor_name=(user.full_name or "") if user else "",
        )
    if joins:
        await welcome_new_members(update, context)
    if normal_message and user and not user.is_bot:
        sight_user_profile_from_tg(store, chat.id, user)
        keyword_text = (message.text or message.caption or "").strip()
        if keyword_text:
            for raffle in store.active_raffles_by_keyword(chat.id, keyword_text):
                joined, count = store.join_raffle(
                    int(raffle["id"]), user.id, user.username or "",
                    user.full_name or user.username or str(user.id),
                    via_keyword=True,
                )
                if joined:
                    mention = telegram_user_link(
                        user.id, user.full_name or user.username or str(user.id)
                    )
                    show_count = raffle_count_visible(store, chat.id)
                    count_line = (
                        f"\n👥 当前参与人数：{count} 人" if show_count else ""
                    )
                    await message.reply_text(
                        f"✅ {mention} 通过关键词参加抽奖 #{raffle['id']}\n"
                        f"🎁 奖品：\n{raffle_prize_text(str(raffle['prize']), int(raffle['winner_count']))}\n"
                        f"🏆 中奖名额：{raffle['winner_count']} 人{count_line}",
                        parse_mode=ParseMode.HTML,
                    )
        for raffle in (store.qualify_activity_raffles(
            chat.id, user.id, user.username or "",
            user.full_name or user.username or "群成员",
        ) if counted_message else ()):
            mention = telegram_user_link(
                user.id, user.full_name or user.username or str(user.id)
            )
            show_count = raffle_count_visible(store, chat.id)
            count_line = (
                f"\n👥 当前参与人数：{raffle['entries']} 人" if show_count else ""
            )
            kind = str(raffle["raffle_type"] or "")
            if kind == "universal":
                joined_line = (
                    f"✅ {mention} 发言达标，已自动参加抽奖 #{raffle['id']}" + "\n"
                )
            else:
                joined_line = (
                    f"✅ {mention} 已达到要求，成功参加抽奖 #{raffle['id']}" + "\n"
                )
            await message.reply_text(
                joined_line
                + "🎁 奖品：\n"
                + raffle_prize_text(str(raffle["prize"]), int(raffle["winner_count"]))
                + "\n"
                + f"🏆 中奖名额：{raffle['winner_count']} 人{count_line}",
                parse_mode=ParseMode.HTML,
            )
    if effective_counted and points_tracking and user:
        reward = store.award_activity_points(
            chat.id, user.id, user.username or "", user.full_name or "群成员"
        )
        if reward:
            points, balance, _, today_messages = reward
            with persistent_message():
                await message.reply_text(
                    f"🔥 今日已有效发言 {today_messages} 条（{EFFECTIVE_RULE_TEXT}），"
                    f"随机奖励 +{format_points(points)} 积分\n"
                    f"⭐ 当前积分：{format_points(balance)}"
                )
    if effective_counted and user and points_config["is_enabled"]:
        await award_effective_message_rewards(update, context, store, chat, user, message)
    content = message.text or message.caption or ""
    user_data = getattr(context, "user_data", {})
    if user_data.pop("allowed_invite_message_id", None) == message.message_id:
        return
    if (
        not content
        or settings.get("group_moderation_enabled") != "1"
        or not user
        or user.is_bot
    ):
        return
    blocked_keywords = store.moderation_keyword_values()
    entities = tuple(message.entities or ()) + tuple(message.caption_entities or ())
    has_link_entity = any(entity.type in {"url", "text_link"} for entity in entities)
    reason = group_violation_reason(content, blocked_keywords, has_link_entity)
    if not reason or await is_telegram_chat_admin(context, chat.id, user.id):
        return
    operation_errors = []
    try:
        await message.delete()
    except TelegramError as exc:
        operation_errors.append(f"删除失败：{exc}")
    violations = store.add_group_violation(
        chat.id, user.id, user.username or "", user.full_name or str(user.id), reason
    )
    store.record_group_operation(
        chat.id, "intercept", user.id, user.username or "",
        user.full_name or str(user.id), reason,
        actor_name="机器人自动处理",
    )
    action_text = ""
    if violations >= 5:
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.unban_chat_member(chat.id, user.id, only_if_banned=True)
            action_text = "累计达到 5 次，已踢出群组。"
            store.record_group_operation(
                chat.id, "ban", user.id, user.username or "",
                user.full_name or str(user.id), f"累计{violations}次；{reason}",
                actor_name="机器人自动处理",
            )
        except TelegramError as exc:
            operation_errors.append(f"踢出失败：{exc}")
    else:
        until_date = datetime.now(timezone.utc) + timedelta(days=3)
        try:
            await context.bot.restrict_chat_member(
                chat.id, user.id, ChatPermissions.no_permissions(), until_date=until_date
            )
            action_text = "已禁言 3 天。"
        except TelegramError as exc:
            operation_errors.append(f"禁言失败：{exc}")
    store.record_group_activity(
        chat.id, chat.title or "", chat.username or "", chat.type, blocked=1
    )
    store.audit(
        "group-monitor", "member.violation", f"{chat.id}:{user.id}",
        f"count={violations}; reason={reason}; errors={' | '.join(operation_errors)}",
    )
    name = telegram_user_link(user.id, user.full_name or user.username or str(user.id))
    result_lines = [
        f"{name} 违规处理",
        f"原因：{html.escape(reason)}",
        f"累计：{violations}/5 次",
    ]
    if action_text:
        result_lines.append(action_text)
    if operation_errors:
        result_lines.append("机器人权限不足：" + html.escape("；".join(operation_errors)))
    await context.bot.send_message(
        chat.id, "\n".join(result_lines), parse_mode=ParseMode.HTML
    )


async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    user = update.effective_user
    if not user or not await is_group_admin(update, context, user.id, "stats"):
        await update.effective_message.reply_text("只有群管理员可以查看群统计。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    await hydrate_group_speaker_names(context, chat.id, 1, 0)
    text, keyboard = group_stats_page(store, chat.id, 0, 1, True)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


def group_stats_page(
    store: DirectoryStore, chat_id: int, page: int, period_days: int = 1,
    show_history: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None]:
    data = store.group_stats(chat_id)
    if not data:
        return "群统计尚未生成，请稍后再试。", None
    if period_days not in {1, 7, 31} or (period_days > 1 and not show_history):
        period_days = 1
    period_label = {1: "今日", 7: "近7天", 31: "近31天"}[period_days]
    total = store.count_group_speakers(chat_id, period_days)
    page_count = max(1, (total + GROUP_STATS_PAGE_SIZE - 1) // GROUP_STATS_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * GROUP_STATS_PAGE_SIZE
    today_operations = store.group_operation_totals(chat_id, 1)
    all_operations = store.group_operation_totals(chat_id, 0)
    lines = [
        f"{data['title']} 群统计\n\n"
        f"今日消息：{data['today_messages']}\n"
        f"今日活跃用户：{data['today_active']}\n"
        f"今日进群：{today_operations['join']} · 今日退群：{today_operations['leave']}\n"
        f"今日拦截：{today_operations['intercept']} · 今日封禁：{today_operations['ban']}"
    ]
    if show_history:
        lines.append(
            f"\n累计消息：{data['message_count']}\n"
            f"累计进群：{all_operations['join']} · 累计退群：{all_operations['leave']}\n"
            f"累计拦截：{all_operations['intercept']} · 累计封禁：{all_operations['ban']}"
        )
    lines.append(f"\n统计时间：{beijing_now_text()}")
    speakers = store.group_speaker_ranking(
        chat_id, period_days, limit=GROUP_STATS_PAGE_SIZE, offset=start
    )
    if speakers:
        lines.append(
            f"\n成员{period_label}发言排行"
            f"\n第 {page + 1}/{page_count} 页 · 共 {total} 人："
        )
        for index, row in enumerate(speakers, start + 1):
            display_name = str(row["display_name"] or "")
            if not display_name or display_name == str(row["user_id"]):
                display_name = str(row["username"] or "群成员")
            name = telegram_user_link(int(row["user_id"]), display_name)
            lines.append(f"{index}. {name} · {row['period_messages']} 条")
    else:
        lines.append(f"\n{period_label}还没有成员发言记录。")
    rows = []
    if show_history:
        rows.append([
            InlineKeyboardButton(
                ("✓ " if period_days == days else "") + label,
                callback_data=f"groupstats:{days}:0",
            )
            for days, label in ((1, "今日"), (7, "近7天"), (31, "近31天"))
        ])
    buttons = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                "上一页", callback_data=f"groupstats:{period_days}:{page - 1}"
            )
        )
    if page + 1 < page_count:
        buttons.append(
            InlineKeyboardButton(
                "下一页", callback_data=f"groupstats:{period_days}:{page + 1}"
            )
        )
    if buttons:
        rows.append(buttons)
    rows.append([InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")])
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    return "\n".join(lines), keyboard


def group_active_page(
    store: DirectoryStore, chat_id: int, page: int, period_days: int = 1,
    show_month: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    data = store.group_stats(chat_id)
    title = str(data["title"] if data else "本群")
    period_days = period_days if period_days in {1, 7, 31} else 1
    period_label = {1: "今日", 7: "近7天", 31: "近31天"}[period_days]
    total = store.count_group_speakers(chat_id, period_days)
    page_count = max(1, (total + GROUP_STATS_PAGE_SIZE - 1) // GROUP_STATS_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * GROUP_STATS_PAGE_SIZE
    rows = store.group_speaker_ranking(
        chat_id, period_days, GROUP_STATS_PAGE_SIZE, start
    )
    lines = [f"🔥 {title} {period_label}活跃排行", ""]
    for index, row in enumerate(rows, start + 1):
        display = str(row["display_name"] or row["username"] or "群成员")
        if display == str(row["user_id"]):
            display = "群成员"
        lines.append(
            f"{index}. {telegram_user_link(int(row['user_id']), display)} · "
            f"发言 {row['period_messages']} 条"
        )
    if not rows:
        lines.append("暂无发言记录。")
    periods = [(1, "今日"), (7, "近7天")]
    if show_month:
        periods.append((31, "近31天"))
    keyboard_rows = [[
        InlineKeyboardButton(
            ("✓ " if period_days == days else "") + label,
            callback_data=f"groupactive:{days}:0",
        )
        for days, label in periods
    ]]
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton(
            "上一页", callback_data=f"groupactive:{period_days}:{page - 1}"
        ))
    if page + 1 < page_count:
        page_buttons.append(InlineKeyboardButton(
            "下一页", callback_data=f"groupactive:{period_days}:{page + 1}"
        ))
    if page_buttons:
        keyboard_rows.append(page_buttons)
    keyboard_rows.append([InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


async def lookup_public_telegram_username(
    username: str, config: Config | None = None,
) -> dict:
    value = username.strip().removeprefix("@").casefold()
    if not re.fullmatch(r"[a-zA-Z0-9_]{4,32}", value):
        raise ValueError("Telegram 用户名格式不正确")
    errors: list[str] = []
    if config and config.telegram_api_id and config.telegram_api_hash:
        client = None
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(), config.telegram_api_id, config.telegram_api_hash
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.sign_in(bot_token=config.bot_token)
            entity = await client.get_entity(value)
            return {
                "id": int(entity.id),
                "type": "bot" if bool(getattr(entity, "bot", False)) else "user",
                "username": str(getattr(entity, "username", "") or value),
                "first_name": str(getattr(entity, "first_name", "") or ""),
                "last_name": str(getattr(entity, "last_name", "") or ""),
            }
        except Exception as exc:
            errors.append(f"MTProto: {exc}")
        finally:
            if client:
                await client.disconnect()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for attempt in range(3):
            try:
                response = await client.get(
                    "https://telecrm.xyz/api/tgkit/resolve-username",
                    params={"username": value}, headers={"Accept": "application/json"},
                )
                payload = response.json()
                if response.is_success and payload.get("id"):
                    return payload
                errors.append(str(payload.get("detail") or response.status_code))
                if response.status_code < 500 and response.status_code != 429:
                    break
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(str(exc))
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
        try:
            page = await client.get("https://tg-user.id/")
            page.raise_for_status()
            token_match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"', page.text
            )
            if token_match:
                response = await client.post(
                    "https://tg-user.id/api/get-userid",
                    json={"username": value},
                    headers={
                        "Accept": "application/json",
                        "Origin": "https://tg-user.id",
                        "Referer": "https://tg-user.id/",
                        "X-CSRF-Token": token_match.group(1),
                    },
                )
                payload = response.json()
                if response.is_success and payload.get("id"):
                    return {
                        "id": payload["id"], "type": payload.get("type", "user"),
                        "username": payload.get("username", value),
                        "first_name": payload.get("firstName", ""),
                        "last_name": payload.get("lastName", ""),
                        "title": payload.get("title", ""),
                    }
                errors.append(str(payload.get("error") or response.status_code))
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(str(exc))
    raise ValueError(
        "公开用户名服务暂时繁忙。请把对方的一条消息转发给本机器人查询。"
    )


async def user_info_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    target_override: str | None = None,
) -> None:
    chat = update.effective_chat
    requester = update.effective_user
    message = update.effective_message
    if not chat or not requester or not message:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    forwarded_user = getattr(
        getattr(message, "forward_origin", None), "sender_user", None
    )
    target_user = (
        message.reply_to_message.from_user if message.reply_to_message else forwarded_user
    )
    target_value = (
        target_override.strip() if target_override is not None
        else " ".join(context.args).strip()
    )
    row = None
    external: dict | None = None
    if not target_user and target_value:
        row = (
            store.find_group_user(chat.id, target_value)
            if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
            else None
        ) or store.find_known_user(target_value) or store.find_any_group_user(target_value)
        if row:
            target_id = int(row["user_id"])
        elif not target_value.lstrip("-").isdigit() and (
            target_value.startswith("@")
            or re.fullmatch(r"[A-Za-z0-9_]{4,32}", target_value)
        ):
            try:
                external = await lookup_public_telegram_username(
                    target_value,
                    context.application.bot_data.get("config"),
                )
            except ValueError as exc:
                username = target_value.strip().removeprefix("@")
                await message.reply_text(
                    str(exc),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🌐 打开公开用户名查询",
                            url=f"https://tg-user.id/from/username/{username}",
                        )
                    ]]),
                )
                return
            target_id = int(external["id"])
            if str(external.get("type") or "") in {"user", "bot"}:
                store.touch_user(
                    target_id, str(external.get("username") or ""),
                    str(external.get("first_name") or ""),
                    str(external.get("last_name") or ""), "",
                )
        else:
            await message.reply_text(
                "没有找到该账号。请让对方先与机器人或本群互动，"
                "也可以回复对方消息或使用数字ID。"
            )
            return
    elif target_user:
        target_id = target_user.id
    else:
        target_user = requester
        target_id = requester.id
    username = (
        target_user.username if target_user else str(row["username"] or "") if row else ""
    ) or str((external or {}).get("username") or "")
    label = f"@{username}" if username else f"ID {target_id}"
    await message.reply_text(
        f"🆔 {html.escape(label)} 的 Telegram 数字ID：\n"
        f"<code>{target_id}</code>",
        parse_mode=ParseMode.HTML,
    )


def invite_reward_label(config, kind: str) -> str:
    keys = set(config.keys())
    if f"{kind}_msg_points" not in keys:
        return "关闭"
    threshold = int(config[f"{kind}_msg_threshold"] or 0)
    points = normalize_points(config[f"{kind}_msg_points"] or 0)
    parts = []
    if points > 0:
        parts.append(f"有效发言满 {max(1, threshold)} 条 +{format_points(points)}")
    if kind == "premium":
        boost = normalize_points(config["premium_boost_points"] or 0)
        if boost > 0:
            parts.append(f"每次助推 +{format_points(boost)}")
    return "，".join(parts) if parts else "关闭"


def invite_menu_view(
    store: DirectoryStore, chat_id: int, config=None, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    config = config or store.invite_config(chat_id)
    stats = store.invite_stats(chat_id)
    expire = (
        "无限制" if not int(config["expire_seconds"])
        else f"{int(config['expire_seconds']) // 3600} 小时"
    )
    maximum = "无限制" if not int(config["member_limit"]) else str(config["member_limit"])
    text = (
        "🔗 邀请链接生成\n\n"
        "开启后群成员使用 /link 自动生成个人邀请链接并查询统计。\n\n"
        "防作弊：只有第一次进群视为有效邀请；退群后使用其他人的链接不重复计算。\n\n"
        "统计与成员记录自动保留近6个月；关闭功能不会删除历史。\n\n"
        f"┌状态: {'✅开启' if config['is_enabled'] else '❌关闭'}\n"
        f"├链接过期时间: {expire}\n"
        f"├最大邀请人数: {maximum}\n"
        f"├每人邀请积分: {format_points(config['points_per_invite'])}\n"
        f"├会员邀请奖励: {invite_reward_label(config, 'premium')}\n"
        f"└普通邀请奖励: {invite_reward_label(config, 'normal')}\n"
        f"（有效发言：{EFFECTIVE_RULE_TEXT}；被邀请人退群/被踢会扣回对应奖励，取消助推扣回该次助推奖励）\n\n"
        "统计：\n"
        f"┌已生成链接数: {stats['links']}\n"
        f"├总邀请人数: {stats['invites']}\n"
        f"└已退出人数: {stats['exits']}"
    )
    owners = store.invite_owner_stats(chat_id)
    if owners:
        page_size = 10
        page_count = max(1, (len(owners) + page_size - 1) // page_size)
        page = min(max(page, 0), page_count - 1)
        owner_lines = ["", f"链接归属（第 {page + 1}/{page_count} 页）："]
        for row in owners[page * page_size:(page + 1) * page_size]:
            owner_name = str(row["display_name"] or row["user_id"])
            username = f" @{row['username']}" if row["username"] else ""
            invites = int(row["invites"] or 0)
            exits = int(row["exits"] or 0)
            owner_lines.append(
                f"• {owner_name}{username} (ID {row['user_id']})\n"
                f"  链接 {row['links']} · 进群 {invites} · 退出 {exits} · 现有 {invites - exits}"
            )
        owner_lines.append(f"共 {len(owners)} 位链接主人，每页10位。")
        text += "\n".join(owner_lines)
    keyboard_rows = [
        [
            InlineKeyboardButton("✅ 开启", callback_data="invite:on"),
            InlineKeyboardButton("关闭", callback_data="invite:off"),
        ],
        [InlineKeyboardButton("🛠 链接过期时间", callback_data="invite:set:expire")],
        [InlineKeyboardButton("🛠 最大邀请人数", callback_data="invite:set:max")],
        [InlineKeyboardButton("⭐ 每人邀请积分", callback_data="invite:set:points")],
        [
            InlineKeyboardButton("💎 会员邀请奖励", callback_data="invite:set:premium"),
            InlineKeyboardButton("👤 普通邀请奖励", callback_data="invite:set:normal"),
        ],
        [InlineKeyboardButton("🔎 查询邀请链接", callback_data="invite:query")],
        [InlineKeyboardButton("🔄 重置链接", callback_data="invite:reset")],
    ]
    if owners:
        owner_nav = []
        if page:
            owner_nav.append(InlineKeyboardButton(
                "上一页", callback_data=f"invite:owners:{page - 1}"
            ))
        if page + 1 < page_count:
            owner_nav.append(InlineKeyboardButton(
                "下一页", callback_data=f"invite:owners:{page + 1}"
            ))
        if owner_nav:
            keyboard_rows.append(owner_nav)
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回群组管理", callback_data="nav:group")
    ])
    return text, InlineKeyboardMarkup(keyboard_rows)


def invite_query_view(
    store: DirectoryStore, chat_id: int, link, page: int = 0,
    status: str = "joined", show_settings_back: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    if status not in {
        "joined", "exited", "remaining", "renamed",
        "remaining_unspoken", "remaining_spoken",
    }:
        status = "joined"
    aggregate_owner = isinstance(link, list)
    links = link if aggregate_owner else [link]
    if not links:
        raise ValueError("没有找到邀请链接记录")
    link = links[0]
    link_ids = [int(item["id"]) for item in links]
    page_size = 10
    stats = store.invite_links_stats(chat_id, link_ids)
    total = store.count_invite_links_members(chat_id, link_ids, status)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    members = store.invite_links_members(
        chat_id, link_ids, page_size, page * page_size, status
    )
    owner = store.find_group_user(chat_id, str(link["user_id"]))
    owner_name = (
        str(owner["display_name"] or owner["username"] or link["user_id"])
        if owner else str(link["display_name"] or link["username"] or link["user_id"])
    )
    owner_username = (
        f" @{owner['username']}" if owner and owner["username"]
        else f" @{link['username']}" if link["username"] else ""
    )
    callback_base = (
        f"inviteowner:{link['user_id']}" if aggregate_owner
        else f"invitequery:{link['id']}"
    )
    link_line = (
        f"链接：共 {len(links)} 个（当前：{html.escape(str(link['invite_link']))}）"
        if aggregate_owner else f"链接：{html.escape(str(link['invite_link']))}"
    )
    lines = [
        "🔎 邀请链接查询", "",
        f"归属：{html.escape(owner_name + owner_username)}（ID {link['user_id']}）",
        link_line,
        f"进群 {stats['invites']} · 退出 {stats['exits']} · 仍在 {stats['remaining']}", "",
    ]
    def member_line(row, timestamp_field: str) -> str:
        username = f" @{row['shown_username']}" if row["shown_username"] else ""
        name = telegram_user_link(
            int(row["user_id"]), str(row["shown_name"] or row["user_id"])
        )
        speech = (
            f"已发言 {int(row['shown_message_count'] or 0)} 条 · "
            f"最后发言 {format_beijing_time(row['shown_last_spoken_at'])}"
            if row["spoken"] and row["shown_last_spoken_at"] else
            f"已发言 {int(row['shown_message_count'] or 0)} 条"
            if row["spoken"] else "未发言"
        )
        line = (
            f"• {name}{html.escape(username)}（ID {row['user_id']}） · "
            f"{format_beijing_time(row[timestamp_field])}\n  {speech}"
        )
        if status == "renamed":
            original = str(row["first_spoken_name"] or row["display_name"] or "未记录")
            current = str(row["current_display_name"] or row["shown_name"] or "未记录")
            line += (
                f"\n  进群姓名：{html.escape(original)}"
                f"\n  现在姓名：{html.escape(current)}"
                f"\n  发现修改：{format_beijing_time(row['name_changed_at'])}"
            )
        return line

    labels = {
        "joined": ("加入人员记录", "joined_at"),
        "exited": ("退出人员记录", "left_at"),
        "remaining": ("还在人员", "joined_at"),
        "renamed": ("改名记录", "name_changed_at"),
        "remaining_unspoken": ("仍在群未发言人员", "joined_at"),
        "remaining_spoken": ("仍在群已发言人员", "joined_at"),
    }
    label, timestamp_field = labels[status]
    if members:
        lines.append(f"{label}：")
        lines.extend(member_line(row, timestamp_field) for row in members)
    if not members:
        lines.append("该链接还没有有效邀请记录。")
    lines.append(f"\n第 {page + 1}/{page_count} 页 · 共 {total} 人")
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(
            "上一页", callback_data=f"{callback_base}:{status}:{page - 1}"
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "下一页", callback_data=f"{callback_base}:{status}:{page + 1}"
        ))
    keyboard_rows = [[
        InlineKeyboardButton(
            "加入人员记录" + (" ✅" if status == "joined" else ""),
            callback_data=f"{callback_base}:joined:0",
        ),
        InlineKeyboardButton(
            "退出人员记录" + (" ✅" if status == "exited" else ""),
            callback_data=f"{callback_base}:exited:0",
        ),
        InlineKeyboardButton(
            "还在人员" + (" ✅" if status == "remaining" else ""),
            callback_data=f"{callback_base}:remaining:0",
        ),
    ], [
        InlineKeyboardButton(
            "改名记录" + (" ✅" if status == "renamed" else ""),
            callback_data=f"{callback_base}:renamed:0",
        ),
    ], [
        InlineKeyboardButton(
            "仍在群未发言" + (" ✅" if status == "remaining_unspoken" else ""),
            callback_data=f"{callback_base}:remaining_unspoken:0",
        ),
        InlineKeyboardButton(
            "仍在群已发言" + (" ✅" if status == "remaining_spoken" else ""),
            callback_data=f"{callback_base}:remaining_spoken:0",
        ),
    ]]
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append([InlineKeyboardButton(
        "复制链接", copy_text=CopyTextButton(str(link["invite_link"]))
    )])
    if show_settings_back:
        keyboard_rows.append([
            InlineKeyboardButton("⬅️ 返回邀请设置", callback_data="invite:menu")
        ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def extract_telegram_invite_link(text: str) -> str:
    match = re.fullmatch(
        r"https?://(?:t|telegram)\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+",
        text.strip(), re.IGNORECASE,
    )
    return match.group(0) if match else ""


async def send_personal_invite_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
) -> bool:
    invite_url = extract_telegram_invite_link(text)
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not invite_url or not chat or not user or not message:
        return False
    if (
        chat.type == ChatType.PRIVATE
        and context.user_data.get("menu_mode") not in {None, "", "invite_query"}
    ):
        return False
    store: DirectoryStore = context.application.bot_data["store"]
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        link = store.invite_link_by_url(chat.id, invite_url)
    elif chat.type == ChatType.PRIVATE:
        link = store.invite_link_by_url_any(invite_url)
    else:
        return False
    if not link:
        return False
    target_chat_id = int(link["chat_id"])
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if not store.invite_config(chat.id)["is_enabled"]:
            return False
    elif chat.type == ChatType.PRIVATE:
        if not store.invite_config(target_chat_id)["is_enabled"]:
            return False
    is_owner = int(link["user_id"]) == user.id
    is_admin = is_developer_user(context, user.id)
    if not is_owner and not is_admin:
        is_admin = await is_chat_admin(context, target_chat_id, user.id)
    if not is_owner and not is_admin:
        await message.reply_text("只能查询自己的邀请链接。")
        return True
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        context.user_data["allowed_invite_message_id"] = message.message_id
    context.user_data["selected_group_id"] = target_chat_id
    if context.user_data.get("menu_mode") == "invite_query":
        context.user_data.pop("menu_mode", None)
    result, markup = invite_query_view(
        store, target_chat_id, link, 0, show_settings_back=is_admin,
    )
    await message.reply_text(
        result, parse_mode=ParseMode.HTML, reply_markup=markup,
        disable_web_page_preview=True,
    )
    return True


async def personal_invite_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or not user:
        await message.reply_text("请在群内使用 /link。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    config = store.invite_config(chat.id)
    if not config["is_enabled"]:
        return
    row = store.active_invite_link(chat.id, user.id)
    if not row:
        expire_date = None
        if int(config["expire_seconds"]):
            expire_date = datetime.now(timezone.utc) + timedelta(
                seconds=int(config["expire_seconds"])
            )
        try:
            link = await context.bot.create_chat_invite_link(
                chat.id, name=f"uid:{user.id}", expire_date=expire_date,
                member_limit=int(config["member_limit"]) or None,
            )
        except TelegramError as exc:
            await message.reply_text(f"生成失败，请确认机器人有邀请用户权限：{exc}")
            return
        store.save_invite_link(
            chat.id, user.id, link.invite_link, link.name or "",
            user.username or "", user.full_name or str(user.id),
        )
        row = store.active_invite_link(chat.id, user.id)
    stats = store.invite_stats(chat.id, user.id)
    await message.reply_text(
        f"🔗 你的专属邀请链接\n{row['invite_link']}\n\n"
        f"有效邀请人数：{stats['invites']}\n"
        f"已退出人数：{stats['exits']}\n"
        "只有成员第一次进群会计数。",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "复制链接", copy_text=CopyTextButton(str(row["invite_link"]))
            )
        ]]),
    )


async def track_personal_invite(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    change = update.chat_member
    if not change:
        return
    old_status = str(change.old_chat_member.status)
    new_status = str(change.new_chat_member.status)
    active_statuses = {
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED,
    }
    member = change.new_chat_member.user
    store: DirectoryStore = context.application.bot_data["store"]
    if old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED} and new_status in active_statuses:
        sight_user_profile_from_tg(store, change.chat.id, member)
        store.record_group_operation(
            change.chat.id, "join", member.id, member.username or "",
            member.full_name or str(member.id), "Telegram成员状态更新",
            actor_user_id=change.from_user.id,
            actor_username=change.from_user.username or "",
            actor_name=change.from_user.full_name or "",
        )
    elif old_status in active_statuses and new_status == ChatMemberStatus.LEFT:
        store.record_group_operation(
            change.chat.id, "leave", member.id, member.username or "",
            member.full_name or str(member.id), "Telegram成员状态更新",
            actor_user_id=change.from_user.id,
            actor_username=change.from_user.username or "",
            actor_name=change.from_user.full_name or "",
        )
    elif old_status in active_statuses and new_status == ChatMemberStatus.BANNED:
        store.record_group_operation(
            change.chat.id, "ban", member.id, member.username or "",
            member.full_name or str(member.id), "Telegram成员状态更新",
            actor_user_id=change.from_user.id,
            actor_username=change.from_user.username or "",
            actor_name=change.from_user.full_name or "",
        )
    if old_status in active_statuses and new_status in {
        ChatMemberStatus.LEFT, ChatMemberStatus.BANNED,
    }:
        departed = store.record_invite_leave(change.chat.id, member.id)
        if departed:
            extra = store.revoke_invite_extra_rewards(
                change.chat.id, member.id, int(departed["inviter_id"]),
                departed["msg_reward"] if "msg_reward" in departed.keys() else 0,
            )
            if extra > 0:
                store.audit(
                    f"tg:{member.id}", "invite.leave.extra", str(change.chat.id),
                    f"inviter={departed['inviter_id']}; points=-{extra}",
                )
        if departed and normalize_points(departed["points_awarded"] or 0) != 0:
            inviter_id = int(departed["inviter_id"])
            inviter = store.find_group_user(change.chat.id, str(inviter_id))
            store.adjust_points(
                change.chat.id, inviter_id,
                -normalize_points(departed["points_awarded"]),
                f"邀请成员 {member.id} 退出群组，扣回邀请积分", 0,
                str(inviter["username"] or "") if inviter else "",
                str(inviter["display_name"] or "") if inviter else "",
                allow_negative=True,
            )
            store.audit(
                f"tg:{member.id}", "invite.leave", str(change.chat.id),
                f"inviter={inviter_id}; points=-{departed['points_awarded']}",
            )
    if (
        not change.invite_link
        or old_status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
        or new_status not in active_statuses
    ):
        return
    row = store.invite_link_by_url(change.chat.id, change.invite_link.invite_link)
    if not row:
        return
    member_id = member.id
    if member_id == int(row["user_id"]):
        return
    invite_config = store.invite_config(change.chat.id)
    invite_points = normalize_points(invite_config["points_per_invite"] or 0)
    if not store.points_config(change.chat.id)["is_enabled"]:
        invite_points = 0
    if store.record_invite_join(
        change.chat.id, member_id, int(row["user_id"]), int(row["id"]),
        invite_points, member.username or "", member.full_name or str(member_id),
        credit_points=True, inviter_username=str(row["username"] or ""),
        inviter_name=str(row["display_name"] or ""),
    ):
        store.mark_invitee_premium(
            change.chat.id, member_id, bool(getattr(member, "is_premium", False))
        )
        store.audit(
            f"tg:{member_id}", "invite.join", str(change.chat.id),
            f"inviter={row['user_id']}",
        )


def _boost_source_user(boost):
    source = getattr(boost, "source", None)
    return getattr(source, "user", None) if source is not None else None


async def track_chat_boost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """chat_boost: an invited member boosted the group -> inviter earns points."""
    change = getattr(update, "chat_boost", None)
    if not change:
        return
    boost = getattr(change, "boost", None)
    booster = _boost_source_user(boost)
    if boost is None or booster is None:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    chat_id = int(change.chat.id)
    result = store.award_invite_boost(chat_id, str(boost.boost_id), int(booster.id))
    if not result:
        return
    join, points, balance = result
    inviter_id = int(join["inviter_id"])
    inviter = store.find_group_user(chat_id, str(inviter_id))
    inviter_name = (
        str(inviter["display_name"] or inviter["username"] or inviter_id) if inviter else str(inviter_id)
    )
    store.audit(f"tg:{booster.id}", "invite.boost", str(chat_id), f"inviter={inviter_id}; points={points}")
    try:
        with persistent_message():
            await context.bot.send_message(
                chat_id,
                f"⚡ 助推奖励：{telegram_user_link(inviter_id, inviter_name)} 邀请的会员 "
                f"{telegram_user_link(booster.id, booster.full_name or booster.username or str(booster.id))} "
                f"助推了本群\n邀请人 +{format_points(points)} 积分，当前积分：{format_points(balance)}",
                parse_mode=ParseMode.HTML,
            )
    except TelegramError as exc:
        logging.info("Could not announce boost reward in %s: %s", chat_id, exc)


async def track_removed_chat_boost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """removed_chat_boost: boost removed/expired -> claw back that boost's reward."""
    removed = getattr(update, "removed_chat_boost", None)
    if not removed:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    chat_id = int(removed.chat.id)
    result = store.revoke_invite_boost(chat_id, str(removed.boost_id))
    if not result:
        return
    row, points, _balance = result
    store.audit(
        f"tg:{row['user_id']}", "invite.boost.removed", str(chat_id),
        f"inviter={row['inviter_id']}; points=-{points}",
    )


async def moderation_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    if not has_super_admin_access(context, user.id if user else None):
        await update.effective_message.reply_text("只有超级管理员可以开启或关闭违规处罚。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    action = context.args[0].strip().casefold() if context.args else "status"
    if action in {"on", "1", "开启", "开"}:
        store.set_setting("group_moderation_enabled", "1")
        store.audit(f"tg:{user.id}", "moderation.enable", str(chat.id))
        await update.effective_message.reply_text(
            "群违规处罚已开启：链接或违规关键词消息会被删除；"
            "第1-4次禁言3天，第5次踢出群组。"
        )
        return
    if action in {"off", "0", "关闭", "关"}:
        store.set_setting("group_moderation_enabled", "0")
        store.audit(f"tg:{user.id}", "moderation.disable", str(chat.id))
        await update.effective_message.reply_text("群违规处罚已关闭。")
        return
    if action not in {"status", "状态"}:
        await update.effective_message.reply_text("用法：/moderation on、off 或 status")
        return
    enabled = store.get_settings().get("group_moderation_enabled") == "1"
    await update.effective_message.reply_text(
        "群违规处罚状态：" + ("已开启" if enabled else "已关闭")
    )


def moderation_summary(store: DirectoryStore) -> str:
    enabled = store.get_settings().get("group_moderation_enabled") == "1"
    rows = store.list_moderation_keywords()
    lines = [
        "🚫 违规管理",
        "",
        "处罚状态：" + ("已开启" if enabled else "已关闭"),
        "处罚规则：违规消息立即删除；第1-4次禁言3天，第5次踢出。",
        "违规范围：消息中的链接，以及管理员维护的违规关键词。",
        "",
        f"违规关键词（{len(rows)}个）：",
    ]
    lines.extend(f"• {row['keyword']}" for row in rows[:50])
    if len(rows) > 50:
        lines.append(f"另有 {len(rows) - 50} 个，请在后台查看。")
    if not rows:
        lines.append("暂无。链接仍会按违规处理。")
    return "\n".join(lines)


async def badword_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if not has_permission(context, user.id if user else None, "moderation"):
        await update.effective_message.reply_text("仅管理员可维护违规关键词。")
        return
    store: DirectoryStore = context.application.bot_data["store"]
    action = context.args[0].casefold() if context.args else "list"
    value = " ".join(context.args[1:]).strip()
    try:
        if action in {"add", "添加", "增加"}:
            if not value:
                raise ValueError("用法：/badword add 违规关键词")
            keyword_id = store.add_moderation_keyword(value, user.id)
            store.audit(f"tg:{user.id}", "moderation_keyword.add", str(keyword_id), value)
            text = f"已添加违规关键词：{value}"
        elif action in {"del", "delete", "删除"}:
            if not value:
                raise ValueError("用法：/badword del 违规关键词")
            if not store.remove_moderation_keyword_value(value):
                raise ValueError("没有找到这个违规关键词")
            store.audit(f"tg:{user.id}", "moderation_keyword.delete", value)
            text = f"已删除违规关键词：{value}"
        elif action in {"list", "列表", "查看"}:
            text = moderation_summary(store)
        else:
            raise ValueError("用法：/badword add、del 或 list")
    except ValueError as exc:
        text = str(exc)
    await update.effective_message.reply_text(
        text,
        reply_markup=moderation_menu_keyboard(
            has_super_admin_access(context, user.id if user else None)
        ),
    )


def search_stats_page(
    store: DirectoryStore, mode: str, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    mode = mode if mode in {"keywords", "events"} else "keywords"
    total = (
        store.count_search_keywords() if mode == "keywords"
        else store.count_search_events()
    )
    page_count = max(1, (total + SEARCH_STATS_PAGE_SIZE - 1) // SEARCH_STATS_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    offset = page * SEARCH_STATS_PAGE_SIZE
    if mode == "keywords":
        rows = store.search_keyword_rankings(SEARCH_STATS_PAGE_SIZE, offset)
        lines = [f"搜索关键词排名 · 第 {page + 1}/{page_count} 页 · 共 {total} 个", ""]
        for index, row in enumerate(rows, offset + 1):
            people = []
            searchers = list(row["searchers"])
            for searcher in searchers[:5]:
                display_name = str(searcher["display_name"] or searcher["username"] or searcher["user_id"])
                people.append(
                    f"{telegram_user_link(int(searcher['user_id']), display_name)}"
                    f"({searcher['searches']}次)"
                )
            if len(searchers) > 5:
                people.append(f"另 {len(searchers) - 5} 人")
            lines.append(
                f"{index}. <b>{html.escape(str(row['query']))}</b> · "
                f"{row['searches']} 次 / {row['unique_users']} 人\n"
                f"   搜索者：{'、'.join(people) or '无'}"
            )
        if not rows:
            lines.append("暂无搜索记录。")
    else:
        rows = store.list_search_events(SEARCH_STATS_PAGE_SIZE, offset)
        lines = [f"搜索明细 · 第 {page + 1}/{page_count} 页 · 共 {total} 条", ""]
        for index, row in enumerate(rows, offset + 1):
            display_name = str(row["display_name"] or row["username"] or row["user_id"])
            user_link = telegram_user_link(int(row["user_id"]), display_name)
            chat_type = str(row["chat_type"] or "")
            source = (
                "群聊" if chat_type in {ChatType.GROUP, ChatType.SUPERGROUP}
                else "私聊"
            )
            source_detail = {
                "search_command": "搜索命令",
                "menu_search": "菜单搜索",
                "group_keyword": "关键词搜索",
                "group_exact_keyword": "精准关键词",
                "private_group_keyword": "关键词搜索",
                "private_exact_keyword": "精准关键词",
            }.get(str(row["source"]), "搜索")
            lines.append(
                f"{index}. {user_link} 搜索 <b>{html.escape(str(row['query']))}</b>\n"
                f"   {source} · {source_detail} · 命中 {row['result_count']} 条 · "
                f"{format_beijing_time(row['created_at'])}"
            )
        if not rows:
            lines.append("暂无搜索记录。")
    keyboard_rows = [[
        InlineKeyboardButton(
            ("✓ " if mode == "keywords" else "") + "关键词排行",
            callback_data="searchstats:keywords:0",
        ),
        InlineKeyboardButton(
            ("✓ " if mode == "events" else "") + "搜索明细",
            callback_data="searchstats:events:0",
        ),
    ]]
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton(
            "上一页", callback_data=f"searchstats:{mode}:{page - 1}"
        ))
    if page + 1 < page_count:
        page_buttons.append(InlineKeyboardButton(
            "下一页", callback_data=f"searchstats:{mode}:{page + 1}"
        ))
    if page_buttons:
        keyboard_rows.append(page_buttons)
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回搜索服务", callback_data="nav:search")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def bot_usage_page(
    store: DirectoryStore, page: int, page_size: int = 10,
) -> tuple[str, InlineKeyboardMarkup]:
    _, total = store.bot_usage_users(1, 0)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows, _ = store.bot_usage_users(page_size, page * page_size)
    lines = [
        f"👤 机器人使用人员 · 第 {page + 1}/{page_count} 页",
        f"近一年共 {total} 人，按最近使用时间排列。", "",
    ]
    for position, row in enumerate(rows, page * page_size + 1):
        display_name = str(row["display_name"] or row["username"] or row["user_id"])
        user_link = telegram_user_link(int(row["user_id"]), display_name)
        username = f" @{html.escape(str(row['username']))}" if row["username"] else ""
        lines.append(
            f"{position}. {user_link}{username}\n"
            f"   使用 {row['use_count']} 次 · 最近："
            f"{format_beijing_time(row['last_used_at'])}"
        )
    if not rows:
        lines.append("近一年暂无使用记录。")
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(
            "上一页", callback_data=f"admin:usage:{page - 1}"
        ))
    if page + 1 < page_count:
        buttons.append(InlineKeyboardButton(
            "下一页", callback_data=f"admin:usage:{page + 1}"
        ))
    keyboard_rows = [buttons] if buttons else []
    keyboard_rows.append([
        InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


async def search_stats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not has_developer_access(context, user.id if user else None):
        await update.effective_message.reply_text(developer_only_text(context))
        return
    if not chat or chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("搜索统计包含用户信息，请私聊机器人查看。")
        return
    mode = "events" if context.args and context.args[0] in {"2", "明细"} else "keywords"
    text, keyboard = search_stats_page(
        context.application.bot_data["store"], mode, 0
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def raffle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("抽奖只能在群组中发起。")
        return
    if not user or not await is_group_admin(update, context, user.id, "raffles"):
        await update.effective_message.reply_text("只有群管理员可以发起抽奖。")
        return
    if len(context.args) < 3 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.effective_message.reply_text(
            "用法：/raffle 分钟 中奖人数 奖品名称\n"
            "单档示例：/raffle 10 2 会员奖励\n"
            "多档示例：/raffle 10 3 1*188RMB | 2*88RMB"
        )
        return
    minutes = int(context.args[0])
    winner_count = int(context.args[1])
    prize = " ".join(context.args[2:]).strip()
    if not 1 <= minutes <= 10080 or not 1 <= winner_count <= 50 or not prize:
        await update.effective_message.reply_text("分钟范围 1-10080，中奖人数范围 1-50。")
        return
    try:
        parse_raffle_prizes(prize, winner_count, strict=True)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    store: DirectoryStore = context.application.bot_data["store"]
    ends_at = utc_after_minutes_text(minutes)
    raffle_id = store.create_raffle(chat.id, user.id, prize, winner_count, ends_at)
    raffle = store.get_raffle(raffle_id)
    show_count = raffle_count_visible(store, chat.id)
    with persistent_message():
        sent = await update.effective_message.reply_text(
            raffle_text(raffle, show_count),
            parse_mode=ParseMode.HTML,
            reply_markup=raffle_keyboard(raffle_id, 0, show_count, raffle=raffle),
        )
    store.set_raffle_message(raffle_id, sent.message_id)
    await pin_raffle_message(context, chat.id, sent.message_id, raffle_id)
    store.audit(f"tg:{user.id}", "raffle.create", str(raffle_id), prize)


async def raffles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    rows = context.application.bot_data["store"].list_raffles(chat.id, limit=10)
    if not rows:
        await update.effective_message.reply_text("当前群还没有抽奖记录。")
        return
    lines = ["最近抽奖："]
    for row in rows:
        lines.append(
            f"#{row['id']} · {row['prize']} · {row['status']} · {row['entries']} 人 · "
            f"开奖 {format_beijing_time(row['ends_at'])}"
        )
    await update.effective_message.reply_text("\n".join(lines))


def raffle_history_view(
    store: DirectoryStore, chat_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 10
    total = store.raffle_history_count(chat_id)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    rows = store.raffle_history(chat_id, page_size, page * page_size)
    lines = ["🎁 抽奖历史", f"历史抽奖次数：{total}", ""]
    for row in rows:
        status = "已开奖" if row["status"] == "drawn" else "已取消"
        winners = str(row["winner_names"] or "")
        lines.extend([
            f"#{row['id']} · {status} · 参加 {row['entries']} 人",
            f"奖品：{row['prize']}",
            f"中奖详情：{winners or '无'}",
            f"开奖时间：{format_beijing_time(row['drawn_at'] or row['ends_at'])}", "",
        ])
    if not rows:
        lines.append("本群还没有已结束的抽奖。")
    lines.append(f"第 {page + 1}/{pages} 页")
    buttons = []
    if page:
        buttons.append(InlineKeyboardButton("上一页", callback_data=f"rafflehistory:{page - 1}"))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton("下一页", callback_data=f"rafflehistory:{page + 1}"))
    keyboard_rows = [buttons] if buttons else []
    keyboard_rows.append([InlineKeyboardButton("关闭", callback_data="rafflehistory:close")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await raffle_admin_action(update, context, "draw")


async def cancel_raffle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await raffle_admin_action(update, context, "cancel")


async def raffle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("该命令只能在群组中使用。")
        return
    if not user or not await is_group_admin(update, context, user.id, "raffles"):
        await update.effective_message.reply_text("只有群管理员可以执行。")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"用法：/{'draw' if action == 'draw' else 'cancelraffle'} 抽奖编号")
        return
    raffle_id = int(context.args[0])
    store: DirectoryStore = context.application.bot_data["store"]
    raffle = store.get_raffle(raffle_id)
    if not raffle or int(raffle["chat_id"]) != chat.id:
        await update.effective_message.reply_text("当前群没有这个抽奖。")
        return
    if action == "cancel":
        if store.cancel_raffle(raffle_id):
            store.audit(f"tg:{user.id}", "raffle.cancel", str(raffle_id))
            await update.effective_message.reply_text(f"抽奖 #{raffle_id} 已取消。")
        else:
            await update.effective_message.reply_text("抽奖已结束或已取消。")
        return
    await draw_raffle(context, raffle_id, requested_by=f"tg:{user.id}")


async def draw_raffle(context: ContextTypes.DEFAULT_TYPE, raffle_id: int, requested_by: str = "scheduler") -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    raffle = store.get_raffle(raffle_id)
    if not raffle or raffle["status"] != "active":
        return
    raffle_type = str(raffle["raffle_type"] or "universal")
    if raffle_type.startswith("activity_"):
        candidates = store.active_raffle_candidates(raffle_id)
        store.sync_raffle_entries(raffle_id, candidates)
        entries = store.raffle_entries(raffle_id)
    elif raffle_type == "universal" and store._universal_auto_min_messages(raffle) > 0:
        candidates = store.universal_message_candidates(raffle_id)
        store.sync_raffle_entries(raffle_id, candidates)
        entries = store.raffle_entries(raffle_id)
    else:
        entries = store.raffle_entries(raffle_id)
    min_participants = raffle_min_participants(raffle)
    if min_participants > 0 and len(entries) < min_participants:
        await postpone_raffle_for_min_participants(
            context, raffle, len(entries), min_participants, requested_by,
        )
        return
    winner_total = min(int(raffle["winner_count"]), len(entries))
    if raffle_type == "activity_rank":
        candidate_order = {
            int(row["user_id"]): index
            for index, row in enumerate(store.active_raffle_candidates(raffle_id))
        }
        winners = sorted(
            entries, key=lambda row: candidate_order.get(int(row["user_id"]), 10**9)
        )[:winner_total]
    else:
        winners = (
            raffle_fair.pick_winners(entries, winner_total, store=store, chat_id=int(raffle["chat_id"]))
            if winner_total else []
        )
    winner_notes: dict[int, str] = {}
    if winners:
        chat_id = int(raffle["chat_id"])
        for row in winners:
            uid = int(row["user_id"])
            parts: list[str] = []
            if not str(raffle["raffle_type"] or "universal").startswith("activity_"):
                unmet = await evaluate_raffle_unmet(context, raffle, uid)
                if unmet:
                    parts.append("未达标:" + "/".join(unmet))
            if store.user_has_name_change_on_beijing_day(chat_id, uid):
                parts.append("当天已改名字姓氏")
            if parts:
                winner_notes[uid] = "；".join(parts)
    if not store.complete_raffle(
        raffle_id, [int(row["user_id"]) for row in winners], winner_notes,
    ):
        return
    try:
        if raffle["message_id"]:
            await context.bot.edit_message_reply_markup(
                chat_id=int(raffle["chat_id"]), message_id=int(raffle["message_id"]), reply_markup=None
            )
    except TelegramError:
        logging.info("Could not remove raffle button for %s", raffle_id)
    if winners:
        prize_slots: list[str] = []
        for quantity, label in parse_raffle_prizes(
            str(raffle["prize"]), int(raffle["winner_count"])
        ):
            prize_slots.extend([label] * quantity)
        winner_lines = []
        for position, row in enumerate(winners, 1):
            display_name = str(row["display_name"] or row["username"] or row["user_id"])
            mention = telegram_user_link(int(row["user_id"]), display_name)
            prize_label = html.escape(prize_slots[len(winner_lines)])
            note = winner_notes.get(int(row["user_id"]), "")
            note_line = f"\n⚠️ {html.escape(note)}" if note else ""
            winner_lines.append(
                f"🏆 第{position}名：{mention}\n🎁 奖品：{prize_label}{note_line}"
            )
        creator_name = str(raffle["creator_id"])
        try:
            creator = await context.bot.get_chat_member(
                int(raffle["chat_id"]), int(raffle["creator_id"])
            )
            creator_name = creator.user.full_name or creator.user.username or creator_name
        except TelegramError:
            pass
        creator_link = telegram_user_link(int(raffle["creator_id"]), creator_name)
        win_rate = winner_total / len(entries) * 100 if entries else 0
        result = (
            "<b>福利活动持续中</b>\n\n"
            "🎁 <b>活动开奖啦！</b>\n"
            f"总共参与 {len(entries)} 人，综合中奖率 {win_rate:.2f}%\n\n"
            "🥳🥳 <b>恭喜以下中奖用户：</b>\n\n"
            + "\n\n".join(winner_lines)
            + f"\n\n👮🏼 抽奖创建者：{creator_link}\n\n"
            "『联系该群管理员领取您的奖品吧』\n"
            "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉"
        )
    else:
        auto_msg = raffle_type.startswith("activity_") or (
            raffle_type == "universal" and store._universal_auto_min_messages(raffle) > 0
        )
        result = (
            f"抽奖 #{raffle_id} 已结束，但没有成员达到活跃条件。"
            if auto_msg
            else f"抽奖 #{raffle_id} 已结束，但没有用户报名。"
        )
    try:
        with persistent_message():
            result_message = await context.bot.send_message(
                int(raffle["chat_id"]), result, parse_mode=ParseMode.HTML
            )
        await pin_raffle_message(
            context, int(raffle["chat_id"]),
            getattr(result_message, "message_id", 0), raffle_id,
        )
    except TelegramError as exc:
        logging.exception("Could not announce raffle %s", raffle_id)
        store.audit("bot", "raffle.announce_failed", str(raffle_id), str(exc))
    store.audit(requested_by, "raffle.draw", str(raffle_id), f"entries={len(entries)} winners={winner_total}")


def next_postponed_ends_at(ends_at: str, now: datetime | None = None) -> str:
    """Same Beijing clock on the next day (repeat until it is in the future)."""
    now = now or datetime.now(timezone.utc)
    end_dt = DirectoryStore._parse_utc_text(str(ends_at or "")) or now
    new_dt = end_dt + timedelta(days=1)  # Beijing has no DST: +24h == next day same clock
    while new_dt <= now:
        new_dt += timedelta(days=1)
    return new_dt.strftime("%Y-%m-%d %H:%M:%S")


def raffle_postpone_text(raffle, entries: int, minimum: int, new_ends_at: str) -> str:
    title = str(_raffle_field(raffle, "title", "") or "").strip()
    name = f"抽奖 #{raffle['id']}" + (f"「{html.escape(title)}」" if title else "")
    return (
        f"⏳ {name}人数不足（当前 {entries}/{minimum}），"
        f"顺延到 {html.escape(format_raffle_draw_time(new_ends_at))} 开奖。\n"
        f"已参与记录保留，参与人数达到 {minimum} 人后将按时开奖。"
    )


async def postpone_raffle_for_min_participants(
    context: ContextTypes.DEFAULT_TYPE, raffle, entries: int, minimum: int,
    requested_by: str = "scheduler",
) -> str:
    """Postpone an under-subscribed raffle by one day; announce + pin in group."""
    store: DirectoryStore = context.application.bot_data["store"]
    raffle_id = int(raffle["id"])
    chat_id = int(raffle["chat_id"])
    new_ends_at = next_postponed_ends_at(str(raffle["ends_at"] or ""))
    if not store.postpone_raffle(raffle_id, new_ends_at):
        return ""
    store.audit(
        requested_by, "raffle.postpone", str(raffle_id),
        f"entries={entries}/{minimum}; ends={new_ends_at}",
    )
    try:
        await refresh_raffle_announcement(context, raffle_id, chat_id)
    except TelegramError as exc:
        logging.info("Could not refresh postponed raffle %s: %s", raffle_id, exc)
    try:
        with persistent_message():
            notice = await context.bot.send_message(
                chat_id, raffle_postpone_text(raffle, entries, minimum, new_ends_at),
                parse_mode=ParseMode.HTML,
            )
        await pin_raffle_message(
            context, chat_id, getattr(notice, "message_id", 0), raffle_id,
        )
    except TelegramError as exc:
        logging.info("Could not announce raffle postponement %s: %s", raffle_id, exc)
        store.audit("bot", "raffle.postpone_announce_failed", str(raffle_id), str(exc))
    return new_ends_at


async def announce_new_raffle(
    context: ContextTypes.DEFAULT_TYPE, raffle_id: int,
) -> None:
    """Post + pin the start announcement for a raffle created without one (daily recur)."""
    store: DirectoryStore = context.application.bot_data["store"]
    raffle = store.get_raffle(raffle_id)
    if not raffle:
        return
    chat_id = int(raffle["chat_id"])
    show_count = raffle_count_visible(store, chat_id)
    markup = (
        raffle_keyboard(raffle_id, int(raffle["entries"] or 0), show_count, raffle=raffle)
        if str(raffle["raffle_type"] or "") == "universal" else None
    )
    try:
        with persistent_message():
            sent = await context.bot.send_message(
                chat_id, raffle_text(raffle, show_count),
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
    except TelegramError as exc:
        logging.info("Could not announce recurring raffle %s: %s", raffle_id, exc)
        store.audit("bot", "raffle.announce_failed", str(raffle_id), str(exc))
        return
    store.set_raffle_message(raffle_id, sent.message_id)
    await pin_raffle_message(context, chat_id, sent.message_id, raffle_id)


async def spawn_daily_recur_raffles(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recreate drawn universal raffles that opted into daily recurrence."""
    store: DirectoryStore = context.application.bot_data["store"]
    for raffle in store.due_daily_recur_templates():
        try:
            template = json.loads(str(raffle["template_json"] or "") or "{}")
        except json.JSONDecodeError:
            store.mark_raffle_recur_spawned(int(raffle["id"]))
            continue
        if not isinstance(template, dict) or not template:
            store.mark_raffle_recur_spawned(int(raffle["id"]))
            continue
        clock = str(template.get("draw_clock") or "").strip()
        try:
            ends_at = beijing_datetime_to_utc_text(clock) if clock else utc_after_minutes_text(24 * 60)
        except ValueError:
            ends_at = utc_after_minutes_text(24 * 60)
        conditions = template.get("conditions") if isinstance(template.get("conditions"), dict) else {}
        stats_mode = str(template.get("stats_start_mode") or "immediate")
        # Fresh Beijing draw-day stats start — never copy prior absolute timestamp.
        stats_start_at = DirectoryStore.day_stats_start_utc(
            ends_at,
            stats_start_mode=stats_mode,
            stats_start_at=str(_raffle_field(raffle, "stats_start_at", "") or ""),
            created_at="",  # new raffle; do not clamp to prior created_at
        )
        prize = str(template.get("prize") or raffle["prize"])
        winner_count = int(template.get("winner_count") or raffle["winner_count"] or 1)
        rules = template.get("rules") if isinstance(template.get("rules"), list) else []
        new_id = store.create_raffle(
            int(raffle["chat_id"]), int(raffle["creator_id"]), prize, winner_count, ends_at,
            "universal",
            title=str(template.get("title") or _raffle_field(raffle, "title", "") or ""),
            rules_json=json.dumps(rules, ensure_ascii=False),
            conditions_json=json.dumps(conditions, ensure_ascii=False),
            how_to_join=str(template.get("how_to_join") or _raffle_field(raffle, "how_to_join", "") or ""),
            join_keyword=str(conditions.get("keyword") or _raffle_field(raffle, "join_keyword", "") or ""),
            channel_ref=str(conditions.get("channel") or _raffle_field(raffle, "channel_ref", "") or ""),
            min_messages=int(conditions.get("messages") or _raffle_field(raffle, "min_messages", 0) or 0),
            min_boosts=int(conditions.get("boosts") or _raffle_field(raffle, "min_boosts", 0) or 0),
            recur_daily=1,
            stats_start_mode=stats_mode,
            stats_start_at=stats_start_at,
            template_json=str(raffle["template_json"] or ""),
            min_participants=int(
                template.get("min_participants")
                or raffle_min_participants(raffle) or 0
            ),
        )
        store.mark_raffle_recur_spawned(int(raffle["id"]))
        store.audit("scheduler", "raffle.recur", str(raffle["id"]), f"new={new_id}; ends={ends_at}")
        await announce_new_raffle(context, new_id)


async def draw_due_raffles(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for raffle in store.due_raffles():
        await draw_raffle(context, int(raffle["id"]))
    await spawn_daily_recur_raffles(context)


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    store.heartbeat("bot", f"outbox_pending={len(store.pending_outbox(limit=100))}")


async def schedule_incoming_cleanup(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    bot = context.bot
    preserve_id = context.user_data.pop("preserve_incoming_message_id", None)
    if (
        message and chat and chat.type == ChatType.PRIVATE
        and isinstance(bot, AutoDeleteBot) and message.message_id != preserve_id
    ):
        bot.schedule_delete(message.chat_id, message.message_id)


def tron_history_volume_limit(days: int) -> tuple[int, int]:
    if days == 7:
        return 7, 2_000
    if days == 30:
        return 30, 5_000
    return (90 if days == 90 else 365), 10_000


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_permission(context, update.effective_user.id if update.effective_user else None, "stats"):
        await update.effective_message.reply_text("没有后台统计权限。")
        return
    data = context.application.bot_data["store"].dashboard_stats()
    await update.effective_message.reply_text("\n".join(f"{key}: {value}" for key, value in data.items()))


async def run_tron_history_query(
    application: Application, store: DirectoryStore,
    chat_id: int, message_id: int, user_id: int,
    address: str, asset: str, direction: str, days: int, page: int,
    task_id: str,
) -> None:
    probe_semaphore = application.bot_data.setdefault(
        "tron_history_probe_semaphore", asyncio.Semaphore(6)
    )
    deep_semaphore = application.bot_data.setdefault(
        "tron_history_deep_semaphore", asyncio.Semaphore(1)
    )
    probe_acquired = False
    deep_acquired = False
    large_query = False
    last_progress_update = 0.0
    provider_name = "TronGrid"

    async def edit_progress(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                disable_web_page_preview=True,
            )
        except TelegramError:
            pass

    async def progress(info: dict[str, object]) -> None:
        nonlocal probe_acquired, deep_acquired, large_query
        nonlocal last_progress_update, provider_name
        provider_name = str(info.get("provider") or provider_name)
        if info.get("switching"):
            await edit_progress(
                "🔄 当前接口暂时不可用，正在自动切换备用接口…\n\n"
                f"币种：{asset}\n"
                f"时间：{'全部' if not days else f'近{days}天'}\n"
                f"地址：<code>{html.escape(address)}</code>"
            )
            return
        if not info.get("large"):
            return
        if not large_query:
            large_query = True
            if probe_acquired:
                probe_semaphore.release()
                probe_acquired = False
            await edit_progress(
                "⏳ <b>该地址交易量较大，已转入后台查询</b>\n\n"
                f"查询范围：{'全部' if not days else f'近{days}天'}\n"
                f"已读取：{int(info.get('scanned_records') or 0):,} 笔\n"
                f"当前接口：{html.escape(provider_name)}\n\n"
                "查询期间可以继续使用机器人，完成后会自动显示结果。",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "取消查询", callback_data=f"tronhistorycancel:{task_id}"
                    )
                ]]),
            )
            await deep_semaphore.acquire()
            deep_acquired = True
        now = asyncio.get_running_loop().time()
        if now - last_progress_update < 10:
            return
        last_progress_update = now
        oldest_timestamp = int(info.get("oldest_timestamp") or 0)
        scanned_days = 0
        if oldest_timestamp:
            scanned_days = max(0, int(
                (datetime.now(timezone.utc).timestamp() * 1000 - oldest_timestamp)
                / 86_400_000
            ))
            if days:
                scanned_days = min(days, scanned_days)
        await edit_progress(
            f"⏳ <b>正在后台查询{'近' + str(days) + '天' if days else '全部'}交易</b>\n\n"
            f"已读取：{int(info.get('scanned_records') or 0):,} 笔\n"
            f"有效记录：{int(info.get('kept_records') or 0):,} 笔\n"
            + (f"当前进度：约 {scanned_days} 天\n" if scanned_days else "")
            + f"当前接口：{html.escape(provider_name)}\n"
            f"任务编号：{task_id}\n\n"
            "查询期间可以继续使用机器人。",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "取消查询", callback_data=f"tronhistorycancel:{task_id}"
                )
            ]]),
        )

    callback_data = f"tronrecords:{asset}:{direction}:{days}:{page}:{address}"
    try:
        await probe_semaphore.acquire()
        probe_acquired = True
        cache_key = (address, asset, days)
        cache = application.bot_data.setdefault("tron_history_cache", {})
        cached = cache.get(cache_key)
        truncated_cache = application.bot_data.setdefault(
            "tron_history_truncated", {}
        )
        now_timestamp = datetime.now(timezone.utc).timestamp()
        if cached and now_timestamp - float(cached[0]) < 600:
            transactions = cached[1]
            truncated = bool(truncated_cache.get(cache_key, False))
        else:
            async with asyncio.timeout(180):
                chain = application.bot_data["chain"]
                _, record_limit = tron_history_volume_limit(days)
                transactions = await chain.tron_transaction_history(
                    address, asset, max_records=record_limit + 1,
                    days=days, progress=progress,
                )
                truncated = len(transactions) > record_limit
                if truncated:
                    transactions = transactions[:record_limit]
            cache[cache_key] = (now_timestamp, transactions)
            truncated_cache[cache_key] = truncated
        _, record_limit = tron_history_volume_limit(days)
        limit_notice = (
            f"该地址疑似为交易所或热钱包，近{days}天交易较多，"
            f"仅显示已读取的前{record_limit:,}条。"
            if truncated and days else
            f"该地址疑似为交易所或热钱包，交易较多，"
            f"仅显示已读取的前{record_limit:,}条。"
            if truncated else ""
        )
        store.add_chain_query(
            user_id, address, f"history_{asset.casefold()}",
            f"records={len(transactions)} days={days}",
        )
        text, keyboard = tron_records_view(
            address, transactions, asset, direction, days, page,
            application.bot_data.get("kkpay_emoji_ids"),
            application.bot_data.get("tron_direction_emoji_ids"),
            store.chain_query_count(),
            str(application.bot_data.get("bot_username") or ""),
            limit_notice,
        )
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError:
            fallback_text, fallback_keyboard = tron_records_view(
                address, transactions, asset, direction, days, page,
                query_count=store.chain_query_count(),
                bot_username=str(application.bot_data.get("bot_username") or ""),
                limit_notice=limit_notice,
            )
            await application.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=fallback_text,
                parse_mode=ParseMode.HTML, reply_markup=fallback_keyboard,
                disable_web_page_preview=True,
            )
    except asyncio.CancelledError:
        await edit_progress(
            "查询已取消。",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("重新查询", callback_data=callback_data),
                InlineKeyboardButton("返回地址查询", callback_data=f"tronback:{address}"),
            ]]),
        )
    except (TimeoutError, ValueError, ChainQueryError) as exc:
        store.add_chain_query(
            user_id, address, f"history_{asset.casefold()}", str(exc), False
        )
        reason = "后台查询超过3分钟，已自动停止" if isinstance(exc, TimeoutError) else str(exc)
        await edit_progress(
            f"交易记录读取失败：{html.escape(reason)}\n\n"
            f"累计查询 {store.chain_query_count():,} 次",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("重新查询", callback_data=callback_data),
                InlineKeyboardButton("返回地址查询", callback_data=f"tronback:{address}"),
            ]]),
        )
    finally:
        if probe_acquired:
            probe_semaphore.release()
        if deep_acquired:
            deep_semaphore.release()
        application.bot_data.setdefault("tron_history_tasks", {}).pop(task_id, None)


async def commit_settings_draft(update, context, draft):
    mode, answers = draft.mode, list(draft.answers)
    if callback_group_id(context, update.effective_chat) != draft.group_id:
        raise ValueError("所选群组已改变，请重新打开设置")
    if draft.action_data:
        context.user_data.pop("menu_mode", None)
        if answers[0] != "确认":
            await update.callback_query.edit_message_text("已取消，未修改设置。")
            return
        class ActionQuery:
            data = draft.action_data

            def __getattr__(self, name):
                return getattr(update.callback_query, name)

            async def answer(self, *args, **kwargs):
                if args or kwargs.get("text"):
                    await context.bot.send_message(draft.chat_id, args[0] if args else kwargs["text"])

        class ActionUpdate:
            callback_query = ActionQuery()

            def __getattr__(self, name):
                return getattr(update, name)

        await dispatch_callback(ActionUpdate(), context)
        record_selected_bot_usage(update, context.application.bot_data["store"])
        return
    if mode == "price_alert":
        await commit_price_alert(update, context, draft)
        return
    permission = menu_mode_group_permission(mode)
    if mode.startswith("groupperm_"):
        if not has_super_admin_access(context, update.effective_user.id):
            raise ValueError("当前账号没有分配群管理员权限的权限")
    elif permission and mode != "points_drawcost":
        if not draft.group_id or not await is_chat_admin(context, draft.group_id, update.effective_user.id, permission):
            raise ValueError("当前账号已没有这个功能的管理权限")
    if mode == "group_poll_question":
        if not 1 <= len(answers[0]) <= 300:
            raise ValueError("投票问题需要1-300个字符")
        parse_group_poll_options(answers[1])
        context.user_data["group_poll_question"] = answers[0]
        mode, answers = "group_poll_options", [answers[1]]
    elif mode == "raffle_active_start":
        if answers[0] == "0":
            start_at = datetime.now(timezone.utc)
        else:
            start_at = datetime.strptime(beijing_datetime_to_utc_text(answers[0] + " 00:00", allow_past=True), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if not now - timedelta(days=31) <= start_at <= now:
                raise ValueError("发言起始日期只能选择近31天")
        context.user_data["raffle_active_start_at"] = start_at.strftime("%Y-%m-%d %H:%M:%S")
        mode, answers = "raffle_active_details", answers[1:]
        if answers[0] == "排名":
            del answers[3]
    elif mode == "raffle_tiers":
        answers = [answers[0], *[line.strip() for line in answers[1].splitlines() if line.strip()]]
    elif mode == "raffle_pro":
        # Keep answers as a list for build_raffle_extras_from_pro via setting_answers.
        pass
    elif mode == "admin_manage_permissions":
        aliases = {"管理处罚": "moderation", "群组管理": "group_manage", "双向客服": "support", "统计": "stats"}
        answers[1] = ",".join(aliases.get(item.strip(), item.strip()) for item in answers[1].replace("，", ",").replace("、", ",").split(","))
    if mode.startswith("admin_manage_"):
        answers[0] = answers[0].lstrip("#")
    if mode in {"quickpost_button", "quickpost_buttonedit"}:
        label_index = 0 if mode == "quickpost_button" else 1
        answers[label_index], custom = button_content(draft.messages[label_index])
        context.user_data["quickpost_button_emoji_id"] = custom
    context.user_data["menu_mode"] = mode
    message = settings_wizard.MessageInput(draft.messages[-1], " | ".join(answers), answers)
    submitted = settings_wizard.UpdateInput(update, message)
    if permission or mode.startswith("groupperm_"):
        await commit_group_menu_input(submitted, context)
    else:
        await process_menu_input(submitted, context, message.text)
    if not context.user_data.get("menu_mode"):
        record_selected_bot_usage(update, context.application.bot_data["store"])


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if str(getattr(update.callback_query, "data", "") or "").startswith("stk:"):
        if await guard(update, context):
            await sticker_clone.handle_callback(update, context)
        return
    if await settings_wizard.callback(update, context, commit_settings_draft):
        return
    query = update.callback_query
    if await reject_foreign_panel_click(query, context, query.data or ""):
        return
    previous_mode = context.user_data.get("menu_mode")
    if context.user_data.pop("settings_draft", None):
        context.user_data.pop("menu_mode", None)
        context.user_data.pop("edit_raffle_id", None)
        context.user_data.pop("wizard_prefills", None)
        context.user_data.pop("wizard_action_title", None)
        previous_mode = None
    data = str(query.data or "")
    if query.message and (callback_is_group_mutation(data) or data in {"moderation:on", "moderation:off"} or data.startswith(("groupperm:toggle:", "tronmonitor:off:"))):
        group_id = callback_group_id(context, update.effective_chat)
        permission = callback_group_permission(data)
        if (permission and (not group_id or not has_group_permission(context, group_id, update.effective_user.id, permission))
                or (data.startswith(("moderation:", "groupperm:")) and not has_super_admin_access(context, update.effective_user.id))):
            await query.answer("你没有这个功能的设置权限。", show_alert=True)
            return
        label = group_mutation_label(data)
        if query.message.reply_markup:
            for row in query.message.reply_markup.inline_keyboard:
                for button in row:
                    if button.callback_data == data:
                        label += "：" + button.text
        await settings_wizard.begin_action(update, context, group_id, label)
        return
    await dispatch_callback(update, context)
    mode = context.user_data.get("menu_mode")
    if mode in settings_wizard.FLOWS and (mode != previous_mode or ":set:" in str(update.callback_query.data)):
        await settings_wizard.begin(update, context, callback_group_id(context, update.effective_chat))


async def dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if await reject_foreign_panel_click(query, context, data):
        return
    if (
        query.message
        and not data.startswith("raffle:join:")
        and not data.startswith("verify:")
    ):
        schedule_setting_cleanup(context, query.message)
    user_id = query.from_user.id
    is_admin_user = has_admin_access(context, user_id)
    is_super_user = has_super_admin_access(context, user_id)
    chat = query.message.chat if query.message else None
    store: DirectoryStore = context.application.bot_data["store"]
    if data.startswith(("rate:", "tronrecords:", "tronrecent:")):
        record_selected_bot_usage(update, store)
    target_group_id = callback_group_id(context, chat)
    required_group_permission = callback_group_permission(data)
    if required_group_permission:
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not has_group_permission(
            context, target_group_id, user_id, required_group_permission
        ):
            await query.answer("你没有这个功能的管理权限。", show_alert=True)
            return
        schedule_setting_cleanup(context, query.message)
        if callback_is_group_mutation(data):
            store.record_group_operation(
                target_group_id, "setting", user_id, query.from_user.username or "",
                query.from_user.full_name, group_mutation_label(data),
                user_id, query.from_user.username or "", query.from_user.full_name,
            )
            if query.message:
                schedule_setting_cleanup(context, query.message)
    if await feature_callback(update, context, data):
        return
    if data == "nav:main":
        context.user_data.pop("menu_mode", None)
        context.user_data.pop("support_mode", None)
        await query.answer()
        settings = store.get_settings()
        await query.edit_message_text(
            settings.get("welcome_text", "欢迎使用。"),
            reply_markup=main_keyboard(
                is_admin_user, is_super_user, clone_available(context),
                has_developer_access(context, user_id),
            ),
        )
        return
    if data == "nav:search":
        context.user_data.pop("menu_mode", None)
        await query.answer()
        await query.edit_message_text(
            "🔎 搜索服务\n\n请选择要使用的功能。",
            reply_markup=search_menu_keyboard(
                has_developer_access(context, user_id)
            ),
        )
        return
    if data.startswith("rate:"):
        parts = data.split(":")
        if (
            len(parts) != 3 or parts[1] not in {"buy", "sell"}
            or parts[2] not in {"bank", "wechat", "alipay"}
        ):
            await query.answer("汇率筛选参数无效。", show_alert=True)
            return
        if store.get_settings().get("rate_enabled") != "1":
            await query.answer("汇率查询目前已关闭。", show_alert=True)
            return
        await query.answer()
        try:
            text, keyboard = await rate_result_view(
                context, parts[1], parts[2]
            )
        except ChainQueryError as exc:
            await query.edit_message_text(
                f"汇率查询失败：{html.escape(str(exc))}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "重试", callback_data=f"rate:{parts[1]}:{parts[2]}"
                    )
                ]]),
            )
            return
        store.add_chain_query(
            user_id, "", "rate", f"{parts[1]}:{parts[2]}"
        )
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
        return
    if data.startswith("tronrecent:"):
        address = data.partition(":")[2]
        await query.answer("正在读取最近交易…")
        await query.edit_message_text(
            "⏳ 正在读取最近10条 TRX / USDT 交易…\n\n"
            f"地址：<code>{html.escape(address)}</code>",
            parse_mode=ParseMode.HTML,
        )
        try:
            chain: ChainService = context.application.bot_data["chain"]
            result, usdt_rows, trx_rows = await asyncio.gather(
                chain.tron_balance(address),
                chain.tron_transaction_history(address, "USDT", max_records=10),
                chain.tron_transaction_history(address, "TRX", max_records=10),
                return_exceptions=True,
            )
        except (ValueError, ChainQueryError) as exc:
            store.add_chain_query(user_id, address, "recent10", str(exc), False)
            await query.edit_message_text(
                f"最近交易读取失败：{html.escape(str(exc))}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("重试", callback_data=f"tronrecent:{address}")
                ]]),
            )
            return
        if isinstance(result, Exception):
            store.add_chain_query(user_id, address, "recent10", str(result), False)
            await query.edit_message_text(
                "波场账户接口暂时不可用，请稍后重试。",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("重试", callback_data=f"tronrecent:{address}")
                ]]),
            )
            return
        history_errors = [
            str(rows) for rows in (usdt_rows, trx_rows) if isinstance(rows, Exception)
        ]
        merged = {
            item.tx_id: item
            for rows in (usdt_rows, trx_rows)
            if not isinstance(rows, Exception)
            for item in rows
        }
        transactions = tuple(sorted(
            merged.values(), key=lambda item: item.timestamp_ms, reverse=True
        )[:10])
        result = replace(
            result, transactions=transactions,
            transactions_error="；".join(history_errors)[:500],
        )
        store.add_chain_query(
            user_id, address, "recent10", f"transactions={len(result.transactions[:10])}"
        )
        text, keyboard = tron_recent_view(
            result,
            context.application.bot_data.get("kkpay_emoji_ids"),
            context.application.bot_data.get("tron_direction_emoji_ids"),
            store.chain_query_count(),
            str(context.application.bot_data.get("bot_username") or ""),
        )
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError:
            fallback_text, fallback_keyboard = tron_recent_view(
                result, query_count=store.chain_query_count(),
                bot_username=str(context.application.bot_data.get("bot_username") or ""),
            )
            await query.edit_message_text(
                fallback_text, parse_mode=ParseMode.HTML,
                reply_markup=fallback_keyboard, disable_web_page_preview=True,
            )
        return
    if data.startswith("tronmonitor:address:"):
        address = data.partition("tronmonitor:address:")[2]
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人监控该地址。", show_alert=True)
            return
        try:
            context.user_data["tron_monitor_address"] = validate_tron_address(address)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer("正在读取地址统计…")
        prompt = await tron_monitor_asset_prompt(
            context, context.user_data["tron_monitor_address"]
        )
        await query.edit_message_text(
            prompt,
            parse_mode=ParseMode.HTML,
            reply_markup=tron_monitor_prompt_keyboard(context),
        )
        return
    if data.startswith("tronhistorycancel:"):
        task_id = data.rsplit(":", 1)[-1]
        task = context.application.bot_data.setdefault(
            "tron_history_tasks", {}
        ).get(task_id)
        if not task or task.done():
            await query.answer("查询任务已经结束。", show_alert=True)
            return
        task.cancel()
        await query.answer("正在取消查询…")
        return
    if data.startswith("tronrecords:"):
        parts = data.split(":", 5)
        if (
            len(parts) != 6 or parts[1] not in {"USDT", "TRX"}
            or parts[2] not in {"all", "in", "out"}
            or not parts[3].isdigit() or int(parts[3]) not in {0, 7, 30, 90}
            or not parts[4].isdigit()
        ):
            await query.answer("交易记录参数无效。", show_alert=True)
            return
        await query.answer("正在读取交易记录…")
        await query.edit_message_text(
            "⏳ 正在快速检查交易量…\n\n"
            f"币种：{parts[1]}\n"
            f"时间：{'全部' if int(parts[3]) == 0 else '近' + parts[3] + '天'}\n"
            f"地址：<code>{html.escape(parts[5])}</code>\n\n"
            "少量记录会直接显示；交易量较大时自动转入后台查询。",
            parse_mode=ParseMode.HTML,
        )
        if not query.message:
            return
        task_id = secrets.token_hex(4)
        task = context.application.create_task(run_tron_history_query(
            context.application, store,
            query.message.chat_id, query.message.message_id, user_id,
            parts[5], parts[1], parts[2], int(parts[3]), int(parts[4]), task_id,
        ))
        context.application.bot_data.setdefault(
            "tron_history_tasks", {}
        )[task_id] = task
        return
    if data.startswith("tronback:"):
        address = data.partition(":")[2]
        try:
            result = await context.application.bot_data["chain"].tron_balance(address)
        except (ValueError, ChainQueryError) as exc:
            store.add_chain_query(user_id, address, "balance_back", str(exc), False)
            await query.answer(str(exc), show_alert=True)
            return
        store.add_chain_query(
            user_id, address, "balance_back",
            f"TRX={result.trx:f}, USDT={result.usdt:f}",
        )
        text, keyboard = tron_result_view(
            result,
            emoji_ids=context.application.bot_data.get("kkpay_emoji_ids"),
            direction_emoji_ids=context.application.bot_data.get("tron_direction_emoji_ids"),
            status_emoji_ids=context.application.bot_data.get("tron_status_emoji_ids"),
            query_count=store.chain_query_count(),
            bot_username=str(context.application.bot_data.get("bot_username") or ""),
        )
        await query.answer()
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError:
            fallback_text, fallback_keyboard = tron_result_view(
                result, query_count=store.chain_query_count(),
                bot_username=str(context.application.bot_data.get("bot_username") or ""),
            )
            await query.edit_message_text(
                fallback_text, parse_mode=ParseMode.HTML,
                reply_markup=fallback_keyboard, disable_web_page_preview=True,
            )
        return
    if data == "clone:start":
        if not clone_available(context):
            await query.answer("当前机器人暂不支持克隆。", show_alert=True)
            return
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人操作。", show_alert=True)
            return
        context.user_data["menu_mode"] = "clone_token"
        await query.answer()
        await query.edit_message_text(
            "🤖 克隆机器人\n\n请发送从 @BotFather 获得的 Bot Token。\n"
            "Token 验证后会立即删除这条消息并提交审核，通过后自动启动。\n"
            "你将成为新机器人的超级管理员，新机器人拥有独立的搜索收录数据。\n\n"
            "发送 /cancel 取消。"
        )
        return
    if data.startswith("clonetree:") or data.startswith("clonedel:"):
        await handle_clone_tree_callback(update, context, data)
        return
    if data == "admin:clones":
        if not has_developer_access(context, user_id):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        text, keyboard = clone_records_view(store)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("clone:approve:") or data.startswith("clone:reject:"):
        if not has_developer_access(context, user_id):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        raw_id = data.rsplit(":", 1)[-1]
        if not raw_id.isdigit():
            await query.answer("申请编号无效。", show_alert=True)
            return
        clone_id = int(raw_id)
        manager: CloneManager | None = context.application.bot_data.get("clone_manager")
        if not manager:
            await query.answer("当前实例不能审核克隆。", show_alert=True)
            return
        try:
            record = store.bot_clone(clone_id)
            from_child = bool(record and int(record["parent_clone_id"] or 0))
            if data.startswith("clone:approve:"):
                owner_id, username = await manager.approve(clone_id, user_id)
                result = f"克隆申请 #{clone_id} 已通过，@{username} 已启动。"
                owner_notice = clone_result_notice(clone_id, username, True)
                action = "clone.approve"
            else:
                owner_id = manager.reject(clone_id, user_id)
                result = f"克隆申请 #{clone_id} 已拒绝。"
                owner_notice = clone_result_notice(clone_id, "", False)
                action = "clone.reject"
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        store.audit(f"tg:{user_id}", action, str(clone_id))
        if not from_child:
            # 子机器人上提交的申请由提交所在的机器人通知申请人
            try:
                with persistent_message():
                    await context.bot.send_message(owner_id, owner_notice)
            except TelegramError:
                logging.exception("Failed to send clone review result")
        await query.answer(result)
        await query.edit_message_text(result)
        return
    if data == "nav:group":
        context.user_data.pop("menu_mode", None)
        context.user_data.pop("selected_group_id", None)
        if chat and chat.type == ChatType.PRIVATE:
            text, keyboard = await private_group_selector(context, user_id)
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        bot_username = str(context.application.bot_data.get("bot_username") or "")
        await query.answer()
        await query.edit_message_text(
            "👥 群组管理已移到机器人私聊中，避免群成员点击管理按钮。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "打开私聊设置",
                    url=f"https://t.me/{bot_username}?start=groups",
                )
            ]]) if bot_username else None,
        )
        return
    if data.startswith("groupselect:"):
        raw_chat_id = data.rsplit(":", 1)[-1]
        if not raw_chat_id.lstrip("-").isdigit() or int(raw_chat_id) >= 0:
            await query.answer("群组编号无效。", show_alert=True)
            return
        target_chat_id = int(raw_chat_id)
        if not await is_chat_admin(context, target_chat_id, user_id):
            await query.answer("你没有这个群组的管理权限。", show_alert=True)
            return
        context.user_data["selected_group_id"] = target_chat_id
        schedule_setting_cleanup(context, query.message)
        group = store.group_stats(target_chat_id)
        title = str(group["title"] if group else target_chat_id)
        permissions = store.group_admin_permissions(target_chat_id, user_id)
        await query.answer()
        await query.edit_message_text(
            f"⌛ 正在设置 {title}\n\nID: {target_chat_id}\n请选择要更改的项目。",
            reply_markup=group_menu_keyboard(True, is_super_user, permissions),
        )
        return
    if data == "nav:groupmenu":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("请重新选择群组。", show_alert=True)
            return
        group = store.group_stats(target_group_id)
        title = str(group["title"] if group else target_group_id)
        permissions = store.group_admin_permissions(target_group_id, user_id)
        await query.answer()
        await query.edit_message_text(
            f"⌛ 正在设置 {title}\n\nID: {target_group_id}\n请选择要更改的项目。",
            reply_markup=group_menu_keyboard(True, is_super_user, permissions),
        )
        return
    if data == "group:permissions":
        if target_group_id is None or not is_super_user:
            await query.answer("仅超级管理员可以分配群管理员权限。", show_alert=True)
            return
        text, keyboard = group_permissions_view(store, target_group_id)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data in {"groupperm:set", "groupperm:reset"}:
        if target_group_id is None or not is_super_user:
            await query.answer("仅超级管理员可以操作。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        try:
            text, keyboard = await group_admin_selector_view(
                context, target_group_id, action
            )
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("groupperm:choose:"):
        parts = data.split(":")
        if (
            len(parts) != 4 or parts[2] not in {"set", "reset"}
            or not parts[3].isdigit() or target_group_id is None or not is_super_user
        ):
            await query.answer("权限操作参数无效。", show_alert=True)
            return
        target_id = int(parts[3])
        try:
            member = await context.bot.get_chat_member(target_group_id, target_id)
        except TelegramError:
            await query.answer("无法读取该群管理员。", show_alert=True)
            return
        if str(member.status) not in {
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER,
        }:
            await query.answer("该用户已不是群管理员。", show_alert=True)
            return
        target_name = member.user.full_name or member.user.username or str(target_id)
        if parts[2] == "reset":
            store.reset_group_admin_permissions(target_group_id, target_id)
            store.record_group_operation(
                target_group_id, "permission", target_id,
                member.user.username or "", target_name, "权限已重置",
                user_id, query.from_user.username or "", query.from_user.full_name,
            )
            text, keyboard = group_permissions_view(store, target_group_id)
            await query.answer("权限已重置。")
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        context.user_data[f"groupperm_name:{target_id}"] = target_name
        text, keyboard = group_permission_editor_view(
            store, target_group_id, target_id, target_name
        )
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("groupperm:toggle:"):
        parts = data.split(":")
        if (
            len(parts) != 4 or not parts[2].isdigit()
            or parts[3] not in GROUP_PERMISSIONS
            or target_group_id is None or not is_super_user
        ):
            await query.answer("权限操作参数无效。", show_alert=True)
            return
        target_id, permission = int(parts[2]), parts[3]
        if not await is_telegram_chat_admin(context, target_group_id, target_id):
            await query.answer("该用户已不是群管理员。", show_alert=True)
            return
        current = store.group_admin_permissions(target_group_id, target_id)
        enabled = permission not in current
        if enabled:
            current.add(permission)
        else:
            current.discard(permission)
        store.set_group_admin_permissions(target_group_id, target_id, current, user_id)
        target_name = str(
            context.user_data.get(f"groupperm_name:{target_id}") or target_id
        )
        store.record_group_operation(
            target_group_id, "permission", target_id, "", target_name,
            f"{GROUP_PERMISSION_LABELS[permission]} {'开启' if enabled else '关闭'}",
            user_id, query.from_user.username or "", query.from_user.full_name,
        )
        text, keyboard = group_permission_editor_view(
            store, target_group_id, target_id, target_name
        )
        await query.answer("已开启。" if enabled else "已关闭。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "nav:admin":
        context.user_data.pop("menu_mode", None)
        if not is_admin_user:
            await query.answer("仅管理员可用。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            "🛡 管理员功能\n\n请选择管理项目。",
            reply_markup=admin_menu_keyboard(
                is_super_user, has_developer_access(context, user_id)
            ),
        )
        return
    if data == "channelbroadcast:menu" or data.startswith("channelbroadcast:view:"):
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        section = data.rsplit(":", 1)[-1] if data.startswith("channelbroadcast:view:") else "main"
        selected_id = int(context.user_data.get("channel_message_selected") or 0)
        text, keyboard = channel_broadcast_view(store, section, selected_id)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("channelbroadcast:message:select:"):
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        message_id = int(data.rsplit(":", 1)[-1])
        if not store.channel_message(message_id):
            await query.answer("消息不存在。", show_alert=True)
            return
        context.user_data["channel_message_selected"] = message_id
        text, keyboard = channel_broadcast_view(store, "messages", message_id)
        await query.answer("已选择。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("channelbroadcast:set:"):
        if not is_super_user:
            await query.answer("仅超级管理员可设置。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        modes = {
            "add": "channel_add", "delete": "channel_delete",
            "groupadd": "channel_groupadd", "groupdel": "channel_groupdel",
            "assign": "channel_assign", "messageadd": "channel_messageadd",
            "messageedit": "channel_messageedit", "messagedel": "channel_messagedel",
            "schedule": "channel_schedule",
        }
        if action not in modes:
            await query.answer("未知设置。", show_alert=True)
            return
        context.user_data["menu_mode"] = modes[action]
        await query.answer()
        return
    if data.startswith("channelbroadcast:send:"):
        if not is_super_user:
            await query.answer("仅超级管理员可发送。", show_alert=True)
            return
        parts = data.split(":")
        selected_id = int(context.user_data.get("channel_message_selected") or 0)
        message_row = store.channel_message(selected_id) if selected_id else None
        if not message_row:
            await query.answer("请先在消息管理中选择一条消息。", show_alert=True)
            return
        await query.answer("正在发送…")
        sent, failed, error = await send_channel_broadcast(
            context, message_row, parts[2], int(parts[3])
        )
        store.audit(f"tg:{user_id}", "channel.broadcast", str(selected_id), f"成功{sent} 失败{failed} {error}")
        record_selected_bot_usage(update, store)
        text, keyboard = channel_broadcast_view(store, "send", selected_id)
        await query.edit_message_text(
            f"发送完成：成功 {sent} 个，失败 {failed} 个。\n\n{text}",
            reply_markup=keyboard,
        )
        return
    if data in {"search:prompt", "tron:prompt"}:
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人使用该输入功能。", show_alert=True)
            return
        context.user_data["menu_mode"] = (
            "directory_search" if data == "search:prompt" else "tron_search"
        )
        prompt = (
            "请输入要搜索的收录关键词。"
            if data == "search:prompt"
            else "请输入以 T 开头的波场地址。"
        )
        await query.answer()
        await query.edit_message_text(
            prompt + "\n\n发送 /cancel 可取消。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回搜索服务", callback_data="nav:search")
            ]]),
        )
        return
    if data == "account:prompt":
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人查询账户信息。", show_alert=True)
            return
        context.user_data["menu_mode"] = "account_info"
        await query.answer()
        await query.edit_message_text(
            "👤 查询数字ID\n\n"
            "请发送 @用户名，机器人会返回对应的 Telegram 数字ID。\n\n"
            "发送 /cancel 取消。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回搜索服务", callback_data="nav:search")
            ]]),
        )
        return
    if data == "tronmonitor:menu" or data.startswith("tronmonitor:page:"):
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人设置地址监控。", show_alert=True)
            return
        raw_page = data.rsplit(":", 1)[-1] if data.startswith("tronmonitor:page:") else "0"
        page = int(raw_page) if raw_page.isdigit() else 0
        text, keyboard = tron_monitor_menu(store, user_id, page)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "tronmonitor:add":
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人设置。", show_alert=True)
            return
        context.user_data["menu_mode"] = "tron_monitor_address"
        await query.answer()
        await query.edit_message_text(
            "请输入要监控的波场地址。\n\n发送 /cancel 取消。"
        )
        return
    if data.startswith("tronmonitor:asset:"):
        asset = data.rsplit(":", 1)[-1]
        if asset not in {"trx", "usdt", "both"} or not context.user_data.get("tron_monitor_address"):
            await query.answer("监控设置已过期，请重新添加。", show_alert=True)
            return
        context.user_data["tron_monitor_asset"] = asset
        context.user_data["tron_monitor_low"] = ""
        context.user_data["tron_monitor_high"] = ""
        context.user_data["tron_monitor_notify"] = True
        context.user_data["tron_monitor_minimum"] = "0.1"
        context.user_data["tron_monitor_delete_days"] = 7
        context.user_data.pop("menu_mode", None)
        setup_text, setup_keyboard = tron_monitor_setup_view(context)
        await query.answer()
        await query.edit_message_text(setup_text, reply_markup=setup_keyboard)
        return
    if data.startswith("tronmonitor:set:"):
        setting = data.rsplit(":", 1)[-1]
        modes = {
            "low": ("tron_monitor_low", "请输入小于多少余额时播报；发送“关闭”可停用。"),
            "high": ("tron_monitor_high", "请输入大于多少余额时播报；发送“关闭”可停用。"),
            "transfer": (
                "tron_monitor_transfer",
                "请输入交易播报的最小金额，默认0.1；小于该金额不播报。发送“关闭”可停用交易播报。",
            ),
            "delete": (
                "tron_monitor_delete",
                "请输入提醒消息多少天后撤回（0-30）；0表示不自动撤回。",
            ),
        }
        if setting not in modes or not context.user_data.get("tron_monitor_asset"):
            await query.answer("监控设置已过期，请重新添加。", show_alert=True)
            return
        context.user_data["menu_mode"] = modes[setting][0]
        await query.answer()
        await query.edit_message_text(modes[setting][1] + "\n\n发送 /cancel 取消。")
        return
    if data == "tronmonitor:save":
        address = str(context.user_data.get("tron_monitor_address") or "")
        asset = str(context.user_data.get("tron_monitor_asset") or "")
        if not address or asset not in {"trx", "usdt", "both"}:
            await query.answer("监控设置已过期，请重新添加。", show_alert=True)
            return
        low = "" if asset == "both" else str(context.user_data.get("tron_monitor_low") or "")
        high = "" if asset == "both" else str(context.user_data.get("tron_monitor_high") or "")
        notify = bool(context.user_data.get("tron_monitor_notify", True))
        if not notify and not low and not high:
            await query.answer("至少开启一种播报条件。", show_alert=True)
            return
        await query.answer("正在保存监控…")
        try:
            chain = context.application.bot_data["chain"]
            # 交易次数只用于展示（选择币种时已统计过，不再重复请求，也不拦截）
            cached_count = context.user_data.get("tron_monitor_recent_count")
            recent_count = int(cached_count) if isinstance(cached_count, int) else None
            monitored_assets = ("usdt", "trx") if asset == "both" else (asset,)
            snapshot, latest_rows = await asyncio.gather(
                chain.tron_monitor_balance(address),
                chain.tron_monitor_latest_transactions(address, monitored_assets),
            )
            balance_value = (
                f"USDT={snapshot.usdt};TRX={snapshot.trx}"
                if asset == "both" else
                str(snapshot.trx if asset == "trx" else snapshot.usdt)
            )
            balances = {"usdt": snapshot.usdt, "trx": snapshot.trx}
            low_value = Decimal(low) if low else None
            high_value = Decimal(high) if high else None
            baseline_states = {
                item: (
                    "low" if low_value is not None and balances[item] < low_value else
                    "high" if high_value is not None and balances[item] > high_value else
                    "normal"
                )
                for item in monitored_assets
            }
            alert_state = ",".join(
                f"{item}:{baseline_states[item]}" for item in monitored_assets
            )
            latest = max(
                latest_rows,
                key=lambda item: (item.block_number, item.timestamp_ms, item.tx_id),
                default=None,
            )
            monitor_id = store.upsert_tron_monitor(
                user_id, address, asset, low, high, balance_value,
                [row.tx_id for row in latest_rows if row.tx_id],
                int(context.user_data.get("tron_monitor_delete_days", 7)),
                notify,
                str(context.user_data.get("tron_monitor_minimum") or "0.1"),
                alert_state=alert_state,
                monitor_state="live",
                cursor_tx_id=latest.tx_id if latest else "",
                cursor_block=latest.block_number if latest else 0,
                cursor_timestamp_ms=latest.timestamp_ms if latest else 0,
            )
        except (TimeoutError, ValueError, ChainQueryError) as exc:
            if isinstance(exc, TimeoutError):
                exc = ChainQueryError("近一年交易量校验超时，请稍后重试")
            await query.edit_message_text(f"地址初始化失败：{exc}")
            return
        recent_count_text = (
            "暂时无法统计" if recent_count is None
            else "超过 10,000" if recent_count > 10_000 else f"{recent_count:,}"
        )
        for key in list(context.user_data):
            if key.startswith("tron_monitor_"):
                context.user_data.pop(key, None)
        monitor_text, monitor_keyboard = tron_monitor_menu(store, user_id)
        asset_label = "USDT + TRX" if asset == "both" else asset.upper()
        await query.edit_message_text(
            "📡 <b>监听该地址</b>\n\n"
            f"<code>{html.escape(address)}</code>\n\n"
            "🟢 监听添加成功\n"
            f"监听类型：<b>{asset_label}</b>\n"
            f"近一年交易次数：{recent_count_text}\n\n"
            f"提醒只发送给你。\n\n{monitor_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=monitor_keyboard,
        )
        return
    if data.startswith("tronmonitor:off:"):
        raw_id = data.rsplit(":", 1)[-1]
        if not raw_id.isdigit() or not store.disable_tron_monitor(user_id, int(raw_id)):
            await query.answer("没有找到该监控。", show_alert=True)
            return
        text, keyboard = tron_monitor_menu(store, user_id)
        await query.answer("监控已关闭。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "menu:help":
        await query.answer()
        await query.edit_message_text(
            HELP_TEXT, reply_markup=main_keyboard(
                is_admin_user, is_super_user, clone_available(context),
                has_developer_access(context, user_id),
            )
        )
        return
    if data == "menu:contact":
        await query.answer()
        await query.edit_message_text(
            "👤 联系开发者",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("打开 @xinyuan188", url="https://t.me/xinyuan188")
            ]]),
        )
        return
    if data in {"group:stats", "group:active"}:
        if target_group_id is None:
            await query.answer("请先在私聊中选择群组。", show_alert=True)
            return
        if data == "group:stats" and not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以查看。", show_alert=True)
            return
        await hydrate_group_speaker_names(context, target_group_id, 1, 0)
        if data == "group:active":
            text, keyboard = group_active_page(
                store, target_group_id, 0, 1, has_super_admin_access(context, user_id)
            )
        else:
            text, keyboard = group_stats_page(store, target_group_id, 0, 1, True)
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data == "group:points":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        can_manage = await is_chat_admin(context, target_group_id, user_id)
        can_manage_draw = has_super_admin_access(context, user_id)
        can_manage_dice_odds = has_group_permission(
            context, target_group_id, user_id, "diceodds"
        )
        config = store.points_config(target_group_id)
        await query.answer()
        await query.edit_message_text(
            points_status_text(store, target_group_id, can_manage_draw),
            reply_markup=points_menu_keyboard(
                can_manage, bool(config["is_enabled"]), can_manage_draw,
                bool(config["dice_enabled"]), can_manage_dice_odds,
            ),
        )
        return
    if data == "group:joincfg":
        if (
            target_group_id is None
            or not await is_chat_admin(context, target_group_id, user_id)
        ):
            await query.answer("只有群管理员可以设置欢迎与验证。", show_alert=True)
            return
        config = store.group_join_config(target_group_id)
        await query.answer()
        await query.edit_message_text(
            "👋 进群欢迎与验证\n\n"
            f"欢迎消息：{'开启' if config['welcome_enabled'] else '关闭'}\n"
            f"点击验证：{'开启' if config['verification_enabled'] else '关闭'}\n"
            f"欢迎语：{config['welcome_text']}",
            reply_markup=group_join_keyboard(config),
        )
        return
    if data == "quickpost:menu":
        if (
            target_group_id is None
            or not await is_chat_admin(context, target_group_id, user_id)
        ):
            await query.answer("只有群管理员可以设置快捷发布。", show_alert=True)
            return
        selected_id = int(context.user_data.get(f"quickpost_selected:{target_group_id}") or 0)
        try:
            row = store.quick_post(target_group_id, selected_id or None)
        except ValueError:
            row = store.quick_post(target_group_id)
        context.user_data[f"quickpost_selected:{target_group_id}"] = int(row["id"])
        await query.answer()
        await query.edit_message_text(
            quick_post_menu_text(store, row, str(context.application.bot_data.get("bot_username") or "")),
            reply_markup=quick_post_menu_keyboard(store, row),
        )
        return
    if data.startswith("quickpost:view:"):
        if target_group_id is None or not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以管理快捷发布。", show_alert=True)
            return
        selected_id = int(context.user_data.get(f"quickpost_selected:{target_group_id}") or 0)
        row = store.quick_post(target_group_id, selected_id or None)
        section = data.rsplit(":", 1)[-1]
        await query.answer()
        await query.edit_message_text(
            quick_post_menu_text(store, row, str(context.application.bot_data.get("bot_username") or "")),
            reply_markup=quick_post_menu_keyboard(store, row, section),
        )
        return
    if data.startswith("quickpost:select:"):
        if target_group_id is None or not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以选择消息。", show_alert=True)
            return
        try:
            row = store.quick_post(target_group_id, int(data.rsplit(":", 1)[-1]))
        except (ValueError, TypeError):
            await query.answer("这条快捷发布消息不存在。", show_alert=True)
            return
        context.user_data[f"quickpost_selected:{target_group_id}"] = int(row["id"])
        await query.answer("已选择。")
        await query.edit_message_text(
            quick_post_menu_text(store, row, str(context.application.bot_data.get("bot_username") or "")),
            reply_markup=quick_post_menu_keyboard(store, row, "messages"),
        )
        return
    if data.startswith("quickpost:set:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        prompts = {
            "add": "请设置新快捷发布消息。",
            "text": "请发送帖子文字内容。",
            "media": "请发送图片、视频、动画、音频或文件。",
            "button": "请设置按钮文字、链接和颜色。",
            "buttonedit": "请选择按钮并修改内容、链接、颜色和长短类型。",
            "buttondel": "请发送要删除的按钮编号。",
            "schedule": "请选择消息并设置发布时间。",
            "schedulecancel": "请发送要取消的定时任务编号。",
        }
        if action not in prompts:
            await query.answer("未知设置。", show_alert=True)
            return
        context.user_data["menu_mode"] = f"quickpost_{action}"
        await query.answer()
        await query.edit_message_text(prompts[action] + "\n\n发送 /cancel 取消。")
        return
    if data == "quickpost:publish":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以发布。", show_alert=True)
            return
        try:
            selected_id = int(context.user_data.get(f"quickpost_selected:{target_group_id}") or 0)
            row = store.quick_post(target_group_id, selected_id or None)
            await send_quick_post(
                context.bot, target_group_id, row, store
            )
        except (ValueError, TelegramError) as exc:
            await query.answer(str(exc), show_alert=True)
            return
        store.audit(f"tg:{user_id}", "quickpost.publish", str(target_group_id))
        await query.answer("发布成功。")
        return
    if data == "quickpost:delete":
        selected_id = int(context.user_data.get(f"quickpost_selected:{target_group_id}") or 0)
        row = store.quick_post(target_group_id, selected_id or None)
        store.delete_quick_post(target_group_id, int(row["id"]))
        row = store.quick_post(target_group_id)
        context.user_data[f"quickpost_selected:{target_group_id}"] = int(row["id"])
        await query.answer("当前消息已删除。")
        await query.edit_message_text(
            quick_post_menu_text(store, row, str(context.application.bot_data.get("bot_username") or "")),
            reply_markup=quick_post_menu_keyboard(store, row),
        )
        return
    if data == "quickpost:clear":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以清空。", show_alert=True)
            return
        selected_id = int(context.user_data.get(f"quickpost_selected:{target_group_id}") or 0)
        row = store.quick_post(target_group_id, selected_id or None)
        store.update_quick_post(
            target_group_id, user_id, int(row["id"]), text="", file_id="", file_type="", file_name="",
            entities_json="[]",
            button_text="", button_url="",
        )
        for button in store.quick_post_buttons(int(row["id"])):
            store.delete_quick_post_button(int(row["id"]), int(button["id"]))
        row = store.quick_post(target_group_id, int(row["id"]))
        await query.answer("已清空。")
        await query.edit_message_text(
            quick_post_menu_text(store, row, str(context.application.bot_data.get("bot_username") or "")),
            reply_markup=quick_post_menu_keyboard(store, row),
        )
        return
    if data == "invite:menu" or data.startswith("invite:owners:"):
        if (
            target_group_id is None
            or not await is_chat_admin(context, target_group_id, user_id)
        ):
            await query.answer("只有群管理员可以设置邀请链接。", show_alert=True)
            return
        raw_page = data.rsplit(":", 1)[-1] if data.startswith("invite:owners:") else "0"
        page = int(raw_page) if raw_page.isdigit() else 0
        text, keyboard = invite_menu_view(store, target_group_id, page=page)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data in {"invite:on", "invite:off"}:
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置。", show_alert=True)
            return
        enabled = data == "invite:on"
        if not enabled:
            for row in store.active_invite_links(target_group_id):
                try:
                    await context.bot.revoke_chat_invite_link(target_group_id, str(row["invite_link"]))
                except TelegramError:
                    pass
            store.revoke_invite_links(target_group_id)
        store.update_invite_config(target_group_id, user_id, enabled=enabled)
        text, keyboard = invite_menu_view(store, target_group_id)
        await query.answer("邀请链接功能已开启。" if enabled else "邀请链接功能已关闭。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data in {
        "invite:set:expire", "invite:set:max", "invite:set:points",
        "invite:set:premium", "invite:set:normal",
    }:
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        if action in {"points", "premium", "normal"} and not is_super_user:
            await query.answer("邀请积分仅超级管理员可以设置。", show_alert=True)
            return
        context.user_data["menu_mode"] = {
            "expire": "invite_expirehours",
            "max": "invite_maxmembers",
            "points": "invite_points",
            "premium": "invite_premium",
            "normal": "invite_normal",
        }[action]
        prompt = {
            "expire": "请输入链接有效小时数，0表示无限制。",
            "max": "请输入最大邀请人数，0表示无限制。",
            "points": "请输入每成功邀请1人奖励的积分，0表示不奖励。",
            "premium": "邀请 Telegram 会员：有效发言条数 | 达标奖励 | 每次助推奖励，0 表示关闭。",
            "normal": "邀请普通成员：有效发言条数 | 达标奖励，0 表示关闭。",
        }[action]
        await query.answer()
        await query.edit_message_text(prompt + "\n\n发送 /cancel 取消。")
        return
    if data == "invite:reset":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以操作。", show_alert=True)
            return
        for row in store.active_invite_links(target_group_id):
            try:
                await context.bot.revoke_chat_invite_link(target_group_id, str(row["invite_link"]))
            except TelegramError:
                pass
        store.revoke_invite_links(target_group_id)
        answer = "个人邀请链接已重置，成员下次 /link 会重新生成。统计记录仍保留。"
        text, keyboard = invite_menu_view(store, target_group_id)
        await query.answer(answer)
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "invite:query":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以查询。", show_alert=True)
            return
        context.user_data["menu_mode"] = "invite_query"
        await query.answer()
        await query.edit_message_text(
            "请发送 @用户名 或完整邀请链接。\n\n"
            "用户名可查询他的个人链接；链接可查询进群、退出和仍在人员。\n\n"
            "发送 /cancel 取消。"
        )
        return
    if data.startswith(("invitequery:", "inviteowner:")):
        parts = data.split(":")
        if len(parts) == 3:
            parts = [parts[0], parts[1], "joined", parts[2]]
        if (
            target_group_id is None or len(parts) != 4
            or not parts[1].isdigit()
            or parts[2] not in {
                "joined", "exited", "remaining", "renamed",
                "remaining_unspoken", "remaining_spoken",
            }
            or not parts[3].isdigit()
        ):
            await query.answer("邀请查询已失效。", show_alert=True)
            return
        aggregate_owner = parts[0] == "inviteowner"
        links = (
            store.invite_links_by_owner_id(target_group_id, int(parts[1]))
            if aggregate_owner else []
        )
        link = (
            links[0] if links else
            store.invite_link_by_id(target_group_id, int(parts[1]))
            if not aggregate_owner else None
        )
        if not link:
            await query.answer("邀请链接不存在。", show_alert=True)
            return
        is_owner = int(link["user_id"]) == user_id
        is_admin = is_developer_user(context, user_id)
        if not is_owner and not is_admin:
            is_admin = await is_chat_admin(context, target_group_id, user_id)
        if not is_owner and not is_admin:
            await query.answer("只能查询自己的邀请链接。", show_alert=True)
            return
        text, keyboard = invite_query_view(
            store, target_group_id, links if aggregate_owner else link,
            int(parts[3]), parts[2],
            show_settings_back=is_admin,
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return
    if data == "group:recent" or data.startswith("group:recent:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以查看近期操作。", show_alert=True)
            return
        parts = data.split(":")
        kind = parts[2] if len(parts) >= 3 and parts[2] in {"bot", "group"} else "group"
        raw_page = parts[3] if len(parts) >= 4 else "0"
        page = int(raw_page) if raw_page.isdigit() else 0
        recent_text, recent_keyboard = group_recent_operations_view(
            store, target_group_id, page, kind
        )
        await query.answer()
        await query.edit_message_text(
            recent_text, reply_markup=recent_keyboard,
        )
        return
    if data.startswith("joincfg:"):
        if (
            target_group_id is None
            or not await is_chat_admin(context, target_group_id, user_id)
        ):
            await query.answer("只有群管理员可以设置。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        config = store.group_join_config(target_group_id)
        if action == "text":
            context.user_data["menu_mode"] = "group_join_welcome"
            await query.answer()
            await query.edit_message_text(
                "请发送新的欢迎语。可使用 {name} 和 {group} 占位符。\n\n发送 /cancel 取消。"
            )
            return
        if action == "welcome":
            store.set_group_join_config(
                target_group_id, user_id, welcome_enabled=not bool(config["welcome_enabled"])
            )
        elif action == "verify":
            store.set_group_join_config(
                target_group_id, user_id,
                verification_enabled=not bool(config["verification_enabled"]),
            )
        else:
            await query.answer("未知设置。", show_alert=True)
            return
        config = store.group_join_config(target_group_id)
        await query.answer("设置已更新。")
        await query.edit_message_text(
            "👋 进群欢迎与验证\n\n"
            f"欢迎消息：{'开启' if config['welcome_enabled'] else '关闭'}\n"
            f"点击验证：{'开启' if config['verification_enabled'] else '关闭'}\n"
            f"欢迎语：{config['welcome_text']}",
            reply_markup=group_join_keyboard(config),
        )
        return
    if data.startswith("verify:"):
        raw_member_id = data.rsplit(":", 1)[-1]
        if not raw_member_id.isdigit() or not chat:
            await query.answer("验证信息无效。", show_alert=True)
            return
        member_id = int(raw_member_id)
        if user_id != member_id and not await is_group_admin(
            update, context, user_id, "points"
        ):
            await query.answer("只能由新成员本人完成验证。", show_alert=True)
            return
        try:
            await context.bot.restrict_chat_member(
                chat.id, member_id, ChatPermissions.all_permissions()
            )
        except TelegramError as exc:
            await query.answer(f"解除限制失败：{exc}", show_alert=True)
            return
        await query.answer("验证成功。")
        await query.edit_message_text("✅ 验证成功，已恢复发言权限。")
        return
    if data == "points:draw":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        reset_point_draw_spend(context, target_group_id)
        text, keyboard = point_draw_view(
            store, target_group_id,
            has_group_permission(context, target_group_id, user_id, "points"),
            None,
        )
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "points:drawcost":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        context.user_data["menu_mode"] = "points_drawcost"
        await query.answer()
        await query.edit_message_text(
            "请输入本次抽奖要消耗的积分。\n"
            "消耗越高，按礼品积分自动换算的中奖率越高。\n\n"
            "发送 /cancel 取消。"
        )
        return
    if data.startswith("points:drawgift:"):
        raw_gift_id = data.rsplit(":", 1)[-1]
        if raw_gift_id.isdigit():
            await point_draw_gift(
                update, context, int(raw_gift_id), target_group_id
            )
        return
    if data in {"points:checkin", "points:balance", "points:gifts"}:
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        await query.answer()
        if data == "points:checkin":
            try:
                points, balance_value, streak, today_number = store.checkin_points(
                    target_group_id, user_id, query.from_user.username or "",
                    query.from_user.full_name or "群成员",
                )
                with persistent_message():
                    await query.message.reply_text(
                        f"📅 今日第 {today_number} 个签到\n"
                        f"签到成功 +{format_points(points)} 积分\n⭐ 当前积分：{format_points(balance_value)}"
                    )
            except ValueError as exc:
                await query.message.reply_text(str(exc))
        elif data == "points:balance":
            await query.message.reply_text(point_balance_text(store, target_group_id, user_id))
        else:
            await query.message.reply_text(point_gifts_text(store, target_group_id))
        return
    if data.startswith("points:rank:") or data == "points:rank":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        raw_page = data.rsplit(":", 1)[-1] if data.count(":") == 2 else "0"
        page = int(raw_page) if raw_page.isdigit() else 0
        text, keyboard = point_ranking_page(store, target_group_id, page)
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if any(data.startswith(f"points:{kind}:") for kind in ("ledger", "wins", "redeems", "games")):
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        _, kind, raw_page = data.split(":", 2)
        page = int(raw_page) if raw_page.isdigit() else 0
        if kind == "games":
            text, keyboard = point_game_records_page(
                store, target_group_id, user_id, page=page
            )
        else:
            text, keyboard = point_records_page(store, target_group_id, user_id, kind, page)
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("points:membergames:"):
        parts = data.split(":")
        if (
            target_group_id is None or len(parts) != 4
            or not re.fullmatch(r"-?\d+", parts[2]) or not parts[3].isdigit()
            or not has_group_permission(context, target_group_id, user_id, "points")
        ):
            await query.answer("游戏记录查询已失效或你没有权限。", show_alert=True)
            return
        text, keyboard = point_game_records_page(
            store, target_group_id, int(parts[2]), "",
            int(parts[3]), admin_view=True,
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("pointmemberledger:"):
        parts = data.split(":")
        if (
            target_group_id is None or len(parts) != 3
            or not re.fullmatch(r"-?\d+", parts[1]) or not parts[2].isdigit()
            or not has_group_permission(context, target_group_id, user_id, "points")
        ):
            await query.answer("账单查询已失效或你没有权限。", show_alert=True)
            return
        text, keyboard = point_member_ledger_view(
            store, target_group_id, int(parts[1]), "", int(parts[2])
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data in {"points:enable", "points:disable"}:
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置积分。", show_alert=True)
            return
        enabled = data == "points:enable"
        store.set_points_enabled(target_group_id, enabled, user_id)
        store.audit(f"tg:{user_id}", "points.enable" if enabled else "points.disable", str(target_group_id))
        await query.answer("积分功能已开启。" if enabled else "积分功能已关闭。")
        config = store.points_config(target_group_id)
        await query.edit_message_text(
            points_status_text(store, target_group_id, has_super_admin_access(context, user_id)),
            reply_markup=points_menu_keyboard(
                True, enabled, has_super_admin_access(context, user_id),
                bool(config["dice_enabled"]),
                has_group_permission(context, target_group_id, user_id, "diceodds"),
            ),
        )
        return
    if data in {"groupdice:on", "groupdice:off"}:
        # Voice/text toggle buttons always bind to the callback message's chat.
        group_id = (
            int(chat.id)
            if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
            else target_group_id
        )
        if group_id is None or not can_toggle_group_dice(context, group_id, user_id):
            await query.answer("你没有开启/关闭骰子的权限。", show_alert=True)
            return
        enabled = data == "groupdice:on"
        store.set_dice_enabled(group_id, enabled, user_id)
        store.audit(
            f"tg:{user_id}",
            "points.diceon" if enabled else "points.diceoff",
            str(group_id),
            "groupdice",
        )
        await query.answer("骰子游戏已开启。" if enabled else "骰子游戏已关闭。")
        await query.edit_message_text(
            "骰子游戏已开启。" if enabled else "骰子游戏已关闭。"
        )
        return
    if data == "points:dice:menu":
        if target_group_id is None or not has_group_permission(
            context, target_group_id, user_id, "diceodds"
        ):
            await query.answer("你没有骰子设置权限。", show_alert=True)
            return
        text, keyboard = dice_settings_view(store.points_config(target_group_id))
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data in {"points:diceon", "points:diceoff"}:
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置骰子。", show_alert=True)
            return
        enabled = data == "points:diceon"
        store.set_dice_enabled(target_group_id, enabled, user_id)
        store.audit(f"tg:{user_id}", "points.diceon" if enabled else "points.diceoff", str(target_group_id))
        await query.answer("骰子游戏已开启。" if enabled else "骰子游戏已关闭。")
        config = store.points_config(target_group_id)
        text, keyboard = dice_settings_view(config)
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "points:activityoff":
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置积分。", show_alert=True)
            return
        store.set_activity_points_enabled(target_group_id, False, user_id)
        await query.answer("每日活跃积分已关闭。")
        config = store.points_config(target_group_id)
        await query.edit_message_text(
            points_status_text(store, target_group_id, has_super_admin_access(context, user_id)),
            reply_markup=points_menu_keyboard(
                True, bool(config["is_enabled"]),
                has_super_admin_access(context, user_id),
                bool(config["dice_enabled"]),
                has_group_permission(context, target_group_id, user_id, "diceodds"),
            ),
        )
        return
    if data.startswith("points:set:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置积分。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        if action in {"drawconfig", "drawtoggle", "drawmincost", "drawrate", "drawmsgmin"} and not has_group_permission(
            context, target_group_id, user_id, "points"
        ):
            await query.answer("你没有积分抽奖设置权限。", show_alert=True)
            return
        if action in {
            "diceodds", "dicemin", "dicemax", "diceschedule", "dicemsgmin", "dicemsgfree",
        } and not has_group_permission(
            context, target_group_id, user_id, "diceodds"
        ):
            await query.answer("你没有骰子设置权限。", show_alert=True)
            return
        prompts = {
            "checkin": "请发送：签到最小积分 | 签到最大积分 | 连续3天额外积分\n例如：5 | 10 | 3",
            "activity": "请发送：消息目标最小 | 消息目标最大 | 奖励最小 | 奖励最大\n例如：10 | 30 | 2 | 8\n消息目标按有效发言计：1 分钟内最多算 2 条，少于 3 个字不算。",
            "giftadd": "请发送：所需积分 | 礼品名称 | 库存\n库存填 -1 表示不限量，例如：100 | 会员奖励 | 10",
            "giftdel": "请发送要删除的礼品编号，例如：#1。",
            "tieradd": "请发送：今日有效发言条数 | 奖励积分\n例如：50 | 20（同条数再次设置即修改）",
            "tierdel": "请发送要删除的阶梯编号，例如：#1；发送“全部”清空。",
            "redeemmsgmin": "请设置当日有效发言满多少条才能兑换积分礼品，0 表示不限。\n有效发言：1 分钟内最多算 2 条，少于 3 个字不算。",
            "adjust": "请发送：@用户名或数字ID | 增减数量 | 原因\n例如：@alice | +1.5 | 活动奖励",
            "clear": "请发送 @用户名或数字ID；发送 all 清零本群所有成员积分。",
            "memberledger": "请发送要查询账单的 @用户名或数字ID。",
            "membergames": "请发送 @用户名或数字ID（也可回复对方消息）",
            "drawmincost": "请发送每次抽奖最低消耗积分，范围0.01-1000000（最多两位小数）。",
            "drawmsgmin": "请设置当日有效发言满多少条才能参与积分抽奖，0 表示不限。\n有效发言：1 分钟内最多算 2 条，少于 3 个字不算。",
            "drawrate": "请发送中奖概率倍率，范围0-5。\n0表示不会中奖，1表示自动换算倍率。",
            "diceodds": "请发送骰子赔率（1.7-2.0，也可写 1700-2000；例如 1.95，押1000中奖反1950）",
            "dicemin": "请设置每次玩骰子的最低积分。",
            "dicemax": "请设置骰子单注最高积分，0 表示不限。",
            "dicemsgmin": "请设置当日有效发言满多少条才能玩骰子，0 表示不限。\n有效发言：1 分钟内最多算 2 条，少于 3 个字不算。",
            "dicemsgfree": "请设置当日有效发言满多少条可不受骰子定时限制，0 表示关闭。\n有效发言：1 分钟内最多算 2 条，少于 3 个字不算。",
            "diceschedule": "请设置每日定时开关和开放时段。",
        }
        if action == "activitymenu":
            text, keyboard = activity_settings_view(store, target_group_id)
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if action == "drawconfig":
            text, keyboard = point_draw_settings_view(store, target_group_id)
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if action == "drawtoggle":
            config = store.points_config(target_group_id)
            store.set_point_draw_config(
                target_group_id, not bool(config["draw_enabled"]),
                normalize_points(config["draw_cost"]),
                float(config["draw_rate_multiplier"]),
                user_id,
            )
            text, keyboard = point_draw_settings_view(store, target_group_id)
            await query.answer("积分抽奖已开启。" if not config["draw_enabled"] else "积分抽奖已关闭。")
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if action not in prompts:
            await query.answer("未知积分设置。", show_alert=True)
            return
        context.user_data["menu_mode"] = f"points_{action}"
        await query.answer()
        await query.edit_message_text(prompts[action] + "\n\n发送 /cancel 取消。")
        return
    if data == "group:polls":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            "🗳 群投票\n\n"
            "发起后会以 Telegram 原生投票发到本群。删除投票会撤回群里的那条消息。",
            reply_markup=group_poll_menu_keyboard(),
        )
        return
    if data == "grouppoll:create":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        context.user_data["menu_mode"] = "group_poll_question"
        context.user_data.pop("group_poll_question", None)
        await query.answer()
        await query.edit_message_text(
            "请发送投票问题（1-300 个字符）。\n\n发送 /cancel 取消。"
        )
        return
    if data.startswith("grouppoll:delete:"):
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and parts[2] == "menu" and parts[3].isdigit():
            text, keyboard = group_poll_delete_view(
                store, target_group_id, int(parts[3])
            )
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if (
            len(parts) != 5 or parts[2] != "item"
            or not parts[3].isdigit() or not parts[4].isdigit()
        ):
            await query.answer("删除参数无效。", show_alert=True)
            return
        poll_id = int(parts[3])
        row = store.delete_group_poll(target_group_id, poll_id)
        if not row:
            await query.answer("当前群没有这个投票。", show_alert=True)
            return
        try:
            await context.bot.delete_message(target_group_id, int(row["message_id"]))
        except TelegramError:
            pass
        store.audit(f"tg:{user_id}", "group_poll.delete", str(poll_id))
        text, keyboard = group_poll_delete_view(
            store, target_group_id, int(parts[4])
        )
        await query.answer(f"群投票 #{poll_id} 已删除。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data == "group:raffles":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            "🎁 选择抽奖类型\n\n"
            "🔥 通用抽奖：群员点击按钮参与\n"
            "Ⓜ️ 积分抽奖：消耗积分抽取积分礼品\n"
            "🥰 群活跃抽奖：按发言排行或达到发言次数后随机抽取\n\n"
            "邀请人数、指定群报道和娱乐抽奖需要额外群权限，暂不在本群直接创建。",
            reply_markup=raffle_type_keyboard(
                raffle_count_visible(store, target_group_id)
            ),
        )
        return
    if data.startswith("raffle:count:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以设置。", show_alert=True)
            return
        show_count = data.rsplit(":", 1)[-1] == "on"
        store.set_setting(
            f"group_raffle_show_count:{target_group_id}",
            "1" if show_count else "0",
        )
        await query.answer("参与人数已显示。" if show_count else "参与人数已隐藏。")
        await query.edit_message_reply_markup(
            reply_markup=raffle_type_keyboard(show_count)
        )
        return
    if data == "group:lottery":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            lottery_subscription_text(store, target_group_id),
            reply_markup=lottery_subscription_keyboard(store, target_group_id),
        )
        return
    if data.startswith("lotterysub:"):
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以修改。", show_alert=True)
            return
        selectors = {str(row["selector"]) for row in store.lottery_subscriptions(target_group_id)}
        enabled_codes: set[str] = set()
        for selector in selectors:
            enabled_codes.update(lottery_codes_for_selector(selector))
        if data == "lotterysub:all":
            store.remove_lottery_subscription(target_group_id)
            if not enabled_codes:
                store.add_lottery_subscription(target_group_id, "all", user_id)
        else:
            game_code = data.rsplit(":", 1)[-1]
            if game_code not in LOTTERY_GAMES:
                await query.answer("未知彩种。", show_alert=True)
                return
            target_enabled = game_code not in enabled_codes
            store.remove_lottery_subscription(target_group_id)
            new_codes = set(enabled_codes)
            if target_enabled:
                new_codes.add(game_code)
            else:
                new_codes.discard(game_code)
            for code in LOTTERY_GAMES:
                if code in new_codes:
                    store.add_lottery_subscription(target_group_id, code, user_id)
        store.audit(f"tg:{user_id}", "lottery.subscription_toggle", str(target_group_id), data)
        await query.answer("开奖订阅已更新。")
        await query.edit_message_text(
            lottery_subscription_text(store, target_group_id),
            reply_markup=lottery_subscription_keyboard(store, target_group_id),
        )
        return
    if data == "group:ads" or data == "ad:status":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以设置广告。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            group_ad_status_text(store, target_group_id), reply_markup=group_ad_keyboard()
        )
        return
    if data.startswith("ad:set:"):
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以设置广告。", show_alert=True)
            return
        position = data.rsplit(":", 1)[-1]
        context.user_data["menu_mode"] = f"group_ad_{position}"
        prompt = (
            "请发送：间隔分钟 | 广告文字。也可发送图片/视频/文件，并把同样格式写在说明中。"
            if position == "interval"
            else "请发送广告文字。文字会直接合并到机器人同一条消息中。"
        )
        await query.answer()
        await query.edit_message_text(prompt + "\n\n发送 /cancel 取消。")
        return
    if data.startswith("ad:disable:"):
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以设置广告。", show_alert=True)
            return
        position = data.rsplit(":", 1)[-1]
        if position not in {"all", "interval", "prefix", "suffix"}:
            await query.answer("未知广告位置。", show_alert=True)
            return
        disabled = store.disable_group_ad(
            target_group_id, None if position == "all" else position
        )
        await query.answer(f"已关闭 {disabled} 项广告。")
        await query.edit_message_text(
            group_ad_status_text(store, target_group_id), reply_markup=group_ad_keyboard()
        )
        return
    if data.startswith("raffletype:"):
        if target_group_id is None:
            await query.answer("请先在私聊中选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以创建抽奖。", show_alert=True)
            return
        raffle_type = data.rsplit(":", 1)[-1]
        if raffle_type == "universal":
            await query.answer()
            await query.edit_message_text(
                "🔥 创建通用抽奖\n\n请选择开奖方式。",
                reply_markup=raffle_plan_keyboard(),
            )
            return
        if raffle_type == "points":
            reset_point_draw_spend(context, target_group_id)
            text, keyboard = point_draw_view(
                store, target_group_id,
                has_group_permission(context, target_group_id, user_id, "points"),
                None,
            )
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if raffle_type == "active":
            context.user_data["menu_mode"] = "raffle_active_start"
            await query.answer()
            await query.edit_message_text(
                "🎁 创建群活跃抽奖（/cancel 返回首页）\n\n"
                "❓ 发言次数从什么时间开始统计？最大限制近一个月。\n"
                "如果从抽奖发布后开始统计，请输入：0\n"
                "指定日期格式：年-月-日\n"
                "例如：2026-08-28"
            )
            return
        await query.answer("未知抽奖类型。", show_alert=True)
        return
    if data.startswith("raffleedit:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以修改抽奖。", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "menu" and parts[2].isdigit():
            text, keyboard = raffle_edit_view(
                store, target_group_id, int(parts[2])
            )
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if (
            len(parts) != 4 or parts[1] != "item"
            or not parts[2].isdigit() or not parts[3].isdigit()
        ):
            await query.answer("修改参数无效。", show_alert=True)
            return
        raffle_id = int(parts[2])
        raffle = store.get_raffle(raffle_id)
        if not raffle or int(raffle["chat_id"]) != target_group_id:
            await query.answer("当前群没有这个抽奖。", show_alert=True)
            return
        if str(raffle["status"] or "") != "active":
            await query.answer("只能修改进行中的抽奖。", show_alert=True)
            return
        if str(raffle["raffle_type"] or "") != "universal":
            await query.answer("目前仅支持修改通用/样板抽奖。", show_alert=True)
            return
        prefills = raffle_pro_answers_from_row(raffle)
        context.user_data["menu_mode"] = "raffle_pro"
        context.user_data["edit_raffle_id"] = raffle_id
        context.user_data["wizard_prefills"] = prefills
        context.user_data["wizard_action_title"] = f"修改抽奖 #{raffle_id}"
        await query.answer()
        return
    if data.startswith("raffledelete:"):
        if target_group_id is None or not await is_chat_admin(
            context, target_group_id, user_id
        ):
            await query.answer("只有群管理员可以删除抽奖。", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "menu" and parts[2].isdigit():
            text, keyboard = raffle_delete_view(
                store, target_group_id, int(parts[2])
            )
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if (
            len(parts) != 4 or parts[1] != "item"
            or not parts[2].isdigit() or not parts[3].isdigit()
        ):
            await query.answer("删除参数无效。", show_alert=True)
            return
        raffle_id = int(parts[2])
        raffle = store.get_raffle(raffle_id)
        if not raffle or int(raffle["chat_id"]) != target_group_id:
            await query.answer("当前群没有这个抽奖。", show_alert=True)
            return
        if not store.delete_raffle(target_group_id, raffle_id):
            await query.answer("抽奖已不存在。", show_alert=True)
            return
        if raffle["message_id"]:
            try:
                await context.bot.delete_message(
                    target_group_id, int(raffle["message_id"])
                )
            except TelegramError:
                pass
        store.audit(f"tg:{user_id}", "raffle.delete", str(raffle_id))
        text, keyboard = raffle_delete_view(
            store, target_group_id, int(parts[3])
        )
        await query.answer(f"抽奖 #{raffle_id} 已删除。")
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("raffleplan:"):
        if target_group_id is None:
            await query.answer("请先在私聊中选择群组。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, user_id):
            await query.answer("只有群管理员可以创建抽奖。", show_alert=True)
            return
        plan = data.rsplit(":", 1)[-1]
        if plan == "list":
            rows = store.list_raffles(target_group_id, limit=10)
            text = "📋 最近抽奖\n\n" + ("\n".join(
                f"#{row['id']} · {row['prize']} · {row['status']} · {row['entries']}人"
                for row in rows
            ) if rows else "当前群还没有抽奖记录。")
            await query.answer()
            await query.edit_message_text(text, reply_markup=raffle_plan_keyboard())
            return
        if plan == "delete":
            context.user_data["menu_mode"] = "raffle_delete"
            await query.answer()
            await query.edit_message_text(
                "请发送要删除的群抽奖编号，例如：#1\n\n发送 /cancel 取消。"
            )
            return
        if plan == "pro":
            context.user_data.pop("edit_raffle_id", None)
            context.user_data.pop("wizard_action_title", None)
            # 最少参与人数默认 0（不限制），可直接「保留并下一步」
            context.user_data["wizard_prefills"] = [None] * 8 + ["0"]
            context.user_data["menu_mode"] = "raffle_pro"
            await query.answer()
            return
        context.user_data["menu_mode"] = f"raffle_{plan}"
        prompts = {
            "minutes": "请发送：分钟 | 中奖人数 | 奖品",
            "at": "请发送：开奖时间 | 中奖人数 | 奖品\n例如：2026-08-27 21:30 | 3 | 188RMB",
            "tiers": "请发送：分钟 | 多档奖品\n例如：60 | 1*188RMB | 2*88RMB",
            "quick": "请发送：中奖人数 | 奖品。系统将在10分钟后开奖。",
        }
        if plan not in prompts:
            await query.answer("未知抽奖方案。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(prompts[plan] + "\n\n发送 /cancel 取消。")
        return
    if data == "group:renamehist":
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        if not has_group_permission(context, target_group_id, user_id, "renamehist"):
            await query.answer("你没有改名记录权限。", show_alert=True)
            return
        context.user_data["menu_mode"] = "renamehist_query"
        context.user_data["selected_group_id"] = target_group_id
        await query.answer()
        await query.edit_message_text(
            "📝 改名记录查询\n\n请发送 @用户名或数字ID（也可回复对方消息）。\n\n发送 /cancel 取消。"
        )
        return
    if data in {"group:moderation", "admin:badwords"}:
        allowed = (
            target_group_id is not None
            and has_group_permission(context, target_group_id, user_id, "moderation")
            if data == "group:moderation"
            else has_permission(context, user_id, "moderation")
        )
        if not allowed:
            await query.answer("仅管理员可用。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            moderation_summary(store),
            reply_markup=moderation_menu_keyboard(
                is_super_user, "nav:group" if data == "group:moderation" else "nav:admin"
            ),
        )
        return
    if data in {"badword:add", "badword:delete"}:
        if not has_permission(context, user_id, "moderation"):
            await query.answer("仅管理员可用。", show_alert=True)
            return
        if not chat or chat.type != ChatType.PRIVATE:
            await query.answer("请私聊机器人维护违规关键词。", show_alert=True)
            return
        context.user_data["menu_mode"] = (
            "badword_add" if data == "badword:add" else "badword_delete"
        )
        await query.answer()
        await query.edit_message_text(
            "请发送要添加的违规关键词。" if data == "badword:add"
            else "请发送要删除的违规关键词。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回违规管理", callback_data="admin:badwords")
            ]]),
        )
        return
    if data in {"moderation:on", "moderation:off"}:
        if not is_super_user:
            await query.answer("只有超级管理员可以开关处罚。", show_alert=True)
            return
        enabled = data == "moderation:on"
        store.set_setting("group_moderation_enabled", "1" if enabled else "0")
        store.audit(f"tg:{user_id}", "moderation.enable" if enabled else "moderation.disable")
        await query.answer("违规处罚已开启。" if enabled else "违规处罚已关闭。")
        await query.edit_message_text(
            moderation_summary(store),
            reply_markup=moderation_menu_keyboard(True),
        )
        return
    if data == "admin:pending":
        if not has_review_access(context, user_id):
            await query.answer("仅超级管理员可审核收录。", show_alert=True)
            return
        await query.answer()
        await send_entries(
            query.message, store.list_entries(status="pending", limit=10),
            "没有待审核提交。", True,
        )
        return
    if data == "admin:buttons":
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        text, keyboard = custom_buttons_admin_view(store)
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("admin:button:"):
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        prompts = {
            "addsupport": (
                "custom_button_support",
                "发送：按钮文字 | 联系姓名 | @用户名或数字ID\n"
                "例如：双向联系九爷 | jiuye | @jiuye",
            ),
            "addmenu": (
                "custom_button_menu",
                "发送：按钮文字 | 回复内容或https://链接",
            ),
            "delete": ("custom_button_delete", "发送要删除的按钮编号。"),
        }
        if action not in prompts:
            await query.answer("操作无效。", show_alert=True)
            return
        context.user_data["menu_mode"], prompt = prompts[action]
        await query.answer()
        await query.edit_message_text(prompt + "\n\n发送 /cancel 取消。")
        return
    if data.startswith("admin:manage:"):
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        prompts = {
            "add": ("admin_manage_add", "发送要添加的管理员 Telegram 数字ID。"),
            "delete": ("admin_manage_delete", "发送要删除的管理员 Telegram 数字ID。"),
            "permissions": (
                "admin_manage_permissions",
                "发送：管理员数字ID | 权限1,权限2\n\n"
                "可选：moderation, group_manage, support, stats",
            ),
            "reset": ("admin_manage_reset", "发送要重置权限的管理员 Telegram 数字ID。"),
        }
        if action not in prompts:
            await query.answer("操作无效。", show_alert=True)
            return
        context.user_data["menu_mode"], prompt = prompts[action]
        await query.answer()
        await query.edit_message_text(prompt + "\n\n发送 /cancel 取消。")
        return
    if data == "admin:admins":
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        rows = store.list_bot_admins(
            viewer_id=user_id,
            include_all=is_developer_user(context, user_id),
        )
        lines = ["👥 机器人管理员", ""]
        role_labels = {
            "developer": "开发者", "super": "超级管理员", "admin": "管理员",
        }
        lines.extend(
            f"{row['user_id']} · "
            f"{role_labels.get(str(row['role']), str(row['role']))}"
            + (f" @{row['username']}" if row['username'] else "")
            + (f" · 权限 {row['permissions']}" if row['permissions'] else "")
            for row in rows
        )
        await query.answer()
        await query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ 添加管理员", callback_data="admin:manage:add"),
                    InlineKeyboardButton("➖ 删除管理员", callback_data="admin:manage:delete"),
                ],
                [
                    InlineKeyboardButton("🔐 分配权限", callback_data="admin:manage:permissions"),
                    InlineKeyboardButton("♻️ 重置权限", callback_data="admin:manage:reset"),
                ],
                [InlineKeyboardButton("⬅️ 返回管理员", callback_data="nav:admin")],
            ])
        )
        return
    if data.startswith("privnote:"):
        if not has_developer_access(context, user_id):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            await query.answer("页码无效。", show_alert=True)
            return
        page = int(parts[1])
        keyword = str(context.user_data.get("privnote_keyword") or "").strip()
        if not keyword:
            await query.answer("请重新发送关键词查询。", show_alert=True)
            return
        await query.answer()
        view = private_notes_page_view(store, keyword, page)
        if view is None:
            await query.edit_message_text(f"没有找到私密笔记：{keyword}")
            return
        text, markup, media_items, page, _rows, _total = view
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        if media_items and query.message:
            await send_private_note_media(context, query.message.chat_id, media_items)
        return
    if data == "admin:notes":
        if not has_developer_access(context, user_id):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        context.user_data["menu_mode"] = "private_note_query"
        await query.answer()
        await query.edit_message_text(
            "🗒 私人笔记\n\n录入：1 关键词 内容（保存9个月）\n"
            "永久：2 关键词 内容；或发 2 关键词 后转发多条消息，再发「保存」\n"
            "查询：直接发送关键词（一次显示 10 条，可翻页）\n"
            "每次显示最近5条，最多保留99条，保存9个月。\n"
            "查询消息和结果连续3分钟没有点击按钮后撤回。",
            reply_markup=admin_menu_keyboard(True, True),
        )
        return
    if data.startswith("admin:usage:"):
        raw_page = data.rsplit(":", 1)[-1]
        if not raw_page.isdigit():
            await query.answer("页码无效。", show_alert=True)
            return
        if (
            not has_developer_access(context, user_id)
            or not chat or chat.type != ChatType.PRIVATE
        ):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        text, keyboard = bot_usage_page(store, int(raw_page))
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("admin:tronmonitors:"):
        if (
            not has_developer_access(context, user_id)
            or not chat or chat.type != ChatType.PRIVATE
        ):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        if action == "query":
            context.user_data["menu_mode"] = "tron_monitor_user_query"
            await query.answer()
            await query.edit_message_text(
                "🔎 查询使用人员监控地址\n\n"
                "请发送 @用户名或 Telegram 数字ID。\n"
                "系统以数字ID为准；没有用户名时直接使用数字ID。\n\n"
                "发送 /cancel 取消。"
            )
            return
        if not action.isdigit():
            await query.answer("页码无效。", show_alert=True)
            return
        text, keyboard = tron_monitor_stats_view(store, int(action))
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data == "admin:stats":
        if not is_super_user:
            await query.answer("仅超级管理员可用。", show_alert=True)
            return
        stats_data = store.dashboard_stats()
        await query.answer()
        await query.edit_message_text(
            "📊 后台统计\n\n" + "\n".join(
                f"{key}: {value}" for key, value in stats_data.items()
            ) + "\n\n" + storage_usage_text(context),
            reply_markup=admin_menu_keyboard(
                True, has_developer_access(context, user_id)
            ),
        )
        return
    if data.startswith("searchstats:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"keywords", "events"} or not parts[2].isdigit():
            await query.answer("无效页码。", show_alert=True)
            return
        chat = query.message.chat if query.message else None
        if (
            not has_developer_access(context, query.from_user.id)
            or not chat
            or chat.type != ChatType.PRIVATE
        ):
            await query.answer(developer_only_text(context), show_alert=True)
            return
        text, keyboard = search_stats_page(
            context.application.bot_data["store"], parts[1], int(parts[2])
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("groupstats:"):
        parts = data.split(":")
        if len(parts) == 3:
            _, raw_period, raw_page = parts
        elif len(parts) == 2:
            _, raw_page = parts
            raw_period = "1"
        else:
            raw_period = raw_page = ""
        if (
            not raw_page.isdigit()
            or not raw_period.isdigit()
            or int(raw_period) not in {1, 7, 31}
            or target_group_id is None
        ):
            await query.answer("无效页码。", show_alert=True)
            return
        if not await is_chat_admin(context, target_group_id, query.from_user.id):
            await query.answer("只有群管理员可以查看。", show_alert=True)
            return
        show_history = True
        period_days = int(raw_period)
        await hydrate_group_speaker_names(
            context, target_group_id, period_days, int(raw_page)
        )
        text, keyboard = group_stats_page(
            context.application.bot_data["store"], target_group_id, int(raw_page),
            period_days, show_history,
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("groupactive:"):
        parts = data.split(":")
        if (
            len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit()
            or int(parts[1]) not in {1, 7, 31}
        ):
            await query.answer("无效页码。", show_alert=True)
            return
        if target_group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return
        period_days, page = int(parts[1]), int(parts[2])
        is_super = has_super_admin_access(context, query.from_user.id)
        if period_days == 31 and not is_super:
            await query.answer("近31天统计仅超级管理员可用。", show_alert=True)
            return
        await hydrate_group_speaker_names(context, target_group_id, period_days, page)
        text, keyboard = group_active_page(
            store, target_group_id, page, period_days, is_super
        )
        await query.answer()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return
    if data.startswith("lotteryhist:"):
        _, game_code, raw_page = data.split(":", 2)
        if game_code not in LOTTERY_GAMES or not raw_page.isdigit():
            await query.answer("无效页码。", show_alert=True)
            return
        text, keyboard = lottery_history_page(
            context.application.bot_data["store"], game_code, int(raw_page)
        )
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("rafflehistory:"):
        raw_page = data.partition(":")[2]
        if raw_page == "close":
            await query.answer()
            await query.edit_message_text("抽奖历史已关闭。")
            return
        if not raw_page.isdigit() or target_group_id is None:
            await query.answer("抽奖历史页码无效。", show_alert=True)
            return
        text, keyboard = raffle_history_view(store, target_group_id, int(raw_page))
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("raffle:join:"):
        raw_id = data.rsplit(":", 1)[-1]
        if not raw_id.isdigit():
            return
        store: DirectoryStore = context.application.bot_data["store"]
        user = query.from_user
        joined, count = store.join_raffle(
            int(raw_id), user.id, user.username or "", user.full_name or str(user.id)
        )
        if not count:
            await query.answer("抽奖已结束或不存在。", show_alert=True)
            return
        await query.answer("报名成功。" if joined else "你已经报名过了。", show_alert=not joined)
        try:
            raffle = store.get_raffle(int(raw_id))
            show_count = raffle_count_visible(store, int(raffle["chat_id"]))
            await query.edit_message_text(
                raffle_text(raffle, show_count),
                parse_mode=ParseMode.HTML,
                reply_markup=raffle_keyboard(
                    int(raw_id), count, show_count, raffle=raffle,
                ),
            )
            if joined:
                mention = telegram_user_link(
                    user.id, user.full_name or user.username or str(user.id)
                )
                count_line = f"\n👥 当前参与人数：{count} 人" if show_count else ""
                await context.bot.send_message(
                    int(raffle["chat_id"]),
                    f"✅ {mention} 成功参加抽奖 #{raw_id}\n"
                    f"🎁 奖品：\n{raffle_prize_text(str(raffle['prize']), int(raffle['winner_count']))}\n"
                    f"🏆 中奖名额：{raffle['winner_count']} 人{count_line}",
                    parse_mode=ParseMode.HTML,
                )
        except TelegramError:
            pass
        return
    await query.answer()
    if data == "menu:list":
        store: DirectoryStore = context.application.bot_data["store"]
        entries = store.list_entries(limit=3)
        empty_text = store.get_settings().get("not_found_text", "地址没有收录，请联系管理员。")
        await send_entries(query.message, entries, empty_text)
        return
    if data == "menu:my":
        entries = context.application.bot_data["store"].my_entries(query.from_user.id)
        if not entries:
            await query.message.reply_text("你还没有提交记录。")
            return
        await query.message.reply_text(my_entries_text(entries), parse_mode=ParseMode.HTML)
        return
    action, _, raw_id = data.partition(":")
    if action not in {"approve", "reject"} or not raw_id.isdigit():
        return
    if not has_review_access(context, query.from_user.id):
        await query.edit_message_text("仅超级管理员可审核收录。")
        return
    entry_id = int(raw_id)
    status = "approved" if action == "approve" else "rejected"
    ok = await apply_status(context, entry_id, status, "", f"tg:{query.from_user.id}")
    await query.edit_message_text(f"#{entry_id} 已更新为 {status}。" if ok else f"未找到 #{entry_id}。")


async def send_entries(message, entries: list[Entry], empty_text: str, with_keyboard: bool = False) -> None:
    """Public: each entry's original content. Review: original content + info card."""
    if not entries:
        await message.reply_text(empty_text)
        return
    for entry in entries:
        try:
            await deliver_entry(entry, message=message)
        except (TelegramError, ValueError):
            logging.exception("Failed to send entry %s content", entry.id)
        if with_keyboard:
            await message.reply_text(
                entry_text(entry, include_owner=True), parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(entry.id), disable_web_page_preview=True,
            )


def my_entries_text(entries: list[Entry]) -> str:
    labels = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝", "removed": "已下架"}
    lines = ["📋 我的提交", ""]
    for entry in entries:
        lines.append(
            f"#{entry.id} · {html.escape(entry.title)} · {labels.get(entry.status, entry.status)}"
        )
    return "\n".join(lines)


async def process_outbox(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: DirectoryStore = context.application.bot_data["store"]
    for item in store.pending_outbox(limit=15):
        try:
            prefix = "管理员回复：\n\n" if item["kind"] == "support" else ""
            if item["kind"] == "lottery":
                with persistent_message():
                    await context.bot.send_message(int(item["user_id"]), prefix + str(item["body"]))
            else:
                await context.bot.send_message(int(item["user_id"]), prefix + str(item["body"]))
        except Forbidden as exc:
            store.finish_outbox(int(item["id"]), False, str(exc))
        except TelegramError as exc:
            store.finish_outbox(int(item["id"]), False, str(exc))
        else:
            store.finish_outbox(int(item["id"]), True)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "打开分类主菜单"),
        BotCommand("help", "查看使用教程"),
        BotCommand("submit", "提交关键词和地址"),
        BotCommand("search", "搜索已收录地址"),
        BotCommand("my", "查看我的提交"),
        BotCommand("balance", "查询波场地址"),
        BotCommand("rate", "OKX P2P商户报价榜"),
        BotCommand("lottery", "查询最新开奖结果"),
        BotCommand("lotteryhistory", "查询最近100期开奖"),
        BotCommand("groupstats", "查看今日群统计"),
        BotCommand("userinfo", "查看账户信息"),
        BotCommand("link", "生成个人邀请链接"),
        BotCommand("raffle", "群管理员发起抽奖"),
        BotCommand("raffleat", "按时间定时抽奖"),
        BotCommand("jx", "复制贴纸包并改标题"),
        BotCommand("life", "人生指南"),
        BotCommand("price", "查询币价，如 /price btc"),
        BotCommand("pricealert", "币价涨跌监控，如 /pricealert btc 5 2"),
        BotCommand("pricealerts", "查看我的币价涨跌监控"),
    ]
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username or ""
    try:
        emoji_set = await application.bot.get_sticker_set("kkpay")
        if len(emoji_set.stickers) >= 23:
            application.bot_data["kkpay_emoji_ids"] = {
                "USDT": str(emoji_set.stickers[22].custom_emoji_id or ""),
                "TRX": str(emoji_set.stickers[21].custom_emoji_id or ""),
            }
    except TelegramError:
        logging.warning("Could not load kkpay custom emoji set; using standard emoji")
    direction_ids: dict[str, str] = {}
    status_ids: dict[str, str] = {}
    try:
        outgoing_set = await application.bot.get_sticker_set("EmojiStatus")
        outgoing_base = next(
            (
                index for index, sticker in enumerate(outgoing_set.stickers)
                if str(sticker.emoji or "") in {"⛔", "🚫", "➖"}
            ), 0,
        )
        outgoing_index = min(outgoing_base + 7, len(outgoing_set.stickers) - 1)
        outgoing = outgoing_set.stickers[outgoing_index] if outgoing_set.stickers else None
        if outgoing and outgoing.custom_emoji_id:
            direction_ids["out"] = str(outgoing.custom_emoji_id)
            status_ids["negative"] = str(outgoing.custom_emoji_id)
        for key, offset in {
            "positive": -1, "energy": -2,
            "bandwidth": -3, "free_bandwidth": -4,
        }.items():
            index = outgoing_index + offset
            if 0 <= index < len(outgoing_set.stickers):
                emoji_id = outgoing_set.stickers[index].custom_emoji_id
                if emoji_id:
                    status_ids[key] = str(emoji_id)
    except TelegramError:
        logging.warning("Could not load EmojiStatus custom emoji set")
    try:
        incoming_set = await application.bot.get_sticker_set("NewsEmoji")
        incoming = (
            incoming_set.stickers[65]
            if len(incoming_set.stickers) >= 66 else next(
                (
                    sticker for sticker in incoming_set.stickers
                    if str(sticker.emoji or "") == "➕"
                ),
                None,
            )
        )
        if incoming and incoming.custom_emoji_id:
            direction_ids["in"] = str(incoming.custom_emoji_id)
    except TelegramError:
        logging.warning("Could not load NewsEmoji custom emoji set")
    if direction_ids:
        application.bot_data["tron_direction_emoji_ids"] = direction_ids
    if status_ids:
        application.bot_data["tron_status_emoji_ids"] = status_ids
    await application.bot.set_my_commands(commands)
    clone_manager = application.bot_data.get("clone_manager")
    if clone_manager:
        clone_manager.start_all()


async def post_shutdown(application: Application) -> None:
    clone_manager = application.bot_data.get("clone_manager")
    if clone_manager:
        clone_manager.stop_all()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled Telegram update", exc_info=context.error)


def setup_tron_monitoring(application: Application, config: Config, chain: ChainService) -> None:
    """Block scanner in the mother process only; every process (mother and
    clones) shares the matches file and the cross-process rate limiter."""
    role = f"clone-{getattr(config, 'clone_id', 0)}" if config.is_clone else "mother"
    logging.info("TRON network [%s]: %s", role, chain.governor.describe())
    if not chain.governor.keys:
        logging.warning(
            "TRONGRID_API_KEY/TRONGRID_API_KEYS 未配置：TronGrid 无 key 仅约 1 QPS，"
            "将优先使用公共备用节点"
        )
    if not getattr(config, "tron_block_scan", True):
        logging.info("TRON block scanner disabled (TRON_BLOCK_SCAN=0); per-address polling")
        return
    path = shared_scan_db_path(config)
    if path is None:
        return
    try:
        db = ScanDB(path)
    except Exception:
        logging.exception("TRON scan database unavailable: %s", path)
        return
    application.bot_data["tron_scan_db"] = db
    if not config.is_clone:
        application.bot_data["tron_scanner"] = TronBlockScanner(
            chain, db, lag=getattr(config, "tron_scan_lag_blocks", 1),
            max_catchup=getattr(config, "tron_scan_max_catchup_blocks", 1200),
            use_events=bool(chain.governor.keys),
        )
        logging.info(
            "TRON block scanner enabled (lag=%s blocks, max catch-up=%s blocks, "
            "USDT source=%s, db=%s)", getattr(config, "tron_scan_lag_blocks", 1),
            getattr(config, "tron_scan_max_catchup_blocks", 1200),
            "TronGrid events" if chain.governor.keys else "node receipts", path,
        )


def build_application(config: Config) -> Application:
    store = DirectoryStore(config.db_path)
    store.init()
    store.ensure_config_admins(
        config.admin_ids, config.super_admin_ids, config.developer_ids
    )
    bot = AutoDeleteBot(
        token=config.bot_token,
        auto_delete_seconds=config.message_auto_delete_seconds,
    )
    application = (
        ApplicationBuilder().bot(bot).post_init(post_init)
        .post_shutdown(post_shutdown).build()
    )
    chain = ChainService(config)
    application.bot_data.update(
        config=config, store=store, chain=chain, lottery=LotteryService(config)
    )
    setup_tron_monitoring(application, config, chain)
    if not config.is_clone:
        application.bot_data["clone_manager"] = CloneManager(config, store)
    elif config.mother_db_path and Path(config.mother_db_path).exists():
        # 子机器人也能继续克隆：申请写入母机器人的登记表，由母机器人审核和启动
        application.bot_data["clone_manager"] = CloneManager(
            config, DirectoryStore(Path(config.mother_db_path)), manage_processes=False
        )

    async def ad_decorator(bot_instance, chat_id, position):
        return await group_ad_text(store, chat_id, position)

    async def media_ad_sender(bot_instance, chat_id, position):
        # 文字前后广告一律合并进原消息；只有带媒体文件的广告才另发
        if not isinstance(chat_id, int) or chat_id >= 0:
            return
        row = store.group_ad(chat_id, position)
        if row and row["file_id"]:
            await send_stored_media(bot_instance, chat_id, row)

    async def markup_decorator(bot_instance, chat_id, position):
        if not isinstance(chat_id, int) or chat_id >= 0:
            return None
        row = store.group_ad(chat_id, position)
        return buttons_markup(row) if row else None

    object.__setattr__(bot, "message_decorator", ad_decorator)
    object.__setattr__(bot, "media_ad_sender", media_ad_sender)
    object.__setattr__(bot, "markup_decorator", markup_decorator)
    object.__setattr__(bot, "deletion_recorder", store.schedule_message_deletion)

    application.add_handler(
        CallbackQueryHandler(refresh_callback_cleanup), group=-2
    )
    # /jx 贴纸包复制的输入步骤优先处理，避免被当作关键词搜索
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, sticker_clone.handle_input),
        group=-3,
    )

    submission = ConversationHandler(
        entry_points=[
            CommandHandler("submit", submit_start),
            CallbackQueryHandler(submit_start, pattern=r"^submit:start$"),
        ],
        states={
            URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, submit_url),
                MessageHandler(
                    (filters.PHOTO | filters.VIDEO | filters.Document.ALL
                     | filters.ANIMATION | filters.AUDIO)
                    & ~filters.COMMAND,
                    rich_submission_media,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(submission)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("list", list_entries))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("my", my_entries))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("rate", rate))
    application.add_handler(CommandHandler("lottery", lottery_command))
    application.add_handler(CommandHandler("lotteryhistory", lottery_history_command))
    application.add_handler(CommandHandler("lotterysub", lottery_subscribe_command))
    application.add_handler(CommandHandler("lotteryunsub", lottery_unsubscribe_command))
    application.add_handler(CommandHandler("lotterysubs", lottery_subscriptions_command))
    application.add_handler(CommandHandler("contact", contact))
    application.add_handler(CommandHandler("reply", reply_command))
    application.add_handler(CommandHandler("pending", pending))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("edit", edit_entry_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("groupstats", group_stats_command))
    application.add_handler(CommandHandler("userinfo", user_info_command))
    application.add_handler(CommandHandler("link", personal_invite_link))
    application.add_handler(CommandHandler("moderation", moderation_command))
    application.add_handler(CommandHandler("badword", badword_command))
    application.add_handler(CommandHandler("searchstats", search_stats_command))
    application.add_handler(CommandHandler("raffle", raffle_command))
    application.add_handler(CommandHandler("raffleat", raffle_at_command))
    application.add_handler(CommandHandler("raffles", raffles_command))
    application.add_handler(CommandHandler("draw", draw_command))
    application.add_handler(CommandHandler("cancelraffle", cancel_raffle_command))
    application.add_handler(CommandHandler("admins", admins_command))
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("deladmin", delete_admin_command))
    application.add_handler(CommandHandler("setperm", set_permissions_command))
    application.add_handler(CommandHandler("resetperm", reset_permissions_command))
    application.add_handler(CommandHandler("noteadd", note_add_command))
    application.add_handler(CommandHandler("notes", notes_command))
    application.add_handler(CommandHandler("jx", jx_command))
    application.add_handler(CommandHandler(["life", "rensheng"], life_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("pricealert", price_alert_command))
    application.add_handler(CommandHandler("pricealerts", price_alerts_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(InlineQueryHandler(quick_post_inline))
    application.add_handler(ChatMemberHandler(track_personal_invite, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatBoostHandler(track_chat_boost, ChatBoostHandler.CHAT_BOOST))
    application.add_handler(ChatBoostHandler(track_removed_chat_boost, ChatBoostHandler.REMOVED_CHAT_BOOST))
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, group_menu_input),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, group_menu_input),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message)
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.VOICE & ~filters.COMMAND,
            group_voice_dice_toggle,
        )
    )
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_keyword_reply)
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.AUDIO)
            & ~filters.COMMAND,
            rich_submission_media,
        )
    )
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_activity), group=1)
    application.add_handler(MessageHandler(filters.ALL, schedule_incoming_cleanup), group=2)
    application.add_error_handler(error_handler)
    single_job = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}
    application.job_queue.run_repeating(
        process_outbox, interval=3, first=3, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        draw_due_raffles, interval=15, first=5, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        send_due_group_ads, interval=30, first=10, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        send_due_quick_posts, interval=15, first=8, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        send_due_channel_broadcasts, interval=15, first=12, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        poll_tron_monitors, interval=3, first=3, job_kwargs=single_job
    )
    if application.bot_data.get("tron_scan_db") is not None:
        application.job_queue.run_repeating(
            sync_tron_scan, interval=1, first=2, job_kwargs=single_job
        )
    if application.bot_data.get("tron_scanner") is not None:
        application.job_queue.run_repeating(
            scan_tron_blocks, interval=1, first=4, job_kwargs=single_job
        )
    application.job_queue.run_repeating(
        process_scheduled_deletions, interval=60, first=25, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        heartbeat, interval=30, first=1, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        poll_lottery_results, interval=5, first=5, job_kwargs=single_job
    )
    application.job_queue.run_repeating(
        poll_price_alerts, interval=max(10, int(getattr(config, "price_alert_poll_seconds", 30) or 30)),
        first=20, job_kwargs=single_job,
    )
    if "clone_manager" in application.bot_data:
        application.job_queue.run_repeating(
            sync_clone_requests if not config.is_clone else notify_clone_results,
            interval=20, first=15, job_kwargs=single_job,
        )
    return application


# ===================================================================
# 人生指南 / 币价 / 抽奖识别 / 分类菜单
# ===================================================================

PRICE_SERVICE_KEY = "price_service"
RAFFLE_PARSE_KEY = "raffle_parse"
RAFFLE_PARSE_WAIT_KEY = "raffle_parse_wait"
RAFFLE_PARSE_TTL = 1800


def price_service(context: ContextTypes.DEFAULT_TYPE) -> crypto_price.PriceService:
    data = context.application.bot_data
    service = data.get(PRICE_SERVICE_KEY)
    if service is None:
        chain = data.get("chain")
        c2c = None
        if chain is not None and hasattr(chain, "okx_p2p_quotes"):
            async def c2c() -> Decimal:
                quotes = await chain.okx_p2p_quotes("buy")
                return Decimal(str(quotes[0].price))
        service = crypto_price.PriceService(c2c_rate=c2c)
        data[PRICE_SERVICE_KEY] = service
    return service


def price_symbols(store: DirectoryStore) -> set[str]:
    return crypto_price.known_symbols(store.get_settings().get(crypto_price.SETTING_EXTRA, ""))


def price_refresh_markup(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新", callback_data=f"price:q:{symbol}")],
        [InlineKeyboardButton(crypto_alert.BUTTON_TEXT, callback_data=f"palert:set:{symbol}")],
    ])


async def price_reply_text(context: ContextTypes.DEFAULT_TYPE, symbol: str) -> str:
    try:
        quote = await price_service(context).quote(symbol)
    except crypto_price.PriceError as exc:
        return f"币价查询失败：{exc}"
    except Exception:  # noqa: BLE001 - 行情接口异常不影响机器人
        logging.exception("Price query failed for %s", symbol)
        return "币价查询失败：行情服务暂时不可用，请稍后再试。"
    return crypto_price.quote_text(quote)


async def reply_price(message, context: ContextTypes.DEFAULT_TYPE, symbol: str) -> None:
    text = await price_reply_text(context, symbol)
    ok = not text.startswith("币价查询失败")
    await message.reply_text(text, reply_markup=price_refresh_markup(symbol) if ok else None)


def group_price_enabled(store: DirectoryStore, chat_id: int) -> bool:
    return crypto_price.group_enabled(store.get_settings(), chat_id)


def price_menu_view(store: DirectoryStore, can_manage: bool) -> tuple[str, InlineKeyboardMarkup]:
    extra = store.get_settings().get(crypto_price.SETTING_EXTRA, "").split()
    text = crypto_price.HELP_TEXT
    if can_manage:
        text += (
            "\n\n管理员：/price add 代码 添加自定义币种，/price del 代码 删除。\n"
            f"已添加：{'、'.join(extra) if extra else '无'}"
        )
    rows = [[
        InlineKeyboardButton(symbol, callback_data=f"price:q:{symbol}")
        for symbol in ("BTC", "ETH", "SOL", "TRX")
    ], [InlineKeyboardButton(crypto_alert.LIST_BUTTON_TEXT, callback_data="palert:list")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")]]
    return text, InlineKeyboardMarkup(rows)


def group_price_view(store: DirectoryStore, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    enabled = group_price_enabled(store, chat_id)
    text = (
        "💹 币价回复\n\n"
        f"当前状态：{'✅ 已开启' if enabled else '⛔ 已关闭'}\n"
        "开启后，群成员发送 BTC、ETH 等常见币种代码或「币价 代码」时，机器人回复实时价格。\n"
        "波场地址、比特币地址不会被当作币种。"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ 关闭币价回复" if enabled else "✅ 开启币价回复",
                              callback_data="price:group:off" if enabled else "price:group:on")],
        [InlineKeyboardButton("⬅️ 返回群设置", callback_data="nav:groupmenu")],
    ])


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    store: DirectoryStore = context.application.bot_data["store"]
    args = [arg for arg in (context.args or []) if arg.strip()]
    if args and args[0].casefold() in {"add", "del", "添加", "删除"}:
        if not has_admin_access(context, user.id if user else None):
            await message.reply_text("只有机器人管理员可以修改自定义币种。")
            return
        if len(args) < 2 or not crypto_price.normalize_symbol(args[1]):
            await message.reply_text("用法：/price add 代码 或 /price del 代码，例如 /price add NOT")
            return
        symbol = crypto_price.normalize_symbol(args[1])
        current = [s for s in store.get_settings().get(crypto_price.SETTING_EXTRA, "").split() if s]
        if args[0].casefold() in {"add", "添加"}:
            if symbol not in current:
                if len(current) >= crypto_price.MAX_EXTRA_SYMBOLS:
                    await message.reply_text(f"自定义币种最多 {crypto_price.MAX_EXTRA_SYMBOLS} 个。")
                    return
                current.append(symbol)
            note = f"已添加 {symbol}，发送 {symbol} 即可查询价格。"
        else:
            current = [s for s in current if s != symbol]
            note = f"已删除 {symbol}。"
        store.set_setting(crypto_price.SETTING_EXTRA, " ".join(current))
        await message.reply_text(note)
        return
    if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and not group_price_enabled(store, chat.id):
        await message.reply_text("本群已关闭币价查询。")
        return
    if not args:
        text, markup = price_menu_view(store, has_admin_access(context, user.id if user else None))
        await message.reply_text(text, reply_markup=markup)
        return
    symbol = crypto_price.normalize_symbol(args[0])
    if not symbol:
        await message.reply_text("币种代码格式不正确，例如：/price btc")
        return
    await reply_price(message, context, symbol)


async def life_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    chat = update.effective_chat
    await life_guide.send_home(
        update.effective_message, private=bool(chat and chat.type == ChatType.PRIVATE),
    )


def future_draw_clock(value: str) -> str:
    """Keep a draw time that is still in the future; otherwise move it to the
    next occurrence of the same clock time."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    try:
        beijing_datetime_to_utc_text(raw)
        return raw
    except ValueError:
        moved, _ = raffle_parse.parse_draw_time(raw)
        return moved.strftime("%Y-%m-%d %H:%M:%S") if moved else ""


def raffle_template_prefills(raffle) -> list[str]:
    answers = raffle_pro_answers_from_row(raffle)
    answers[2] = future_draw_clock(answers[2])
    return answers


async def raffle_groups_for(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[tuple[int, str]]:
    store: DirectoryStore = context.application.bot_data["store"]
    result = []
    for group in store.list_groups(100):
        chat_id = int(group["chat_id"])
        if has_group_permission(context, chat_id, user_id, "raffles"):
            result.append((chat_id, str(group["title"] or group["username"] or chat_id)))
    return result


def raffle_parse_view(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    parsed: raffle_parse.ParsedRaffle = state["parsed"]
    recur = bool(state.get("recur"))
    groups = state.get("groups") or []
    chat_id = state.get("chat_id")
    label = next((title for cid, title in groups if cid == chat_id), "")
    text = raffle_parse.summary_text(parsed, recur, label)
    rows: list[list[InlineKeyboardButton]] = []
    if chat_id is None:
        text += "\n\n请选择要发布到哪个群："
        rows.extend([[InlineKeyboardButton(title[:40], callback_data=f"rparse:g:{cid}")]]
                    for cid, title in groups[:20])
    else:
        rows.append([
            InlineKeyboardButton("✅ 确认创建", callback_data="rparse:ok"),
            InlineKeyboardButton("✏️ 修改", callback_data="rparse:edit"),
        ])
    rows.append([InlineKeyboardButton(f"🔁 每日重复：{'是' if recur else '否'}", callback_data="rparse:recur")])
    if chat_id is not None and len(groups) > 1:
        rows.append([InlineKeyboardButton("🔄 换一个群", callback_data="rparse:groups")])
    rows.append([InlineKeyboardButton("取消", callback_data="rparse:cancel")])
    return text, InlineKeyboardMarkup(rows)


async def start_raffle_recognition(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, group_chat_id: int | None = None,
) -> None:
    message = update.effective_message
    user = update.effective_user
    parsed = raffle_parse.parse(text)
    if not (parsed.prizes or parsed.draw_at):
        await message.reply_text(
            "没有识别到抽奖信息。请粘贴或转发完整的抽奖公告（需要包含开奖时间和奖品）。"
        )
        return
    if group_chat_id is not None:
        if not has_group_permission(context, group_chat_id, user.id, "raffles"):
            await message.reply_text("你没有本群的抽奖管理权限。")
            return
        store: DirectoryStore = context.application.bot_data["store"]
        group = store.group_stats(group_chat_id)
        groups = [(group_chat_id, str(group["title"] if group else group_chat_id))]
    else:
        groups = await raffle_groups_for(context, user.id)
        preferred = context.user_data.get("selected_group_id")
        if isinstance(preferred, int) and any(cid == preferred for cid, _ in groups):
            groups.sort(key=lambda item: item[0] != preferred)
    if not groups:
        await message.reply_text("你还没有可以创建抽奖的群组：需要该群的抽奖管理权限。")
        return
    wait = context.user_data.pop(RAFFLE_PARSE_WAIT_KEY, None) or {}
    chosen = wait.get("chat_id") if isinstance(wait, dict) else None
    if not any(cid == chosen for cid, _ in groups):
        chosen = groups[0][0] if len(groups) == 1 else None
    state = {
        "parsed": parsed, "recur": parsed.daily_hint, "chat_id": chosen,
        "groups": groups[:30], "expires": time.time() + RAFFLE_PARSE_TTL,
    }
    context.user_data[RAFFLE_PARSE_KEY] = state
    body, markup = raffle_parse_view(state)
    await message.reply_text(body, reply_markup=markup, disable_web_page_preview=True)


def raffle_copy_view(store: DirectoryStore, chat_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 8
    rows = [row for row in store.list_raffles(chat_id, limit=60)
            if str(row["raffle_type"] or "") == "universal"]
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    lines = ["📋 复制为新抽奖", "", "选择一个抽奖，会把它的标题、规则、条件和奖品带入新抽奖，开奖时间自动顺延："]
    buttons = []
    for row in rows[page * page_size:(page + 1) * page_size]:
        title = (str(row["title"] or "").strip() or str(row["prize"] or ""))[:28]
        buttons.append([InlineKeyboardButton(f"#{row['id']} · {title}", callback_data=f"rafflecopy:item:{row['id']}")])
    if not rows:
        lines.append("当前群还没有通用抽奖可以复制。")
    nav = []
    if page:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"rafflecopy:menu:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"rafflecopy:menu:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回抽奖方案", callback_data="raffletype:universal")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def begin_raffle_prefill(context: ContextTypes.DEFAULT_TYPE, answers: list[str], title: str) -> None:
    context.user_data.pop("edit_raffle_id", None)
    context.user_data["wizard_prefills"] = list(answers)
    context.user_data["wizard_action_title"] = title
    context.user_data["menu_mode"] = "raffle_pro"


# ---- 分类菜单 ------------------------------------------------------------

CATEGORY_TITLES = {
    "raffle": "🎁 抽奖", "points": "⭐ 积分", "dice": "🎲 骰子", "ads": "📢 广告",
}


def category_group_rows(category: str, allowed: set[str]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if category == "raffle":
        if "raffles" in allowed:
            rows.append([InlineKeyboardButton("🎁 抽奖方案", callback_data="group:raffles")])
            rows.append([InlineKeyboardButton("📥 识别抽奖", callback_data="rparse:start")])
            rows.append([
                InlineKeyboardButton("🔁 用上次模板", callback_data="raffleplan:last"),
                InlineKeyboardButton("📋 复制为新抽奖", callback_data="rafflecopy:menu:0"),
            ])
        if "lottery" in allowed:
            rows.append([InlineKeyboardButton("🎟 开奖订阅", callback_data="group:lottery")])
        if "polls" in allowed:
            rows.append([InlineKeyboardButton("🗳 群投票", callback_data="group:polls")])
    elif category == "points":
        if "points" in allowed:
            rows.append([InlineKeyboardButton("⭐ 积分·签到·礼品", callback_data="group:points")])
            rows.append([InlineKeyboardButton("🔥 活跃设置", callback_data="points:set:activitymenu")])
        if "invite" in allowed:
            rows.append([InlineKeyboardButton("🔗 邀请链接与奖励", callback_data="invite:menu")])
    elif category == "dice":
        if "points" in allowed:
            rows.append([InlineKeyboardButton("🎲 骰子设置", callback_data="points:dice:menu")])
            rows.append([InlineKeyboardButton("⭐ 积分·签到·礼品", callback_data="group:points")])
    elif category == "ads":
        if "ads" in allowed:
            rows.append([InlineKeyboardButton("⏱ 定时广告·消息前后广告", callback_data="group:ads")])
        if "quickpost" in allowed:
            rows.append([InlineKeyboardButton("✏️ 快捷发布", callback_data="quickpost:menu")])
    return rows


def category_view(
    category: str, group_title: str, allowed: set[str], private: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    rows = category_group_rows(category, allowed)
    title = CATEGORY_TITLES.get(category, "功能")
    text = f"{title} · {group_title}\n\n请选择："
    if not rows:
        text = f"{title} · {group_title}\n\n你在这个群没有此类功能的管理权限，请联系超级管理员分配。"
    if private:
        rows.append([InlineKeyboardButton("🔄 切换群组", callback_data=f"cat:{category}:pick")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="nav:main")])
    return text, InlineKeyboardMarkup(rows)


def tron_category_view() -> tuple[str, InlineKeyboardMarkup]:
    return "💰 波场/地址监控\n\n请选择：", InlineKeyboardMarkup([
        [InlineKeyboardButton("⛓ 波场查询", callback_data="tron:prompt")],
        [InlineKeyboardButton("⏰ 波场地址监控", callback_data="tronmonitor:menu")],
        [InlineKeyboardButton("🏦 OKX 商户报价", callback_data="rate:buy:bank")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="nav:main")],
    ])


def clone_category_view(is_developer: bool, show_clone: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    if show_clone:
        rows.append([InlineKeyboardButton("🤖 克隆机器人", callback_data="clone:start")])
    if is_developer:
        rows.append([
            InlineKeyboardButton("🤖 克隆审核记录", callback_data="admin:clones"),
            InlineKeyboardButton("🌳 子机器人管理", callback_data="clonetree:0"),
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="nav:main")])
    return "🌳 子机器人\n\n请选择：", InlineKeyboardMarkup(rows)


async def category_group_picker(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, category: str,
) -> tuple[str, InlineKeyboardMarkup]:
    store: DirectoryStore = context.application.bot_data["store"]
    buttons = []
    for group in store.list_groups(100):
        chat_id = int(group["chat_id"])
        if await is_chat_admin(context, chat_id, user_id):
            buttons.append([InlineKeyboardButton(
                str(group["title"] or group["username"] or chat_id)[:50],
                callback_data=f"catsel:{category}:{chat_id}",
            )])
    title = CATEGORY_TITLES.get(category, "功能")
    text = f"{title}\n\n请选择要设置的群组。" if buttons else (
        f"{title}\n\n暂未找到你有管理权限的群组。请先把机器人添加到群组，"
        "群管理员还需要超级管理员分配机器人权限。"
    )
    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="nav:main")])
    return text, InlineKeyboardMarkup(buttons[:51])


def group_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> set[str]:
    if has_super_admin_access(context, user_id):
        return set(GROUP_PERMISSIONS)
    store: DirectoryStore = context.application.bot_data["store"]
    return set(store.group_admin_permissions(chat_id, user_id) or set())


async def feature_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    """Callbacks for the newer features. Returns True when handled."""
    query = update.callback_query
    user_id = query.from_user.id
    chat = query.message.chat if query.message else None
    private = bool(chat and chat.type == ChatType.PRIVATE)
    store: DirectoryStore = context.application.bot_data["store"]

    async def show(text: str, markup: InlineKeyboardMarkup | None = None, answer: str | None = None) -> None:
        await query.answer(answer)
        try:
            await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower() and query.message is not None:
                await query.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)

    if data.startswith("life:"):
        await life_guide.handle_callback(update, context)
        return True
    if data.startswith("price:"):
        parts = data.split(":")
        if parts[1] == "menu":
            text, markup = price_menu_view(store, has_admin_access(context, user_id))
            await show(text, markup)
            return True
        if parts[1] == "q" and len(parts) > 2:
            symbol = crypto_price.normalize_symbol(parts[2])
            if chat and chat.type != ChatType.PRIVATE and not group_price_enabled(store, chat.id):
                await query.answer("本群已关闭币价查询。", show_alert=True)
                return True
            text = await price_reply_text(context, symbol)
            await show(text, price_refresh_markup(symbol), "已刷新")
            return True
        if parts[1] == "group":
            group_id = callback_group_id(context, chat)
            if group_id is None:
                await query.answer("请先选择群组。", show_alert=True)
                return True
            if not has_group_permission(context, group_id, user_id, "view"):
                await query.answer("你没有这个群的管理权限。", show_alert=True)
                return True
            if len(parts) > 2 and parts[2] in {"on", "off"}:
                store.set_setting(f"price_enabled:{group_id}", "1" if parts[2] == "on" else "0")
                store.audit(f"tg:{user_id}", "price.group", str(group_id), parts[2])
            text, markup = group_price_view(store, group_id)
            await show(text, markup)
            return True
        await query.answer()
        return True
    if data.startswith("palert:"):
        await price_alert_callback(update, context, data)
        return True
    if data.startswith("cat:") or data.startswith("catsel:"):
        parts = data.split(":")
        category = parts[1] if len(parts) > 1 else ""
        if data.startswith("catsel:"):
            if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
                await query.answer("群组编号无效。", show_alert=True)
                return True
            target = int(parts[2])
            if not await is_chat_admin(context, target, user_id):
                await query.answer("你没有这个群组的管理权限。", show_alert=True)
                return True
            context.user_data["selected_group_id"] = target
            group = store.group_stats(target)
            text, markup = category_view(category, str(group["title"] if group else target),
                                         group_allowed(context, target, user_id), private)
            await show(text, markup)
            return True
        if category == "tron":
            await show(*tron_category_view())
            return True
        if category == "clone":
            await show(*clone_category_view(has_developer_access(context, user_id), clone_available(context)))
            return True
        if category not in CATEGORY_TITLES:
            await query.answer()
            return True
        context.user_data.pop("menu_mode", None)
        group_id = callback_group_id(context, chat)
        force_pick = len(parts) > 2 and parts[2] == "pick"
        if private and (group_id is None or force_pick):
            await show(*await category_group_picker(context, user_id, category))
            return True
        if group_id is None or not await is_chat_admin(context, group_id, user_id):
            await query.answer("只有群管理员可以使用这里的设置。", show_alert=True)
            return True
        group = store.group_stats(group_id)
        text, markup = category_view(category, str(group["title"] if group else group_id),
                                     group_allowed(context, group_id, user_id), private)
        await show(text, markup)
        return True
    if data == "raffleplan:last" or data.startswith("rafflecopy:"):
        group_id = callback_group_id(context, chat)
        if group_id is None:
            await query.answer("请先选择群组。", show_alert=True)
            return True
        if data == "raffleplan:last":
            raffle = store.latest_universal_raffle(group_id)
            if not raffle:
                await query.answer("这个群还没有可用的上次模板，请先创建一个样板通用抽奖。", show_alert=True)
                return True
            begin_raffle_prefill(context, raffle_template_prefills(raffle), f"用上次模板（#{raffle['id']}）创建")
            await query.answer()
            return True
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "menu" and parts[2].isdigit():
            await show(*raffle_copy_view(store, group_id, int(parts[2])))
            return True
        if len(parts) == 3 and parts[1] == "item" and parts[2].isdigit():
            raffle = store.get_raffle(int(parts[2]))
            if not raffle or int(raffle["chat_id"]) != group_id or str(raffle["raffle_type"] or "") != "universal":
                await query.answer("当前群没有这个通用抽奖。", show_alert=True)
                return True
            begin_raffle_prefill(context, raffle_template_prefills(raffle), f"复制抽奖 #{raffle['id']} 为新抽奖")
            await query.answer()
            return True
        await query.answer("参数无效。", show_alert=True)
        return True
    if data.startswith("rparse:"):
        action = data.split(":", 2)[1] if ":" in data else ""
        if action == "start":
            group_id = callback_group_id(context, chat)
            if not private:
                await query.answer("请回复抽奖公告并发送「识别抽奖」。", show_alert=True)
                return True
            context.user_data[RAFFLE_PARSE_WAIT_KEY] = {
                "expires": time.time() + 600, "chat_id": group_id,
            }
            await show(
                "📥 识别抽奖\n\n请直接粘贴或转发一条抽奖公告给我，我会自动识别标题、规则、开奖时间、条件和奖品。\n\n"
                "发送 /cancel 取消。",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="cat:raffle")]]),
            )
            return True
        state = context.user_data.get(RAFFLE_PARSE_KEY)
        if not isinstance(state, dict) or float(state.get("expires") or 0) < time.time():
            context.user_data.pop(RAFFLE_PARSE_KEY, None)
            await query.answer("识别结果已过期，请重新发送抽奖内容。", show_alert=True)
            return True
        if action == "cancel":
            context.user_data.pop(RAFFLE_PARSE_KEY, None)
            await show("已取消识别抽奖。")
            return True
        if action == "recur":
            state["recur"] = not state.get("recur")
        elif action == "groups":
            state["chat_id"] = None
        elif action == "g":
            raw = data.rsplit(":", 1)[-1]
            if not raw.lstrip("-").isdigit() or not any(cid == int(raw) for cid, _ in state.get("groups") or []):
                await query.answer("群组无效。", show_alert=True)
                return True
            state["chat_id"] = int(raw)
        elif action in {"ok", "edit"}:
            target = state.get("chat_id")
            if target is None:
                await query.answer("请先选择要发布的群组。", show_alert=True)
                return True
            if not has_group_permission(context, int(target), user_id, "raffles"):
                await query.answer("你没有这个群的抽奖管理权限。", show_alert=True)
                return True
            parsed: raffle_parse.ParsedRaffle = state["parsed"]
            answers = raffle_parse.to_answers(parsed, bool(state.get("recur")))
            if action == "edit":
                if private:
                    context.user_data["selected_group_id"] = int(target)
                context.user_data.pop(RAFFLE_PARSE_KEY, None)
                begin_raffle_prefill(context, answers, "识别抽奖 · 修改后创建")
                await query.answer()
                return True
            if parsed.missing():
                await query.answer(f"缺少{'、'.join(parsed.missing())}，请点「✏️ 修改」补充。", show_alert=True)
                return True
            try:
                ends_at, winner_count, prize, extras = build_raffle_extras_from_pro(answers)
                await create_raffle_from_input(
                    update, context, ends_at, winner_count, prize,
                    chat_id_override=int(target), **extras,
                )
            except ValueError as exc:
                await query.answer(f"创建失败：{exc}", show_alert=True)
                return True
            except TelegramError as exc:
                await query.answer(f"发布到群失败：{exc}", show_alert=True)
                return True
            context.user_data.pop(RAFFLE_PARSE_KEY, None)
            label = next((title for cid, title in state.get("groups") or [] if cid == target), str(target))
            await show(f"✅ 抽奖「{parsed.title or '通用抽奖'}」已创建并发布到 {label}。",
                       InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="nav:main")]])
                       if private else None)
            return True
        text, markup = raffle_parse_view(state)
        await show(text, markup)
        return True
    return False


async def quick_text_features(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, private: bool,
) -> bool:
    """Keyword triggers shared by private and group chats. True when handled."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user:
        return False
    if context.user_data.get("menu_mode"):
        return False
    store: DirectoryStore = context.application.bot_data["store"]
    normalized = " ".join((text or "").split())
    # 识别抽奖
    wait = context.user_data.get(RAFFLE_PARSE_WAIT_KEY)
    if private and isinstance(wait, dict) and float(wait.get("expires") or 0) >= time.time() and normalized:
        await start_raffle_recognition(update, context, text)
        return True
    if normalized.startswith("识别抽奖"):
        body = (text or "").strip()[len("识别抽奖"):].strip()
        reply = getattr(message, "reply_to_message", None)
        if not body and reply is not None:
            body = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
        group_id = None if private else (chat.id if chat else None)
        if not body:
            if not private:
                await message.reply_text("请回复一条抽奖公告并发送「识别抽奖」。")
                return True
            context.user_data[RAFFLE_PARSE_WAIT_KEY] = {
                "expires": time.time() + 600, "chat_id": context.user_data.get("selected_group_id"),
            }
            await message.reply_text("请粘贴或转发一条抽奖公告给我。\n\n发送 /cancel 取消。")
            return True
        await start_raffle_recognition(update, context, body, group_id)
        return True
    if private and raffle_parse.looks_like_raffle(text):
        await start_raffle_recognition(update, context, text)
        return True
    # 人生指南
    if life_guide.is_keyword(normalized):
        await life_guide.send_home(message, private=private)
        return True
    # 币价
    if normalized == "币价":
        if not private and chat and not group_price_enabled(store, chat.id):
            return False
        text_body, markup = price_menu_view(store, has_admin_access(context, user.id))
        await message.reply_text(text_body, reply_markup=markup if private else None)
        return True
    symbol = crypto_price.parse_query(normalized)
    bare = "" if symbol else crypto_price.bare_symbol(normalized, price_symbols(store))
    if symbol or bare:
        if not private and chat and not group_price_enabled(store, chat.id):
            return False
        await reply_price(message, context, symbol or bare)
        return True
    return False



# ---- 🔔 币价涨跌监控 -----------------------------------------------------

PRICE_ALERT_TARGET_KEY = "price_alert_target"
PRICE_ALERT_POLLER_KEY = "price_alert_poller"
PRICE_ALERT_GROUP_DENIED = "只有群管理员可以设置本群的涨跌监控；个人提醒请私聊机器人设置。"


async def can_manage_group_alerts(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if has_group_permission(context, chat_id, user_id, "view"):
        return True
    try:
        return await is_telegram_chat_admin(context, chat_id, user_id)
    except Exception:  # noqa: BLE001 - 无法确认时按非管理员处理
        return False


def price_alert_max(context: ContextTypes.DEFAULT_TYPE) -> int:
    config = context.application.bot_data.get("config")
    return int(getattr(config, "price_alert_max_per_chat", crypto_alert.DEFAULT_MAX_PER_CHAT) or 0)


async def price_alert_target(
    context: ContextTypes.DEFAULT_TYPE, chat, user_id: int,
) -> int | None:
    """Private → the user's own alerts; group → the group's alerts (admins only)."""
    if chat is None or chat.type == ChatType.PRIVATE:
        return int(user_id)
    if await can_manage_group_alerts(context, int(chat.id), user_id):
        return int(chat.id)
    return None


def price_alert_list_view(store: DirectoryStore, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = store.list_price_alerts(chat_id)
    where = "本群" if chat_id < 0 else "我的"
    lines = [f"🔔 {where}币价涨跌监控", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("还没有监控。查询币价后点「🔔 监控此币涨跌」，或发送 /pricealert btc 5 2 添加。")
    for row in rows:
        mark = "" if int(row["enabled"] or 0) else "（已停用：机器人无法发送提醒）"
        lines.append(f"• {crypto_alert.monitor_summary(row)}{mark}")
        buttons.append([
            InlineKeyboardButton(f"✏️ {row['symbol']}", callback_data=f"palert:set:{row['symbol']}"),
            InlineKeyboardButton("🗑 删除", callback_data=f"palert:del:{row['id']}"),
        ])
    lines.extend([
        "", "涨跌都会提醒。", f"{crypto_alert.DAILY_LABEL}：每天上涨、下跌各最多提醒一次；",
        f"{crypto_alert.FAST_LABEL}：提醒后冷却 10 分钟。",
    ])
    buttons.append([InlineKeyboardButton("⬅️ 返回币价", callback_data="price:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def price_alert_prefills(row) -> list[str]:
    def number_text(value) -> str:
        return format(Decimal(str(value or 0)).normalize(), "f")
    return [
        number_text(row["daily_pct"]), number_text(row["fast_pct"]),
    ]


def price_alert_saved_text(symbol: str, daily, fast, chat_id: int) -> str:
    return (
        f"✅ 已设置 {symbol} 涨跌监控\n"
        f"{crypto_alert.DAILY_LABEL}：{crypto_alert.threshold_text(daily)}\n"
        f"{crypto_alert.FAST_LABEL}：{crypto_alert.threshold_text(fast)}\n"
        f"提醒发送到：{'本群' if chat_id < 0 else '私聊'}\n"
        "涨跌都会提醒。日涨跌每天上涨、下跌各最多提醒一次；10分钟涨跌提醒后冷却 10 分钟。"
    )


def price_alert_list_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(crypto_alert.LIST_BUTTON_TEXT, callback_data="palert:list")]])


async def price_alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat = query.message.chat if query.message else None
    store: DirectoryStore = context.application.bot_data["store"]
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    group = bool(chat and chat.type != ChatType.PRIVATE)
    if action == "list":
        target = int(chat.id) if group else int(user_id)
        text, markup = price_alert_list_view(store, target)
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower() and query.message is not None:
                await query.message.reply_text(text, reply_markup=markup)
        return
    target = await price_alert_target(context, chat, user_id)
    if target is None:
        await query.answer(PRICE_ALERT_GROUP_DENIED, show_alert=True)
        return
    if action == "del" and len(parts) > 2 and parts[2].isdigit():
        removed = store.delete_price_alert(target, alert_id=int(parts[2]))
        text, markup = price_alert_list_view(store, target)
        await query.answer("已删除" if removed else "监控不存在或已删除")
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass
        return
    if action == "set" and len(parts) > 2:
        symbol = crypto_price.normalize_symbol(parts[2])
        if not symbol or symbol == "USDT":
            await query.answer("币种无效。", show_alert=True)
            return
        existing = store.price_alert_for(target, symbol)
        if existing is None and price_alert_max(context) > 0 and len(store.list_price_alerts(target)) >= price_alert_max(context):
            await query.answer(f"每个聊天最多监控 {price_alert_max(context)} 个币种，请先删除不需要的。", show_alert=True)
            return
        await query.answer()
        try:
            panel = await context.bot.send_message(chat.id if chat else user_id, f"🔔 正在设置 {symbol} 涨跌监控…")
        except TelegramError:
            panel = None
        context.user_data.pop("settings_draft", None)
        context.user_data[PRICE_ALERT_TARGET_KEY] = {"chat_id": target, "symbol": symbol}
        if existing is not None:
            context.user_data["wizard_prefills"] = price_alert_prefills(existing)
        else:
            context.user_data.pop("wizard_prefills", None)
        if panel is not None:
            context.user_data["wizard_panel_id"] = panel.message_id
        context.user_data["wizard_action_title"] = (
            f"{symbol} 涨跌监控（{'提醒发到本群' if target < 0 else '私聊提醒'}）"
        )
        context.user_data["menu_mode"] = "price_alert"
        return
    await query.answer("操作无效。")


async def commit_price_alert(update, context: ContextTypes.DEFAULT_TYPE, draft) -> None:
    target = context.user_data.get(PRICE_ALERT_TARGET_KEY) or {}
    chat_id, symbol = target.get("chat_id"), target.get("symbol")
    if not isinstance(chat_id, int) or not symbol:
        raise ValueError("监控目标已失效，请重新点击「🔔 监控此币涨跌」")
    user_id = update.effective_user.id
    if chat_id < 0 and not await can_manage_group_alerts(context, chat_id, user_id):
        raise ValueError("你已没有本群的管理权限")
    daily = crypto_alert.parse_threshold(draft.answers[0])
    fast = crypto_alert.parse_threshold(draft.answers[1])
    if daily <= 0 and fast <= 0:
        raise ValueError("日涨跌和10分钟涨跌至少设置一个大于 0 的阈值")
    store: DirectoryStore = context.application.bot_data["store"]
    store.upsert_price_alert(chat_id, user_id, symbol, daily, fast, price_alert_max(context))
    store.audit(f"tg:{user_id}", "price_alert.set", f"{chat_id}:{symbol}", f"{daily}/{fast}")
    context.user_data.pop("menu_mode", None)
    context.user_data.pop(PRICE_ALERT_TARGET_KEY, None)
    await context.bot.send_message(
        draft.chat_id, price_alert_saved_text(symbol, daily, fast, chat_id),
        reply_markup=price_alert_list_markup(),
    )


async def price_alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    try:
        command = crypto_alert.parse_command(context.args or [])
    except ValueError as exc:
        await message.reply_text(f"{exc}\n\n{crypto_alert.USAGE}")
        return
    if command.action == "help":
        await message.reply_text(crypto_alert.USAGE, reply_markup=price_alert_list_markup())
        return
    group = bool(chat and chat.type != ChatType.PRIVATE)
    if command.action == "list":
        text, markup = price_alert_list_view(store, int(chat.id) if group else int(user.id))
        await message.reply_text(text, reply_markup=markup)
        return
    target = await price_alert_target(context, chat, user.id)
    if target is None:
        await message.reply_text(PRICE_ALERT_GROUP_DENIED)
        return
    if command.action == "del":
        removed = store.delete_price_alert(target, command.symbol)
        await message.reply_text(
            f"已删除 {command.symbol} 涨跌监控。" if removed else f"没有找到 {command.symbol} 的涨跌监控。"
        )
        return
    try:
        store.upsert_price_alert(
            target, user.id, command.symbol, command.daily, command.fast,
            price_alert_max(context),
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    store.audit(f"tg:{user.id}", "price_alert.set", f"{target}:{command.symbol}",
                f"{command.daily}/{command.fast}")
    await message.reply_text(
        price_alert_saved_text(command.symbol, command.daily, command.fast, target),
        reply_markup=price_alert_list_markup(),
    )


async def price_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user:
        return
    store: DirectoryStore = context.application.bot_data["store"]
    group = bool(chat and chat.type != ChatType.PRIVATE)
    text, markup = price_alert_list_view(store, int(chat.id) if group else int(user.id))
    await message.reply_text(text, reply_markup=markup)


def price_alert_poller(application: Application) -> crypto_alert.AlertPoller:
    poller = application.bot_data.get(PRICE_ALERT_POLLER_KEY)
    if poller is None:
        poller = crypto_alert.AlertPoller()
        application.bot_data[PRICE_ALERT_POLLER_KEY] = poller
    return poller


_ALERT_UNREACHABLE = (
    "chat not found", "not enough rights", "have no rights", "need administrator rights",
    "bot was kicked", "group chat was upgraded", "chat_write_forbidden", "user is deactivated",
)


async def poll_price_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: one OKX tickers request per round, only when monitors exist."""
    application = context.application
    store: DirectoryStore = application.bot_data["store"]
    rows = store.active_price_alerts()
    if not rows:
        return
    poller = price_alert_poller(application)
    symbols = {str(row["symbol"]) for row in rows}
    fast_symbols = {str(row["symbol"]) for row in rows if float(row["fast_pct"] or 0) > 0}
    try:
        snapshots, refs = await poller.snapshot(symbols, fast_symbols)
    except Exception as exc:  # noqa: BLE001 - 行情失败下一轮再试
        logging.warning("Price alert poll failed: %s", exc)
        return
    now_ts = poller.clock()
    now = datetime.now(crypto_price.BJT)
    today = now.strftime("%Y-%m-%d")
    rate: Decimal | None = None
    rate_loaded = False
    for row in rows:
        symbol = str(row["symbol"])
        snapshot = snapshots.get(symbol)
        if snapshot is None:
            continue
        ref = refs.get(symbol)
        fired = crypto_alert.evaluate(row, snapshot, ref, now_ts, today)
        if not fired:
            continue
        if not rate_loaded:
            rate_loaded = True
            try:
                rate, _ = await price_service(context).cny_rate()
            except Exception:  # noqa: BLE001
                rate = None
        fast_change = (snapshot.last - ref) / ref * 100 if ref else None
        text = crypto_alert.alert_text(symbol, snapshot, fired, rate, now, fast_change)
        store.mark_price_alert_fired(
            int(row["id"]),
            daily_up=today if any(f.kind == "daily" and f.direction == "up" for f in fired) else "",
            daily_down=today if any(f.kind == "daily" and f.direction == "down" for f in fired) else "",
            fast_at=now_ts if any(f.kind == "fast" for f in fired) else 0,
        )
        chat_id = int(row["chat_id"])
        try:
            with persistent_message():
                await context.bot.send_message(chat_id, text)
        except Forbidden as exc:
            store.disable_price_alerts(chat_id, str(exc))
            logging.info("Price alerts disabled for %s: %s", chat_id, exc)
        except TelegramBadRequest as exc:
            if any(marker in str(exc).lower() for marker in _ALERT_UNREACHABLE):
                store.disable_price_alerts(chat_id, str(exc))
                logging.info("Price alerts disabled for %s: %s", chat_id, exc)
            else:
                logging.warning("Price alert send failed for %s: %s", chat_id, exc)
        except TelegramError as exc:
            logging.warning("Price alert send failed for %s: %s", chat_id, exc)
