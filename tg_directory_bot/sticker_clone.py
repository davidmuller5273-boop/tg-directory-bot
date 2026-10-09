"""/jx：复制贴纸包（含自定义表情包）并改标题。

流程：/jx → 发送贴纸包链接 → 显示标题与数量 → 发送新标题（联系方式）
→ 后台用原贴纸 file_id 封装新贴纸包（归属发起用户）→ 返回新链接。
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputSticker, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .auto_delete import persistent_message
from .sticker_preview import build_sticker_preview

STATE_KEY = "sticker_clone"
PROMPT_TTL_SECONDS = 600
FIXED_TTL_SECONDS = 24 * 3600      # 固定模式：一次进入后 24 小时内发链接即可
MENU_BUTTON_TEXT = "😊 表情包复制更改标题"
ADD_BUTTON_TEXT = "✨ 免费添加贴纸 ✨"
MAX_INITIAL_STICKERS = 50          # createNewStickerSet 一次最多 50 张
SET_LIMITS = {"regular": 120, "mask": 120, "custom_emoji": 200}
MAX_CONCURRENT_JOBS = 2            # 全局同时封装的任务数
MAX_TITLE_LENGTH = 64
MAX_RETRY_AFTER_SECONDS = 600
ADD_INTERVAL_SECONDS = 0.35
PROGRESS_EVERY = 10
DEFAULT_EMOJI = "🙂"

DELETE_HINT = "如需去掉部分贴图：链接后加空格和序号，例如 链接 3|5|12（序号从 1 开始）"
LINK_PROMPT = (
    "请发送要解析的贴纸包链接（贴纸包或自定义表情包都可以）\n"
    "例如：https://t.me/addstickers/贴纸地址\n"
    f"{DELETE_HINT}\n\n"
    "随时可发送 /cancel 取消"
)
# 创建/添加贴纸可能很慢（尤其 50 张动态/表情），默认 5 秒读超时远远不够。
API_TIMEOUTS = {"read_timeout": 120, "write_timeout": 120, "connect_timeout": 30, "pool_timeout": 30}
TIMEOUT_POLL_ATTEMPTS = 6
TIMEOUT_POLL_SECONDS = 5
START_HINT = "无法为你创建贴纸包：请先私聊本机器人并点击“开始”（/start），然后重新发送 /jx。"

_LINK_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:addstickers|addemoji)/"
    r"|tg://(?:addstickers|addemoji)\?set=)?"
    r"([A-Za-z0-9_]{1,64})/?(?:[?#].*)?$",
    re.IGNORECASE,
)


class StickerCloneError(Exception):
    """User-facing failure of a clone job."""


def parse_sticker_set_name(text: str) -> str | None:
    match = _LINK_RE.match((text or "").strip())
    if not match:
        return None
    name = match.group(1)
    return name if name[0].isalpha() else None


_POSITIONS_RE = re.compile(r"^[\d\s|｜,，、/;；]+$")


def split_link_and_positions(text: str) -> tuple[str, str]:
    """`链接 3|5|12` → (链接, "3|5|12")；没有序号时第二项为空。"""
    raw = (text or "").strip()
    parts = raw.split(None, 1)
    if len(parts) == 2 and _POSITIONS_RE.match(parts[1]) and re.search(r"\d", parts[1]):
        return parts[0], parts[1].strip()
    return raw, ""


def plan_deletions(count: int, spec: str) -> tuple[list[int], list[str]]:
    """Return (sorted 1-based positions to delete, notes about ignored items)."""
    deleted: list[int] = []
    out_of_range: list[str] = []
    duplicates: list[str] = []
    for token in re.findall(r"\d+", spec or ""):
        position = int(token)
        if not 1 <= position <= count:
            if token not in out_of_range:
                out_of_range.append(token)
        elif position in deleted:
            if token not in duplicates:
                duplicates.append(token)
        else:
            deleted.append(position)
    notes = []
    if out_of_range:
        notes.append(f"已忽略超出范围的序号：{'、'.join(out_of_range)}（共 {count} 张）")
    if duplicates:
        notes.append(f"已忽略重复的序号：{'、'.join(duplicates)}")
    return sorted(deleted), notes


def deletion_summary(count: int, deleted: list[int], notes: list[str]) -> list[str]:
    lines = []
    if deleted:
        lines.append(
            f"将删除第 {'、'.join(map(str, deleted))} 张，剩余 {count - len(deleted)} 张"
        )
    lines.extend(notes)
    return lines


def title_length(text: str) -> int:
    """Telegram counts UTF-16 code units for these limits."""
    return len(text.encode("utf-16-le")) // 2


def build_set_name(source: str, bot_username: str, token: str | None = None) -> str:
    """`<orig>_<8hex>_by_<bot>`: letters/digits/underscores, starts with a
    letter, no consecutive underscores, at most 64 characters."""
    token = (token or secrets.token_hex(4)).lower()
    bot = re.sub(r"[^A-Za-z0-9_]", "", bot_username or "").strip("_") or "bot"
    suffix = f"_{token}_by_{bot}"
    base = re.sub(r"[^A-Za-z0-9_]", "", source or "")
    base = re.sub(r"_+", "_", base).strip("_")
    if not base or not base[0].isalpha():
        base = ("s" + base) if base else "s"
    base = base[: max(1, 64 - len(suffix))].rstrip("_") or "s"
    name = re.sub(r"_+", "_", f"{base}{suffix}")
    return name[:64]


def sticker_format(sticker: Any) -> str:
    if getattr(sticker, "is_video", False):
        return "video"
    if getattr(sticker, "is_animated", False):
        return "animated"
    return "static"


def input_sticker_from(sticker: Any, sticker_type: str) -> InputSticker:
    return InputSticker(
        sticker=sticker.file_id,
        emoji_list=[getattr(sticker, "emoji", None) or DEFAULT_EMOJI],
        format=sticker_format(sticker),
        mask_position=(
            getattr(sticker, "mask_position", None) if sticker_type == "mask" else None
        ),
    )


def share_link(name: str, sticker_type: str) -> str:
    kind = "addemoji" if sticker_type == "custom_emoji" else "addstickers"
    return f"https://t.me/{kind}/{name}"


def _error_text(exc: Exception) -> str:
    return str(getattr(exc, "message", "") or exc).lower()


def _is_name_taken(exc: Exception) -> bool:
    text = _error_text(exc)
    return "occupied" in text or "name_occupied" in text


def _is_user_unreachable(exc: Exception) -> bool:
    if isinstance(exc, Forbidden):
        return True
    text = _error_text(exc)
    return any(key in text for key in (
        "peer_id_invalid", "user not found", "user_id_invalid", "chat not found",
        "bot was blocked", "can't initiate",
    ))


def _is_name_invalid(exc: Exception) -> bool:
    text = _error_text(exc)
    return "name_invalid" in text or ("name" in text and "invalid" in text and "set" in text)


def _is_set_gone(exc: Exception) -> bool:
    text = _error_text(exc)
    return any(key in text for key in (
        "stickerset_invalid", "stickers_too_much", "too much stickers",
        "sticker set is full",
    ))


async def call_with_retry(
    func: Callable[..., Awaitable[Any]], *args: Any,
    attempts: int = 6, sleep: Callable[[float], Awaitable[Any]] | None = None,
    retry_timeouts: bool = True, **kwargs: Any,
) -> Any:
    """Call a Bot API method, waiting out flood control and transient errors."""
    sleep = sleep or asyncio.sleep
    for attempt in range(attempts):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            wait = exc.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            if wait > MAX_RETRY_AFTER_SECONDS or attempt == attempts - 1:
                raise
            await sleep(wait + 1)
        except TimedOut:
            if not retry_timeouts or attempt == attempts - 1:
                raise
            await sleep(min(10, 2 * (attempt + 1)))
        except (BadRequest, Forbidden):
            raise
        except NetworkError:
            if attempt == attempts - 1:
                raise
            await sleep(min(10, 2 * (attempt + 1)))
    raise RuntimeError("unreachable")


@dataclass
class CloneResult:
    name: str
    link: str
    added: int
    failed: int
    skipped_over_limit: int
    source_count: int
    sticker_type: str = "regular"
    animated: bool = False
    deleted: int = 0


async def clone_sticker_set(
    bot: Any, user_id: int, source_name: str, title: str, bot_username: str, *,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    name_factory: Callable[[], str] | None = None,
    skip: Any = (),
) -> CloneResult:
    sleep = sleep or asyncio.sleep
    try:
        source = await call_with_retry(bot.get_sticker_set, source_name, sleep=sleep)
    except BadRequest as exc:
        raise StickerCloneError("未找到该贴纸包，可能已被删除。") from exc
    sticker_type = str(getattr(source, "sticker_type", "regular") or "regular")
    original = list(source.stickers or [])
    if not original:
        raise StickerCloneError("该贴纸包里没有贴图。")
    skip_set = {int(p) for p in (skip or ()) if 1 <= int(p) <= len(original)}
    all_stickers = [s for i, s in enumerate(original, start=1) if i not in skip_set]
    if not all_stickers:
        raise StickerCloneError("删除指定序号后没有剩余贴图。")
    limit = SET_LIMITS.get(sticker_type, 120)
    stickers = all_stickers[:limit]
    over_limit = len(all_stickers) - len(stickers)
    inputs = [input_sticker_from(sticker, sticker_type) for sticker in stickers]
    total = len(inputs)
    needs_repainting = (
        any(getattr(s, "needs_repainting", False) for s in stickers)
        if sticker_type == "custom_emoji" else None
    )
    username = {"value": str(bot_username or "").lstrip("@")}

    async def refresh_username() -> bool:
        """Name must end with `_by_<bot username>`: ask Telegram for the real one."""
        get_me = getattr(bot, "get_me", None)
        if get_me is None:
            return False
        try:
            me = await get_me()
        except TelegramError:
            return False
        real = str(getattr(me, "username", "") or "")
        if real and real.casefold() != username["value"].casefold():
            username["value"] = real
            return True
        return False

    if not username["value"]:
        await refresh_username()
    make_name = name_factory or (lambda: build_set_name(source_name, username["value"]))

    async def set_size(name: str) -> int:
        try:
            current = await call_with_retry(bot.get_sticker_set, name, sleep=sleep, attempts=2)
        except TelegramError:
            return -1
        return len(current.stickers or [])

    async def wait_for_set(name: str) -> int:
        """After a timeout Telegram may still be creating the set: poll a while."""
        for attempt in range(TIMEOUT_POLL_ATTEMPTS):
            size = await set_size(name)
            if size > 0:
                return size
            if attempt < TIMEOUT_POLL_ATTEMPTS - 1:
                await sleep(TIMEOUT_POLL_SECONDS)
        return -1

    async def create_with(initial: list[InputSticker]) -> str:
        last_exc: Exception | None = None
        timed_out: set[str] = set()
        name_retries = 0
        refreshed = False
        name = make_name()
        for _ in range(8):
            try:
                await call_with_retry(
                    bot.create_new_sticker_set,
                    user_id=user_id, name=name, title=title, stickers=initial,
                    sticker_type=sticker_type, needs_repainting=needs_repainting,
                    sleep=sleep, retry_timeouts=False, **API_TIMEOUTS,
                )
                return name
            except TimedOut as exc:
                # 可能其实已创建成功（或仍在创建中）：等一会儿再查；
                # 没查到就用同一个名称重试，避免留下多个半成品贴纸包。
                last_exc = exc
                if await wait_for_set(name) > 0:
                    return name
                timed_out.add(name)
            except BadRequest as exc:
                if _is_user_unreachable(exc):
                    raise StickerCloneError(START_HINT) from exc
                if _is_name_taken(exc):
                    if name in timed_out and await wait_for_set(name) > 0:
                        return name  # 之前超时的那次其实成功了
                    last_exc = exc
                    name_retries += 1
                    if name_retries > 5:
                        break
                    name = make_name()
                    continue
                if _is_name_invalid(exc):
                    last_exc = exc
                    if not refreshed and await refresh_username():
                        refreshed = True
                        name = make_name()
                        continue
                    refreshed = True
                    name_retries += 1
                    if name_retries > 2:
                        break
                    name = make_name()
                    continue
                raise
            except Forbidden as exc:
                raise StickerCloneError(START_HINT) from exc
        if isinstance(last_exc, TimedOut):
            raise StickerCloneError("Telegram 响应超时，请稍后重试。") from last_exc
        raise StickerCloneError("贴纸包名称生成失败，请稍后重试。") from last_exc

    failed = 0
    initial = inputs[:MAX_INITIAL_STICKERS]
    try:
        name = await create_with(initial)
        added = len(initial)
        rest = inputs[MAX_INITIAL_STICKERS:]
    except BadRequest as bulk_exc:
        # 某张贴图无效会导致整批创建失败：逐张尝试找到第一张可用的作为起始
        logging.info("Bulk sticker set creation failed, falling back: %s", bulk_exc)
        name = ""
        added = 0
        rest = []
        for index, item in enumerate(inputs):
            if failed >= 10:
                break
            try:
                name = await create_with([item])
            except BadRequest:
                failed += 1
                continue
            added = 1
            rest = inputs[index + 1:]
            break
        if not name:
            raise StickerCloneError("贴纸包创建失败：原贴图无法复用，请换一个贴纸包。") from bulk_exc
    if progress:
        await progress(added + failed, total)

    for offset, item in enumerate(rest, start=1):
        before = added
        try:
            await call_with_retry(
                bot.add_sticker_to_set, user_id=user_id, name=name, sticker=item,
                sleep=sleep, retry_timeouts=False, **API_TIMEOUTS,
            )
            added += 1
        except TimedOut:
            # 超时不盲目重试，避免重复：以实际数量为准
            size = await set_size(name)
            if size > before:
                added = size
            else:
                try:
                    await call_with_retry(
                        bot.add_sticker_to_set, user_id=user_id, name=name,
                        sticker=item, sleep=sleep, **API_TIMEOUTS,
                    )
                    added += 1
                except TelegramError:
                    failed += 1
        except BadRequest as exc:
            if _is_set_gone(exc):
                failed += len(rest) - offset + 1
                break
            if _is_user_unreachable(exc):
                raise StickerCloneError(START_HINT) from exc
            failed += 1
        except RetryAfter:
            failed += 1
        except Forbidden as exc:
            raise StickerCloneError(START_HINT) from exc
        except NetworkError:
            failed += 1
        if progress and (offset % PROGRESS_EVERY == 0 or offset == len(rest)):
            await progress(added + failed, total)
        await sleep(ADD_INTERVAL_SECONDS)
    return CloneResult(
        name, share_link(name, sticker_type), added, failed, over_limit,
        len(all_stickers), sticker_type,
        any(getattr(s, "is_animated", False) or getattr(s, "is_video", False)
            for s in stickers),
        len(skip_set),
    )


def result_text(result: CloneResult) -> str:
    lines = ["贴纸包封装完成：", result.link]
    notes = []
    if result.deleted:
        notes.append(f"已按要求删除 {result.deleted} 张")
    if result.failed:
        notes.append(f"成功 {result.added} 张，{result.failed} 张复制失败已跳过")
    if result.skipped_over_limit:
        notes.append(
            f"原包共 {result.source_count} 张，超出单个贴纸包上限，"
            f"{result.skipped_over_limit} 张未复制"
        )
    if notes:
        lines.extend(["", *notes])
    return "\n".join(lines)


def is_sticker_link(text: str) -> bool:
    """Full t.me/addstickers or addemoji link (bare names don't count)."""
    link, _spec = split_link_and_positions(text)
    raw = link.lower()
    return bool(parse_sticker_set_name(link)) and ("addstickers" in raw or "addemoji" in raw)


def post_caption(title: str, sticker_type: str = "regular", animated: bool = False) -> str:
    motion = "#动态" if animated else "#静态"
    kind = "#表情" if sticker_type == "custom_emoji" else "#贴纸"
    return (
        f"👉 {title} 👈\n\n"
        f"{motion} {kind} #表情包 #斗图\n\n"
        "⬇️点击下方按钮添加表情⬇️"
    )


def add_button_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(ADD_BUTTON_TEXT, url=link)]])


async def send_post(
    bot: Any, chat_id: int, preview: bytes | None, caption: str,
    markup: InlineKeyboardMarkup,
) -> Any:
    """Photo (grid preview) + caption + add button; text fallback without photo."""
    with persistent_message():
        if preview:
            try:
                return await call_with_retry(
                    bot.send_photo, chat_id=chat_id, photo=preview, caption=caption,
                    reply_markup=markup, attempts=3,
                )
            except BadRequest as exc:
                if _is_user_unreachable(exc) or "rights" in _error_text(exc):
                    raise
                logging.info("Preview photo rejected, sending text: %s", exc)
        return await call_with_retry(
            bot.send_message, chat_id=chat_id, text=caption, reply_markup=markup,
            attempts=3,
        )


async def publish_result(
    bot: Any, user_id: int, result: CloneResult, title: str,
    channel_id: int = 0, channel_label: str = "",
) -> str:
    """Send preview post to the user (and the fixed channel). Never raises."""
    preview = None
    try:
        new_set = await call_with_retry(bot.get_sticker_set, result.name, attempts=3)
        preview = await build_sticker_preview(bot, list(new_set.stickers or []))
    except Exception as exc:  # noqa: BLE001 - 预览失败不影响结果
        logging.info("Sticker preview unavailable for %s: %s", result.name, exc)
    caption = post_caption(title, result.sticker_type, result.animated)
    markup = add_button_markup(result.link)
    try:
        await send_post(bot, user_id, preview, caption, markup)
    except Exception as exc:  # noqa: BLE001
        logging.info("Could not send sticker post to user %s: %s", user_id, exc)
    if not channel_id:
        return ""
    label = channel_label or str(channel_id)
    try:
        await send_post(bot, channel_id, preview, caption, markup)
    except Exception as exc:  # noqa: BLE001 - 频道失败只提示用户
        reason = str(getattr(exc, "message", "") or exc)
        notice = (
            f"⚠️ 发送到频道 {label} 失败：{reason}\n"
            "请确认机器人仍是该频道管理员并有发布消息权限，可在固定模式设置中重新设置频道。"
        )
        try:
            with persistent_message():
                await bot.send_message(chat_id=user_id, text=notice)
        except Exception:  # noqa: BLE001
            pass
        return notice
    return f"已发送到频道 {label}"



# ---- Telegram handlers --------------------------------------------------

def _jobs(context: ContextTypes.DEFAULT_TYPE) -> tuple[set[int], asyncio.Semaphore]:
    data = context.application.bot_data
    active = data.setdefault("sticker_clone_active", set())
    semaphore = data.get("sticker_clone_semaphore")
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        data["sticker_clone_semaphore"] = semaphore
    return active, semaphore


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(
        context.application.bot_data.get("bot_username")
        or getattr(context.bot, "username", "") or ""
    )


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry for /jx (and /start jx). Private chats only."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type != ChatType.PRIVATE:
        username = _bot_username(context)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "私聊机器人使用", url=f"https://t.me/{username}?start=jx",
        )]]) if username else None
        await message.reply_text(
            "贴纸包复制需要在私聊中使用（新贴纸包会归属到你的账号），请点击下方按钮私聊机器人。",
            reply_markup=markup,
        )
        return
    active, _ = _jobs(context)
    if user.id in active:
        await message.reply_text("你有一个贴纸包正在封装中，请等待完成后再试。")
        return
    context.user_data[STATE_KEY] = {
        "step": "link", "chat_id": chat.id, "expires": time.time() + PROMPT_TTL_SECONDS,
    }
    args = list(getattr(context, "args", None) or [])
    if args and parse_sticker_set_name(args[0]):
        await _handle_link(update, context, context.user_data[STATE_KEY], " ".join(args))
        return
    await message.reply_text(LINK_PROMPT, disable_web_page_preview=True)


async def _handle_link(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    link, spec = split_link_and_positions(text)
    name = parse_sticker_set_name(link)
    if not name:
        await message.reply_text(
            "链接格式不正确，请发送 https://t.me/addstickers/贴纸地址 或 "
            "https://t.me/addemoji/表情地址\n"
            f"{DELETE_HINT}\n\n发送 /cancel 取消",
            disable_web_page_preview=True,
        )
        return
    try:
        sticker_set = await call_with_retry(context.bot.get_sticker_set, name, attempts=3)
    except BadRequest:
        await message.reply_text("未找到该贴纸包，请检查链接后重新发送。\n\n发送 /cancel 取消")
        return
    except TelegramError:
        await message.reply_text("贴纸包读取失败，请稍后重新发送链接。\n\n发送 /cancel 取消")
        return
    stickers = list(sticker_set.stickers or [])
    if not stickers:
        await message.reply_text("该贴纸包里没有贴图，请换一个链接。\n\n发送 /cancel 取消")
        return
    sticker_type = str(getattr(sticker_set, "sticker_type", "regular") or "regular")
    limit = SET_LIMITS.get(sticker_type, 120)
    deleted, notes = plan_deletions(len(stickers), spec)
    remaining = len(stickers) - len(deleted)
    if remaining <= 0:
        await message.reply_text("删除这些序号后没有剩余贴图，请重新发送。\n\n发送 /cancel 取消")
        return
    state.update({
        "step": "title", "source": sticker_set.name, "count": min(remaining, limit),
        "skip": deleted, "expires": time.time() + PROMPT_TTL_SECONDS,
    })
    kind = "表情" if sticker_type == "custom_emoji" else "贴图"
    lines = [f"已解析{'表情包' if sticker_type == 'custom_emoji' else '贴纸包'}：{sticker_set.title}",
             f"共 {len(stickers)} 张{kind}"]
    lines.extend(deletion_summary(len(stickers), deleted, notes))
    if remaining > limit:
        lines.append(f"（单个贴纸包最多 {limit} 张，将只复制前 {limit} 张）")
    lines.extend(["", "请发送需要更改的联系方式（将作为新贴纸包标题）："])
    await message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def _handle_title(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    title = " ".join(text.split())
    if not 1 <= title_length(title) <= MAX_TITLE_LENGTH:
        await message.reply_text(
            f"标题需要 1-{MAX_TITLE_LENGTH} 个字符，请重新发送。\n\n发送 /cancel 取消"
        )
        return
    if await _start_job(update, context, str(state["source"]), int(state.get("count") or 0), title,
                        skip=list(state.get("skip") or [])):
        context.user_data.pop(STATE_KEY, None)


async def _start_job(
    update, context, source: str, count: int, title: str, *,
    channel_id: int = 0, channel_label: str = "", intro: str = "", skip: Any = (),
) -> bool:
    message = update.effective_message
    user = update.effective_user
    active, semaphore = _jobs(context)
    if user.id in active:
        await message.reply_text("你有一个贴纸包正在封装中，请等待完成后再试。")
        return False
    queued = semaphore.locked()
    with persistent_message():
        progress_message = await message.reply_text(
            intro
            + f"开始封装贴纸包（共 {count} 张），请稍候…"
            + ("\n当前排队中，前面的任务完成后自动开始。" if queued else ""),
            disable_web_page_preview=True,
        )
    active.add(user.id)
    job = run_job(
        context.bot, context.application.bot_data, user.id, source,
        title, _bot_username(context), progress_message,
        channel_id=channel_id, channel_label=channel_label, skip=list(skip or ()),
    )
    spawn = getattr(context.application, "create_task", None)
    if callable(spawn):
        spawn(job)
    else:
        asyncio.create_task(job)
    return True


async def run_job(
    bot: Any, bot_data: dict, user_id: int, source: str, title: str,
    bot_username: str, progress_message: Any, *,
    channel_id: int = 0, channel_label: str = "", publish: bool = True,
    skip: Any = (),
) -> CloneResult | None:
    active = bot_data.setdefault("sticker_clone_active", set())
    semaphore = bot_data.get("sticker_clone_semaphore") or asyncio.Semaphore(
        MAX_CONCURRENT_JOBS
    )
    bot_data["sticker_clone_semaphore"] = semaphore
    last_edit = [0.0]

    async def edit(text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_edit[0] < 3:
            return
        last_edit[0] = now
        try:
            with persistent_message():
                await progress_message.edit_text(text, disable_web_page_preview=True)
        except TelegramError:
            pass

    async def progress(done: int, total: int) -> None:
        await edit(f"正在封装贴纸包：{done}/{total}，请稍候…")

    try:
        async with semaphore:
            result = await clone_sticker_set(
                bot, user_id, source, title, bot_username, progress=progress, skip=skip,
            )
        await edit(result_text(result), force=True)
        if publish:
            await publish_result(
                bot, user_id, result, title, channel_id, channel_label,
            )
        return result
    except StickerCloneError as exc:
        await edit(f"贴纸包封装失败：{exc}", force=True)
    except TelegramError as exc:
        logging.warning("Sticker clone failed for %s: %s", user_id, exc)
        await edit(f"贴纸包封装失败：{exc}", force=True)
    except Exception:  # noqa: BLE001 — never let a background job die silently
        logging.exception("Sticker clone crashed for %s", user_id)
        await edit("贴纸包封装失败，请稍后重试。", force=True)
    finally:
        active.discard(user_id)
    return None


# ---- 固定模式 -----------------------------------------------------------

CHANNEL_PROMPT = (
    "请设置固定发送频道：\n"
    "• 从频道转发一条消息给我，或\n"
    "• 发送 @频道用户名，或 -100 开头的频道ID\n\n"
    "要求：你是该频道的管理员；机器人已加入频道并是管理员，且有“发布消息”权限。\n"
    "不需要频道可点“跳过”。发送 /cancel 取消"
)


def _store(context):
    return context.application.bot_data.get("store")


def channel_label(profile) -> str:
    if not profile or not int(profile["channel_id"] or 0):
        return ""
    title = str(profile["channel_title"] or "")
    username = str(profile["channel_username"] or "")
    if title and username:
        return f"{title}（@{username}）"
    return title or (f"@{username}" if username else str(profile["channel_id"]))


def menu_view() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{MENU_BUTTON_TEXT}\n\n"
        "• 默认模式：发送贴纸包链接后，再发送新标题（联系方式）。\n"
        "• 固定模式：保存固定标题（和发送频道）后，只需发送链接即可自动生成，"
        "设置了频道会自动发到频道。"
    )
    return text, InlineKeyboardMarkup([
        [
            InlineKeyboardButton("默认模式", callback_data="stk:default"),
            InlineKeyboardButton("固定模式", callback_data="stk:fixed"),
        ],
        [InlineKeyboardButton("⚙️ 固定模式设置", callback_data="stk:settings")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")],
    ])


def settings_view(profile) -> tuple[str, InlineKeyboardMarkup]:
    title = str(profile["fixed_title"] or "") if profile else ""
    channel = channel_label(profile)
    text = (
        "⚙️ 固定模式设置\n\n"
        f"固定更改标题：{title or '未设置'}\n"
        f"固定发送频道：{channel or '未设置'}"
    )
    rows = [[
        InlineKeyboardButton("✏️ 修改固定标题", callback_data="stk:set:title"),
        InlineKeyboardButton("📢 设置发送频道", callback_data="stk:set:channel"),
    ]]
    extra = []
    if channel:
        extra.append(InlineKeyboardButton("🚫 清除频道", callback_data="stk:clear:channel"))
    if profile:
        extra.append(InlineKeyboardButton("🗑 清除全部设置", callback_data="stk:clear:all"))
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton("▶️ 使用固定模式", callback_data="stk:fixed")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="stk:menu")])
    return text, InlineKeyboardMarkup(rows)


def fixed_ready_view(profile) -> tuple[str, InlineKeyboardMarkup]:
    channel = channel_label(profile)
    text = (
        "✅ 已进入固定模式\n\n"
        f"固定更改标题：{profile['fixed_title']}\n"
        f"固定发送频道：{channel or '未设置（只发给你）'}\n\n"
        "现在直接发送贴纸包链接即可自动生成，可以连续发送。\n"
        "例如：https://t.me/addstickers/贴纸地址\n"
        f"{DELETE_HINT}\n\n"
        "发送 /cancel 退出固定模式"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ 修改设置", callback_data="stk:settings")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="stk:menu")],
    ])


def _skip_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("跳过", callback_data="stk:skip")]])


def _set_state(context, chat_id: int, step: str, ttl: int = PROMPT_TTL_SECONDS, **extra) -> dict:
    state = {"step": step, "chat_id": chat_id, "expires": time.time() + ttl, **extra}
    context.user_data[STATE_KEY] = state
    return state


_CHANNEL_USERNAME = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?(?:t|telegram)\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})/?$"
)


def channel_reference(message: Any) -> int | str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None and str(getattr(origin, "type", "")) == "channel":
        chat = getattr(origin, "chat", None)
        if chat is not None:
            return int(chat.id)
    text = (getattr(message, "text", None) or "").strip()
    if re.fullmatch(r"-100\d{5,}", text):
        return int(text)
    match = _CHANNEL_USERNAME.match(text)
    if match:
        return "@" + match.group(1)
    return None


async def resolve_channel(bot: Any, user_id: int, message: Any) -> Any:
    """Validate the channel: user is admin/creator, bot is admin with post rights."""
    reference = channel_reference(message)
    if reference is None:
        raise StickerCloneError(
            "格式不正确：请转发一条频道消息，或发送 @频道用户名 / -100 开头的频道ID。"
        )
    try:
        chat = await bot.get_chat(reference)
    except TelegramError as exc:
        raise StickerCloneError(
            "找不到该频道：请先把机器人加入频道并设为管理员，再重新设置。"
        ) from exc
    if str(getattr(chat, "type", "")) != "channel":
        raise StickerCloneError("这不是频道，只能设置频道（Channel）。")
    try:
        member = await bot.get_chat_member(chat.id, user_id)
    except TelegramError as exc:
        raise StickerCloneError(
            "无法确认你在该频道的身份：请确认机器人已是该频道管理员。"
        ) from exc
    if str(getattr(member, "status", "")) not in {"creator", "administrator"}:
        raise StickerCloneError("你不是该频道的管理员，不能设置为发送频道。")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError as exc:
        raise StickerCloneError("机器人不是该频道的管理员，请先把机器人设为频道管理员。") from exc
    if str(getattr(me, "status", "")) != "administrator":
        raise StickerCloneError("机器人不是该频道的管理员，请先把机器人设为频道管理员。")
    if not getattr(me, "can_post_messages", False):
        raise StickerCloneError("机器人在该频道没有“发布消息”权限，请在频道管理员设置中开启。")
    return chat


async def _send_or_edit(update, text: str, markup=None) -> None:
    query = getattr(update, "callback_query", None)
    if query is not None and getattr(query, "message", None) is not None:
        try:
            await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramError:
            pass
    await update.effective_message.reply_text(
        text, reply_markup=markup, disable_web_page_preview=True,
    )


async def _enter_fixed(update, context) -> None:
    chat = update.effective_chat
    user = update.effective_user
    store = _store(context)
    profile = store.sticker_profile(user.id) if store else None
    if not profile or not str(profile["fixed_title"] or ""):
        _set_state(context, chat.id, "fixed_title", setup=True)
        await _send_or_edit(
            update,
            "首次使用固定模式，请先发送固定更改标题（1-64 个字符，例如你的联系方式）：\n\n"
            "发送 /cancel 取消",
        )
        return
    _set_state(context, chat.id, "fixed_link", FIXED_TTL_SECONDS)
    text, markup = fixed_ready_view(profile)
    await _send_or_edit(update, text, markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = str(query.data or "")
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type != ChatType.PRIVATE:
        await query.answer("请私聊机器人使用表情包功能。", show_alert=True)
        return
    store = _store(context)
    await query.answer()
    if data == "stk:menu":
        if (context.user_data.get(STATE_KEY) or {}).get("step") != "fixed_link":
            context.user_data.pop(STATE_KEY, None)
        text, markup = menu_view()
        await _send_or_edit(update, text, markup)
    elif data == "stk:default":
        active, _ = _jobs(context)
        if user.id in active:
            await _send_or_edit(update, "你有一个贴纸包正在封装中，请等待完成后再试。")
            return
        _set_state(context, chat.id, "link")
        await _send_or_edit(update, LINK_PROMPT)
    elif data == "stk:fixed":
        await _enter_fixed(update, context)
    elif data == "stk:settings":
        context.user_data.pop(STATE_KEY, None)
        text, markup = settings_view(store.sticker_profile(user.id) if store else None)
        await _send_or_edit(update, text, markup)
    elif data == "stk:set:title":
        _set_state(context, chat.id, "fixed_title", setup=False)
        await _send_or_edit(
            update, "请发送新的固定更改标题（1-64 个字符）：\n\n发送 /cancel 取消",
        )
    elif data == "stk:set:channel":
        _set_state(context, chat.id, "fixed_channel", setup=False)
        await _send_or_edit(update, CHANNEL_PROMPT, _skip_markup())
    elif data == "stk:skip":
        state = context.user_data.get(STATE_KEY) or {}
        if state.get("setup"):
            await _enter_fixed(update, context)
        else:
            context.user_data.pop(STATE_KEY, None)
            text, markup = settings_view(store.sticker_profile(user.id) if store else None)
            await _send_or_edit(update, text, markup)
    elif data in {"stk:clear:channel", "stk:clear:all"} and store:
        if data == "stk:clear:all":
            store.clear_sticker_profile(user.id)
            context.user_data.pop(STATE_KEY, None)
        else:
            store.clear_sticker_channel(user.id)
        text, markup = settings_view(store.sticker_profile(user.id))
        await _send_or_edit(update, ("已清除。\n\n" + text), markup)


async def _handle_fixed_title(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    user = update.effective_user
    store = _store(context)
    title = " ".join(text.split())
    if not 1 <= title_length(title) <= MAX_TITLE_LENGTH:
        await message.reply_text(
            f"标题需要 1-{MAX_TITLE_LENGTH} 个字符，请重新发送。\n\n发送 /cancel 取消"
        )
        return
    store.set_sticker_fixed_title(user.id, title)
    if state.get("setup"):
        _set_state(context, update.effective_chat.id, "fixed_channel", setup=True)
        await message.reply_text(
            f"已保存固定更改标题：{title}\n\n" + CHANNEL_PROMPT, reply_markup=_skip_markup(),
        )
        return
    context.user_data.pop(STATE_KEY, None)
    text, markup = settings_view(store.sticker_profile(user.id))
    await message.reply_text(f"已保存固定更改标题：{title}\n\n" + text, reply_markup=markup)


async def _handle_fixed_channel(update, context, state: dict) -> None:
    message = update.effective_message
    user = update.effective_user
    store = _store(context)
    try:
        chat = await resolve_channel(context.bot, user.id, message)
    except StickerCloneError as exc:
        await message.reply_text(f"{exc}\n\n可重新发送，或点“跳过”。", reply_markup=_skip_markup())
        return
    store.set_sticker_channel(
        user.id, int(chat.id), str(getattr(chat, "title", "") or ""),
        str(getattr(chat, "username", "") or ""),
    )
    profile = store.sticker_profile(user.id)
    saved = f"已设置固定发送频道：{channel_label(profile)}\n\n"
    if state.get("setup"):
        _set_state(context, update.effective_chat.id, "fixed_link", FIXED_TTL_SECONDS)
        text, markup = fixed_ready_view(profile)
    else:
        context.user_data.pop(STATE_KEY, None)
        text, markup = settings_view(profile)
    await message.reply_text(saved + text, reply_markup=markup, disable_web_page_preview=True)


async def _handle_fixed_link(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    user = update.effective_user
    store = _store(context)
    profile = store.sticker_profile(user.id) if store else None
    title = str(profile["fixed_title"] or "") if profile else ""
    if not title:
        context.user_data.pop(STATE_KEY, None)
        await message.reply_text("固定标题未设置，请先在主菜单“😊 表情包复制更改标题 → 固定模式”中设置。")
        return
    state["expires"] = time.time() + FIXED_TTL_SECONDS
    link, spec = split_link_and_positions(text)
    name = parse_sticker_set_name(link)
    try:
        sticker_set = await call_with_retry(context.bot.get_sticker_set, name, attempts=3)
    except BadRequest:
        await message.reply_text("未找到该贴纸包，请检查链接后重新发送。")
        return
    except TelegramError:
        await message.reply_text("贴纸包读取失败，请稍后重新发送链接。")
        return
    stickers = list(sticker_set.stickers or [])
    if not stickers:
        await message.reply_text("该贴纸包里没有贴图，请换一个链接。")
        return
    sticker_type = str(getattr(sticker_set, "sticker_type", "regular") or "regular")
    limit = SET_LIMITS.get(sticker_type, 120)
    kind = "表情" if sticker_type == "custom_emoji" else "贴图"
    channel_id = int(profile["channel_id"] or 0)
    deleted, notes = plan_deletions(len(stickers), spec)
    remaining = len(stickers) - len(deleted)
    if remaining <= 0:
        await message.reply_text("删除这些序号后没有剩余贴图，请重新发送。")
        return
    deletion_lines = "".join(f"{line}\n" for line in deletion_summary(len(stickers), deleted, notes))
    intro = (
        f"已解析贴纸包：{sticker_set.title}\n共 {len(stickers)} 张{kind}\n"
        + deletion_lines
        + (f"（单个贴纸包最多 {limit} 张，将只复制前 {limit} 张）\n" if remaining > limit else "")
        + f"固定标题：{title}\n"
        + (f"完成后发送到频道：{channel_label(profile)}\n" if channel_id else "")
        + "\n"
    )
    await _start_job(
        update, context, sticker_set.name, min(remaining, limit), title,
        channel_id=channel_id, channel_label=channel_label(profile), intro=intro,
        skip=deleted,
    )


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """High-priority handler for the sticker prompts (stops other handlers)."""
    state = context.user_data.get(STATE_KEY) if context.user_data is not None else None
    message = update.effective_message
    chat = update.effective_chat
    if not state or not message or not chat or chat.type != ChatType.PRIVATE:
        return
    if state.get("chat_id") != chat.id:
        return
    if float(state.get("expires") or 0) < time.time():
        context.user_data.pop(STATE_KEY, None)
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # /cancel 等命令交给命令处理器
    step = state.get("step")
    if step == "fixed_link" and not is_sticker_link(text):
        return  # 固定模式只拦截贴纸包链接，其它消息照常处理
    context.user_data["consumed_private_message"] = message.message_id
    if step == "fixed_channel":
        await _handle_fixed_channel(update, context, state)
    elif not text:
        await message.reply_text("请发送文字。\n\n发送 /cancel 取消")
    elif step == "link":
        await _handle_link(update, context, state, text)
    elif step == "title":
        await _handle_title(update, context, state, text)
    elif step == "fixed_title":
        await _handle_fixed_title(update, context, state, text)
    elif step == "fixed_link":
        await _handle_fixed_link(update, context, state, text)
    raise ApplicationHandlerStop
