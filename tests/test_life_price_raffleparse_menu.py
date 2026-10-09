"""人生指南、币价、抽奖识别、分类菜单。"""
import asyncio
import json
import random
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from tg_directory_bot import bot as botmod
from tg_directory_bot import crypto_price as cp
from tg_directory_bot import life_guide as lg
from tg_directory_bot import raffle_parse as rp
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.time_utils import BEIJING_TZ

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = """📜 规则：
1. 本群每日晚上 9 点自动开奖
2. 中奖后请在 24 小时内联系管理员领奖，逾期视为放弃
3. 小号、广告号中奖无效

每日活动福利🎁
├活动类型: 通用抽奖
├定时开奖: 2026-10-08 21:00:00 +0800
├关注频道: @meirifuli
├最低发言: 10 条
├最低助推: 1
├最少参与: 5 人（不足自动顺延一天）
├已参与: 12 人
├奖品列表:
  ├ 10U x 3
  ├ 5U x 2
[如何参与？]
当天发言达到 10 条即自动参与，无需点击按钮。"""

USER_SAMPLE = """抽奖 #37

📜 规则：
1、关注频道: @jiuyecc (https://t.me/jiuyecc)
2、助推群2次。
3、一共5个88也可以当日有充值500的领取
4、有充值的开奖前必须找 @huanhuan (https://t.me/huanhuan) 报备否者无效。
5、所有奖品必须当日23点领取，过时不侯

每日活动福利🎁
├活动类型: 通用抽奖
├定时开奖: 2026-10-09 21:22:00 +0800
├关注频道: @jiuyecc (https://t.me/jiuyecc)
├最低发言: 9 条
├最低助推: 2
├最少参与: 39 人（不足自动顺延一天）
├已参与: 25 人
├奖品列表:
  ├ 5*88RMB x 5
[如何参与？]
1、先关注频道 @jiuyecc (https://t.me/jiuyecc)  。2、群助推两次。3、群内发言 9条自动参加。"""

USER_RULES = [
    "1、关注频道: @jiuyecc (https://t.me/jiuyecc)",
    "2、助推群2次。",
    "3、一共5个88也可以当日有充值500的领取",
    "4、有充值的开奖前必须找 @huanhuan (https://t.me/huanhuan) 报备否者无效。",
    "5、所有奖品必须当日23点领取，过时不侯",
]
USER_HOW_TO = "1、先关注频道 @jiuyecc (https://t.me/jiuyecc)  。2、群助推两次。3、群内发言 9条自动参加。"

RULES = [
    "1. 本群每日晚上 9 点自动开奖",
    "2. 中奖后请在 24 小时内联系管理员领奖，逾期视为放弃",
    "3. 小号、广告号中奖无效",
]


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def callbacks(markup):
    return [b.callback_data for b in buttons(markup) if b.callback_data]


# ---------------------------------------------------------------- 人生指南

class LifeGuideTest(unittest.TestCase):
    def test_data_bundled_with_attribution(self):
        data = lg.load()
        self.assertEqual(len(data["chapters"]), 34)
        self.assertGreater(sum(len(c["items"]) for c in data["chapters"]), 600)
        self.assertIn("github.com/eternity4719/HowToLiveBetter", lg.attribution())
        self.assertIn("CC BY 4.0", lg.attribution())

    def test_navigation_and_callback_sizes(self):
        text, markup = lg.home_view()
        self.assertIn("📖 人生指南", text)
        data = callbacks(markup)
        self.assertIn("life:rand", data)
        self.assertIn("life:help", data)
        self.assertIn("life:ch:1", data)
        self.assertIn("nav:main", data)
        self.assertNotIn("nav:main", callbacks(lg.home_view(private=False)[1]))
        for chapter in lg.chapters():
            pages = -(-len(chapter["items"]) // lg.ITEMS_PER_PAGE)
            for page in range(pages):
                _t, m = lg.chapter_view(chapter["id"], page)
                for cb in callbacks(m):
                    self.assertLessEqual(len(cb.encode()), 64)
            for item in chapter["items"]:
                text, m = lg.item_view(chapter["id"], item["n"])
                self.assertLessEqual(len(text), 4096)
                self.assertIn(item["title"], text)

    def test_item_paging_and_random(self):
        text, markup = lg.item_view(1, 1)
        self.assertIn("系安全带", text)
        self.assertIn("💡 说人话", text)
        self.assertIn("📊 证据等级：A", text)
        self.assertIn("life:i:1:2", callbacks(markup))
        a = lg.random_view(random.Random(7))[0]
        b = lg.random_view(random.Random(7))[0]
        self.assertEqual(a, b)

    def test_view_for_and_help(self):
        self.assertIn("使用帮助", lg.view_for("life:help")[0])
        self.assertIn("📖 人生指南", lg.view_for("life:bogus:x")[0])
        self.assertIn("第 2 节", lg.view_for("life:c:2:0")[0])
        self.assertTrue(lg.is_keyword(" 人生指南 "))
        self.assertFalse(lg.is_keyword("人生指南呢"))

    def test_help_text_single_line(self):
        lines = [line for line in botmod.HELP_TEXT.splitlines() if "人生指南" in line]
        self.assertEqual(len(lines), 1)


# ---------------------------------------------------------------- 币价

class FakeFetch:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url == cp.FX_URL:
            return {"rates": {"CNY": 7.1}}
        inst = params["instId"]
        if inst == "BTC-USDT":
            return {"code": "0", "data": [{"last": "100000", "open24h": "95000",
                                           "high24h": "101000", "low24h": "94000"}]}
        return {"code": "51001", "data": [], "msg": "Instrument ID does not exist"}


class CryptoPriceTest(unittest.TestCase):
    def test_symbol_detection(self):
        symbols = cp.known_symbols("NOT  abc")
        self.assertEqual(cp.bare_symbol("btc", symbols), "BTC")
        self.assertEqual(cp.bare_symbol(" ETH ", symbols), "ETH")
        self.assertEqual(cp.bare_symbol("NOT", symbols), "NOT")
        self.assertEqual(cp.bare_symbol("op", symbols), "")          # 容易误触的短词需大写
        self.assertEqual(cp.bare_symbol("OP", symbols), "OP")
        self.assertEqual(cp.bare_symbol("hello", symbols), "")
        self.assertEqual(cp.bare_symbol("USDT", symbols), "")
        self.assertEqual(cp.parse_query("币价 btc"), "BTC")
        self.assertEqual(cp.parse_query("币价：pepe"), "PEPE")
        for address in ("TJRabPrwbZy45sbavfcjinPJC18kjpRTv8", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                        "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
                        "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"):
            self.assertTrue(cp.is_address(address))
            self.assertEqual(cp.bare_symbol(address, symbols), "")
            self.assertEqual(cp.parse_query("币价 " + address), "")

    def test_quote_with_c2c_rate_and_cache(self):
        fetch = FakeFetch()
        now = [100.0]

        async def c2c():
            return Decimal("7.25")
        service = cp.PriceService(fetch_json=fetch, c2c_rate=c2c, clock=lambda: now[0])
        quote = asyncio.run(service.quote("btc"))
        text = cp.quote_text(quote, datetime(2026, 10, 9, 12, 0, tzinfo=BEIJING_TZ))
        self.assertIn("💹 BTC/USDT（OKX 现货）", text)
        self.assertIn("最新价：100,000.00 USDT", text)
        self.assertIn("¥725,000.00（OKX C2C 1 USDT≈¥7.25）", text)
        self.assertIn("24h 涨跌：+5.26% 📈", text)
        asyncio.run(service.quote("BTC"))
        self.assertEqual(len(fetch.calls), 1)          # 10 秒内走缓存
        now[0] += 11
        asyncio.run(service.quote("BTC"))
        self.assertEqual(len(fetch.calls), 2)

    def test_fallback_rate_and_errors(self):
        fetch = FakeFetch()

        async def broken():
            raise RuntimeError("down")
        service = cp.PriceService(fetch_json=fetch, c2c_rate=broken)
        quote = asyncio.run(service.quote("BTC"))
        self.assertEqual(quote.rate_source, "美元汇率")
        self.assertIn("美元汇率", cp.quote_text(quote))
        with self.assertRaises(cp.PriceError) as ctx:
            asyncio.run(service.quote("ZZZZ"))
        self.assertIn("没有 ZZZZ/USDT", str(ctx.exception))
        with self.assertRaises(cp.PriceError):
            asyncio.run(service.quote("USDT"))

    def test_format_small_prices(self):
        self.assertEqual(cp.format_price(Decimal("0.00001234567")), "0.0000123457")
        self.assertEqual(cp.format_price(Decimal("2.5")), "2.5")

    def test_group_toggle_default_on(self):
        self.assertTrue(cp.group_enabled({}, -1))
        self.assertFalse(cp.group_enabled({"price_enabled:-1": "0"}, -1))


# ---------------------------------------------------------------- 抽奖识别

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=BEIJING_TZ)


class RaffleParseTest(unittest.TestCase):
    def test_sample_exact(self):
        parsed = rp.parse(SAMPLE, now=NOW)
        self.assertEqual(parsed.title, "每日活动福利🎁")
        self.assertEqual(parsed.rules, RULES)
        self.assertEqual(parsed.raffle_type, "通用抽奖")
        # 原时间已过 → 顺延到下一次 21:00
        self.assertEqual(parsed.draw_at, datetime(2026, 10, 9, 21, 0, tzinfo=BEIJING_TZ))
        self.assertTrue(parsed.draw_adjusted)
        self.assertTrue(parsed.daily_hint)
        self.assertEqual(parsed.channel, "@meirifuli")
        self.assertEqual((parsed.messages, parsed.boosts, parsed.min_participants), (10, 1, 5))
        self.assertEqual(parsed.prizes, [(3, "10U"), (2, "5U")])
        self.assertEqual(parsed.how_to, "当天发言达到 10 条即自动参与，无需点击按钮。")
        self.assertEqual(parsed.warnings, [])
        self.assertTrue(rp.looks_like_raffle(SAMPLE))

    def test_answers_build_raffle_identical_text(self):
        parsed = rp.parse(SAMPLE, now=datetime.now(BEIJING_TZ) - timedelta(days=400))
        parsed.draw_at = datetime.now(BEIJING_TZ) + timedelta(hours=2)
        answers = rp.to_answers(parsed)
        self.assertEqual(answers[1], "\n".join(RULES))
        self.assertEqual(answers[4], "频道 @meirifuli\n发言 10\n助推 1")
        self.assertEqual(answers[6], "3*10U\n2*5U")
        self.assertEqual(answers[7], "是")
        self.assertEqual(answers[8], "5")
        ends_at, winners, prize, extras = botmod.build_raffle_extras_from_pro(answers)
        self.assertEqual(winners, 5)
        self.assertEqual(prize, "3*10U | 2*5U")
        self.assertEqual(json.loads(extras["rules_json"]), RULES)
        row = dict(extras, raffle_type="universal", ends_at=ends_at, prize=prize,
                   winner_count=winners, entries=0)

        class Row(dict):
            def keys(self):
                return super().keys()
        text = botmod.raffle_text(Row(row), show_count=False)
        self.assertEqual(text.count("📜 规则"), 1)       # 规则标题不重复
        for line in RULES + ["每日活动福利🎁", "├活动类型: 通用抽奖", "├关注频道: @meirifuli",
                             "├最低发言: 10 条", "├最低助推: 1", "├最少参与: 5 人（不足自动顺延一天）",
                             "  ├ 10U x 3", "  ├ 5U x 2", "当天发言达到 10 条即自动参与，无需点击按钮。"]:
            self.assertIn(line, text)
        # 再识别一次机器人自己的公告，结果不变（含“已参与”被忽略）
        again = rp.parse(text.replace("&amp;", "&"), now=NOW)
        self.assertEqual((again.title, again.rules, again.prizes), (parsed.title, RULES, parsed.prizes))

    def test_user_sample_exact(self):
        parsed = rp.parse(USER_SAMPLE, now=NOW)
        self.assertEqual(parsed.title, "每日活动福利🎁")
        self.assertEqual(parsed.rules, USER_RULES)
        self.assertEqual(parsed.raffle_type, "通用抽奖")
        self.assertEqual(parsed.draw_at, datetime(2026, 10, 9, 21, 22, tzinfo=BEIJING_TZ))
        self.assertFalse(parsed.draw_adjusted)
        self.assertTrue(parsed.daily_hint)
        self.assertEqual(parsed.channel, "@jiuyecc")
        self.assertEqual((parsed.messages, parsed.boosts, parsed.min_participants), (9, 2, 39))
        self.assertEqual(parsed.prizes, [(5, "5*88RMB")])
        self.assertEqual(parsed.how_to, USER_HOW_TO)
        self.assertEqual(parsed.warnings, [])
        self.assertTrue(rp.looks_like_raffle(USER_SAMPLE))
        # 开奖时间已过 → 顺延到下一次 21:22（每日重复）
        later = rp.parse(USER_SAMPLE, now=datetime(2026, 10, 9, 22, 1, tzinfo=BEIJING_TZ))
        self.assertEqual(later.draw_at, datetime(2026, 10, 10, 21, 22, tzinfo=BEIJING_TZ))
        self.assertTrue(later.draw_adjusted)
        answers = rp.to_answers(parsed)
        self.assertEqual(answers[1], "\n".join(USER_RULES))
        self.assertEqual(answers[4], "频道 @jiuyecc\n发言 9\n助推 2")
        self.assertEqual(answers[5], USER_HOW_TO)
        self.assertEqual(answers[6], "5 | 5*88RMB")
        self.assertEqual(answers[7], "是")
        self.assertEqual(answers[8], "39")
        answers[2] = (datetime.now(BEIJING_TZ) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        ends_at, winners, prize, extras = botmod.build_raffle_extras_from_pro(answers)
        self.assertEqual((winners, prize), (5, "5*88RMB"))
        self.assertEqual(json.loads(extras["rules_json"]), USER_RULES)
        self.assertEqual(extras["how_to_join"], USER_HOW_TO)
        self.assertEqual((extras["min_messages"], extras["min_boosts"], extras["min_participants"],
                          extras["recur_daily"], extras["channel_ref"]), (9, 2, 39, 1, "@jiuyecc"))

        class Row(dict):
            pass
        text = botmod.raffle_text(Row(dict(extras, raffle_type="universal", ends_at=ends_at,
                                               prize=prize, winner_count=winners, entries=0)), False)
        self.assertEqual(text.count("规则"), 1)
        self.assertIn("  ├ 5*88RMB x 5", text)
        again = rp.parse(text.replace("&amp;", "&"), now=NOW)
        self.assertEqual((again.rules, again.prizes, again.how_to), (USER_RULES, [(5, "5*88RMB")], USER_HOW_TO))

    def test_loose_text(self):
        text = ("周末福利\n开奖时间：10月12日 20:30\n关注频道 https://t.me/abcd_channel\n"
                "最低发言 5 条\n助推 2\n最少参与 3 人\n奖品：100U 2名")
        parsed = rp.parse(text, now=NOW)
        self.assertEqual(parsed.title, "周末福利")
        self.assertEqual(parsed.draw_at, datetime(2026, 10, 12, 20, 30, tzinfo=BEIJING_TZ))
        self.assertFalse(parsed.draw_adjusted)
        self.assertEqual(parsed.channel, "@abcd_channel")
        self.assertEqual((parsed.messages, parsed.boosts, parsed.min_participants), (5, 2, 3))
        self.assertEqual(parsed.prizes, [(2, "100U")])
        self.assertFalse(parsed.daily_hint)
        self.assertEqual(rp.to_answers(parsed)[6], "2 | 100U")
        loose2 = rp.parse("定时开奖 21:00\n奖品\n1*iPhone\n2*红包", now=NOW)
        self.assertEqual(loose2.prizes, [(1, "iPhone"), (2, "红包")])
        self.assertEqual(loose2.draw_at.hour, 21)

    def test_missing_fields_reported(self):
        parsed = rp.parse("随便聊聊\n奖品：100U", now=NOW)
        self.assertIn("开奖时间", parsed.missing())
        self.assertIn("缺少：开奖时间", rp.summary_text(parsed, False))


def make_config(db_path, **kwargs):
    base = dict(bot_token="1:x", admin_ids={7}, super_admin_ids={7}, db_path=Path(db_path),
                categories=("other",), blocked_keywords=())
    base.update(kwargs)
    return Config(**base)


class FeatureFlowTest(unittest.TestCase):
    GROUP = -100777

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "db.sqlite3"
        self.store = DirectoryStore(db)
        self.store.init()
        self.store.record_group_activity(self.GROUP, "福利群", "", "supergroup", 1, "a", "A")
        self.config = make_config(db)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
            pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock(),
        )
        self.user_data = {}
        self.context = SimpleNamespace(
            bot=self.bot, user_data=self.user_data, args=[],
            application=SimpleNamespace(bot_data={"store": self.store, "config": self.config},
                                        create_task=lambda c: asyncio.ensure_future(c)),
        )

    def tearDown(self):
        self.temp.cleanup()

    def message(self, text):
        return SimpleNamespace(text=text, caption=None, reply_to_message=None, message_id=3,
                               reply_text=AsyncMock(), chat_id=7)

    def update_for(self, message, chat_type=ChatType.PRIVATE, chat_id=7):
        chat = SimpleNamespace(id=chat_id, type=chat_type)
        return SimpleNamespace(effective_message=message, effective_chat=chat,
                               effective_user=SimpleNamespace(id=7, username="admin", full_name="Admin"))

    def click(self, data):
        chat = SimpleNamespace(id=7, type=ChatType.PRIVATE)
        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=7, username="admin", full_name="Admin"),
            message=SimpleNamespace(chat=chat, reply_text=AsyncMock()),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=chat,
                                 effective_user=query.from_user, effective_message=query.message)
        handled = asyncio.run(botmod.feature_callback(update, self.context, data))
        return handled, query

    def test_paste_sample_then_confirm_creates_raffle(self):
        message = self.message(SAMPLE)
        with patch("tg_directory_bot.bot.pin_raffle_message", AsyncMock()):
            handled = asyncio.run(botmod.quick_text_features(self.update_for(message), self.context, SAMPLE, True))
            self.assertTrue(handled)
            summary = message.reply_text.await_args.args[0]
            self.assertIn("标题：每日活动福利🎁", summary)
            self.assertIn("发布到：福利群", summary)
            self.assertIn("每日重复：是", summary)
            markup = message.reply_text.await_args.kwargs["reply_markup"]
            self.assertIn("rparse:ok", callbacks(markup))
            self.assertIn("rparse:edit", callbacks(markup))
            self.assertIn("rparse:cancel", callbacks(markup))
            self.click("rparse:recur")
            self.assertFalse(self.user_data[botmod.RAFFLE_PARSE_KEY]["recur"])
            self.click("rparse:recur")
            handled, query = self.click("rparse:ok")
        self.assertTrue(handled)
        self.assertIn("已创建并发布到 福利群", query.edit_message_text.await_args.args[0])
        raffle = self.store.latest_universal_raffle(self.GROUP)
        self.assertEqual(raffle["title"], "每日活动福利🎁")
        self.assertEqual(int(raffle["recur_daily"]), 1)
        self.assertEqual(int(raffle["min_participants"]), 5)
        self.assertEqual(json.loads(raffle["rules_json"]), RULES)
        sent = self.bot.send_message.await_args.args[1]
        self.assertEqual(sent.count("📜 规则"), 1)

    def test_edit_prefills_wizard_and_last_template(self):
        message = self.message("识别抽奖\n" + SAMPLE)
        asyncio.run(botmod.quick_text_features(self.update_for(message), self.context, message.text, True))
        self.click("rparse:edit")
        self.assertEqual(self.user_data["menu_mode"], "raffle_pro")
        self.assertEqual(self.user_data["wizard_prefills"][0], "每日活动福利🎁")
        self.assertEqual(self.user_data["selected_group_id"], self.GROUP)
        # 用上次模板：没有记录时提示
        self.user_data.pop("menu_mode")
        _h, query = self.click("raffleplan:last")
        self.assertIn("还没有可用的上次模板", query.answer.await_args.args[0])

    def test_copy_and_last_template_prefill_future_time(self):
        past = (datetime.now(BEIJING_TZ) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        answers = rp.to_answers(rp.parse(SAMPLE, now=NOW))
        answers[2] = (datetime.now(BEIJING_TZ) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        ends_at, winners, prize, extras = botmod.build_raffle_extras_from_pro(answers)
        extras["template_json"] = extras["template_json"].replace(answers[2], past)
        raffle_id = self.store.create_raffle(self.GROUP, 7, prize, winners, ends_at, "universal", "", 0, **extras)
        self.user_data["selected_group_id"] = self.GROUP
        self.click("raffleplan:last")
        self.assertEqual(self.user_data["menu_mode"], "raffle_pro")
        draw = self.user_data["wizard_prefills"][2]
        self.assertGreater(datetime.strptime(draw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ),
                           datetime.now(BEIJING_TZ))
        self.user_data.pop("menu_mode")
        _h, query = self.click("rafflecopy:menu:0")
        self.assertIn(f"rafflecopy:item:{raffle_id}", callbacks(query.edit_message_text.await_args.kwargs["reply_markup"]))
        self.click(f"rafflecopy:item:{raffle_id}")
        self.assertEqual(self.user_data["wizard_action_title"], f"复制抽奖 #{raffle_id} 为新抽奖")

    def test_group_reply_recognition_needs_permission(self):
        reply = SimpleNamespace(text=SAMPLE, caption=None)
        message = self.message("识别抽奖")
        message.reply_to_message = reply
        update = self.update_for(message, ChatType.SUPERGROUP, self.GROUP)
        update.effective_user = SimpleNamespace(id=99, username="x", full_name="X")
        asyncio.run(botmod.quick_text_features(update, self.context, "识别抽奖", False))
        self.assertIn("没有本群的抽奖管理权限", message.reply_text.await_args.args[0])

    def test_life_keyword_and_price_toggle(self):
        message = self.message("人生指南")
        asyncio.run(botmod.quick_text_features(
            self.update_for(message, ChatType.SUPERGROUP, self.GROUP), self.context, "人生指南", False))
        self.assertIn("📖 人生指南", message.reply_text.await_args.args[0])
        self.store.set_setting(f"price_enabled:{self.GROUP}", "0")
        message = self.message("BTC")
        handled = asyncio.run(botmod.quick_text_features(
            self.update_for(message, ChatType.SUPERGROUP, self.GROUP), self.context, "BTC", False))
        self.assertFalse(handled)
        message.reply_text.assert_not_awaited()
        self.store.set_setting(f"price_enabled:{self.GROUP}", "1")
        fetch = FakeFetch()
        self.context.application.bot_data[botmod.PRICE_SERVICE_KEY] = cp.PriceService(fetch_json=fetch)
        handled = asyncio.run(botmod.quick_text_features(
            self.update_for(message, ChatType.SUPERGROUP, self.GROUP), self.context, "BTC", False))
        self.assertTrue(handled)
        self.assertIn("BTC/USDT", message.reply_text.await_args.args[0])
        # 地址不被当成币种
        addr = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        self.assertFalse(asyncio.run(botmod.quick_text_features(
            self.update_for(self.message(addr)), self.context, addr, True)))
        # 群设置开关
        self.user_data["selected_group_id"] = self.GROUP
        _h, query = self.click("price:group:off")
        self.assertEqual(self.store.get_settings()[f"price_enabled:{self.GROUP}"], "0")
        self.assertIn("已关闭", query.edit_message_text.await_args.args[0])

    def test_wizard_mode_not_hijacked(self):
        self.user_data["menu_mode"] = "raffle_pro"
        message = self.message("BTC")
        self.assertFalse(asyncio.run(botmod.quick_text_features(
            self.update_for(message), self.context, "BTC", True)))


# ---------------------------------------------------------------- 分类菜单

OLD_CALLBACKS = {
    # 旧主菜单
    "nav:search", "submit:start", "nav:group", "nav:admin", "stk:menu", "menu:help", "clone:start",
    # 旧搜索菜单
    "search:prompt", "menu:list", "menu:my", "tron:prompt", "tronmonitor:menu", "account:prompt",
    "searchstats:keywords:0",
    # 旧群组管理菜单
    "group:stats", "group:active", "group:raffles", "group:lottery", "group:polls", "group:ads",
    "group:points", "group:joincfg", "quickpost:menu", "invite:menu", "group:recent:bot:0",
    "group:recent:group:0", "group:moderation", "group:renamehist", "group:permissions",
    # 旧管理员菜单
    "admin:badwords", "admin:admins", "admin:buttons", "admin:stats", "channelbroadcast:menu",
    "admin:pending", "admin:notes", "admin:clones", "clonetree:0", "admin:usage:0",
    "admin:tronmonitors:0",
}


def menu_children(cb, store):
    """Static views reached by a callback (only menu-type pages)."""
    allp = set(botmod.GROUP_PERMISSIONS)
    table = {
        "nav:search": lambda: botmod.search_menu_keyboard(True),
        "nav:admin": lambda: botmod.admin_menu_keyboard(True, True),
        "nav:group": lambda: botmod.group_menu_keyboard(True, True, allp),
        "nav:groupmenu": lambda: botmod.group_menu_keyboard(True, True, allp),
        "cat:tron": lambda: botmod.tron_category_view()[1],
        "cat:clone": lambda: botmod.clone_category_view(True, True)[1],
        "price:menu": lambda: botmod.price_menu_view(store, True)[1],
        "price:group": lambda: botmod.group_price_view(store, -1)[1],
        "life:home": lambda: lg.home_view()[1],
        "group:raffles": lambda: botmod.raffle_type_keyboard(),
        "raffletype:universal": lambda: botmod.raffle_plan_keyboard(),
        "nav:main": lambda: botmod.main_keyboard(True, True, True, True),
    }
    for cat in botmod.CATEGORY_TITLES:
        table[f"cat:{cat}"] = (lambda c=cat: botmod.category_view(c, "群", allp)[1])
    maker = table.get(cb)
    return maker() if maker else None


class MenuTreeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DirectoryStore(Path(self.temp.name) / "db.sqlite3")
        self.store.init()
        sources = [ROOT / "tg_directory_bot" / name for name in
                   ("bot.py", "sticker_clone.py", "life_guide.py")]
        self.source = "\n".join(p.read_text(encoding="utf-8") for p in sources)

    def tearDown(self):
        self.temp.cleanup()

    def handled(self, cb):
        if f'"{cb}"' in self.source:
            return True
        for index, char in enumerate(cb):
            if char == ":" and (f'"{cb[:index + 1]}' in self.source or f"'{cb[:index + 1]}" in self.source):
                return True
        return False

    def walk(self):
        seen, queue = set(), ["nav:main"]
        while queue:
            cb = queue.pop()
            if cb in seen:
                continue
            seen.add(cb)
            markup = menu_children(cb, self.store)
            if markup is None:
                continue
            for child in callbacks(markup):
                queue.append(child)
        return seen

    def test_all_previous_callbacks_reachable(self):
        reachable = self.walk()
        missing = OLD_CALLBACKS - reachable
        self.assertEqual(missing, set())
        for new in ("cat:raffle", "cat:points", "cat:dice", "cat:ads", "cat:tron", "cat:clone",
                    "price:menu", "life:home", "rparse:start", "raffleplan:last",
                    "rafflecopy:menu:0", "points:dice:menu", "price:group"):
            self.assertIn(new, reachable)

    def test_no_dead_buttons(self):
        dead = sorted(cb for cb in self.walk() if not self.handled(cb))
        self.assertEqual(dead, [])

    def test_every_submenu_has_back(self):
        allp = set(botmod.GROUP_PERMISSIONS)
        views = [botmod.category_view(c, "群", allp)[1] for c in botmod.CATEGORY_TITLES]
        views += [botmod.category_view(c, "群", set())[1] for c in botmod.CATEGORY_TITLES]
        views += [botmod.tron_category_view()[1], botmod.clone_category_view(False, True)[1],
                  botmod.price_menu_view(self.store, False)[1]]
        for markup in views:
            self.assertTrue(buttons(markup)[-1].text.startswith("⬅️ 返回"))

    def test_developer_entries_hidden(self):
        regular = callbacks(botmod.main_keyboard(True, True, True, False))
        self.assertNotIn("admin:notes", regular)
        self.assertNotIn("cat:clone", regular)
        dev = callbacks(botmod.main_keyboard(True, True, True, True))
        self.assertIn("admin:notes", dev)
        self.assertIn("cat:clone", dev)

    def test_help_doc_in_sync_and_no_fairness_mention(self):
        doc = (ROOT / "使用帮助.md").read_text(encoding="utf-8")
        self.assertIn(botmod.HELP_TEXT, doc)
        for text in (botmod.HELP_TEXT, doc, lg.HELP_TEXT, cp.HELP_TEXT):
            for word in ("新人", "平衡", "近 30 天", "三分之一", "加权"):
                self.assertNotIn(word, text)

    def test_category_permission_filter(self):
        text, markup = botmod.category_view("raffle", "群", {"lottery"})
        data = callbacks(markup)
        self.assertNotIn("group:raffles", data)
        self.assertIn("group:lottery", data)
        text, markup = botmod.category_view("ads", "群", set())
        self.assertIn("没有此类功能的管理权限", text)


if __name__ == "__main__":
    unittest.main()
