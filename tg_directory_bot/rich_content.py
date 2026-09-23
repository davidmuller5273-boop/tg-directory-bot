"""Persist Telegram text entities without converting or substituting emoji."""

import json

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity


def button_content(message):
    """Extract a leading custom icon using Telegram's UTF-16 entity offsets."""
    text, raw_entities = capture_content(message)
    custom = [item for item in json.loads(raw_entities) if item.get("type") == "custom_emoji"]
    if len(custom) > 1:
        raise ValueError("每个按钮仅支持一个自定义 Emoji；多个普通 Emoji 可以原样使用。")
    if not custom:
        return text, ""
    entity = custom[0]
    encoded = text.encode("utf-16-le")
    start, end = entity["offset"] * 2, (entity["offset"] + entity["length"]) * 2
    before = encoded[:start].decode("utf-16-le")
    if before.strip():
        raise ValueError("按钮的自定义 Emoji 只能显示在文字前，请把它放在最前面。")
    label = (encoded[:start] + encoded[end:]).decode("utf-16-le")
    return label or " ", str(entity["custom_emoji_id"])


def capture_content(message):
    message = getattr(message, "original_message", message)
    text = message.text if message.text is not None else (message.caption or "")
    entities = message.entities if message.text is not None else message.caption_entities
    return text, json.dumps([entity.to_dict() for entity in (entities or ())], ensure_ascii=False)


def content_entities(row):
    raw = row["entities_json"] if "entities_json" in row.keys() else "[]"
    return [MessageEntity.de_json(item, None) for item in json.loads(raw or "[]")]


def validate_content(text, file_id="", file_type=""):
    limit = 1024 if file_id and file_type not in {"sticker", "video_note"} else 4096
    if len(text) > limit:
        raise ValueError(f"内容超过 {limit} 字，请缩短后再保存；不会截断原内容。")
    if not text.strip() and not file_id:
        raise ValueError("请发送文字或媒体内容。")


async def send_content(bot, chat_id, row, reply_markup=None):
    text = str(row["text"] or "")
    file_id = str(row["file_id"] or "")
    file_type = str(row["file_type"] or "")
    validate_content(text, file_id, file_type)
    entities = content_entities(row)
    if not file_id:
        if not text:
            raise ValueError("请先设置文本或媒体")
        return await bot.send_message(
            chat_id, text, entities=entities, parse_mode=None, reply_markup=reply_markup,
        )
    methods = {
        "photo": bot.send_photo, "video": bot.send_video,
        "animation": bot.send_animation, "audio": bot.send_audio,
        "document": bot.send_document, "voice": bot.send_voice,
        "sticker": bot.send_sticker, "video_note": bot.send_video_note,
    }
    if file_type not in methods:
        raise ValueError("不支持的媒体类型，请重新设置")
    if file_type in {"sticker", "video_note"}:
        if text:
            await bot.send_message(chat_id, text, entities=entities, parse_mode=None)
        return await methods[file_type](chat_id, file_id, reply_markup=reply_markup)
    return await methods[file_type](
        chat_id, file_id, caption=text or None, caption_entities=entities,
        parse_mode=None, reply_markup=reply_markup,
    )


def capture_buttons(message) -> str:
    """Serialize inline keyboard from a message as JSON (URL / callback / web_app).

    Also persists Bot API button chrome fields when present:
    - style (primary / success / danger)
    - icon_custom_emoji_id (custom emoji icon on the button)
    Text is kept as-is (ordinary emoji in the label are not stripped).
    """
    message = getattr(message, "original_message", message)
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or ()
    payload = []
    for row in rows:
        out_row = []
        for button in row:
            item = {"text": str(getattr(button, "text", "") or "")}
            url = getattr(button, "url", None)
            callback = getattr(button, "callback_data", None)
            web_app = getattr(button, "web_app", None)
            if url:
                item["url"] = str(url)
            elif callback is not None:
                item["callback_data"] = str(callback)
            elif web_app is not None and getattr(web_app, "url", None):
                item["web_app"] = {"url": str(web_app.url)}
            else:
                continue
            # Prefer to_dict() so both PTB-known attrs and api_kwargs fields are covered.
            try:
                raw = button.to_dict() if hasattr(button, "to_dict") else {}
            except Exception:
                raw = {}
            style = raw.get("style") if isinstance(raw, dict) else None
            if style and str(style) != "default":
                item["style"] = str(style)
            icon_id = raw.get("icon_custom_emoji_id") if isinstance(raw, dict) else None
            if icon_id:
                item["icon_custom_emoji_id"] = str(icon_id)
            out_row.append(item)
        if out_row:
            payload.append(out_row)
    return json.dumps(payload, ensure_ascii=False)


def buttons_markup(row_or_json) -> InlineKeyboardMarkup | None:
    """Rebuild InlineKeyboardMarkup from buttons_json or a DB row.

    Restores style / icon_custom_emoji_id via api_kwargs (same pattern as
    quick_post_markup) so fallback rebuild keeps colored icons when copy_message
    is unavailable.
    """
    if row_or_json is None:
        return None
    if hasattr(row_or_json, "keys"):
        raw = row_or_json["buttons_json"] if "buttons_json" in row_or_json.keys() else "[]"
    else:
        raw = row_or_json
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    keyboard = []
    for row in data:
        if not isinstance(row, list):
            continue
        buttons = []
        for item in row:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip() or " "
            api_kwargs = {}
            if item.get("style"):
                api_kwargs["style"] = str(item["style"])
            if item.get("icon_custom_emoji_id"):
                api_kwargs["icon_custom_emoji_id"] = str(item["icon_custom_emoji_id"])
            kwargs = {}
            if api_kwargs:
                kwargs["api_kwargs"] = api_kwargs
            if item.get("url"):
                buttons.append(InlineKeyboardButton(text, url=str(item["url"]), **kwargs))
            elif item.get("callback_data") is not None:
                buttons.append(
                    InlineKeyboardButton(
                        text, callback_data=str(item["callback_data"]), **kwargs
                    )
                )
            elif isinstance(item.get("web_app"), dict) and item["web_app"].get("url"):
                from telegram import WebAppInfo
                buttons.append(
                    InlineKeyboardButton(
                        text, web_app=WebAppInfo(str(item["web_app"]["url"])), **kwargs
                    )
                )
        if buttons:
            keyboard.append(buttons)
    return InlineKeyboardMarkup(keyboard) if keyboard else None


def merge_reply_markups(*markups) -> InlineKeyboardMarkup | None:
    """Stack inline keyboards; keep earlier (original) rows first."""
    rows = []
    for markup in markups:
        if not markup:
            continue
        for row in getattr(markup, "inline_keyboard", None) or ():
            rows.append(list(row))
    return InlineKeyboardMarkup(rows) if rows else None


def forward_channel_source(message) -> tuple[int, int] | None:
    """Return (channel_chat_id, message_id) when message is a channel forward.

    Hypothesis: Telegram Bot API omits reply_markup on forwarded messages, so the
    only reliable recovery path is copy_message from MessageOriginChannel.
    MessageOriginChat/User do not expose a recoverable source message_id.
    """
    message = getattr(message, "original_message", message)
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    try:
        from telegram import MessageOriginChannel
        if not isinstance(origin, MessageOriginChannel):
            return None
    except ImportError:
        if type(origin).__name__ != "MessageOriginChannel":
            return None
    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)
    if chat is None or message_id is None:
        return None
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    return int(chat_id), int(message_id)


async def capture_buttons_resolving(bot, message, scratch_chat_id: int) -> str:
    """Serialize inline buttons; recover them from channel forwards via copy_message.

    If reply_markup is already present, capture it directly. Otherwise, when the
    message is a channel forward, temporarily copy the original into scratch_chat_id
    (the admin private chat), capture buttons from the returned Message, then delete
    the temporary copy.
    """
    raw = capture_buttons(message)
    if raw not in ("[]", ""):
        return raw
    source = forward_channel_source(message)
    if not source or not scratch_chat_id:
        return raw or "[]"
    from_chat_id, message_id = source
    temp = None
    try:
        temp = await bot.copy_message(
            chat_id=int(scratch_chat_id),
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
        recovered = capture_buttons(temp)
        return recovered if recovered else (raw or "[]")
    except Exception:
        return raw or "[]"
    finally:
        if temp is not None:
            try:
                await bot.delete_message(int(scratch_chat_id), int(temp.message_id))
            except Exception:
                pass
