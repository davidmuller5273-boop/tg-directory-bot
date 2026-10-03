import asyncio
import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from tg_directory_bot.bot import (
    _tron_poll_interval, poll_tron_monitors, sync_tron_scan, tron_adaptive_interval,
)
from tg_directory_bot.chain import (
    ChainQueryError, ChainService, TronBalance, tron_hex_to_base58,
)
from tg_directory_bot.config import load_config
from tg_directory_bot.storage import DirectoryStore
from tg_directory_bot.tron_net import (
    API_KEY_HEADER, BUSY_MESSAGE, COOLDOWN_HEADER, GovernedTransport, TronGovernor,
    fresh_requests, mask_key, parse_api_keys, retry_after_seconds,
)
from tg_directory_bot.tron_scanner import (
    ScanDB, TronBlockScanner, match_events, parse_block_events,
    parse_txinfo_events, parse_usdt_event_rows,
)

SAMPLE = json.loads((Path(__file__).parent / "tron_block_sample.json").read_text())
TRX_FROM = "TJk32PNXFRhQ89zd5vtcWK9XnE5KWD1gXU"
TRX_TO = "TAhcUtiUMNkEQBrPqb4fxNQyEsKxFKLQto"
USDT_FROM = "TCNGdXJqFenGm6Ji2z495tJH1926j9Rk96"
USDT_TO = tron_hex_to_base58("415649e6dc3fd6bf048355c8c462078d5a6af34e8d")
USDT_TX = "9c9975799aad17400b65ecdd696ed3236409e8c1d8a65fa1a3deba74b01754b7"


def hexify_block(block):
    """visible=false variant of the (visible=true) live sample."""
    from tg_directory_bot.chain import tron_address_hex
    data = json.loads(json.dumps(block))
    for tx in data["transactions"]:
        value = tx["raw_data"]["contract"][0]["parameter"]["value"]
        for key in ("owner_address", "to_address", "contract_address"):
            if key in value:
                value[key] = tron_address_hex(value[key])
    return data


class ParserTest(unittest.TestCase):
    def test_live_block_sample_trx_transfer_and_activity(self):
        for block in (SAMPLE["block"], hexify_block(SAMPLE["block"])):
            events = parse_block_events(block)
            trx = [e for e in events if e.asset == "TRX"]
            self.assertEqual(len(trx), 1)
            self.assertEqual((trx[0].sender, trx[0].recipient), (TRX_FROM, TRX_TO))
            self.assertEqual(trx[0].amount, Decimal("3.58704"))
            self.assertEqual(trx[0].block_number, 86796340)
            activity = [e for e in events if e.asset == "ACTIVITY"]
            self.assertEqual([e.sender for e in activity], [USDT_FROM])

    def test_live_txinfo_sample_usdt_transfer_log(self):
        events = parse_txinfo_events(SAMPLE["txinfo"])
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event.sender, event.recipient), (USDT_FROM, USDT_TO))
        self.assertEqual(event.amount, Decimal("33.05516"))
        self.assertEqual(event.tx_id, USDT_TX)
        self.assertEqual(event.timestamp_ms, 1791061674000)

    def test_non_usdt_logs_and_other_topics_ignored(self):
        info = json.loads(json.dumps([i for i in SAMPLE["txinfo"] if i.get("log")][0]))
        info["log"].append(dict(info["log"][0], address="aa" * 20))
        info["log"].append(dict(info["log"][0], topics=["8c5be1e5" + "0" * 56] + info["log"][0]["topics"][1:]))
        self.assertEqual(len(parse_txinfo_events([info])), 1)

    def test_internal_trx_transfer(self):
        info = {"id": "abc", "blockNumber": 5, "blockTimeStamp": 9, "internal_transactions": [{
            "caller_address": "41" + "11" * 20, "transferTo_address": "415649e6dc3fd6bf048355c8c462078d5a6af34e8d",
            "callValueInfo": [{"callValue": 2_500_000}],
        }, {"caller_address": "41" + "11" * 20, "transferTo_address": "41" + "22" * 20,
            "callValueInfo": [{"callValue": 1}], "rejected": True}]}
        events = parse_txinfo_events([info])
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].asset, events[0].recipient, events[0].amount),
                         ("TRX", USDT_TO, Decimal("2.5")))

    def test_event_server_rows(self):
        rows = [{
            "block_number": 86796375, "block_timestamp": 1791061779000,
            "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "event_name": "Transfer",
            "result": {"from": "0x1a4cd9078cf2db25d73ce5c6084e4cc7b4cab9eb",
                       "to": "0x5649e6dc3fd6bf048355c8c462078d5a6af34e8d", "value": "122640000"},
            "transaction_id": "4f73",
        }, {"event_name": "Approval", "transaction_id": "x", "result": {}}]
        events = parse_usdt_event_rows(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].sender, events[0].recipient, events[0].amount),
                         (USDT_FROM, USDT_TO, Decimal("122.64")))

    def test_match_directions(self):
        events = parse_txinfo_events(SAMPLE["txinfo"]) + parse_block_events(SAMPLE["block"])
        found = match_events(events, {USDT_TO: "usdt", USDT_FROM: "usdt", "Tnobody": "both"})
        directions = sorted((address, direction, event.asset) for address, event, direction in found)
        self.assertEqual(directions, sorted([
            (USDT_TO, "转入", "USDT"), (USDT_FROM, "转出", "USDT"), (USDT_FROM, "活动", "ACTIVITY"),
        ]))


class FakeChain:
    def __init__(self, head=100, events=None, txinfo=None, block=None):
        self.head = head
        self.events = events or {}
        self.txinfo = txinfo or {}
        self.block = block or {}
        self.calls = []

    async def tron_head(self):
        self.calls.append(("head",))
        return self.head, int(time.time() * 1000) - 10_000

    async def tron_usdt_events(self, number):
        self.calls.append(("events", number))
        value = self.events.get(number, [])
        if isinstance(value, Exception):
            raise value
        return value

    async def tron_block_txinfo(self, number):
        self.calls.append(("txinfo", number))
        value = self.txinfo.get(number, [])
        if isinstance(value, Exception):
            raise value
        return value

    async def tron_block(self, number):
        self.calls.append(("block", number))
        return self.block.get(number, {"block_header": {"raw_data": {"number": number}}})


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = ScanDB(Path(self.temp.name) / "tron-scan.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_starts_at_head_then_scans_only_matches(self):
        self.db.publish_watch("mother", {USDT_TO: "usdt"})
        chain = FakeChain(head=86796341, txinfo={86796340: SAMPLE["txinfo"]})
        scanner = TronBlockScanner(chain, self.db, lag=1, use_events=False)
        asyncio.run(scanner.step())
        self.assertEqual(self.db.get("last_block"), "86796340")
        self.db.set("last_block", 86796339)
        chain.head = 86796342
        scanner._next_head_check = 0
        scanner.head = 0  # forces a head refresh
        self.assertEqual(asyncio.run(scanner.step()), 2)
        rows = self.db.matches_after(0)
        self.assertEqual(len(rows), 1)  # nothing else from the block is stored
        self.assertEqual((rows[0]["address"], rows[0]["direction"], rows[0]["amount"]),
                         (USDT_TO, "转入", "33.05516"))
        # USDT-only watch list: full blocks are never downloaded
        self.assertFalse([c for c in chain.calls if c[0] == "block"])
        # re-scanning the same block does not duplicate matches
        asyncio.run(scanner.scan_block(86796340, {USDT_TO: "usdt"}))
        self.assertEqual(len(self.db.matches_after(0)), 1)

    def test_trx_watch_downloads_block_and_events_fallback_to_receipts(self):
        self.db.publish_watch("clone-3", {TRX_TO: "trx"})
        self.db.publish_watch("mother", {USDT_TO: "both"})
        chain = FakeChain(head=86796341, events={86796340: []},
                          txinfo={86796340: SAMPLE["txinfo"]},
                          block={86796340: SAMPLE["block"]})
        scanner = TronBlockScanner(chain, self.db, lag=1, use_events=True)
        asyncio.run(scanner.scan_block(86796340, self.db.watched()))
        kinds = [c[0] for c in chain.calls]
        self.assertEqual(kinds, ["events", "txinfo", "block"])
        rows = {(r["address"], r["asset"]) for r in self.db.matches_after(0)}
        self.assertEqual(rows, {(TRX_TO, "TRX"), (USDT_TO, "USDT")})

    def test_event_rows_used_without_receipts(self):
        self.db.publish_watch("mother", {USDT_TO: "usdt"})
        rows = [{"block_number": 7, "block_timestamp": 1, "event_name": "Transfer",
                 "result": {"from": "0x" + "11" * 20, "to": "0x5649e6dc3fd6bf048355c8c462078d5a6af34e8d",
                            "value": "1000000"}, "transaction_id": "t1"}]
        chain = FakeChain(events={7: rows})
        scanner = TronBlockScanner(chain, self.db, lag=1, use_events=True)
        asyncio.run(scanner.scan_block(7, self.db.watched()))
        self.assertEqual([c[0] for c in chain.calls], ["events"])
        self.assertEqual(len(self.db.matches_after(0)), 1)

    def test_failure_keeps_position_for_retry(self):
        self.db.publish_watch("mother", {USDT_TO: "usdt"})
        self.db.set("last_block", 99)
        chain = FakeChain(head=102, txinfo={100: ChainQueryError(BUSY_MESSAGE)})
        scanner = TronBlockScanner(chain, self.db, lag=1, use_events=False)
        with self.assertRaises(ChainQueryError):
            asyncio.run(scanner.step())
        self.assertEqual(self.db.get("last_block"), "99")
        chain.txinfo[100] = []
        self.assertEqual(asyncio.run(scanner.step()), 2)
        self.assertEqual(self.db.get("last_block"), "101")

    def test_far_behind_skips_with_notice_and_requests_reconcile(self):
        self.db.publish_watch("mother", {USDT_TO: "usdt"})
        self.db.set("last_block", 1000)
        chain = FakeChain(head=10_000)
        scanner = TronBlockScanner(chain, self.db, lag=1, max_catchup=50,
                                   use_events=False, max_blocks_per_step=5)
        asyncio.run(scanner.step())
        skipped = json.loads(self.db.get("skipped"))
        self.assertEqual(skipped["from"], 1001)
        self.assertEqual(skipped["to"], 10_000 - 1 - 50)
        self.assertTrue(float(self.db.get("reconcile_requested")))
        self.assertEqual(int(self.db.get("last_block")), 10_000 - 1 - 50 + 5)

    def test_idle_without_watch_list_makes_no_requests(self):
        chain = FakeChain()
        asyncio.run(TronBlockScanner(chain, self.db).step())
        self.assertEqual(chain.calls, [])
        self.assertTrue(self.db.scanner_healthy())

    def test_stale_watch_entries_from_dead_clone_ignored(self):
        self.db.publish_watch("clone-9", {USDT_TO: "usdt"})
        conn = self.db.connect()
        conn.execute("UPDATE watch SET updated_at = 0")
        conn.commit()
        conn.close()
        self.assertEqual(self.db.watched(), {})


def mock_governor(handler, **kwargs):
    governor = TronGovernor(**kwargs)
    calls = []

    def recorder(request):
        calls.append(request)
        return handler(request)

    def client():
        return httpx.AsyncClient(transport=GovernedTransport(governor, httpx.MockTransport(recorder)))
    return governor, calls, client


class GovernorTest(unittest.TestCase):
    def test_key_header_on_all_trongrid_calls_and_never_to_fallback_nodes(self):
        governor, calls, client = mock_governor(
            lambda r: httpx.Response(200, json={}), keys=("key-aaaa-1111",),
            fallback_nodes=("https://tron-rpc.publicnode.com",), cache_seconds=0,
        )

        async def run():
            async with client() as c:
                await c.get("https://api.trongrid.io/v1/accounts/TX")
                await c.post("https://api.trongrid.io/wallet/getaccount", json={})
                await c.post("https://api.trongrid.io/wallet/triggerconstantcontract", json={})
                await c.post("https://api.trongrid.io/walletsolidity/gettransactioninfobyid",
                             json={}, headers={API_KEY_HEADER: "stale"})
                await c.post("https://tron-rpc.publicnode.com/wallet/getaccount", json={},
                             headers={API_KEY_HEADER: "key-aaaa-1111"})
        asyncio.run(run())
        self.assertEqual([r.headers.get(API_KEY_HEADER) for r in calls[:4]], ["key-aaaa-1111"] * 4)
        self.assertNotIn(API_KEY_HEADER, calls[4].headers)

    def test_round_robin_and_429_switches_key_with_cooldown(self):
        seen = []

        def handler(request):
            key = request.headers.get(API_KEY_HEADER)
            seen.append(key)
            if key == "k1-xxxxxxxx":
                return httpx.Response(429, headers={"Retry-After": "30"})
            return httpx.Response(200, json={"ok": key})
        governor, calls, client = mock_governor(
            handler, keys=("k1-xxxxxxxx", "k2-yyyyyyyy"), cache_seconds=0,
        )

        async def run():
            async with client() as c:
                results = []
                for _ in range(4):
                    results.append((await c.get("https://api.trongrid.io/v1/x")).json()["ok"])
                return results
        self.assertEqual(asyncio.run(run()), ["k2-yyyyyyyy"] * 4)
        self.assertEqual(seen.count("k1-xxxxxxxx"), 1)  # cooled down after its 429

    def test_all_keys_limited_fail_fast_locally(self):
        governor, calls, client = mock_governor(
            lambda r: httpx.Response(429, text="suspended for 5 s"), keys=(), cache_seconds=0,
        )

        async def run():
            async with client() as c:
                first = await c.get("https://api.trongrid.io/v1/x")
                second = await c.get("https://api.trongrid.io/v1/x")
                return first, second
        first, second = asyncio.run(run())
        self.assertEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers.get(COOLDOWN_HEADER), "1")
        self.assertEqual(second.text, BUSY_MESSAGE)
        self.assertEqual(len(calls), 1)
        self.assertGreater(governor.cooldown_remaining("trongrid"), 4)

    def test_cache_and_coalescing_but_fresh_bypasses(self):
        async def slow(request):
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"n": 1})

        governor = TronGovernor(keys=("abcdefghijkl",), cache_seconds=5)
        calls = []

        class Inner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                calls.append(request)
                response = await slow(request)
                return response

        async def run():
            async with httpx.AsyncClient(transport=GovernedTransport(governor, Inner())) as c:
                url = "https://api.trongrid.io/v1/accounts/TA"
                await asyncio.gather(*(c.get(url) for _ in range(5)))
                await c.get(url)
                with fresh_requests():
                    await c.get(url)
        asyncio.run(run())
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(governor.stats["coalesced"] + governor.stats["cache_hits"], 5)

    def test_token_bucket_shared_between_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            a = TronGovernor(keys=("abcdefghijkl",), max_qps=2, state_path=path)
            b = TronGovernor(keys=("abcdefghijkl",), max_qps=2, state_path=path)
            waits = [a._reserve("trongrid"), b._reserve("trongrid"),
                     a._reserve("trongrid"), b._reserve("trongrid")]
            self.assertEqual(waits[:2], [0.0, 0.0])
            self.assertGreater(waits[2], 0.3)
            self.assertGreater(waits[3], waits[2])
            # 429 cooldown recorded by one process is honoured by the other
            a.report("trongrid", None, 429, 20)
            self.assertIsNone(b._reserve("trongrid"))

    def test_helpers(self):
        self.assertEqual(parse_api_keys("c", "a, b;c\n a"), ("a", "b", "c"))
        self.assertEqual(mask_key("1234567890abcdef"), "1234…cdef")
        self.assertEqual(retry_after_seconds({}, b"the query server is suspended for 5 s."), 5.0)
        self.assertEqual(retry_after_seconds({"Retry-After": "7"}), 7.0)


class ConfigTest(unittest.TestCase):
    def test_multiple_keys_and_backward_compat(self):
        env = {"BOT_TOKEN": "x", "TRONGRID_API_KEYS": "k1, k2", "TRONGRID_API_KEY": "k3",
               "TRON_MAX_QPS": "12", "TRON_FALLBACK_NODES": "https://a.example/,https://b.example"}
        with patch.dict(os.environ, env, clear=False):
            config = load_config(None)
        self.assertEqual(config.trongrid_api_keys, ("k1", "k2", "k3"))
        self.assertEqual(config.trongrid_api_key, "k1")
        self.assertEqual(config.tron_max_qps, 12.0)
        self.assertEqual(config.tron_fallback_nodes, ("https://a.example", "https://b.example"))
        with patch.dict(os.environ, {"BOT_TOKEN": "x", "TRONGRID_API_KEY": "solo"}, clear=False):
            os.environ.pop("TRONGRID_API_KEYS", None)
            config = load_config(None)
        self.assertEqual(config.trongrid_api_keys, ("solo",))
        self.assertTrue(config.tron_block_scan)


class FriendlyErrorTest(unittest.TestCase):
    def test_all_balance_providers_limited_shows_friendly_text(self):
        chain = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://api.trongrid.io",
            tronscan_api_url="https://apilist.tronscanapi.com", tokenview_api_key="",
        ))
        request = httpx.Request("GET", "https://api.trongrid.io/v1/accounts/TX")
        error = httpx.HTTPStatusError(
            "Client error '429 Too Many Requests' for url 'https://api.trongrid.io/v1/accounts/TX'",
            request=request, response=httpx.Response(429, request=request),
        )
        with patch.object(chain, "_trongrid_node_balance", AsyncMock(side_effect=ChainQueryError(BUSY_MESSAGE))), \
                patch.object(chain, "_tronscan_balance", AsyncMock(side_effect=ChainQueryError(BUSY_MESSAGE))):
            with self.assertRaises(ChainQueryError) as caught:
                asyncio.run(chain._fallback_tron_balance("TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR", error))
        self.assertEqual(str(caught.exception), BUSY_MESSAGE)
        self.assertNotIn("http", str(caught.exception))

    def test_node_post_falls_back_to_public_node_on_429(self):
        chain = ChainService(SimpleNamespace(
            trongrid_api_key="kkkkkkkkkkkk", trongrid_url="https://api.trongrid.io",
            tron_fallback_nodes=("https://tron-rpc.publicnode.com",),
        ))
        urls = []

        class Client:
            async def post(self, url, json=None, headers=None):
                urls.append(url)
                status = 429 if "trongrid" in url else 200
                return httpx.Response(status, json={"address": "x"}, request=httpx.Request("POST", url))
        payload = asyncio.run(chain._node_post(Client(), "/wallet/getaccount", {}))
        self.assertEqual(payload, {"address": "x"})
        self.assertEqual(urls, ["https://api.trongrid.io/wallet/getaccount",
                                "https://tron-rpc.publicnode.com/wallet/getaccount"])


class BlockFetchTest(unittest.TestCase):
    def make_chain(self):
        return ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://api.trongrid.io",
            tron_fallback_nodes=("https://lagging.example", "https://good.example"),
        ))

    def run_with(self, chain, handler, coro):
        calls = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                calls.append(url)
                status, payload = handler(url, json)
                return httpx.Response(status, json=payload, request=httpx.Request("POST", url))
        with patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=Client()):
            return asyncio.run(coro()), calls

    def test_empty_receipts_from_lagging_node_are_not_trusted(self):
        chain = self.make_chain()

        def handler(url, body):
            if url.startswith("https://lagging.example"):
                return 200, ([] if "gettransactioninfo" in url else {})
            if "gettransactioninfo" in url:
                return 200, SAMPLE["txinfo"]
            return 200, {}
        rows, calls = self.run_with(chain, handler, lambda: chain.tron_block_txinfo(86796340))
        self.assertEqual(len(rows), 2)
        self.assertIn("https://lagging.example/wallet/getblock", calls)

    def test_empty_block_confirmed_by_header(self):
        chain = self.make_chain()

        def handler(url, body):
            if "getblock" in url:
                return 200, {"block_header": {"raw_data": {"number": 5}}}
            return 200, []
        rows, calls = self.run_with(chain, handler, lambda: chain.tron_block_txinfo(5))
        self.assertEqual(rows, [])
        self.assertEqual(len(calls), 2)

    def test_block_not_available_anywhere_raises(self):
        chain = self.make_chain()
        with self.assertRaises(ChainQueryError):
            self.run_with(chain, lambda url, body: (200, [] if "info" in url else {}),
                          lambda: chain.tron_block_txinfo(10**9))
        block, _ = self.run_with(chain, lambda url, body: (200, {}),
                                 lambda: chain.tron_block(10**9))
        self.assertEqual(block, {})

    def test_scanner_node_calls_prefer_public_nodes_when_key_configured(self):
        chain = ChainService(SimpleNamespace(
            trongrid_api_key="kkkkkkkkkkkk", trongrid_url="https://api.trongrid.io",
            tron_fallback_nodes=("https://good.example",),
        ))
        self.assertEqual(chain._node_bases()[0], "https://api.trongrid.io")
        self.assertEqual(chain._node_bases(prefer_public=True)[0], "https://good.example")


class ScannerDispatchTest(unittest.TestCase):
    def test_match_triggers_instant_alert_with_fresh_balance_no_history_poll(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            store.upsert_tron_monitor(7, USDT_TO, "usdt", "", "", "", ["old"], 7, True)
            db = ScanDB(Path(temp) / "tron-scan.sqlite3")
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(),
                tron_monitor_transactions=AsyncMock(),
                transaction_with_block=AsyncMock(side_effect=lambda t: t),
                tron_verified_balance=AsyncMock(return_value=TronBalance(
                    USDT_TO, True, Decimal("5"), Decimal("1033.05516"))),
            )
            bot = SimpleNamespace(send_message=AsyncMock(
                return_value=SimpleNamespace(chat_id=7, message_id=1)))
            context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={
                "store": store, "chain": chain, "tron_scan_db": db,
                "config": SimpleNamespace(is_clone=True, clone_id=3, tron_block_scan=True,
                                          tron_reconcile_seconds=300,
                                          tron_fallback_poll_seconds=30),
                "bot_username": "example_bot",
            }))
            asyncio.run(sync_tron_scan(context))  # publishes the clone's watch list
            self.assertEqual(db.watched(), {USDT_TO: "usdt"})
            now_ms = int(time.time() * 1000)
            from tg_directory_bot.chain import TronTransaction
            db.add_matches([(USDT_TO, TronTransaction(
                USDT_TX, now_ms, "转入", "USDT", Decimal("33.05516"), USDT_FROM, 86796340))])
            asyncio.run(sync_tron_scan(context))
            bot.send_message.assert_awaited_once()
            text = bot.send_message.await_args.args[1]
            self.assertIn("检测到新的USDT收入", text)
            self.assertIn("1,033.05516", text)
            chain.tron_monitor_transactions.assert_not_awaited()
            chain.tron_verified_balance.assert_awaited_once()
            self.assertEqual(chain.tron_verified_balance.await_args.kwargs["min_block"], 86796340)
            # consumed once: a second sync does not re-alert
            asyncio.run(sync_tron_scan(context))
            bot.send_message.assert_awaited_once()
            self.assertEqual(store.get_settings()["tron_scan_match_cursor"], "1")

    def test_reconciliation_is_slow_when_scanner_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            store.upsert_tron_monitor(7, USDT_TO, "usdt", "", "", "", ["old"], 7, True)
            db = ScanDB(Path(temp) / "tron-scan.sqlite3")
            db.set("heartbeat", time.time())
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    USDT_TO, True, Decimal("1"), Decimal("1"))),
                tron_monitor_transactions=AsyncMock(return_value=()),
            )
            context = SimpleNamespace(bot=SimpleNamespace(), application=SimpleNamespace(bot_data={
                "store": store, "chain": chain, "tron_scan_db": db,
                "config": SimpleNamespace(is_clone=False, tron_block_scan=True,
                                          tron_reconcile_seconds=300,
                                          tron_fallback_poll_seconds=30),
            }))
            for _ in range(5):
                asyncio.run(poll_tron_monitors(context))
            # live monitor: first reconciliation spread over 300 s, not every 3 s
            self.assertLessEqual(chain.tron_monitor_transactions.await_count, 1)
            schedule = context.application.bot_data["tron_poll_schedule"]
            self.assertTrue(all(v > time.monotonic() - 1 for v in schedule.values()))


class UnlimitedMonitorsTest(unittest.TestCase):
    def test_no_per_user_or_global_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            for index in range(620):
                store.upsert_tron_monitor(7 if index < 20 else 1000 + index,
                                          f"Taddr{index}", "usdt", seen_tx_ids=[])
            self.assertEqual(len(store.active_tron_monitors()), 620)
            self.assertEqual(len([r for r in store.active_tron_monitors() if r["owner_id"] == 7]), 20)
            self.assertTrue(store.can_add_tron_monitor(7, "Tanother"))
            self.assertEqual(len(store.active_tron_monitors(10)), 10)

    def test_adaptive_interval_formula(self):
        self.assertEqual(tron_adaptive_interval(5, 300, 0.5), 300)
        self.assertEqual(tron_adaptive_interval(100, 300, 0.5), 1400)
        self.assertEqual(tron_adaptive_interval(1000, 300, 0.5), 14000)
        self.assertEqual(tron_adaptive_interval(100, 30, 2.0), 350)
        # total request rate never exceeds the budget
        for count in (1, 50, 500, 5000):
            interval = tron_adaptive_interval(count, 300, 0.5)
            self.assertLessEqual(count * 7 / interval, 0.5 + 1e-9)

    def test_interval_counts_monitors_of_all_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            db = ScanDB(Path(temp) / "tron-scan.sqlite3")
            db.set("heartbeat", time.time())
            db.publish_watch("clone-1", {f"T{i}": "usdt" for i in range(80)})
            db.publish_watch("mother", {"Tm": "usdt"})
            context = SimpleNamespace(application=SimpleNamespace(bot_data={
                "tron_scan_db": db,
                "config": SimpleNamespace(is_clone=False, tron_block_scan=True,
                                          tron_reconcile_seconds=300, tron_reconcile_qps=0.5,
                                          tron_fallback_poll_seconds=30, tron_fallback_qps=2.0),
            }))
            interval, healthy = _tron_poll_interval(context, 20)
            self.assertTrue(healthy)
            self.assertEqual(interval, (20 + 80) * 7 / 0.5)
            db.set("heartbeat", 0)
            interval, healthy = _tron_poll_interval(context, 20)
            self.assertFalse(healthy)
            self.assertEqual(interval, (20 + 80) * 7 / 2.0)


class HotAddressBatchTest(unittest.TestCase):
    def test_hot_address_transfers_batched_with_one_verification(self):
        from tg_directory_bot.chain import TronTransaction
        with tempfile.TemporaryDirectory() as temp:
            store = DirectoryStore(Path(temp) / "db.sqlite3")
            store.init()
            store.upsert_tron_monitor(7, USDT_TO, "usdt", "", "", "", ["old"], 7, True)
            db = ScanDB(Path(temp) / "tron-scan.sqlite3")
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(), tron_monitor_transactions=AsyncMock(),
                transaction_with_block=AsyncMock(side_effect=lambda t: t),
                tron_verified_balance=AsyncMock(return_value=TronBalance(
                    USDT_TO, True, Decimal("5"), Decimal("100"))),
            )
            bot = SimpleNamespace(send_message=AsyncMock(
                return_value=SimpleNamespace(chat_id=7, message_id=1)))
            bot_data = {"store": store, "chain": chain, "tron_scan_db": db,
                        "config": SimpleNamespace(is_clone=False, tron_block_scan=True),
                        "bot_username": "example_bot"}
            context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data=bot_data))
            asyncio.run(sync_tron_scan(context))
            now_ms = int(time.time() * 1000)

            def tx(n):
                return TronTransaction(f"hot{n}", now_ms + n, "转入", "USDT",
                                       Decimal("1"), USDT_FROM, 100 + n)
            db.add_matches([(USDT_TO, tx(1))])
            asyncio.run(sync_tron_scan(context))
            self.assertEqual(chain.tron_verified_balance.await_count, 1)
            db.add_matches([(USDT_TO, tx(2)), (USDT_TO, tx(3))])
            asyncio.run(sync_tron_scan(context))  # within 6 s: deferred, not lost
            self.assertEqual(chain.tron_verified_balance.await_count, 1)
            self.assertEqual(len(bot_data["tron_pending_matches"][USDT_TO]), 2)
            bot_data["tron_address_dispatch"][USDT_TO] -= 10
            asyncio.run(sync_tron_scan(context))
            self.assertEqual(chain.tron_verified_balance.await_count, 2)
            self.assertEqual(chain.tron_verified_balance.await_args.kwargs["min_block"], 103)
            self.assertEqual(bot.send_message.await_count, 3)
            self.assertNotIn(USDT_TO, bot_data["tron_pending_matches"])


if __name__ == "__main__":
    unittest.main()
