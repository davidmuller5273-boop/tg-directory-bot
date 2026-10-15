"""📡 频道管理：跟踪机器人所在频道、分组、批量改名/头像/简介、批量复制或转发内容。

Telegram Bot API 无法列出机器人加入过的频道，因此：
- 通过 my_chat_member（机器人被加为管理员/移除）与 channel_post 自动记录；
- 也可在面板里转发一条频道消息或发送 @用户名 / 链接 / 数字ID 手动添加。
每个用户只能看到「自己和机器人都是管理员」的频道（getChatMember 校验，缓存 10 分钟）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from . import settings_wizard
from .settings_wizard import Question

ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, "administrator", "creator"}
CACHE_SECONDS = 600
SEND_GAP_SECONDS = 0.05  # 不同频道之间的间隔；同一频道每次操作之间至少 1 秒
MAX_RETRIES = 3
TITLE_PLACEHOLDER = "{原名}"

# 测试可替换
_sleep = asyncio.sleep

ADD_HINT = (
    "Telegram 不提供「机器人所在频道列表」接口。机器人被设为频道管理员后会自动记录；"
    "更早加入的频道请点 ➕ 添加频道（转发一条该频道消息，或发送 @用户名 / 链接 / 数字ID），"
    "或在频道里发一条消息。"
)

settings_wizard.FLOWS.update({
    "chm_add": ("添加频道", [Question("频道", "转发一条该频道的消息，或发送 @用户名、t.me 链接或 -100 开头的数字ID。")]),
    "chm_title": ("修改频道名称", [Question("新名称", f"1-128 个字；可用 {TITLE_PLACEHOLDER} 代表频道原名称，例如：{TITLE_PLACEHOLDER}｜官方")]),
    "chm_desc": ("修改频道简介", [Question("新简介", "0-255 个字；发送「清空」删除简介。")]),
    "chm_photo": ("修改频道头像", [Question("新头像", "请发送一张图片（作为照片发送）。", "media")]),
    "chm_send": ("发送内容到频道", [
        Question("发送方式", "复制：像机器人自己发的，保留按钮；转发：显示来源。", choices=("复制", "转发")),
        Question("内容", "发送或转发要发出的消息（文字、图片、视频、文件等，带按钮的频道帖也可以）。", "content"),
    ]),
    "chm_groupadd": ("新建频道分组", [Question("分组名称", "例如：主频道、备用频道")]),
    "chm_grouprename": ("重命名频道分组", [Question("新名称")]),
})

OP_LABELS = {"title": "✏️ 改名称", "photo": "🖼 改头像", "desc": "📝 改简介", "send": "📤 发送内容"}


# ---- helpers ---------------------------------------------------------------

def _store(context):
    return context.application.bot_data["store"]


def _allowed(context, user_id) -> bool:
    """频道管理对所有用户开放（私聊）；每个用户只能看到/操作自己和机器人都是管理员的频道。"""
    return user_id is not None


def rights_of(member) -> dict:
    keys = ("can_post_messages", "can_edit_messages", "can_delete_messages", "can_change_info", "can_invite_users")
    return {key: bool(getattr(member, key, False)) for key in keys}


def channel_name(row) -> str:
    title = str(row["title"] or row["chat_id"])
    return f"{title}（@{row['username']}）" if row["username"] else title


async def track_my_chat_member(update: Update, context) -> None:
    """机器人在频道中的身份变化：设为管理员→记录；被移除/降级→标记离开。"""
    change = update.my_chat_member
    if not change or change.chat.type != ChatType.CHANNEL:
        return
    member = change.new_chat_member
    status = "active" if member.status in ADMIN_STATUSES else "left"
    _store(context).upsert_managed_channel(
        change.chat.id, change.chat.title or "", change.chat.username or "",
        rights_of(member) if status == "active" else {}, status,
        change.from_user.id if change.from_user else 0,
    )


async def track_channel_post(update: Update, context) -> None:
    post = update.channel_post or update.edited_channel_post
    if not post or post.chat.type != ChatType.CHANNEL:
        return
    store = _store(context)
    row = store.managed_channel(post.chat.id)
    store.upsert_managed_channel(
        post.chat.id, post.chat.title or "", post.chat.username or "",
        None if row else {}, "active",
    )


async def visible_channels(context, user_id: int, refresh: bool = False) -> list:
    """Channels where both the user and the bot are admins (cached)."""
    store = _store(context)
    cache = context.user_data.get("chm_visible")
    rows = {int(r["chat_id"]): r for r in store.managed_channels()}
    if not refresh and isinstance(cache, dict) and time.monotonic() - cache.get("at", 0) < CACHE_SECONDS:
        return [rows[cid] for cid in cache.get("ids", []) if cid in rows]
    bot_id = context.bot.id
    ids = []
    for cid, row in rows.items():
        try:
            mine = await context.bot.get_chat_member(cid, user_id)
            if mine.status not in ADMIN_STATUSES:
                continue
            if refresh:
                me = await context.bot.get_chat_member(cid, bot_id)
                if me.status not in ADMIN_STATUSES:
                    store.upsert_managed_channel(cid, row["title"], row["username"], {}, "left")
                    continue
                store.upsert_managed_channel(cid, row["title"], row["username"], rights_of(me), "active")
        except Forbidden:
            store.upsert_managed_channel(cid, row["title"], row["username"], {}, "left")
            continue
        except TelegramError as exc:
            logging.info("chm: cannot check %s: %s", cid, exc)
            continue
        ids.append(cid)
    context.user_data["chm_visible"] = {"at": time.monotonic(), "ids": ids}
    rows = {int(r["chat_id"]): r for r in store.managed_channels()}
    return [rows[cid] for cid in ids if cid in rows]


async def resolve_targets(context, user_id: int, target: str, fresh: bool = False) -> tuple[str, list]:
    if fresh:
        context.user_data.pop("chm_visible", None)
    visible = await visible_channels(context, user_id)
    by_id = {int(r["chat_id"]): r for r in visible}
    kind, raw = target[0], target[1:]
    if kind == "c":
        row = by_id.get(int(raw))
        return (channel_name(row) if row else "频道", [row] if row else [])
    if kind == "g":
        group = _store(context).chm_group(int(raw), user_id)
        ids = _store(context).chm_group_channel_ids(int(raw)) if group else []
        return (f"分组「{group['name']}」" if group else "分组", [by_id[i] for i in ids if i in by_id])
    return ("全部频道", visible)


def _error_text(exc: Exception) -> str:
    text = str(getattr(exc, "message", "") or exc)
    low = text.lower()
    if "not enough rights" in low or "rights" in low or "admin_required" in low or "chat_admin_required" in low:
        return "机器人缺少相应管理权限"
    if isinstance(exc, Forbidden) or "chat not found" in low or "kicked" in low:
        return "机器人不在频道或已被移除"
    return text[:80]


async def _call_with_retry(func):
    for attempt in range(MAX_RETRIES):
        try:
            return await func()
        except RetryAfter as exc:
            wait = exc.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            if attempt == MAX_RETRIES - 1:
                raise
            await _sleep(float(wait) + 0.5)
    return None


async def fan_out(channels, action) -> tuple[list, list]:
    """Run ``action(row)`` for each channel; returns (ok_rows, [(row, reason)])."""
    ok, failed = [], []
    for index, row in enumerate(channels):
        if index:
            await _sleep(SEND_GAP_SECONDS)
        try:
            await _call_with_retry(lambda row=row: action(row))
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                ok.append(row)
            else:
                failed.append((row, _error_text(exc)))
        except TelegramError as exc:
            failed.append((row, _error_text(exc)))
        else:
            ok.append(row)
    return ok, failed


def summary(title: str, label: str, ok, failed) -> str:
    lines = [f"{title} · {label}", f"✅ 成功 {len(ok)} 个，❌ 失败 {len(failed)} 个"]
    lines += [f"✅ {channel_name(row)}" for row in ok[:30]]
    lines += [f"❌ {channel_name(row)}：{reason}" for row, reason in failed[:30]]
    return "\n".join(lines)


# ---- views -----------------------------------------------------------------

def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def ops_rows(target: str) -> list:
    return [
        [_btn(OP_LABELS["title"], f"chm:set:title:{target}"), _btn(OP_LABELS["desc"], f"chm:set:desc:{target}")],
        [_btn(OP_LABELS["photo"], f"chm:set:photo:{target}"), _btn(OP_LABELS["send"], f"chm:set:send:{target}")],
    ]


def menu_view(visible) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📡 频道管理\n"
        f"你和机器人都是管理员的频道：{len(visible)} 个\n\n"
        "可按分组或单个频道批量改名称、头像、简介，或把内容复制/转发到所有频道。\n\n"
        "提示：" + ADD_HINT
    )
    rows = [
        [_btn("📺 频道列表", "chm:list"), _btn("🗂 分组管理", "chm:groups")],
        [_btn("📢 全部频道批量操作", "chm:all")],
        [_btn("➕ 添加频道", "chm:set:add:a0"), _btn("🔄 刷新", "chm:refresh")],
        [_btn("⬅️ 返回主菜单", "nav:main")],
    ]
    return text, InlineKeyboardMarkup(rows)


def list_view(visible) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_btn(str(r["title"] or r["chat_id"])[:40], f"chm:ch:{r['chat_id']}")] for r in visible[:60]]
    rows.append([_btn("⬅️ 返回频道管理", "chm:menu")])
    text = "📺 频道列表（点选频道进行操作）" if visible else "还没有可管理的频道。\n\n" + ADD_HINT
    return text, InlineKeyboardMarkup(rows)


def channel_view(store, row, user_id=None) -> tuple[str, InlineKeyboardMarkup]:
    rights = json.loads(row["rights_json"] or "{}")
    groups = [g["name"] for g in store.chm_groups(user_id) if int(row["chat_id"]) in store.chm_group_channel_ids(int(g["id"]))]
    text = (
        f"📺 {channel_name(row)}\nID：{row['chat_id']}\n"
        f"发消息权限：{'✅' if rights.get('can_post_messages') else '❔'}  "
        f"修改信息权限：{'✅' if rights.get('can_change_info') else '❔'}\n"
        f"所在分组：{'、'.join(groups) or '无'}"
    )
    rows = ops_rows(f"c{row['chat_id']}")
    rows.append([_btn("⬅️ 返回频道列表", "chm:list")])
    return text, InlineKeyboardMarkup(rows)


def groups_view(store, user_id) -> tuple[str, InlineKeyboardMarkup]:
    groups = store.chm_groups(user_id)
    rows = [[_btn(f"🗂 {g['name']}（{g['size']}）", f"chm:g:{g['id']}")] for g in groups]
    rows.append([_btn("➕ 新建分组", "chm:set:groupadd:a0")])
    rows.append([_btn("⬅️ 返回频道管理", "chm:menu")])
    text = "🗂 频道分组（一个频道可加入多个分组）" if groups else "🗂 频道分组\n\n还没有分组，点 ➕ 新建分组。"
    return text, InlineKeyboardMarkup(rows)


def group_view(store, group, visible) -> tuple[str, InlineKeyboardMarkup]:
    member_ids = set(store.chm_group_channel_ids(int(group["id"])))
    names = [channel_name(r) for r in visible if int(r["chat_id"]) in member_ids]
    text = f"🗂 分组「{group['name']}」\n频道：{len(names)} 个\n" + "\n".join(f"• {n}" for n in names[:40])
    rows = ops_rows(f"g{group['id']}")
    rows += [
        [_btn("➕➖ 选择频道", f"chm:gpick:{group['id']}")],
        [_btn("✏️ 重命名分组", f"chm:set:grouprename:g{group['id']}"), _btn("🗑 删除分组", f"chm:gdel:{group['id']}")],
        [_btn("⬅️ 返回分组管理", "chm:groups")],
    ]
    return text, InlineKeyboardMarkup(rows)


def group_pick_view(store, group, visible) -> tuple[str, InlineKeyboardMarkup]:
    member_ids = set(store.chm_group_channel_ids(int(group["id"])))
    rows = [[_btn(("✅ " if int(r["chat_id"]) in member_ids else "⬜ ") + str(r["title"])[:36],
                  f"chm:gtog:{group['id']}:{r['chat_id']}")] for r in visible[:60]]
    rows.append([_btn("✅ 完成", f"chm:g:{group['id']}")])
    return f"🗂 分组「{group['name']}」：点频道加入或移出", InlineKeyboardMarkup(rows)


# ---- callbacks -------------------------------------------------------------

async def handle_callback(update: Update, context) -> None:
    query = update.callback_query
    data = str(query.data or "")
    user_id = query.from_user.id
    store = _store(context)
    if not _allowed(context, user_id):
        await query.answer("无法识别用户。", show_alert=True)
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await query.answer("请私聊机器人使用频道管理。", show_alert=True)
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    async def show(view):
        await query.edit_message_text(view[0], reply_markup=view[1])

    if action in {"menu", "refresh"}:
        visible = await visible_channels(context, user_id, refresh=action == "refresh")
        await query.answer("已刷新。" if action == "refresh" else None)
        await show(menu_view(visible))
        return
    if action == "set":
        # chm:set:<op>:<target>  → 进入问答（由 handle_callback 统一启动向导）
        op, target = parts[2], parts[3] if len(parts) > 3 else "a0"
        if op in OP_LABELS:
            label, channels = await resolve_targets(context, user_id, target)
            if not channels:
                await query.answer("没有可操作的频道（需要你和机器人都是管理员）。", show_alert=True)
                return
        context.user_data["chm_target"] = target
        context.user_data["menu_mode"] = f"chm_{op}"
        await query.answer()
        return
    visible = await visible_channels(context, user_id)
    if action == "list":
        await query.answer()
        await show(list_view(visible))
        return
    if action == "all":
        await query.answer()
        await query.edit_message_text(
            f"📢 全部频道（{len(visible)} 个）批量操作",
            reply_markup=InlineKeyboardMarkup(ops_rows("a0") + [[_btn("⬅️ 返回频道管理", "chm:menu")]]),
        )
        return
    if action == "ch":
        row = next((r for r in visible if int(r["chat_id"]) == int(parts[2])), None)
        if row is None:
            await query.answer("你或机器人已不是该频道管理员。", show_alert=True)
            return
        await query.answer()
        await show(channel_view(store, row, user_id))
        return
    if action == "groups":
        await query.answer()
        await show(groups_view(store, user_id))
        return
    group = store.chm_group(int(parts[2]), user_id) if len(parts) > 2 and parts[2].isdigit() else None
    if group is None:
        await query.answer("分组不存在。", show_alert=True)
        return
    if action == "g":
        await query.answer()
        await show(group_view(store, group, visible))
    elif action == "gpick":
        await query.answer()
        await show(group_pick_view(store, group, visible))
    elif action == "gtog":
        cid = int(parts[3])
        if cid not in {int(r["chat_id"]) for r in visible}:
            await query.answer("你或机器人已不是该频道管理员。", show_alert=True)
            return
        now_in = store.toggle_chm_group_channel(int(group["id"]), cid)
        await query.answer("已加入分组。" if now_in else "已移出分组。")
        await show(group_pick_view(store, group, visible))
    elif action == "gdel":
        store.delete_chm_group(int(group["id"]))
        await query.answer("分组已删除。")
        await show(groups_view(store, user_id))
    else:
        await query.answer("未知操作。")


# ---- wizard commit -----------------------------------------------------------

async def _resolve_channel_input(context, message, text):
    original = getattr(message, "original_message", message)
    origin = getattr(original, "forward_origin", None)
    chat = getattr(origin, "chat", None) or getattr(original, "forward_from_chat", None)
    if chat is None:
        raw = text.strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if raw.startswith(prefix):
                raw = "@" + raw[len(prefix):].split("/")[0]
        if raw.lstrip("-").isdigit():
            raw = int(raw)
        elif not str(raw).startswith("@"):
            raw = "@" + str(raw)
        chat = await context.bot.get_chat(raw)
    if chat.type != ChatType.CHANNEL:
        raise ValueError("只支持频道；请转发频道消息或发送频道 @用户名/链接/ID")
    return chat


async def commit(update: Update, context, mode: str) -> bool:
    """Called from process_menu_input for chm_* wizard modes."""
    message = update.effective_message
    user = update.effective_user
    store = _store(context)
    context.user_data.pop("menu_mode", None)
    if not _allowed(context, user.id):
        await message.reply_text("无法识别用户。")
        return True
    answers = list(getattr(message, "setting_answers", None) or [])
    target = str(context.user_data.pop("chm_target", "a0"))
    op = mode.removeprefix("chm_")
    back = InlineKeyboardMarkup([[_btn("⬅️ 返回频道管理", "chm:menu")]])
    if op == "add":
        chat = await _resolve_channel_input(context, message, answers[0] if answers else "")
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status not in ADMIN_STATUSES:
            raise ValueError("机器人还不是该频道管理员，请先把机器人设为频道管理员")
        mine = await context.bot.get_chat_member(chat.id, user.id)
        if mine.status not in ADMIN_STATUSES:
            raise ValueError("你不是该频道管理员")
        store.upsert_managed_channel(chat.id, chat.title or "", chat.username or "", rights_of(member), "active", user.id)
        context.user_data.pop("chm_visible", None)
        await message.reply_text(f"✅ 已添加频道：{chat.title}", reply_markup=back)
        return True
    if op == "groupadd":
        gid = store.add_chm_group(answers[0], user.id)
        group = store.chm_group(gid, user.id)
        visible = await visible_channels(context, user.id)
        text, markup = group_pick_view(store, group, visible)
        await message.reply_text(f"✅ 分组已创建。\n\n{text}", reply_markup=markup)
        return True
    if op == "grouprename":
        gid = int(target[1:])
        if store.chm_group(gid, user.id) is None:
            raise ValueError("分组不存在")
        if not store.rename_chm_group(gid, answers[0]):
            raise ValueError("分组不存在")
        await message.reply_text("✅ 分组已重命名。", reply_markup=InlineKeyboardMarkup([[_btn("⬅️ 返回分组", f"chm:g:{gid}")]]))
        return True
    # 执行前重新 getChatMember 校验用户仍是频道管理员
    label, channels = await resolve_targets(context, user.id, target, fresh=True)
    if not channels:
        raise ValueError("没有可操作的频道（需要你和机器人都是管理员）")
    bot = context.bot
    if op == "title":
        template = answers[0].strip()

        def new_title(row):
            return template.replace(TITLE_PLACEHOLDER, str(row["title"] or "")).strip()[:128]

        async def action(row):
            await bot.set_chat_title(int(row["chat_id"]), new_title(row))
            store.set_managed_channel_title(int(row["chat_id"]), new_title(row))
        ok, failed = await fan_out(channels, action)
        text = summary(label, OP_LABELS["title"], ok, failed)
    elif op == "desc":
        value = "" if answers[0].strip() == "清空" else answers[0].strip()[:255]
        ok, failed = await fan_out(channels, lambda row: bot.set_chat_description(int(row["chat_id"]), value))
        text = summary(label, OP_LABELS["desc"], ok, failed)
    elif op == "photo":
        original = getattr(message, "original_message", message)
        photos = getattr(original, "photo", None)
        if not photos:
            raise ValueError("请作为照片发送一张图片")
        file = await bot.get_file(photos[-1].file_id)
        data = bytes(await file.download_as_bytearray())
        ok, failed = await fan_out(channels, lambda row: bot.set_chat_photo(int(row["chat_id"]), data))
        text = summary(label, OP_LABELS["photo"], ok, failed)
    elif op == "send":
        from .rich_content import buttons_markup, capture_buttons_resolving
        original = getattr(message, "original_message", message)
        forward = answers and answers[0] == "转发"
        if forward:
            ok, failed = await fan_out(channels, lambda row: bot.forward_message(
                int(row["chat_id"]), original.chat_id, original.message_id))
        else:
            markup = buttons_markup(await capture_buttons_resolving(bot, original, original.chat_id))
            ok, failed = await fan_out(channels, lambda row: bot.copy_message(
                int(row["chat_id"]), original.chat_id, original.message_id, reply_markup=markup))
        text = summary(label, "📤 " + ("转发" if forward else "复制") + "内容", ok, failed)
    else:
        raise ValueError("未知频道操作")
    store.audit(f"tg:{user.id}", f"channel.{op}", target, f"ok={len(ok)} fail={len(failed)}")
    await message.reply_text(text, reply_markup=back)
    return True
