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

STATE_KEY = "sticker_clone"
PROMPT_TTL_SECONDS = 600
MAX_INITIAL_STICKERS = 50          # createNewStickerSet 一次最多 50 张
SET_LIMITS = {"regular": 120, "mask": 120, "custom_emoji": 200}
MAX_CONCURRENT_JOBS = 2            # 全局同时封装的任务数
MAX_TITLE_LENGTH = 64
MAX_RETRY_AFTER_SECONDS = 600
ADD_INTERVAL_SECONDS = 0.35
PROGRESS_EVERY = 10
DEFAULT_EMOJI = "🙂"

LINK_PROMPT = (
    "请发送要解析的贴纸包链接\n"
    "例如：https://t.me/addstickers/贴纸地址\n\n"
    "随时可发送 /cancel 取消"
)
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


async def clone_sticker_set(
    bot: Any, user_id: int, source_name: str, title: str, bot_username: str, *,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    name_factory: Callable[[], str] | None = None,
) -> CloneResult:
    sleep = sleep or asyncio.sleep
    try:
        source = await call_with_retry(bot.get_sticker_set, source_name, sleep=sleep)
    except BadRequest as exc:
        raise StickerCloneError("未找到该贴纸包，可能已被删除。") from exc
    sticker_type = str(getattr(source, "sticker_type", "regular") or "regular")
    all_stickers = list(source.stickers or [])
    if not all_stickers:
        raise StickerCloneError("该贴纸包里没有贴图。")
    limit = SET_LIMITS.get(sticker_type, 120)
    stickers = all_stickers[:limit]
    over_limit = len(all_stickers) - len(stickers)
    inputs = [input_sticker_from(sticker, sticker_type) for sticker in stickers]
    total = len(inputs)
    needs_repainting = (
        any(getattr(s, "needs_repainting", False) for s in stickers)
        if sticker_type == "custom_emoji" else None
    )
    make_name = name_factory or (lambda: build_set_name(source_name, bot_username))

    async def set_size(name: str) -> int:
        try:
            current = await call_with_retry(bot.get_sticker_set, name, sleep=sleep)
        except TelegramError:
            return -1
        return len(current.stickers or [])

    async def create_with(initial: list[InputSticker]) -> str:
        last_exc: Exception | None = None
        for _ in range(5):
            name = make_name()
            try:
                await call_with_retry(
                    bot.create_new_sticker_set,
                    user_id=user_id, name=name, title=title, stickers=initial,
                    sticker_type=sticker_type, needs_repainting=needs_repainting,
                    sleep=sleep, retry_timeouts=False,
                )
                return name
            except TimedOut as exc:
                # 可能其实已创建成功：查一下
                if await set_size(name) > 0:
                    return name
                last_exc = exc
            except BadRequest as exc:
                if _is_user_unreachable(exc):
                    raise StickerCloneError(START_HINT) from exc
                if _is_name_taken(exc):
                    last_exc = exc
                    continue
                raise
            except Forbidden as exc:
                raise StickerCloneError(START_HINT) from exc
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
                sleep=sleep, retry_timeouts=False,
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
                        sticker=item, sleep=sleep,
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
        len(all_stickers),
    )


def result_text(result: CloneResult) -> str:
    lines = ["贴纸包封装完成：", result.link]
    notes = []
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
        await _handle_link(update, context, context.user_data[STATE_KEY], args[0])
        return
    await message.reply_text(LINK_PROMPT, disable_web_page_preview=True)


async def _handle_link(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    name = parse_sticker_set_name(text)
    if not name:
        await message.reply_text(
            "链接格式不正确，请发送 https://t.me/addstickers/贴纸地址 或 "
            "https://t.me/addemoji/表情地址\n\n发送 /cancel 取消",
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
    state.update({
        "step": "title", "source": sticker_set.name, "count": len(stickers),
        "expires": time.time() + PROMPT_TTL_SECONDS,
    })
    kind = "表情" if sticker_type == "custom_emoji" else "贴图"
    lines = [f"已解析贴纸包：{sticker_set.title}", f"共 {len(stickers)} 张{kind}"]
    if len(stickers) > limit:
        lines.append(f"（单个贴纸包最多 {limit} 张，将只复制前 {limit} 张）")
    lines.extend(["", "请发送需要更改的联系方式（将作为新贴纸包标题）："])
    await message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def _handle_title(update, context, state: dict, text: str) -> None:
    message = update.effective_message
    user = update.effective_user
    title = " ".join(text.split())
    if not 1 <= title_length(title) <= MAX_TITLE_LENGTH:
        await message.reply_text(
            f"标题需要 1-{MAX_TITLE_LENGTH} 个字符，请重新发送。\n\n发送 /cancel 取消"
        )
        return
    active, semaphore = _jobs(context)
    if user.id in active:
        await message.reply_text("你有一个贴纸包正在封装中，请等待完成后再试。")
        return
    context.user_data.pop(STATE_KEY, None)
    queued = semaphore.locked()
    with persistent_message():
        progress_message = await message.reply_text(
            f"开始封装贴纸包（共 {state.get('count')} 张），请稍候…"
            + ("\n当前排队中，前面的任务完成后自动开始。" if queued else "")
        )
    active.add(user.id)
    job = run_job(
        context.bot, context.application.bot_data, user.id, str(state["source"]),
        title, _bot_username(context), progress_message,
    )
    spawn = getattr(context.application, "create_task", None)
    if callable(spawn):
        spawn(job)
    else:
        asyncio.create_task(job)


async def run_job(
    bot: Any, bot_data: dict, user_id: int, source: str, title: str,
    bot_username: str, progress_message: Any,
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
                bot, user_id, source, title, bot_username, progress=progress,
            )
        await edit(result_text(result), force=True)
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


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """High-priority text handler for the /jx prompts (stops other handlers)."""
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
    context.user_data["consumed_private_message"] = message.message_id
    if not text:
        await message.reply_text("请发送文字。\n\n发送 /cancel 取消")
    elif state.get("step") == "link":
        await _handle_link(update, context, state, text)
    elif state.get("step") == "title":
        await _handle_title(update, context, state, text)
    raise ApplicationHandlerStop
