from __future__ import annotations

import asyncio
import html
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from telegram import Message, MessageEntity
from telegram.error import TelegramError
from telegram.ext import ExtBot


_PERSISTENT_MESSAGE: ContextVar[bool] = ContextVar("persistent_bot_message", default=False)
_AD_MESSAGE: ContextVar[bool] = ContextVar("bot_ad_message", default=False)
# 私聊消息统一按指定秒数撤回（如贴纸复制：私聊 10 分钟撤回）；群/频道不受影响
_PRIVATE_DELETE_AFTER: ContextVar[int] = ContextVar("private_delete_after", default=0)


@contextmanager
def private_delete_after(seconds: int) -> Iterator[None]:
    """Bot messages sent to *private* chats inside this scope are deleted after
    ``seconds`` (also persisted, so the deletion survives restarts), even when
    they are marked persistent. Group and channel messages are unaffected."""
    token = _PRIVATE_DELETE_AFTER.set(max(0, int(seconds)))
    try:
        yield
    finally:
        _PRIVATE_DELETE_AFTER.reset(token)


def private_delete_seconds() -> int:
    return int(_PRIVATE_DELETE_AFTER.get())


@contextmanager
def persistent_message() -> Iterator[None]:
    token = _PERSISTENT_MESSAGE.set(True)
    try:
        yield
    finally:
        _PERSISTENT_MESSAGE.reset(token)


def is_persistent_message() -> bool:
    return bool(_PERSISTENT_MESSAGE.get())


@contextmanager
def advertisement_message() -> Iterator[None]:
    token = _AD_MESSAGE.set(True)
    try:
        yield
    finally:
        _AD_MESSAGE.reset(token)


class AutoDeleteBot(ExtBot):
    __slots__ = (
        "auto_delete_seconds", "_delete_tasks", "message_decorator",
        "media_ad_sender", "markup_decorator", "_delete_by_message",
        "deletion_recorder",
    )

    def __init__(self, *args: Any, auto_delete_seconds: int = 180, **kwargs: Any):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "auto_delete_seconds", max(0, int(auto_delete_seconds)))
        object.__setattr__(self, "_delete_tasks", set())
        object.__setattr__(self, "_delete_by_message", {})
        object.__setattr__(self, "message_decorator", None)
        object.__setattr__(self, "media_ad_sender", None)
        object.__setattr__(self, "markup_decorator", None)
        # callable(chat_id, message_id, seconds) that persists a deletion
        object.__setattr__(self, "deletion_recorder", None)

    @staticmethod
    def _chat_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | str | None:
        return kwargs.get("chat_id", args[0] if args else None)

    async def _decoration(self, chat_id: int | str | None, position: str) -> str:
        callback = self.message_decorator
        if callback and chat_id is not None and not _AD_MESSAGE.get():
            return str(await callback(self, chat_id, position) or "")
        return ""

    async def _ad_markup(self, chat_id: int | str | None, position: str):
        callback = self.markup_decorator
        if callback and chat_id is not None and not _AD_MESSAGE.get():
            return await callback(self, chat_id, position)
        return None

    async def _merge_ad_markup(
        self, args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep original reply_markup and append prefix/suffix ad buttons."""
        chat_id = self._chat_id(args, kwargs)
        prefix_markup = await self._ad_markup(chat_id, "prefix")
        suffix_markup = await self._ad_markup(chat_id, "suffix")
        if not prefix_markup and not suffix_markup:
            return kwargs
        original = kwargs.get("reply_markup")
        rows = []
        for markup in (prefix_markup, original, suffix_markup):
            if not markup:
                continue
            for row in getattr(markup, "inline_keyboard", None) or ():
                rows.append(list(row))
        if not rows:
            return kwargs
        from telegram import InlineKeyboardMarkup
        kwargs = dict(kwargs)
        kwargs["reply_markup"] = InlineKeyboardMarkup(rows)
        return kwargs

    async def _send_media_ad(
        self, chat_id: int | str | None, position: str
    ) -> None:
        callback = self.media_ad_sender
        if callback and chat_id is not None and not _AD_MESSAGE.get():
            await callback(self, chat_id, position)

    async def _merge_ad_text(
        self, args: tuple[Any, ...], kwargs: dict[str, Any],
        key: str, index: int, limit: int,
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        chat_id = self._chat_id(args, kwargs)
        prefix = await self._decoration(chat_id, "prefix")
        suffix = await self._decoration(chat_id, "suffix")
        if not prefix and not suffix:
            return args, kwargs
        original = kwargs.get(key, args[index] if len(args) > index else "") or ""
        if not isinstance(original, str):
            return args, kwargs
        parse_mode = kwargs.get("parse_mode")
        if str(parse_mode).casefold() in {"html", "parsemode.html"}:
            prefix, suffix = html.escape(prefix), html.escape(suffix)
        parts = [part for part in (prefix, original, suffix) if part]
        combined = "\n\n".join(parts)
        if len(combined) > limit:
            return args, kwargs
        entities_key = "entities" if key == "text" else "caption_entities"
        if prefix and kwargs.get(entities_key):
            offset = len((prefix + "\n\n").encode("utf-16-le")) // 2
            kwargs = dict(kwargs)
            kwargs[entities_key] = [
                MessageEntity.de_json({**entity.to_dict(), "offset": entity.offset + offset}, self)
                for entity in kwargs[entities_key]
            ]
        if key in kwargs or len(args) <= index:
            kwargs = dict(kwargs)
            kwargs[key] = combined
            return args, kwargs
        values = list(args)
        values[index] = combined
        return tuple(values), kwargs

    def _schedule_delete(self, result: Any) -> None:
        override = _PRIVATE_DELETE_AFTER.get()
        messages = result if isinstance(result, (list, tuple)) else (result,)
        for message in messages:
            if not isinstance(message, Message):
                continue
            if override and int(message.chat_id) > 0:
                self.schedule_private_cleanup(message.chat_id, message.message_id, override)
                continue
            if self.auto_delete_seconds <= 0 or _PERSISTENT_MESSAGE.get():
                continue
            self.schedule_delete(message.chat_id, message.message_id)

    def schedule_private_cleanup(self, chat_id: int, message_id: int, seconds: int) -> bool:
        """Delete a private-chat message after ``seconds`` (in memory and in the
        persistent deletion queue). Returns False for groups/channels."""
        try:
            chat_id, message_id = int(chat_id), int(message_id)
        except (TypeError, ValueError):
            return False
        if chat_id <= 0 or seconds <= 0:
            return False
        self._schedule_delete_after(chat_id, message_id, seconds)
        recorder = self.deletion_recorder
        if recorder is not None:
            try:
                recorder(chat_id, message_id, seconds)
            except Exception:  # noqa: BLE001 - 记录失败不影响发送
                logging.exception("Could not persist deletion of %s/%s", chat_id, message_id)
        return True

    def schedule_delete(self, chat_id: int, message_id: int) -> None:
        if self.auto_delete_seconds <= 0:
            return
        self._schedule_delete_after(chat_id, message_id, self.auto_delete_seconds)

    def _schedule_delete_after(
        self, chat_id: int, message_id: int, seconds: int
    ) -> None:
        key = (int(chat_id), int(message_id))
        previous = self._delete_by_message.pop(key, None)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._delete_after_delay(chat_id, message_id, seconds))
        self._delete_by_message[key] = task
        self._delete_tasks.add(task)
        def cleanup(completed: asyncio.Task) -> None:
            self._delete_tasks.discard(completed)
            if self._delete_by_message.get(key) is completed:
                self._delete_by_message.pop(key, None)
        task.add_done_callback(cleanup)

    def schedule_delete_after(
        self, chat_id: int, message_id: int, seconds: int
    ) -> None:
        if seconds <= 0:
            return
        self._schedule_delete_after(chat_id, message_id, seconds)

    async def _delete_later(self, chat_id: int, message_id: int) -> None:
        await self._delete_after_delay(chat_id, message_id, self.auto_delete_seconds)

    async def _delete_after_delay(
        self, chat_id: int, message_id: int, seconds: int
    ) -> None:
        await asyncio.sleep(seconds)
        try:
            await super().delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            logging.warning(
                "Could not auto-delete message %s in chat %s: %s",
                message_id, chat_id, exc,
            )

    async def send_message(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "text", 1, 4096)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_message(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_photo(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_photo(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_video(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_video(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_audio(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_audio(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_voice(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_voice(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_animation(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_animation(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_document(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        args, kwargs = await self._merge_ad_text(args, kwargs, "caption", 2, 1024)
        kwargs = await self._merge_ad_markup(args, kwargs)
        result = await super().send_document(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result

    async def send_media_group(self, *args: Any, **kwargs: Any) -> tuple[Message, ...]:
        chat_id = self._chat_id(args, kwargs)
        await self._send_media_ad(chat_id, "prefix")
        result = await super().send_media_group(*args, **kwargs)
        self._schedule_delete(result)
        await self._send_media_ad(chat_id, "suffix")
        return result
