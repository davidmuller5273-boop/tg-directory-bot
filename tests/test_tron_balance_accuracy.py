"""Regression tests: TRON monitor alerts must show the exact, verified balance
of the monitored address — never a TRX-valued figure, a fake token, a cached
value, or another monitor's balance."""
from decimal import Decimal
import asyncio
import itertools
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from tg_directory_bot.bot import (
    TRON_BALANCE_UNAVAILABLE,
    poll_tron_monitors,
    tron_monitor_alert_view,
)
from tg_directory_bot.chain import (
    ChainQueryError,
    ChainService,
    TronBalance,
    TronTransaction,
    USDT_TRC20_CONTRACT,
    tronscan_usdt_balance,
)
from tg_directory_bot.storage import DirectoryStore

ADDR_A = "TWjobpgNnsp9XhNvv1cZE1qY7Hdzw3hjLg"
ADDR_B = "TSNTk2X6LP7MHZ5QZomV4xsDicLWfwkkYM"
ADDR_C = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
FAKE_USDT = "TXFakeUsdtTokenContractAddr1234567"

# Real-world shape of the TronScan account payload that caused the bug:
# `amount` is the USDT value priced in TRX (7375.45 × ~2.98566 = 22020.58).
TRONSCAN_ACCOUNT = {
    "address": ADDR_A,
    "activated": True,
    "balance": 12_586_812_345,
    "withPriceTokens": [
        {"tokenId": "_", "tokenAbbr": "trx", "balance": "12586812345",
         "tokenDecimal": 6, "amount": "12586.812345"},
        {"tokenId": FAKE_USDT, "tokenAbbr": "USDT", "tokenName": "Tether USD",
         "balance": "999999000000", "tokenDecimal": 6, "amount": "5"},
        {"tokenId": USDT_TRC20_CONTRACT, "tokenAbbr": "USDT",
         "tokenName": "Tether USD", "balance": "7375450000", "tokenDecimal": 6,
         "amount": "22020.58", "tokenPriceInTrx": 2.98566},
    ],
}


def service() -> ChainService:
    return ChainService(SimpleNamespace(
        trongrid_api_key="", trongrid_url="https://grid.invalid",
        tronscan_api_url="https://scan.invalid", tronscan_api_key="",
        oklink_api_key="", tokenview_api_key="",
    ))


def usdt_hex(amount_units: int) -> str:
    return format(amount_units, "064x")


class FakeClient:
    """Routes httpx calls to a handler(method, url, body) -> (status, payload)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _respond(self, method, url, body):
        self.calls.append((method, url, body))
        status, payload = self.handler(method, url, body)
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    async def get(self, url, params=None, headers=None, **_):
        return self._respond("GET", url, dict(params or {}))

    async def post(self, url, json=None, headers=None, **_):
        return self._respond("POST", url, dict(json or {}))


def node_handler(accounts, *, head=10**9, indexer=None, indexer_status=200):
    """accounts: address -> (trx_sun, usdt_units) or callable returning it."""
    indexer = indexer if indexer is not None else accounts

    def value(table, address):
        item = table.get(address)
        return item() if callable(item) else item

    def handler(method, url, body):
        if url.endswith("/wallet/getnowblock"):
            number = head() if callable(head) else head
            return 200, {"block_header": {"raw_data": {"number": number}}}
        if url.endswith("/wallet/getaccount"):
            item = value(accounts, body["address"])
            if item is None:
                return 200, {}
            return 200, {"address": body["address"], "balance": item[0]}
        if url.endswith("/wallet/triggerconstantcontract"):
            item = value(accounts, body["owner_address"]) or (0, 0)
            return 200, {"result": {"result": True},
                         "constant_result": [usdt_hex(item[1])]}
        if "/v1/accounts/" in url:
            if indexer_status != 200:
                return indexer_status, {}
            address = url.rsplit("/", 1)[-1]
            item = value(indexer, address)
            if item is None:
                return 200, {"data": []}
            return 200, {"data": [{
                "address": address, "balance": item[0],
                "trc20": [{USDT_TRC20_CONTRACT: str(item[1])}],
            }]}
        return 404, {}
    return handler


def run_with_client(handler, coro_factory):
    client = FakeClient(handler)

    async def no_sleep(*_args, **_kwargs):
        return None

    with patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=client), \
            patch("tg_directory_bot.chain.asyncio.sleep", no_sleep):
        return asyncio.run(coro_factory()), client


class TronscanParsingTest(unittest.TestCase):
    def test_amount_field_is_trx_value_not_usdt_quantity(self):
        self.assertEqual(tronscan_usdt_balance(TRONSCAN_ACCOUNT), Decimal("7375.45"))

    def test_fake_usdt_symbol_is_ignored(self):
        account = {"withPriceTokens": [TRONSCAN_ACCOUNT["withPriceTokens"][1]]}
        self.assertEqual(tronscan_usdt_balance(account), Decimal("0"))

    def test_monitor_and_query_tronscan_fallbacks_report_real_quantity(self):
        chain = service()

        def handler(method, url, body):
            if "/api/account" in url:
                return 200, TRONSCAN_ACCOUNT
            return 404, {}

        balance, _ = run_with_client(
            handler, lambda: chain._tronscan_monitor_balance(ADDR_A)
        )
        self.assertEqual(balance.usdt, Decimal("7375.45"))
        self.assertEqual(balance.trx, Decimal("12586.812345"))

        chain._tron_authorization_status = AsyncMock(side_effect=ChainQueryError("x"))
        balance, _ = run_with_client(handler, lambda: chain._tronscan_balance(ADDR_A))
        self.assertEqual(balance.usdt, Decimal("7375.45"))

    def test_tronscan_payload_for_another_address_is_rejected(self):
        chain = service()

        def handler(method, url, body):
            return 200, dict(TRONSCAN_ACCOUNT, address=ADDR_B)

        with self.assertRaises(ChainQueryError):
            run_with_client(handler, lambda: chain._tronscan_monitor_balance(ADDR_A))

    def test_monitor_balance_falls_back_to_node_before_tronscan(self):
        chain = service()
        accounts = {ADDR_A: (12_586_812_345, 7_375_450_000)}
        base = node_handler(accounts, indexer_status=429)

        def handler(method, url, body):
            if "/api/account" in url:
                raise AssertionError("TronScan must not be used when the node answers")
            return base(method, url, body)

        balance, _ = run_with_client(handler, lambda: chain.tron_monitor_balance(ADDR_A))
        self.assertEqual((balance.trx, balance.usdt),
                         (Decimal("12586.812345"), Decimal("7375.45")))


class VerifiedBalanceTest(unittest.TestCase):
    def test_node_and_indexer_agree(self):
        accounts = {ADDR_A: (12_586_812_345, 7_375_450_000)}
        balance, client = run_with_client(
            node_handler(accounts),
            lambda: service().tron_verified_balance(ADDR_A, min_block=100),
        )
        self.assertEqual(balance.address, ADDR_A)
        self.assertEqual(balance.usdt, Decimal("7375.45"))
        self.assertEqual(balance.trx, Decimal("12586.812345"))
        getaccount = [c for c in client.calls if c[1].endswith("/wallet/getaccount")]
        self.assertTrue(all(c[2]["address"] == ADDR_A for c in getaccount))

    def test_never_cached_between_calls(self):
        state = {"usdt": 7_375_450_000}
        accounts = {ADDR_A: lambda: (1_000_000, state["usdt"])}
        chain = service()
        first, _ = run_with_client(
            node_handler(accounts), lambda: chain.tron_verified_balance(ADDR_A)
        )
        state["usdt"] = 6_487_450_000
        second, _ = run_with_client(
            node_handler(accounts), lambda: chain.tron_verified_balance(ADDR_A)
        )
        self.assertEqual((first.usdt, second.usdt),
                         (Decimal("7375.45"), Decimal("6487.45")))

    def test_waits_until_node_reaches_transaction_block(self):
        heads = iter([90, 95, 100])
        accounts = {ADDR_A: (1_000_000, 5_000_000)}
        balance, client = run_with_client(
            node_handler(accounts, head=lambda: next(heads)),
            lambda: service().tron_verified_balance(ADDR_A, min_block=100),
        )
        self.assertEqual(balance.usdt, Decimal("5"))
        self.assertEqual(
            len([c for c in client.calls if c[1].endswith("/wallet/getnowblock")]), 3
        )

    def test_node_behind_block_after_all_attempts_raises(self):
        accounts = {ADDR_A: (1_000_000, 5_000_000)}
        with self.assertRaises(ChainQueryError):
            run_with_client(
                node_handler(accounts, head=90),
                lambda: service().tron_verified_balance(ADDR_A, min_block=100),
            )

    def test_disagreement_accepts_node_only_after_two_equal_readings(self):
        accounts = {ADDR_A: (1_000_000, 7_375_450_000)}
        stale = {ADDR_A: (1_000_000, 8_263_450_000)}
        balance, client = run_with_client(
            node_handler(accounts, indexer=stale),
            lambda: service().tron_verified_balance(ADDR_A),
        )
        self.assertEqual(balance.usdt, Decimal("7375.45"))
        self.assertEqual(
            len([c for c in client.calls if c[1].endswith("/wallet/getaccount")]), 2
        )

    def test_unstable_node_and_disagreeing_indexer_raise(self):
        readings = itertools.count(1)  # every node reading differs
        accounts = {ADDR_A: lambda: (1_000_000, next(readings) * 1_000_000)}
        stale = {ADDR_A: (1_000_000, 9_000_000)}
        with self.assertRaises(ChainQueryError):
            run_with_client(
                node_handler(accounts, indexer=stale),
                lambda: service().tron_verified_balance(ADDR_A),
            )

    def test_indexer_unavailable_uses_node_value(self):
        accounts = {ADDR_A: (1_000_000, 7_375_450_000)}
        balance, _ = run_with_client(
            node_handler(accounts, indexer_status=500),
            lambda: service().tron_verified_balance(ADDR_A),
        )
        self.assertEqual(balance.usdt, Decimal("7375.45"))

    def test_node_down_raises(self):
        with self.assertRaises(ChainQueryError):
            run_with_client(
                lambda method, url, body: (503, {}),
                lambda: service().tron_verified_balance(ADDR_A),
            )

    def test_node_answering_for_other_address_raises(self):
        def handler(method, url, body):
            if url.endswith("/wallet/getaccount"):
                return 200, {"address": ADDR_B, "balance": 1}
            return node_handler({})(method, url, body)

        with self.assertRaises(ChainQueryError):
            run_with_client(handler, lambda: service().tron_verified_balance(ADDR_A))

    def test_failed_balance_of_raises(self):
        def handler(method, url, body):
            if url.endswith("/wallet/triggerconstantcontract"):
                return 200, {"result": {"result": False}}
            return node_handler({ADDR_A: (1, 1)})(method, url, body)

        with self.assertRaises(ChainQueryError):
            run_with_client(handler, lambda: service().tron_verified_balance(ADDR_A))

    def test_verified_balances_of_two_addresses_never_cross(self):
        accounts = {
            ADDR_A: (12_586_812_345, 7_375_450_000),
            ADDR_B: (5_000_000, 100_000),
        }
        chain = service()

        async def both():
            return await asyncio.gather(
                chain.tron_verified_balance(ADDR_A),
                chain.tron_verified_balance(ADDR_B),
            )

        (a, b), _ = run_with_client(node_handler(accounts), both)
        self.assertEqual((a.address, a.usdt, a.trx),
                         (ADDR_A, Decimal("7375.45"), Decimal("12586.812345")))
        self.assertEqual((b.address, b.usdt, b.trx),
                         (ADDR_B, Decimal("0.1"), Decimal("5")))


class BlockNumberTest(unittest.TestCase):
    def test_block_number_retries_and_falls_back(self):
        tx = TronTransaction("edef36bf", 1, "转出", "USDT", Decimal("888"), ADDR_B)

        def handler(method, url, body):
            if url.endswith("/wallet/gettransactioninfobyid"):
                return 429, {}
            if url.endswith("/walletsolidity/gettransactioninfobyid"):
                return 200, {}
            if url.endswith("/api/transaction-info"):
                return 200, {"block": 76543210}
            return 404, {}

        chain = service()
        result, client = run_with_client(handler, lambda: chain.transaction_with_block(tx))
        self.assertEqual(result.block_number, 76543210)
        wallet_calls = [c for c in client.calls
                        if c[1].endswith("/wallet/gettransactioninfobyid")]
        self.assertEqual(len(wallet_calls), 3)

    def test_block_number_from_full_node(self):
        tx = TronTransaction("abc", 1, "转出", "USDT", Decimal("1"), ADDR_B)
        result, _ = run_with_client(
            lambda method, url, body: (200, {"blockNumber": 123}),
            lambda: service().transaction_with_block(tx),
        )
        self.assertEqual(result.block_number, 123)


class AlertViewTest(unittest.TestCase):
    def test_alert_shows_exact_balance_address_and_block(self):
        tx = TronTransaction("h", 1_759_182_396_000, "转出", "USDT",
                             Decimal("888"), ADDR_B, 76543210)
        text, keyboard = tron_monitor_alert_view(
            TronBalance(ADDR_A, True, Decimal("12586.812345"), Decimal("7375.45")),
            tx, 7, "检测到新的USDT支出", address=ADDR_A,
        )
        self.assertIn(f"监控地址：<code>{ADDR_A}</code>", text)
        self.assertIn("USDT余额：<b>7,375.45</b>", text)
        self.assertIn("TRX余额：<b>12,586.812345</b>", text)
        self.assertIn("区块：76543210", text)
        self.assertNotIn("22,020", text)
        self.assertNotIn("数据源未提供", text)
        data = [b.callback_data for b in keyboard.inline_keyboard[0]]
        self.assertTrue(all(item.endswith(ADDR_A) for item in data))

    def test_unverified_balance_shows_placeholder(self):
        text, keyboard = tron_monitor_alert_view(
            None, None, 7, "检测到新的USDT支出", address=ADDR_A,
        )
        self.assertEqual(text.count(TRON_BALANCE_UNAVAILABLE), 2)
        self.assertIn(ADDR_A, text)
        self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.endswith(ADDR_A))

    def test_balance_of_other_address_is_never_rendered(self):
        text, _ = tron_monitor_alert_view(
            TronBalance(ADDR_B, True, Decimal("1"), Decimal("99999")),
            None, 7, "x", address=ADDR_A,
        )
        self.assertNotIn("99,999", text)
        self.assertIn(TRON_BALANCE_UNAVAILABLE, text)


class MultiMonitorPollTest(unittest.TestCase):
    """Several monitors on different addresses polled concurrently."""

    BALANCES = {
        ADDR_A: TronBalance(ADDR_A, True, Decimal("12586.812345"), Decimal("7375.45")),
        ADDR_B: TronBalance(ADDR_B, True, Decimal("3.5"), Decimal("0.0001")),
        ADDR_C: TronBalance(ADDR_C, True, Decimal("250"), Decimal("123456.78")),
    }
    # Quick (non-verified) readings are deliberately wrong to prove alerts
    # only ever use the verified per-address value.
    QUICK = {
        ADDR_A: TronBalance(ADDR_A, True, Decimal("12586.81"), Decimal("22020.58")),
        ADDR_B: TronBalance(ADDR_B, True, Decimal("3.5"), Decimal("7375.45")),
        ADDR_C: TronBalance(ADDR_C, True, Decimal("250"), Decimal("1")),
    }
    DELAYS = {ADDR_A: 0.03, ADDR_B: 0.0, ADDR_C: 0.015}

    def make_store(self, temp_dir):
        store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
        store.init()
        store.upsert_tron_monitor(7, ADDR_A, "both", "", "", "", ["old"], 7, True)
        store.upsert_tron_monitor(7, ADDR_B, "both", "", "", "", ["old"], 7, True)
        store.upsert_tron_monitor(8, ADDR_C, "both", "", "", "", ["old"], 7, True)
        return store

    def make_chain(self, verified_side_effect=None):
        now_ms = int(time.time() * 1000) + 1_000

        async def quick(address):
            await asyncio.sleep(self.DELAYS[address] / 3)
            return self.QUICK[address]

        async def transactions(address, assets, since_ms):
            return (TronTransaction(
                f"tx-{address}", now_ms, "转出", "USDT", Decimal("888"),
                "TReceiver" + address[-6:], 0,
            ),)

        async def with_block(transaction):
            return TronTransaction(
                transaction.tx_id, transaction.timestamp_ms, transaction.direction,
                transaction.asset, transaction.amount, transaction.counterparty,
                1000 + list(self.BALANCES).index(transaction.tx_id[3:]),
            )

        async def verified(address, **kwargs):
            await asyncio.sleep(self.DELAYS[address])
            return self.BALANCES[address]

        return SimpleNamespace(
            tron_monitor_balance=AsyncMock(side_effect=quick),
            tron_monitor_transactions=AsyncMock(side_effect=transactions),
            transaction_with_block=AsyncMock(side_effect=with_block),
            tron_verified_balance=AsyncMock(side_effect=verified_side_effect or verified),
        )

    def run_poll(self, store, chain):
        bot = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(chat_id=7, message_id=99)
        ))
        context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={
            "store": store, "chain": chain, "bot_username": "example_bot",
        }))
        asyncio.run(poll_tron_monitors(context))
        return [call.args for call in bot.send_message.await_args_list]

    def test_each_alert_shows_only_its_own_address_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            chain = self.make_chain()
            sent = self.run_poll(store, chain)
            self.assertEqual(len(sent), 3)
            expected = {
                ADDR_A: ("7,375.45", "12,586.812345", 7),
                ADDR_B: ("0.0001", "3.5", 7),
                ADDR_C: ("123,456.78", "250", 8),
            }
            seen_addresses = set()
            for chat_id, text in sent:
                owners = [a for a in expected if f"监控地址：<code>{a}</code>" in text]
                self.assertEqual(len(owners), 1, text)
                address = owners[0]
                seen_addresses.add(address)
                usdt, trx, owner = expected[address]
                self.assertEqual(chat_id, owner)
                self.assertIn(f"USDT余额：<b>{usdt}</b>", text)
                self.assertIn(f"TRX余额：<b>{trx}</b>", text)
                self.assertIn(f"TReceiver{address[-6:]}", text)
                for other, (other_usdt, _, _) in expected.items():
                    if other != address:
                        self.assertNotIn(f"USDT余额：<b>{other_usdt}</b>", text)
                        self.assertNotIn(other, text)
                # Wrong quick readings never reach the alert.
                self.assertNotIn("22,020.58", text)
                self.assertNotIn("区块：确认中", text)
            self.assertEqual(seen_addresses, set(expected))
            asked = sorted(c.args[0] for c in chain.tron_verified_balance.await_args_list)
            self.assertEqual(asked, sorted(expected))
            for call in chain.tron_verified_balance.await_args_list:
                self.assertGreaterEqual(call.kwargs.get("min_block", 0), 1000)
            saved = {row["address"]: row["last_balance"]
                     for row in store.active_tron_monitors()}
            self.assertEqual(saved[ADDR_A], "USDT=7375.45;TRX=12586.812345")
            self.assertEqual(saved[ADDR_B], "USDT=0.0001;TRX=3.5")
            self.assertEqual(saved[ADDR_C], "USDT=123456.78;TRX=250")

    def test_verification_failure_shows_placeholder_not_other_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)

            async def verified(address, **kwargs):
                if address == ADDR_B:
                    raise ChainQueryError("余额暂未核实")
                await asyncio.sleep(self.DELAYS[address])
                return self.BALANCES[address]

            sent = self.run_poll(store, self.make_chain(verified))
            self.assertEqual(len(sent), 3)
            text_b = next(text for _, text in sent if ADDR_B in text)
            self.assertEqual(text_b.count(TRON_BALANCE_UNAVAILABLE), 2)
            for value in ("7,375.45", "123,456.78", "0.0001", "22,020.58"):
                self.assertNotIn(value, text_b)
            text_a = next(text for _, text in sent if ADDR_A in text)
            self.assertIn("USDT余额：<b>7,375.45</b>", text_a)
            saved = {row["address"]: row["last_balance"]
                     for row in store.active_tron_monitors()}
            self.assertEqual(saved[ADDR_B], "")  # unverified value not stored

    def test_verified_balance_for_wrong_address_is_discarded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)

            async def verified(address, **kwargs):
                return self.BALANCES[ADDR_A]  # buggy source: always address A

            sent = self.run_poll(store, self.make_chain(verified))
            for _, text in sent:
                if ADDR_A in text:
                    self.assertIn("7,375.45", text)
                else:
                    self.assertNotIn("7,375.45", text)
                    self.assertIn(TRON_BALANCE_UNAVAILABLE, text)

    def test_threshold_alert_waits_for_verified_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.upsert_tron_monitor(7, ADDR_A, "usdt", "10000", "", "", ["old"], 7, True)
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=self.BALANCES[ADDR_A]),
                tron_monitor_transactions=AsyncMock(return_value=()),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
                tron_verified_balance=AsyncMock(side_effect=ChainQueryError("x")),
            )
            self.assertEqual(self.run_poll(store, chain), [])
            row = store.active_tron_monitors()[0]
            self.assertNotIn("usdt:low", str(row["alert_state"] or ""))

            chain.tron_verified_balance = AsyncMock(return_value=self.BALANCES[ADDR_A])
            sent = self.run_poll(store, chain)
            self.assertEqual(len(sent), 1)
            self.assertIn("余额 7,375.45 USDT，已低于 10000", sent[0][1])
            chain.tron_verified_balance.assert_awaited_once_with(ADDR_A, min_block=0)

    def test_quick_low_reading_not_confirmed_sends_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.upsert_tron_monitor(7, ADDR_C, "usdt", "10", "", "", ["old"], 7, True)
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=self.QUICK[ADDR_C]),
                tron_monitor_transactions=AsyncMock(return_value=()),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
                tron_verified_balance=AsyncMock(return_value=self.BALANCES[ADDR_C]),
            )
            self.assertEqual(self.run_poll(store, chain), [])
            row = store.active_tron_monitors()[0]
            self.assertEqual(row["alert_state"], "usdt:normal")


if __name__ == "__main__":
    unittest.main()
