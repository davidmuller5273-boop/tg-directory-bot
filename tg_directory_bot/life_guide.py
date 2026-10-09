"""📖 人生指南：节选自《高性价比人生指南》(CC BY 4.0)。

数据：tg_directory_bot/data/life_guide.json（scripts/build_life_guide.py 生成）。
回调：life:home | life:ch:<页> | life:c:<节>:<页> | life:i:<节>:<条> | life:rand | life:help
"""
from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

DATA_PATH = Path(__file__).resolve().parent / "data" / "life_guide.json"
MENU_BUTTON_TEXT = "📖 人生指南"
KEYWORDS = {"人生指南"}
CHAPTERS_PER_PAGE = 10
ITEMS_PER_PAGE = 8
GRADE_TEXT = {"A": "A（证据很硬）", "B": "B（证据较好）", "C": "C（证据一般）"}


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chapters": []}


def chapters() -> list[dict[str, Any]]:
    return list(load().get("chapters") or [])


def chapter(cid: int) -> dict[str, Any] | None:
    return next((c for c in chapters() if int(c["id"]) == int(cid)), None)


def attribution() -> str:
    data = load()
    version = str(data.get("version") or "").split(" ")[-1]
    return (
        "内容节选自《高性价比人生指南》\n"
        f"{data.get('source', 'https://github.com/eternity4719/HowToLiveBetter')}\n"
        f"许可：CC BY 4.0（{data.get('license_url', 'https://creativecommons.org/licenses/by/4.0/')}）\n"
        f"本机器人只摘录了每条的成本、说人话和证据等级并重新排版"
        + (f"，同步于 {version}" if version else "")
        + "。"
    )


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="nav:main")]


def _pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"{prefix}:{page - 1}"))
    if pages > 1:
        row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"{prefix}:{page}"))
    if page < pages - 1:
        row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"{prefix}:{page + 1}"))
    return row


def home_view(page: int = 0, *, private: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    items = chapters()
    total_items = sum(len(c.get("items") or []) for c in items)
    pages = max(1, -(-len(items) // CHAPTERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    text = (
        f"{MENU_BUTTON_TEXT}\n\n"
        f"共 {len(items)} 节、{total_items} 条建议：每条写明花掉什么、换回什么、证据有多硬。\n"
        "点下面的分类查看，或者随机看一条。"
    )
    rows = [[InlineKeyboardButton("🎲 随机一条", callback_data="life:rand"),
             InlineKeyboardButton("❓ 使用帮助", callback_data="life:help")]]
    for c in items[page * CHAPTERS_PER_PAGE:(page + 1) * CHAPTERS_PER_PAGE]:
        rows.append([InlineKeyboardButton(
            f"{c['id']}. {c['title']}"[:60], callback_data=f"life:c:{c['id']}:0",
        )])
    pager = _pager("life:ch", page, pages)
    if pager:
        rows.append(pager)
    if private:
        rows.append(_back_row())
    return text, InlineKeyboardMarkup(rows)


def chapter_view(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    c = chapter(cid)
    if not c:
        return home_view()
    items = list(c.get("items") or [])
    pages = max(1, -(-len(items) // ITEMS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    intro = str(c.get("intro") or "")
    if len(intro) > 600:
        intro = intro[:600].rstrip() + "…"
    text = f"📖 第 {c['id']} 节 · {c['title']}\n\n{intro}\n\n共 {len(items)} 条，点标题查看："
    rows = [[InlineKeyboardButton(
        f"{item['n']}. {item['title']}"[:60], callback_data=f"life:i:{c['id']}:{item['n']}",
    )] for item in items[page * ITEMS_PER_PAGE:(page + 1) * ITEMS_PER_PAGE]]
    pager = _pager(f"life:c:{c['id']}", page, pages)
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton("📚 返回分类", callback_data=f"life:ch:{(chapters().index(c)) // CHAPTERS_PER_PAGE}"),
                 InlineKeyboardButton("🎲 随机一条", callback_data="life:rand")])
    return text, InlineKeyboardMarkup(rows)


def item_text(c: dict[str, Any], item: dict[str, Any]) -> str:
    lines = [f"📖 第 {c['id']} 节 · {c['title']}", "", f"{item['n']}. {item['title']}", ""]
    if item.get("plain"):
        lines.append(f"💡 说人话：{item['plain']}")
    if item.get("cost"):
        lines.append(f"💰 成本：{item['cost']}")
    if item.get("grade"):
        lines.append(f"📊 证据等级：{GRADE_TEXT.get(item['grade'], item['grade'])}")
    lines.extend(["", f"出处：《高性价比人生指南》CC BY 4.0 · {load().get('web', '')}"])
    text = "\n".join(lines)
    return text[:4000]


def item_view(cid: int, n: int) -> tuple[str, InlineKeyboardMarkup]:
    c = chapter(cid)
    if not c:
        return home_view()
    items = list(c.get("items") or [])
    index = next((i for i, it in enumerate(items) if int(it["n"]) == int(n)), None)
    if index is None:
        return chapter_view(cid)
    item = items[index]
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一条", callback_data=f"life:i:{cid}:{items[index - 1]['n']}"))
    if index < len(items) - 1:
        nav.append(InlineKeyboardButton("下一条 ➡️", callback_data=f"life:i:{cid}:{items[index + 1]['n']}"))
    rows = [nav] if nav else []
    rows.append([
        InlineKeyboardButton("📑 本节目录", callback_data=f"life:c:{cid}:{index // ITEMS_PER_PAGE}"),
        InlineKeyboardButton("🎲 随机一条", callback_data="life:rand"),
    ])
    rows.append([InlineKeyboardButton("📚 全部分类", callback_data="life:home")])
    return item_text(c, item), InlineKeyboardMarkup(rows)


def random_view(rng: Any = None) -> tuple[str, InlineKeyboardMarkup]:
    rng = rng or random.SystemRandom()
    pool = [(c, it) for c in chapters() for it in (c.get("items") or [])]
    if not pool:
        return "人生指南内容暂不可用。", InlineKeyboardMarkup([])
    c, it = rng.choice(pool)
    return item_view(int(c["id"]), int(it["n"]))


HELP_TEXT = (
    "❓ 人生指南 · 使用帮助\n\n"
    "• 打开方式：主菜单「📖 人生指南」，或发送 /life、/rensheng，或直接发送「人生指南」（私聊和群里都可以）。\n"
    "• 点分类进入一节，再点标题看具体建议；用「上一条 / 下一条」翻看。\n"
    "• 「🎲 随机一条」随手看一条建议。\n"
    "• 每条包含：说人话（结论）、成本（花多少钱和时间）、证据等级（A 最硬，C 一般）。\n"
    "• 只是节选，完整内容和原始文献请看原书。\n\n"
)


def help_view() -> tuple[str, InlineKeyboardMarkup]:
    return HELP_TEXT + attribution(), InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 返回人生指南", callback_data="life:home")],
    ])


def is_keyword(text: str) -> bool:
    return " ".join((text or "").split()) in KEYWORDS


def view_for(data: str, *, private: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    parts = (data or "").split(":")
    try:
        if data == "life:home" or len(parts) < 2:
            return home_view(private=private)
        if parts[1] == "ch":
            return home_view(int(parts[2]), private=private)
        if parts[1] == "c":
            return chapter_view(int(parts[2]), int(parts[3]) if len(parts) > 3 else 0)
        if parts[1] == "i":
            return item_view(int(parts[2]), int(parts[3]))
        if parts[1] == "rand":
            return random_view()
        if parts[1] == "help":
            return help_view()
    except (ValueError, IndexError):
        pass
    return home_view(private=private)


async def handle_callback(update: Any, context: Any) -> None:
    query = update.callback_query
    chat = update.effective_chat
    private = bool(chat and str(chat.type) == "private")
    text, markup = view_for(str(query.data or ""), private=private)
    try:
        await query.answer()
    except TelegramError:
        pass
    try:
        await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramError as exc:
        if "not modified" in str(exc).lower() or query.message is None:
            return
        await query.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


async def send_home(message: Any, *, private: bool = True) -> Any:
    text, markup = home_view(private=private)
    return await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
