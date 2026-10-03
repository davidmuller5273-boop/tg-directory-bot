from decimal import Decimal
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from telegram.constants import ChatMemberStatus, ChatType

from tg_directory_bot.chain import (
    blocksec_usdt_freeze_status,
    ChainQueryError,
    ChainService,
    TronBalance,
    TronTransaction,
    USDT_TRC20_CONTRACT,
    tron_address_abi_parameter,
    tron_account_is_multisig,
    tron_has_contract_authorization,
    combine_tron_transfer_counts,
    trusted_tronscan_transfer_count,
    tron_hex_to_base58,
    tron_usdt_blacklist_status,
    validate_tron_address,
)
from tg_directory_bot.clones import CloneManager
from tg_directory_bot.auto_delete import AutoDeleteBot
from tg_directory_bot.bot import (
    HELP_TEXT,
    GROUP_PERMISSIONS,
    GROUP_PERMISSION_LABELS,
    dice_side_matched,
    dice_schedule_is_open,
    dice_value_tags,
    entry_text,
    extract_tron_address,
    format_compact_quotes,
    group_violation_reason,
    group_keyword_reply,
    parse_dice_bet,
    lottery_poll_window,
    parse_dice_odds_input,
    parse_dice_toggle_keyword,
    can_toggle_group_dice,
    apply_group_dice_toggle,
    invite_member_overview,
    handle_invite_member_query_input,
    point_dice_bet_reply,
    points_menu_keyboard,
    invite_query_view,
    main_keyboard,
    menu_input_error,
    parse_group_permissions,
    parse_group_trigger,
    parse_rich_submission_command,
    parse_raffle_prizes,
    parse_numbered_id,
    point_ranking_text,
    point_ranking_page,
    point_records_page,
    point_member_ledger_view,
    point_game_records_page,
    point_draw_view,
    point_draw_settings_view,
    poll_tron_monitors,
    private_message,
    private_keyword_reply,
    process_menu_input,
    raffle_delete_view,
    rate_result_view,
    refresh_callback_cleanup,
    reset_point_draw_spend,
    run_tron_history_query,
    search_menu_keyboard,
    send_private_notes,
    send_personal_invite_query,
    should_block_group_content,
    telegram_user_link,
    track_personal_invite,
    tron_monitor_alert_view,
    tron_monitor_asset_prompt,
    tron_monitor_prompt_keyboard,
    tron_monitor_stats_view,
    tron_monitor_user_view,
    tron_recent_view,
    tron_records_view,
    tron_history_volume_limit,
    tron_result_view,
    user_info_command,
    list_entries,
    save_payload,
    track_group_activity,
)
from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore, Entry
from tg_directory_bot.validation import Submission
from run import configure_logging


class ChainTest(unittest.TestCase):
    def test_history_empty_provider_switches_and_success_is_preferred_next_time(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        expected = (TronTransaction(
            "fallback", 123, "转入", "USDT", Decimal("1"), "T" + "b" * 33,
        ),)
        service = ChainService(SimpleNamespace(
            trongrid_api_key="key", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid", tronscan_api_key="",
            oklink_api_key="", tokenview_api_key="",
        ))
        service._trongrid_transaction_history = AsyncMock(return_value=())
        service._tronscan_official_transaction_history = AsyncMock(return_value=expected)
        service._tronscan_transaction_history = AsyncMock()

        first = asyncio.run(service.tron_transaction_history(
            address, "USDT", max_records=10
        ))
        service._tron_history_cache.clear()
        second = asyncio.run(service.tron_transaction_history(
            address, "USDT", max_records=10
        ))

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(service._trongrid_transaction_history.await_count, 1)
        self.assertEqual(service._tronscan_official_transaction_history.await_count, 2)

    def test_invite_username_view_aggregates_multiple_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100901, 77
            first = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+first",
                username="owner77", display_name="Owner",
            )
            store.record_invite_join(chat_id, 101, owner_id, first)
            second = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+second",
                username="owner77", display_name="Owner",
            )
            store.record_invite_join(chat_id, 102, owner_id, second)
            links = store.invite_links_by_owner_query(chat_id, "@owner77")

            text, keyboard = invite_query_view(store, chat_id, links)

            self.assertIn("链接：共 2 个", text)
            self.assertIn("进群 2 · 退出 0 · 仍在 2", text)
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard for button in row
                if button.callback_data
            ]
            self.assertIn("inviteowner:77:joined:0", callbacks)
            self.assertIn("inviteowner:77:remaining_spoken:0", callbacks)

    def test_developer_monitor_stats_and_all_raffle_types_have_delete_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.touch_user(77, "owner77", "Owner", "")
            for index in range(3):
                store.upsert_tron_monitor(
                    77, f"Tmonitor{index}", "both", seen_tx_ids=[]
                )
            stats_text, stats_keyboard = tron_monitor_stats_view(store)
            owner = store.tron_monitor_owner_by_query("77")
            user_text, _ = tron_monitor_user_view(store, owner)
            store.create_raffle(
                -100, 1, "通用奖品", 1, "2099-01-01 00:00:00", "universal"
            )
            store.create_raffle(
                -100, 1, "活跃奖品", 1, "2099-01-01 00:00:00", "activity_rank"
            )
            raffle_text, raffle_keyboard = raffle_delete_view(store, -100)

            self.assertIn("正在监控地址：3 个", stats_text)
            self.assertIn("使用人员：1 人", stats_text)
            self.assertIn("ID 77", stats_text)
            self.assertIn("数字ID：<code>77</code>", user_text)
            self.assertEqual(user_text.count("<code>Tmonitor"), 3)
            stats_callbacks = [
                button.callback_data
                for row in stats_keyboard.inline_keyboard for button in row
            ]
            self.assertIn("admin:tronmonitors:query", stats_callbacks)
            self.assertIn("通用抽奖", raffle_text)
            self.assertIn("活跃排名抽奖", raffle_text)
            delete_callbacks = [
                button.callback_data
                for row in raffle_keyboard.inline_keyboard for button in row
            ]
            self.assertIn("raffledelete:item:1:0", delete_callbacks)
            self.assertIn("raffledelete:item:2:0", delete_callbacks)

    def test_all_bot_next_page_controls_are_inline_buttons(self):
        source = (
            Path(__file__).parents[1] / "tg_directory_bot" / "bot.py"
        ).read_text(encoding="utf-8")
        matches = list(re.finditer("下一页", source))
        self.assertGreater(len(matches), 0)
        for match in matches:
            nearby = source[max(0, match.start() - 100):match.end() + 100]
            self.assertIn("InlineKeyboardButton", nearby)

    def test_tron_security_status_uses_blacklist_permissions_and_approvals(self):
        account = {
            "owner_permission": {
                "threshold": 2,
                "keys": [{"address": "a"}, {"address": "b"}],
            },
        }
        self.assertTrue(tron_account_is_multisig(account))
        self.assertTrue(tron_has_contract_authorization({"total": 1}))
        self.assertFalse(tron_has_contract_authorization({"total": 0}))
        self.assertIsNone(tron_has_contract_authorization({}))
        self.assertTrue(tron_usdt_blacklist_status({
            "result": {"result": True}, "constant_result": ["0" * 63 + "1"],
        }))
        self.assertFalse(tron_usdt_blacklist_status({
            "result": {"result": True}, "constant_result": ["0" * 64],
        }))
        self.assertIsNone(tron_usdt_blacklist_status({"result": {"result": False}}))
        self.assertTrue(blocksec_usdt_freeze_status(200, {"is_frozen": True}))
        self.assertFalse(blocksec_usdt_freeze_status(404, None))
        self.assertIsNone(blocksec_usdt_freeze_status(429, None))
        self.assertEqual(
            tron_address_abi_parameter("TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"),
            "000000000000000000000000718bbfc716227ddfae676a5deaa51cec29d2d470",
        )

    def test_tronscan_headers_do_not_reuse_trongrid_key(self):
        service = ChainService(SimpleNamespace(
            tronscan_api_key="", trongrid_api_key="grid-key",
        ))
        self.assertNotIn("TRON-PRO-API-KEY", service._tronscan_headers())
        service = ChainService(SimpleNamespace(
            tronscan_api_key="scan-key", trongrid_api_key="grid-key",
        ))
        self.assertEqual(service._tronscan_headers()["TRON-PRO-API-KEY"], "scan-key")

    def test_tronscan_get_retries_without_rejected_api_key(self):
        denied = MagicMock(status_code=403)
        denied.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=denied,
        )
        ok = MagicMock(status_code=200)
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"total": 0, "data": []}
        client = SimpleNamespace(get=AsyncMock(side_effect=[denied, ok]))
        service = ChainService(SimpleNamespace(tronscan_api_key="bad-key"))
        response = asyncio.run(service._tronscan_get(
            client, "https://scan.invalid/api/account/approve/list",
            {"address": "T" + "a" * 33},
        ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get.await_count, 2)
        self.assertNotIn(
            "TRON-PRO-API-KEY", client.get.await_args_list[1].kwargs["headers"]
        )

    def test_capped_usdt_count_plus_trx_is_not_treated_as_hot_wallet(self):
        self.assertEqual(combine_tron_transfer_counts([10001, 319], 10_000), 319)
        self.assertEqual(combine_tron_transfer_counts([70, 12], 10_000), 82)
        self.assertEqual(combine_tron_transfer_counts([6000, 6000], 10_000), 10001)

    def test_tron_authorization_uses_official_approval_api_and_api_key(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        service = ChainService(SimpleNamespace(
            tronscan_api_url="https://scan.invalid",
            tronscan_api_key="scan-key",
        ))
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"total": 1, "data": [{}]}
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        status = asyncio.run(service._tron_authorization_status(address, client))
        self.assertTrue(status)
        request = client.get.await_args_list[0]
        self.assertTrue(request.args[0].endswith("/api/account/approve/list"))
        self.assertNotIn("TRON-PRO-API-KEY", request.kwargs["headers"])

    def test_balance_summary_does_not_fetch_transaction_history(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        service = ChainService(SimpleNamespace(
            trongrid_api_key="grid-key",
            trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid",
            tronscan_api_key="scan-key",
            tokenview_api_key="",
        ))
        account_response = MagicMock()
        account_response.raise_for_status.return_value = None
        account_response.json.return_value = {"data": [{
            "address": address, "balance": 1_000_000,
            "trc20": [{USDT_TRC20_CONTRACT: "2000000"}],
        }]}
        resource_response = MagicMock()
        resource_response.raise_for_status.return_value = None
        resource_response.json.return_value = {}
        blacklist_response = MagicMock()
        blacklist_response.raise_for_status.return_value = None
        blacklist_response.json.return_value = {
            "result": {"result": True}, "constant_result": ["0" * 64],
        }
        blocksec_response = MagicMock(status_code=404)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=[resource_response, blacklist_response])
        client.get = AsyncMock(side_effect=[account_response, blocksec_response])
        with (
            patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=client),
            patch.object(service, "_history_get", new=AsyncMock()) as history_get,
            patch.object(
                service, "_tron_authorization_status", new=AsyncMock(return_value=False),
            ),
        ):
            result = asyncio.run(service.tron_balance(address))
        self.assertEqual((result.trx, result.usdt), (Decimal("1"), Decimal("2")))
        self.assertEqual(result.transactions, ())
        history_get.assert_not_awaited()
        self.assertFalse(any(
            "transactions" in str(call.args[0]) for call in client.get.await_args_list
        ))

    def test_tron_monitor_alert_is_compact_with_only_record_buttons(self):
        result = TronBalance(
            "T" + "a" * 33, True, Decimal("12"), Decimal("34"),
            created_at_ms=1_700_000_000_000,
            energy_remaining=100, bandwidth_remaining=200,
            free_bandwidth_remaining=600, is_frozen=True,
            is_multisig=True, has_authorization=True,
        )
        transaction = TronTransaction(
            "tx", 1_700_000_100_000, "转入", "USDT",
            Decimal("8.5"), "T" + "b" * 33, 123,
        )
        text, keyboard = tron_monitor_alert_view(
            result, transaction, 9, "检测到新的USDT转账"
        )
        for hidden in (
            "能量剩余", "带宽剩余", "免费带宽", "冻结状态", "安全状态", "注册时间",
        ):
            self.assertNotIn(hidden, text)
        self.assertEqual(
            [button.text for button in keyboard.inline_keyboard[0]],
            ["🟢 USDT记录", "🔴 TRX记录"],
        )
        self.assertEqual(len(keyboard.inline_keyboard), 1)

    def test_opening_point_draw_resets_temporary_spend(self):
        context = SimpleNamespace(user_data={"point_draw_spend:-100": 999})
        reset_point_draw_spend(context, -100)
        self.assertNotIn("point_draw_spend:-100", context.user_data)

    def test_private_note_mode_accepts_keyword_directly_and_stays_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                admin_ids=set(), super_admin_ids=set(), developer_ids={7},
                db_path=Path(temp_dir) / "db.sqlite3",
                categories=("other",), blocked_keywords=(),
            )
            store = DirectoryStore(config.db_path)
            store.init()
            context = SimpleNamespace(
                user_data={"menu_mode": "private_note_query"},
                application=SimpleNamespace(
                    bot_data={"config": config, "store": store}
                ),
            )
            update = SimpleNamespace(
                effective_message=SimpleNamespace(reply_text=AsyncMock()),
                effective_user=SimpleNamespace(id=7),
            )
            with patch(
                "tg_directory_bot.bot.send_private_notes", new_callable=AsyncMock
            ) as query_notes:
                handled = asyncio.run(
                    process_menu_input(update, context, "  测试关键词  ")
                )
            self.assertTrue(handled)
            self.assertEqual(context.user_data["menu_mode"], "private_note_query")
            query_notes.assert_awaited_once_with(update, context, "测试关键词")

    def test_private_note_query_schedules_user_and_result_for_ten_minutes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            incoming = SimpleNamespace(message_id=101, chat_id=202)
            result_message = SimpleNamespace(message_id=102, chat_id=202)
            incoming.reply_text = AsyncMock(return_value=result_message)
            context = SimpleNamespace(
                user_data={},
                application=SimpleNamespace(bot_data={"store": store}),
            )
            update = SimpleNamespace(
                effective_message=incoming,
                effective_user=SimpleNamespace(id=7),
            )
            with patch("tg_directory_bot.bot.schedule_setting_cleanup") as cleanup:
                asyncio.run(send_private_notes(update, context, "不存在"))
            self.assertEqual(context.user_data["preserve_incoming_message_id"], 101)
            self.assertEqual(cleanup.call_count, 2)
            cleanup.assert_any_call(context, incoming)
            cleanup.assert_any_call(context, result_message)

    def test_developer_can_open_private_note_by_sending_keyword_directly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                admin_ids=set(), super_admin_ids=set(), developer_ids={7},
                db_path=Path(temp_dir) / "db.sqlite3",
                categories=("other",), blocked_keywords=(),
            )
            store = DirectoryStore(config.db_path)
            store.init()
            store.add_private_note("客服资料", "内容", 7)
            message = SimpleNamespace(
                message_id=101, text="客服资料", caption=None,
                forward_origin=None, reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=7),
                effective_chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
            )
            context = SimpleNamespace(
                user_data={}, args=[], bot=SimpleNamespace(),
                application=SimpleNamespace(
                    bot_data={"store": store, "config": config}
                ),
            )
            with (
                patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)),
                patch(
                    "tg_directory_bot.bot.send_private_notes", new_callable=AsyncMock
                ) as send_notes,
            ):
                asyncio.run(private_message(update, context))
            send_notes.assert_awaited_once_with(update, context, "客服资料")

    def test_tron_monitor_transfer_and_threshold_alerts_are_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "100", "1000", "500", ["old"], 7,
                True,
            )
            now_ms = int(time.time() * 1000) + 1_000
            inside_tx = TronTransaction(
                "inside", now_ms, "转入", "USDT", Decimal("1"), address
            )
            outside_tx = TronTransaction(
                "outside", now_ms + 1_000, "转入", "USDT", Decimal("2"), address
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("500")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(inside_tx,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            sent = SimpleNamespace(chat_id=7, message_id=99)
            bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "store": store, "chain": chain, "bot_username": "example_bot",
                }),
            )
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_awaited_once()
            self.assertIn("检测到新的USDT收入", bot.send_message.await_args.args[1])

            bot.send_message.reset_mock()
            chain.tron_monitor_balance.return_value = TronBalance(
                address, True, Decimal("1"), Decimal("1200")
            )
            chain.tron_monitor_transactions.return_value = (outside_tx, inside_tx)
            context.application.bot_data.pop("tron_poll_schedule", None)  # next reconciliation pass due
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_awaited_once()
            self.assertIn("高于 1000", bot.send_message.await_args.args[1])

    def test_tron_monitor_can_disable_transfer_alerts_and_watch_both_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "both", "10", "1000", "USDT=50;TRX=50",
                ["old"], 7, False,
            )
            fresh = TronTransaction(
                "new", int(time.time() * 1000) + 1_000,
                "转入", "TRX", Decimal("2"), address
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("50"), Decimal("50")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(fresh,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "store": store, "chain": chain, "bot_username": "example_bot",
                }),
            )
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_not_awaited()

            chain.tron_monitor_balance.return_value = TronBalance(
                address, True, Decimal("5"), Decimal("50")
            )
            sent = SimpleNamespace(chat_id=7, message_id=99)
            bot.send_message.return_value = sent
            context.application.bot_data.pop("tron_poll_schedule", None)  # next reconciliation pass due
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_awaited_once()
            self.assertIn("TRX，已低于 10", bot.send_message.await_args.args[1])

    def test_tron_monitor_ignores_transfers_below_configured_minimum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "", "", "500", ["old"], 7,
                True, "5",
            )
            transaction = TronTransaction(
                "small", int(time.time() * 1000) + 1_000,
                "转入", "USDT", Decimal("2"), address
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("500")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(transaction,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "store": store, "chain": chain, "bot_username": "example_bot",
                }),
            )
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_not_awaited()

    def test_tron_monitor_never_broadcasts_transactions_before_monitor_started(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "", "", "500", [], 7, True, "0.1"
            )
            historical = TronTransaction(
                "historical", 1_700_000_000_000, "转入", "USDT",
                Decimal("999"), address,
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("500")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(historical,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"store": store, "chain": chain}),
            )
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_not_awaited()

    def test_tron_monitor_bootstrap_and_initial_threshold_are_silent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "", "100", "150", ["baseline"], 7,
                True, "0.1", alert_state="usdt:high",
                monitor_state="bootstrapping", cursor_tx_id="baseline",
                cursor_block=100, cursor_timestamp_ms=int(time.time() * 1000),
            )
            fresh = TronTransaction(
                "fresh", int(time.time() * 1000) + 1_000, "转入", "USDT",
                Decimal("10"), address, 101,
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("150")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(fresh,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"store": store, "chain": chain}),
            )
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_not_awaited()
            saved = store.list_tron_monitors(7)[0]
            self.assertEqual(saved["monitor_state"], "live")
            self.assertEqual(saved["cursor_tx_id"], "fresh")
            self.assertEqual(saved["cursor_block"], 101)

    def test_tron_monitor_asset_prompt_shows_address_and_history_count(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        chain = SimpleNamespace(
            tron_transaction_count=AsyncMock(return_value=1234)
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"chain": chain}), user_data={}
        )
        text = asyncio.run(tron_monitor_asset_prompt(context, address))
        self.assertIn(address, text)
        self.assertIn("近一年交易次数：<b>1,234</b>", text)
        chain.tron_transaction_count.assert_awaited_once_with(
            address, "both", 365, 10_000
        )

    def test_high_volume_address_cannot_open_monitor_buttons(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        chain = SimpleNamespace(
            tron_transaction_count=AsyncMock(return_value=10001)
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"chain": chain}), user_data={}
        )
        text = asyncio.run(tron_monitor_asset_prompt(context, address))
        self.assertIn("疑似为交易所钱包或热钱包", text)
        self.assertIsNone(tron_monitor_prompt_keyboard(context))

    def test_monitor_prompt_allows_setup_when_count_unavailable(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        chain = SimpleNamespace(
            tron_transaction_count=AsyncMock(side_effect=ChainQueryError("down"))
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"chain": chain}), user_data={}
        )
        text = asyncio.run(tron_monitor_asset_prompt(context, address))
        self.assertIn("选择监控币种", text)
        self.assertIn("仍可开启监控", text)
        self.assertFalse(context.user_data["tron_monitor_volume_blocked"])
        self.assertIsNotNone(tron_monitor_prompt_keyboard(context))

    def test_history_volume_limits_match_requested_windows(self):
        self.assertEqual(tron_history_volume_limit(7), (7, 2_000))
        self.assertEqual(tron_history_volume_limit(30), (30, 5_000))
        self.assertEqual(tron_history_volume_limit(90), (90, 10_000))

    def test_monitor_apis_fetch_balance_only_and_transactions_since_cursor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                admin_ids=set(), super_admin_ids=set(), developer_ids={1},
                db_path=Path(temp_dir) / "db.sqlite3",
                categories=("other",), blocked_keywords=(),
            )
            service = ChainService(config)
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"

            balance_response = MagicMock()
            balance_response.raise_for_status.return_value = None
            balance_response.json.return_value = {"data": [{
                "balance": 2_000_000,
                "trc20": [{USDT_TRC20_CONTRACT: "3000000"}],
            }]}
            balance_client = MagicMock()
            balance_client.__aenter__ = AsyncMock(return_value=balance_client)
            balance_client.__aexit__ = AsyncMock(return_value=None)
            balance_client.get = AsyncMock(return_value=balance_response)
            with patch(
                "tg_directory_bot.chain.httpx.AsyncClient",
                return_value=balance_client,
            ):
                balance = asyncio.run(service.tron_monitor_balance(address))
            self.assertEqual((balance.trx, balance.usdt), (Decimal("2"), Decimal("3")))
            requested_url = balance_client.get.await_args.args[0]
            self.assertNotIn("transactions", requested_url)

            transaction_response = MagicMock()
            transaction_response.raise_for_status.return_value = None
            transaction_response.json.return_value = {"data": []}
            transaction_client = MagicMock()
            transaction_client.__aenter__ = AsyncMock(return_value=transaction_client)
            transaction_client.__aexit__ = AsyncMock(return_value=None)
            transaction_client.get = AsyncMock(return_value=transaction_response)
            with patch(
                "tg_directory_bot.chain.httpx.AsyncClient",
                return_value=transaction_client,
            ):
                rows = asyncio.run(service.tron_monitor_transactions(
                    address, ("usdt", "trx"), 1_700_000_000_000,
                ))
            self.assertEqual(rows, ())
            self.assertEqual(transaction_client.get.await_count, 6)
            grid_calls = [
                call for call in transaction_client.get.await_args_list
                if "/v1/accounts/" in call.args[0]
            ]
            self.assertEqual(len(grid_calls), 2)
            for call in grid_calls:
                self.assertEqual(call.kwargs["params"]["min_timestamp"], "1700000000000")
                self.assertEqual(call.kwargs["params"]["limit"], "200")

    def test_clone_token_is_sealed_and_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                admin_ids=set(), super_admin_ids=set(), developer_ids={1},
                db_path=Path(temp_dir) / "db.sqlite3",
                categories=("other",), blocked_keywords=(), session_secret="s" * 32,
            )
            store = DirectoryStore(config.db_path)
            store.init()
            manager = CloneManager(config, store)
            token = "999999:abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN"
            sealed = manager.encrypt(token)
            self.assertNotIn(token, sealed)
            self.assertEqual(manager.decrypt(sealed), token)

    def test_help_text_fits_one_telegram_message(self):
        self.assertLessEqual(len(HELP_TEXT), 4096)
        self.assertIn("🎁 全部抽奖", HELP_TEXT)
        self.assertIn("👥 群组管理", HELP_TEXT)

    def test_tron_result_renders_resources_status_count_and_actions(self):
        result = TronBalance(
            address="T" + "a" * 33, activated=True,
            trx=Decimal("25"), usdt=Decimal("100"), created_at_ms=1_700_000_000_000,
            transactions=(
                TronTransaction(
                    "tx-new", 1_700_000_100_000, "转入", "USDT",
                    Decimal("12"), "T" + "b" * 33, 123456,
                ),
                TronTransaction(
                    "tx-old", 1_600_000_000_000, "转出", "TRX",
                    Decimal("2"), "T" + "c" * 33, 111111,
                ),
            ),
            energy_remaining=123, bandwidth_remaining=456,
            free_bandwidth_remaining=600, is_frozen=True,
            is_multisig=False, has_authorization=True,
        )
        text, keyboard = tron_result_view(
            result, emoji_ids={"USDT": "111", "TRX": "222"},
            direction_emoji_ids={"in": "333", "out": "444"},
            status_emoji_ids={
                "energy": "501", "bandwidth": "502", "free_bandwidth": "503",
                "negative": "504", "positive": "505",
            },
            query_count=362150, bot_username="example_bot",
        )
        self.assertIn("能量剩余：123", text)
        self.assertIn("带宽剩余：456", text)
        self.assertIn("免费带宽：600", text)
        self.assertIn("冻结状态：", text)
        self.assertIn("已冻结", text)
        self.assertNotIn("司法", text)
        self.assertIn("无多签", text)
        self.assertIn("已授权", text)
        self.assertIn("累计查询 362,150 次", text)
        self.assertIn('<tg-emoji emoji-id="502">🟡</tg-emoji> 能量剩余', text)
        self.assertEqual(text.count('<tg-emoji emoji-id="503">🟢</tg-emoji>'), 2)
        self.assertNotIn("收入:", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "🔍 查询最近10条交易")
        self.assertEqual(keyboard.inline_keyboard[0][1].text, "🔊 监控此地址")
        self.assertEqual(
            [button.text for button in keyboard.inline_keyboard[1]],
            ["USDT记录", "TRX记录"],
        )
        self.assertEqual(keyboard.inline_keyboard[2][0].text, "👥 加群查询")
        self.assertFalse(any(
            getattr(button, "copy_text", None)
            for row in keyboard.inline_keyboard for button in row
        ))
        all_button_text = " ".join(
            button.text for row in keyboard.inline_keyboard for button in row
        )
        self.assertNotIn("转发", all_button_text)
        self.assertFalse(any(
            getattr(button, "switch_inline_query", None)
            for row in keyboard.inline_keyboard for button in row
        ))

    def test_tron_resource_icons_use_full_low_and_empty_levels(self):
        status_ids = {
            "energy": "empty", "bandwidth": "low", "free_bandwidth": "full",
        }
        result = TronBalance(
            "T" + "a" * 33, True, Decimal("0"), Decimal("0"),
            energy_remaining=65_000,
            bandwidth_remaining=299,
            free_bandwidth_remaining=0,
        )
        text, _ = tron_result_view(result, status_emoji_ids=status_ids)
        self.assertIn('<tg-emoji emoji-id="full">🟢</tg-emoji> 能量剩余', text)
        self.assertIn('<tg-emoji emoji-id="low">🟡</tg-emoji> 带宽剩余', text)
        self.assertIn('<tg-emoji emoji-id="empty">🔴</tg-emoji> 免费带宽', text)

    def test_point_ledger_uses_six_month_window_and_ten_rows_per_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for index in range(25):
                store.adjust_points(-100, 7, 1, f"记录{index}", 1)
            text, keyboard = point_records_page(store, -100, 7, "ledger", 0)
            self.assertIn("近6个月", text)
            self.assertEqual(text.count("操作人："), 10)
            self.assertEqual(keyboard.inline_keyboard[0][0].text, "下一页")
            second, _ = point_records_page(store, -100, 7, "ledger", 1)
            self.assertEqual(second.count("操作人："), 10)

    def test_tron_records_are_paginated_filterable_and_use_direction_icons(self):
        address = "T" + "a" * 33
        transactions = (
            TronTransaction(
                "dust", 10_000_000_000_000, "转入", "USDT",
                Decimal("0.05"), "T" + "c" * 33, 99,
            ),
        ) + tuple(
            TronTransaction(
                f"tx-{index}", 9_999_999_999_999 - index, "转入", "USDT",
                Decimal("12.500000"), "T" + "b" * 33, 100 + index,
            )
            for index in range(12)
        )
        text, keyboard = tron_records_view(
            address, transactions, "USDT", "all", 0, 0,
            {"USDT": "123"}, {"in": "456", "out": "789"},
        )
        self.assertIn("+150 USDT", text)
        self.assertEqual(text.count("+12.5 USDT"), 10)
        self.assertIn('<tg-emoji emoji-id="456">➕</tg-emoji>', text)
        self.assertIn('<tg-emoji emoji-id="456">➕</tg-emoji>收入', text)
        self.assertNotIn("0.05 USDT", text)
        self.assertIn("第 1/2 页", text)
        self.assertEqual(keyboard.inline_keyboard[-1][0].text, "⬅️ 返回地址查询")
        self.assertFalse(any(
            getattr(button, "copy_text", None)
            for row in keyboard.inline_keyboard for button in row
        ))

    def test_tron_recent_page_only_shows_ten_transactions(self):
        address = "T" + "a" * 33
        result = TronBalance(
            address, True, Decimal("1"), Decimal("2"),
            transactions=tuple(
                TronTransaction(
                    f"tx-{index}", 10_000 - index, "转入", "USDT",
                    Decimal("1"), "T" + "b" * 33, index,
                )
                for index in range(12)
            ),
        )
        text, keyboard = tron_recent_view(
            result, direction_emoji_ids={"in": "456"},
            query_count=99, bot_username="example_bot",
        )
        self.assertEqual(text.count("+1 USDT"), 10)
        self.assertIn("累计查询 99 次", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "🟢 USDT记录")

    def test_group_permissions_accept_chinese_labels(self):
        self.assertEqual(
            parse_group_permissions("统计与活跃排行、抽奖，积分、群投票"),
            {"stats", "raffles", "points", "polls"},
        )
        self.assertEqual(parse_group_permissions("全部"), {
            "stats", "raffles", "lottery", "polls", "ads", "points", "welcome",
            "quickpost", "invite", "recent", "moderation", "diceodds", "renamehist",
        })
        self.assertEqual(parse_group_permissions("骰子赔率、赔率"), {"diceodds"})
        self.assertIn("diceodds", GROUP_PERMISSIONS)
        self.assertEqual(GROUP_PERMISSION_LABELS["diceodds"], "骰子赔率")

    def test_invalid_menu_input_closes_only_after_three_attempts(self):
        context = SimpleNamespace(user_data={"menu_mode": "raffle_point_cost"})
        message = SimpleNamespace(reply_text=AsyncMock())
        for _ in range(3):
            asyncio.run(menu_input_error(
                context, message, "raffle_point_cost", "请输入正整数。"
            ))
        self.assertNotIn("menu_mode", context.user_data)
        self.assertEqual(message.reply_text.await_count, 3)
        self.assertIn("自动关闭", message.reply_text.await_args.args[0])

    def test_tronscan_fallback_filters_dust_transactions(self):
        address = "T" + "a" * 33

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 200, "data": [
                    {
                        "hash": "dust", "block_timestamp": 2000,
                        "amount": "50000", "decimals": 6,
                        "from": "T" + "b" * 33, "to": address, "block": 10,
                    },
                    {
                        "hash": "kept", "block_timestamp": 1000,
                        "amount": "200000", "decimals": 6,
                        "from": "T" + "c" * 33, "to": address, "block": 11,
                    },
                ]}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, *args, **kwargs):
                return Response()

        service = ChainService(SimpleNamespace(
            trongrid_api_key="", tronscan_api_url="https://example.invalid"
        ))
        with patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=Client()):
            result = asyncio.run(service._tronscan_transaction_history(
                address, "USDT", 100, 0
            ))
        self.assertEqual([item.tx_id for item in result], ["kept"])
        self.assertEqual(result[0].amount, Decimal("0.2"))

    def test_trongrid_history_pages_to_time_boundary_and_filters_below_point_11(self):
        address = "T" + "a" * 33
        now_seconds = 2_000_000_000
        now_ms = now_seconds * 1000

        def row(tx_id: str, timestamp_ms: int, amount: str) -> dict:
            return {
                "transaction_id": tx_id,
                "block_timestamp": timestamp_ms,
                "from": "T" + "b" * 33,
                "to": address,
                "value": amount,
                "token_info": {
                    "address": USDT_TRC20_CONTRACT, "decimals": 6,
                },
            }

        payloads = [
            {
                "data": [
                    row("dust-010", now_ms - 86_400_000, "100000"),
                    row("recent", now_ms - 2 * 86_400_000, "200000"),
                ],
                "meta": {"fingerprint": "next-page"},
            },
            {
                "data": [row("older", now_ms - 20 * 86_400_000, "300000")],
                "meta": {},
            },
        ]

        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Client:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params, headers):
                self.calls.append(dict(params))
                return Response(payloads[len(self.calls) - 1])

        client = Client()
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://example.invalid",
        ))
        with (
            patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=client),
            patch("tg_directory_bot.chain.time.time", return_value=now_seconds),
            patch("tg_directory_bot.chain.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(service._trongrid_transaction_history(
                address, "USDT", 100, 30
            ))
        self.assertEqual([item.tx_id for item in result], ["recent", "older"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["min_timestamp"], client.calls[1]["min_timestamp"])
        self.assertEqual(client.calls[1]["fingerprint"], "next-page")

    def test_history_automatically_switches_provider(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        expected = (TronTransaction(
            "fallback", 123, "转入", "USDT", Decimal("1"), "T" + "b" * 33,
        ),)
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid",
        ))
        service._trongrid_transaction_history = AsyncMock(
            side_effect=ChainQueryError("DNS失败")
        )
        service._tronscan_official_transaction_history = AsyncMock(
            return_value=expected
        )
        service._tronscan_transaction_history = AsyncMock()
        progress = AsyncMock()

        result = asyncio.run(service.tron_transaction_history(
            address, "USDT", days=90, progress=progress
        ))

        self.assertEqual(result, expected)
        service._tronscan_official_transaction_history.assert_awaited_once()
        service._tronscan_transaction_history.assert_not_awaited()
        self.assertTrue(any(
            call.args[0].get("switching") for call in progress.await_args_list
        ))

    def test_balance_dns_failure_switches_to_tronscan(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        fallback = TronBalance(address, True, Decimal("2"), Decimal("3"))
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid",
        ))

        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, *args, **kwargs):
                raise httpx.ConnectError("DNS失败")

        with (
            patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=FailingClient()),
            patch.object(
                service, "_trongrid_node_balance",
                new=AsyncMock(side_effect=ChainQueryError("节点失败")),
            ) as node_balance,
            patch.object(service, "_tronscan_balance", new=AsyncMock(return_value=fallback))
            as tronscan_balance,
        ):
            result = asyncio.run(service.tron_balance(address))
        self.assertEqual(result, fallback)
        node_balance.assert_awaited_once_with(address)
        tronscan_balance.assert_awaited_once_with(address)

    def test_balance_failure_tries_native_node_before_tronscan(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        fallback = TronBalance(address, True, Decimal("4"), Decimal("5"))
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid", tokenview_api_key="",
        ))

        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, *args, **kwargs):
                raise httpx.ConnectError("DNS失败")

        with (
            patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=FailingClient()),
            patch.object(
                service, "_trongrid_node_balance",
                new=AsyncMock(return_value=fallback),
            ) as node_balance,
            patch.object(service, "_tronscan_balance", new=AsyncMock())
            as tronscan_balance,
        ):
            result = asyncio.run(service.tron_balance(address))
        self.assertEqual(result, fallback)
        node_balance.assert_awaited_once_with(address)
        tronscan_balance.assert_awaited_once_with(address)

    def test_balance_reuses_recently_successful_fallback_provider_first(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        fallback = TronBalance(address, True, Decimal("4"), Decimal("5"))
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid", tokenview_api_key="",
        ))
        service._tron_balance_provider_preference = ("TronScan", time.monotonic())
        with (
            patch.object(
                service, "_tronscan_balance", new=AsyncMock(return_value=fallback),
            ) as tronscan_balance,
            patch("tg_directory_bot.chain.httpx.AsyncClient") as http_client,
        ):
            result = asyncio.run(service.tron_balance(address))
        self.assertEqual(result, fallback)
        tronscan_balance.assert_awaited_once_with(address)
        http_client.assert_not_called()

    def test_private_invite_query_username_is_not_taken_by_account_lookup(self):
        message = SimpleNamespace(
            message_id=8, text="@ownername", caption=None,
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=77),
            effective_chat=SimpleNamespace(id=77, type=ChatType.PRIVATE),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "config": SimpleNamespace(is_clone=False),
                "store": SimpleNamespace(),
            }),
            user_data={"menu_mode": "invite_query"},
        )
        with (
            patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)),
            patch(
                "tg_directory_bot.bot.process_menu_input", new=AsyncMock()
            ) as process_input,
            patch(
                "tg_directory_bot.bot.user_info_command", new=AsyncMock()
            ) as account_lookup,
        ):
            asyncio.run(private_message(update, context))
        process_input.assert_awaited_once_with(update, context, "@ownername")
        account_lookup.assert_not_awaited()

    def test_tokenview_balance_parser(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        payload = {
            "code": 1,
            "data": {
                "address": address,
                "balance": 2_500_000,
                "create_time": 123456,
                "owner_permission": {
                    "threshold": 1,
                    "keys": [{"address": address, "weight": 1}],
                },
                "active_permission": [{
                    "threshold": 1,
                    "keys": [{"address": address, "weight": 1}],
                }],
                "trc20": [{
                    "hash": USDT_TRC20_CONTRACT,
                    "balance": "12340000",
                    "tokenInfo": {"s": "USDT", "d": "6"},
                }],
            },
        }

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                return Response()

        service = ChainService(SimpleNamespace(tokenview_api_key="key"))
        with patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=Client()):
            result = asyncio.run(service._tokenview_balance(address))
        self.assertEqual(result.trx, Decimal("2.5"))
        self.assertEqual(result.usdt, Decimal("12.34"))
        self.assertFalse(result.is_multisig)

    def test_history_continues_to_oklink_and_tokenview_when_configured(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        expected = (TronTransaction(
            "oklink", 123, "转出", "TRX", Decimal("2"), "T" + "c" * 33,
        ),)
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", trongrid_url="https://grid.invalid",
            tronscan_api_url="https://scan.invalid",
            oklink_api_key="ok", tokenview_api_key="token",
        ))
        service._trongrid_transaction_history = AsyncMock(
            side_effect=ChainQueryError("失败")
        )
        service._tronscan_official_transaction_history = AsyncMock(
            side_effect=ChainQueryError("失败")
        )
        service._tronscan_transaction_history = AsyncMock(
            side_effect=ChainQueryError("失败")
        )
        service._oklink_transaction_history = AsyncMock(return_value=expected)
        service._tokenview_transaction_history = AsyncMock()

        result = asyncio.run(service.tron_transaction_history(
            address, "TRX", days=7
        ))

        self.assertEqual(result, expected)
        service._oklink_transaction_history.assert_awaited_once()
        service._tokenview_transaction_history.assert_not_awaited()

    def test_year_transaction_count_uses_range_totals(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        payloads = iter(({"rangeTotal": 70}, {"rangeTotal": 6}))

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return next(payloads)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, *args, **kwargs):
                return Response()

        service = ChainService(SimpleNamespace(
            trongrid_api_key="", tronscan_api_url="https://scan.invalid",
        ))
        with patch("tg_directory_bot.chain.httpx.AsyncClient", return_value=Client()):
            count = asyncio.run(service.tron_year_transaction_count(address, "both"))
        self.assertEqual(count, 76)

    def test_monitor_volume_check_never_pages_transaction_history(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", tronscan_api_url="https://scan.invalid",
            oklink_api_key="ok", tokenview_api_key="token",
        ))
        service._tronscan_transaction_count = AsyncMock(
            side_effect=ChainQueryError("汇总失败")
        )
        service._tronscan_compat_transaction_count = AsyncMock(
            side_effect=ChainQueryError("汇总失败")
        )
        service._trongrid_transaction_count = AsyncMock()
        service._oklink_transaction_history = AsyncMock()
        service._tokenview_transaction_history = AsyncMock()
        with self.assertRaises(ChainQueryError):
            asyncio.run(service.tron_transaction_count(
                address, "both", 365, 10_000
            ))
        service._trongrid_transaction_count.assert_not_awaited()
        service._oklink_transaction_history.assert_not_awaited()
        service._tokenview_transaction_history.assert_not_awaited()

    def test_monitor_volume_check_uses_trongrid_when_key_present(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        service = ChainService(SimpleNamespace(
            trongrid_api_key="grid-key", tronscan_api_url="https://scan.invalid",
        ))
        service._tronscan_transaction_count = AsyncMock(
            side_effect=ChainQueryError("汇总失败")
        )
        service._tronscan_compat_transaction_count = AsyncMock(
            side_effect=ChainQueryError("汇总失败")
        )
        service._trongrid_transaction_count = AsyncMock(return_value=44)
        count = asyncio.run(service.tron_transaction_count(
            address, "both", 365, 10_000
        ))
        self.assertEqual(count, 44)
        service._trongrid_transaction_count.assert_awaited_once()

    def test_monitor_volume_check_always_prioritizes_tronscan_summary(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        service = ChainService(SimpleNamespace(
            trongrid_api_key="", tronscan_api_url="https://scan.invalid",
        ))
        service._tronscan_transaction_count = AsyncMock(return_value=88)
        service._tronscan_compat_transaction_count = AsyncMock(return_value=99)
        count = asyncio.run(service.tron_transaction_count(
            address, "both", 365, 10_000
        ))
        self.assertEqual(count, 88)
        service._tronscan_transaction_count.assert_awaited_once_with(
            address, ["USDT", "TRX"], 365, 10_000
        )
        service._tronscan_compat_transaction_count.assert_not_awaited()

    def test_large_history_query_reports_background_progress_and_completes(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        transaction = TronTransaction(
            "tx", 1_800_000_000_000, "转入", "USDT",
            Decimal("12"), "T" + "b" * 33,
        )

        requested_limits = []

        async def history(*args, progress, **kwargs):
            requested_limits.append(kwargs.get("max_records"))
            await progress({
                "provider": "TronGrid", "pages": 1,
                "scanned_records": 200, "kept_records": 100,
                "oldest_timestamp": 1_799_000_000_000, "large": False,
            })
            await progress({
                "provider": "TronGrid", "pages": 3,
                "scanned_records": 600, "kept_records": 300,
                "oldest_timestamp": 1_798_000_000_000, "large": True,
            })
            return (transaction,)

        bot = SimpleNamespace(edit_message_text=AsyncMock())
        store = MagicMock()
        store.chain_query_count.return_value = 10
        application = SimpleNamespace(
            bot=bot,
            bot_data={
                "chain": SimpleNamespace(
                    tron_transaction_count=AsyncMock(return_value=100),
                    tron_transaction_history=history,
                ),
                "tron_history_tasks": {"task123": object()},
                "bot_username": "example_bot",
            },
        )
        asyncio.run(run_tron_history_query(
            application, store, 1, 2, 3, address,
            "USDT", "all", 90, 0, "task123",
        ))
        rendered = "\n".join(
            str(call.kwargs.get("text") or "")
            for call in bot.edit_message_text.await_args_list
        )
        self.assertIn("交易量较大，已转入后台查询", rendered)
        self.assertIn("USDT 交易记录", rendered)
        self.assertEqual(requested_limits, [10_001])
        application.bot_data["chain"].tron_transaction_count.assert_not_awaited()
        self.assertNotIn("task123", application.bot_data["tron_history_tasks"])

    def test_history_query_keeps_cap_rows_and_warns_instead_of_failing(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        rows = tuple(
            TronTransaction(
                f"tx-{index}", 1_800_000_000_000 - index,
                "转入", "USDT", Decimal("1"), "T" + "b" * 33,
            )
            for index in range(2_001)
        )
        chain = SimpleNamespace(
            tron_transaction_count=AsyncMock(side_effect=AssertionError("must not count")),
            tron_transaction_history=AsyncMock(return_value=rows),
        )
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        store = MagicMock()
        store.chain_query_count.return_value = 1
        application = SimpleNamespace(
            bot=bot,
            bot_data={
                "chain": chain, "tron_history_tasks": {"task": object()},
                "bot_username": "example_bot",
            },
        )
        asyncio.run(run_tron_history_query(
            application, store, 1, 2, 3, address,
            "USDT", "all", 7, 0, "task",
        ))
        rendered = str(bot.edit_message_text.await_args.kwargs["text"])
        self.assertIn("仅显示已读取的前2,000条", rendered)
        self.assertIn("共 2000 笔", rendered)
        chain.tron_transaction_count.assert_not_awaited()
        chain.tron_transaction_history.assert_awaited_once()
        self.assertEqual(
            chain.tron_transaction_history.await_args.kwargs["max_records"],
            2_001,
        )

    def test_latest_entries_requests_only_three_rows(self):
        store = MagicMock()
        store.list_entries.return_value = []
        store.get_settings.return_value = {}
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            "config": SimpleNamespace(categories=("other",)), "store": store,
        }), args=[])
        update = SimpleNamespace(effective_message=SimpleNamespace())
        with (
            patch("tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)),
            patch("tg_directory_bot.bot.send_entries", new=AsyncMock()),
        ):
            asyncio.run(list_entries(update, context))
        store.list_entries.assert_called_once_with(category=None, limit=3)

    def test_invite_query_has_function_filters_pagination_and_clickable_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id = -100123
            owner_id = 77
            member_id = 88
            link_id = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+test",
                username="owner77", display_name="Owner 77",
            )
            store.record_invite_join(
                chat_id, member_id, owner_id, link_id,
                username="member88", display_name="Member 88",
            )
            store.record_group_activity(
                chat_id, "Test", "test", "supergroup",
                member_id, "member88", "Member 88", messages=1,
            )

            text, keyboard = invite_query_view(
                store, chat_id, store.invite_link_by_id(chat_id, link_id), 0,
            )

            self.assertIn("已发言 1 条 · 最后发言", text)
            buttons = [button for row in keyboard.inline_keyboard for button in row]
            self.assertIn(f'tg://user?id={member_id}', text)
            self.assertFalse(any(button.url for button in buttons))
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:joined:0"
                for button in buttons
            ))
            self.assertTrue(any(
                button.callback_data == "invite:menu" for button in buttons
            ))
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:remaining_unspoken:0"
                for button in buttons
            ))
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:remaining_spoken:0"
                for button in buttons
            ))

    def test_invite_query_filters_each_use_their_own_ten_row_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100456, 77
            link_id = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+many",
                username="owner", display_name="Owner",
            )
            for index in range(25):
                user_id = 1000 + index
                store.record_invite_join(
                    chat_id, user_id, owner_id, link_id,
                    username=f"member{index}", display_name=f"Member {index}",
                )
                if index < 12:
                    store.record_invite_leave(chat_id, user_id)
            link = store.invite_link_by_id(chat_id, link_id)
            joined, joined_keyboard = invite_query_view(store, chat_id, link, 0, "joined")
            exited, exited_keyboard = invite_query_view(store, chat_id, link, 0, "exited")
            remaining, remaining_keyboard = invite_query_view(
                store, chat_id, link, 0, "remaining"
            )
            self.assertIn("第 1/3 页 · 共 25 人", joined)
            self.assertIn("第 1/2 页 · 共 12 人", exited)
            self.assertIn("第 1/2 页 · 共 13 人", remaining)
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:joined:1"
                for row in joined_keyboard.inline_keyboard for button in row
            ))
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:exited:1"
                for row in exited_keyboard.inline_keyboard for button in row
            ))
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:remaining:1"
                for row in remaining_keyboard.inline_keyboard for button in row
            ))

    def test_invite_query_splits_remaining_members_by_speech(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100457, 77
            link_id = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+speech",
                username="owner", display_name="Owner",
            )
            store.record_invite_join(
                chat_id, 88, owner_id, link_id,
                username="speaker", display_name="Speaker",
            )
            store.record_invite_join(
                chat_id, 89, owner_id, link_id,
                username="silent", display_name="Silent",
            )
            store.record_group_activity(
                chat_id, "Test", "test", "supergroup", 88,
                "speaker", "Speaker", messages=3,
            )
            link = store.invite_link_by_id(chat_id, link_id)
            spoken, _ = invite_query_view(
                store, chat_id, link, 0, "remaining_spoken"
            )
            unspoken, _ = invite_query_view(
                store, chat_id, link, 0, "remaining_unspoken"
            )
            self.assertIn("仍在群已发言人员", spoken)
            self.assertIn("Speaker", spoken)
            self.assertIn("已发言 3 条", spoken)
            self.assertNotIn("Silent", spoken)
            self.assertIn("仍在群未发言人员", unspoken)
            self.assertIn("Silent", unspoken)
            self.assertNotIn("Speaker", unspoken)

    def test_owner_can_query_own_invite_link_in_private_chat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100458, 77
            link_url = "https://t.me/+privateQuery"
            store.update_invite_config(chat_id, owner_id, enabled=True)
            store.save_invite_link(chat_id, owner_id, link_url)
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=owner_id, type=ChatType.PRIVATE),
                effective_user=SimpleNamespace(id=owner_id),
                effective_message=message,
            )
            config = SimpleNamespace(developer_ids=set())
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={
                    "store": store, "config": config,
                }),
                user_data={},
            )
            handled = asyncio.run(send_personal_invite_query(
                update, context, link_url
            ))
            self.assertTrue(handled)
            self.assertEqual(context.user_data["selected_group_id"], chat_id)
            message.reply_text.assert_awaited_once()
            markup = message.reply_text.await_args.kwargs["reply_markup"]
            callbacks = [
                button.callback_data
                for row in markup.inline_keyboard for button in row
                if button.callback_data
            ]
            self.assertIn(
                "invitequery:1:remaining_spoken:0", callbacks
            )
            self.assertNotIn("invite:menu", callbacks)

    def test_owner_invite_link_query_is_allowed_through_group_moderation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id = -100459, 77
            link_url = "https://t.me/+groupQuery"
            store.update_invite_config(chat_id, owner_id, enabled=True)
            store.save_invite_link(chat_id, owner_id, link_url)
            message = SimpleNamespace(message_id=456, reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(id=owner_id),
                effective_message=message,
            )
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={
                    "store": store,
                    "config": SimpleNamespace(developer_ids=set()),
                }),
                user_data={},
            )
            handled = asyncio.run(send_personal_invite_query(
                update, context, link_url
            ))
            self.assertTrue(handled)
            self.assertEqual(context.user_data["allowed_invite_message_id"], 456)

    def test_invite_query_tracks_changed_name_as_separate_function(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id, member_id = -100789, 77, 88
            link_id = store.save_invite_link(
                chat_id, owner_id, "https://t.me/+rename",
                username="owner", display_name="Owner",
            )
            store.adjust_points(
                chat_id, owner_id, 1, "test", owner_id,
                "newowner", "Owner New",
            )
            self.assertEqual(
                store.invite_link_by_owner_query(chat_id, "@newowner")["id"],
                link_id,
            )
            store.record_invite_join(
                chat_id, member_id, owner_id, link_id,
                username="member", display_name="进群姓名",
            )
            store.record_group_activity(
                chat_id, "Test", "test", "supergroup", member_id,
                "member", "第一次发言姓名", messages=1,
            )
            store.record_group_activity(
                chat_id, "Test", "test", "supergroup", member_id,
                "member", "现在姓名", messages=1,
            )
            text, keyboard = invite_query_view(
                store, chat_id, store.invite_link_by_id(chat_id, link_id),
                0, "renamed",
            )
            self.assertIn("进群姓名：第一次发言姓名", text)
            self.assertIn("现在姓名：现在姓名", text)
            self.assertIn("发现修改：", text)
            self.assertTrue(any(
                button.callback_data == f"invitequery:{link_id}:renamed:0"
                for row in keyboard.inline_keyboard for button in row
            ))

    def test_other_member_ledger_keeps_target_when_paging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for index in range(23):
                store.adjust_points(-100, 77, 1, f"他人记录{index}", 1, "alice", "Alice")
            text, keyboard = point_member_ledger_view(store, -100, 77, "Alice", 0)
            self.assertEqual(text.count("操作人："), 10)
            callbacks = [
                button.callback_data for row in keyboard.inline_keyboard for button in row
            ]
            self.assertIn("pointmemberledger:77:1", callbacks)
            second, _ = point_member_ledger_view(store, -100, 77, "", 1)
            self.assertIn("第 2/3 页", second)
            self.assertEqual(second.count("操作人："), 10)

    def test_point_draw_hides_multiplier_and_settings_owns_control(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.set_points_enabled(-100, True, 1)
            store.add_point_gift(-100, "礼品", 100, -1, 1)
            store.set_point_draw_config(-100, True, 10, 2.5, 1)
            public_text, public_keyboard = point_draw_view(store, -100, False)
            manager_text, manager_keyboard = point_draw_view(store, -100, True)
            self.assertNotIn("%", public_text)
            self.assertNotIn("倍率", public_text)
            self.assertNotIn("倍率", manager_text)
            public_buttons = " ".join(
                button.text for row in public_keyboard.inline_keyboard for button in row
            )
            manager_buttons = " ".join(
                button.text for row in manager_keyboard.inline_keyboard for button in row
            )
            self.assertNotIn("中奖倍率", public_buttons)
            self.assertNotIn("中奖倍率", manager_buttons)
            settings_text, settings_keyboard = point_draw_settings_view(store, -100)
            settings_buttons = " ".join(
                button.text
                for row in settings_keyboard.inline_keyboard for button in row
            )
            self.assertIn("中奖概率倍率：2.5", settings_text)
            self.assertIn("设置中奖倍率 2.5", settings_buttons)
            self.assertEqual(parse_numbered_id("#1"), 1)
            self.assertEqual(parse_numbered_id("  # 12 "), 12)

    def test_account_username_returns_numeric_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.touch_user(5343326890, "jiuye", "九爷")
            message = SimpleNamespace(
                reply_to_message=None, forward_origin=None,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=99, type=ChatType.PRIVATE),
                effective_user=SimpleNamespace(id=1), effective_message=message,
            )
            context = SimpleNamespace(
                args=[], application=SimpleNamespace(bot_data={"store": store}),
                bot=SimpleNamespace(),
            )
            asyncio.run(user_info_command(update, context, "@jiuye"))
            response = message.reply_text.await_args.args[0]
            self.assertIn("@jiuye", response)
            self.assertIn("5343326890", response)

    def test_group_bare_username_does_not_trigger_numeric_id_reply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.touch_user(5343326890, "jiuye", "九爷")
            message = SimpleNamespace(
                text="@jiuye", reply_to_message=None, forward_origin=None,
                reply_text=AsyncMock(), message_id=1,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=-99, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(id=1), effective_message=message,
            )
            context = SimpleNamespace(
                args=[], user_data={}, bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data={"store": store}),
            )
            with patch(
                "tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)
            ):
                asyncio.run(group_keyword_reply(update, context))
            message.reply_text.assert_not_awaited()

    def test_invite_points_follow_link_owner_and_are_reversed_on_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, inviter_id, invited_id = -9090, 77, 88
            store.set_points_enabled(chat_id, True, 1)
            store.update_invite_config(
                chat_id, 1, enabled=True, points_per_invite=25
            )
            link_url = "https://t.me/+owner77"
            store.save_invite_link(chat_id, inviter_id, link_url)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"store": store})
            )
            member = SimpleNamespace(
                id=invited_id, username="invited", full_name="Invited"
            )
            actor = SimpleNamespace(id=invited_id, username="invited", full_name="Invited")

            def update(old_status, new_status, invite_link=None):
                return SimpleNamespace(chat_member=SimpleNamespace(
                    chat=SimpleNamespace(id=chat_id),
                    old_chat_member=SimpleNamespace(status=old_status),
                    new_chat_member=SimpleNamespace(status=new_status, user=member),
                    from_user=actor,
                    invite_link=(
                        SimpleNamespace(invite_link=invite_link) if invite_link else None
                    ),
                ))

            asyncio.run(track_personal_invite(
                update(ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER, link_url), context
            ))
            self.assertEqual(store.point_account(chat_id, inviter_id)["balance"], 25)
            self.assertEqual(
                int(store.point_account(chat_id, inviter_id)["invite_source_cycle"]), 1
            )
            self.assertTrue(store.user_has_invite_point_source(chat_id, inviter_id))
            self.assertIsNone(store.point_account(chat_id, 99))

            asyncio.run(track_personal_invite(
                update(ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT), context
            ))
            self.assertEqual(store.point_account(chat_id, inviter_id)["balance"], 0)
            # Balance 0 (<=5) clears the current cycle flag
            self.assertEqual(
                int(store.point_account(chat_id, inviter_id)["invite_source_cycle"]), 0
            )
            self.assertFalse(store.user_has_invite_point_source(chat_id, inviter_id))
            self.assertEqual(store.invite_stats(chat_id, inviter_id)["exits"], 1)

    def test_http_clients_do_not_log_telegram_token_urls(self):
        configure_logging()
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_auto_delete_bot_configuration(self):
        bot = AutoDeleteBot(
            token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            auto_delete_seconds=180,
        )
        self.assertEqual(bot.auto_delete_seconds, 180)

    def test_validate_known_tron_address(self):
        self.assertEqual(validate_tron_address(USDT_TRC20_CONTRACT), USDT_TRC20_CONTRACT)
        self.assertEqual(extract_tron_address(USDT_TRC20_CONTRACT), USDT_TRC20_CONTRACT)
        self.assertIsNone(extract_tron_address("普通消息"))

    def test_auto_delete_can_schedule_incoming_message(self):
        deleted = []

        class TrackingBot(AutoDeleteBot):
            async def _delete_after_delay(self, chat_id, message_id, seconds):
                deleted.append((chat_id, message_id, seconds))

        async def run_test():
            bot = TrackingBot(
                token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                auto_delete_seconds=180,
            )
            bot.schedule_delete(123, 456)
            await asyncio.sleep(0)

        asyncio.run(run_test())
        self.assertEqual(deleted, [(123, 456, 180)])

    def test_button_click_restarts_ten_minute_cleanup(self):
        scheduled = []

        class TrackingBot(AutoDeleteBot):
            def schedule_delete_after(self, chat_id, message_id, seconds):
                scheduled.append((chat_id, message_id, seconds))

        bot = TrackingBot(
            token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            auto_delete_seconds=180,
        )
        store = SimpleNamespace(schedule_message_deletion=MagicMock())
        context = SimpleNamespace(
            bot=bot, application=SimpleNamespace(bot_data={"store": store})
        )
        message = SimpleNamespace(chat_id=123, message_id=456)
        update = SimpleNamespace(callback_query=SimpleNamespace(
            message=message, data="nav:main"
        ))

        asyncio.run(refresh_callback_cleanup(update, context))

        self.assertEqual(scheduled, [(123, 456, 600)])
        store.schedule_message_deletion.assert_called_once_with(123, 456, 600)

    def test_message_ads_are_merged_into_the_same_message(self):
        async def run_test():
            bot = AutoDeleteBot(
                token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                auto_delete_seconds=180,
            )

            async def decorator(_bot, _chat_id, position):
                return "前置<&" if position == "prefix" else "后置广告"

            object.__setattr__(bot, "message_decorator", decorator)
            args, kwargs = await bot._merge_ad_text(
                (-100, "机器人回复"), {"parse_mode": "HTML"}, "text", 1, 4096
            )
            return args, kwargs

        args, kwargs = asyncio.run(run_test())
        self.assertEqual(args[1], "前置&lt;&amp;\n\n机器人回复\n\n后置广告")
        self.assertEqual(kwargs["parse_mode"], "HTML")

    def test_regular_user_can_submit_keyword_and_address_directly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="", admin_ids={999}, super_admin_ids={999},
                db_path=Path(temp_dir) / "db.sqlite3", categories=("other",),
                blocked_keywords=("菠菜",),
            )
            store = DirectoryStore(config.db_path)
            store.init()
            store.ensure_config_admins(config.admin_ids, config.super_admin_ids)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"config": config, "store": store}),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=123, username="regular")
            )
            message = SimpleNamespace(reply_text=AsyncMock())
            asyncio.run(save_payload(update, context, "菠菜 t.me/example", message))
            rows = store.list_entries(status="pending")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].user_id, 123)
            self.assertEqual(rows[0].title, "菠菜")
            self.assertEqual(rows[0].url, "https://t.me/example")
            # 审核通知：先发原样内容，再发审核卡片
            self.assertEqual(context.bot.send_message.await_count, 2)
            self.assertEqual(
                context.bot.send_message.await_args_list[0].args[1], "https://t.me/example"
            )
            self.assertIn("待审核", context.bot.send_message.await_args_list[1].args[1])

    def test_approved_keyword_triggers_without_address_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="", admin_ids=set(), super_admin_ids=set(),
                db_path=Path(temp_dir) / "db.sqlite3", categories=("other",),
                blocked_keywords=(),
            )
            store = DirectoryStore(config.db_path)
            store.init()
            store.add_submission(
                Submission("https://example.com/", "v8", "other", ""),
                1, "owner", "approved",
            )
            message = SimpleNamespace(
                text="v8", message_id=9, chat_id=-9009,
                entities=[], caption_entities=[], reply_text=AsyncMock(),
            )
            user = SimpleNamespace(
                id=88, username="member", first_name="Member", last_name="",
                full_name="Member", is_bot=False,
            )
            update = SimpleNamespace(
                effective_message=message, effective_user=user,
                effective_chat=SimpleNamespace(
                    id=-9009, type=ChatType.SUPERGROUP, title="Direct", username=""
                ),
            )
            context = SimpleNamespace(
                user_data={}, bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data={"config": config, "store": store}),
            )
            asyncio.run(group_keyword_reply(update, context))
            message.reply_text.assert_awaited_once()
            self.assertIn("https://example.com/", message.reply_text.await_args.args[0])

    def test_group_violation_mutes_then_kicks_on_fifth_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="", admin_ids=set(), super_admin_ids=set(),
                db_path=Path(temp_dir) / "db.sqlite3", categories=("other",),
                blocked_keywords=("搜索词",),
            )
            store = DirectoryStore(config.db_path)
            store.init()
            store.add_moderation_keyword("违规词", 999)
            store.set_setting("group_moderation_enabled", "1")
            bot = SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
                ),
                restrict_chat_member=AsyncMock(),
                ban_chat_member=AsyncMock(),
                unban_chat_member=AsyncMock(),
                send_message=AsyncMock(),
            )
            user = SimpleNamespace(
                id=321, username="member", full_name="Member", is_bot=False
            )
            chat = SimpleNamespace(
                id=-5005, title="Moderation", username="moderation",
                type=ChatType.SUPERGROUP,
            )
            message = SimpleNamespace(
                text="这里有违规词", caption=None, new_chat_members=[],
                left_chat_member=None, entities=[], caption_entities=[],
                delete=AsyncMock(), chat_id=chat.id, message_id=1,
            )
            update = SimpleNamespace(
                effective_chat=chat, effective_message=message, effective_user=user
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"config": config, "store": store}),
            )

            async def run_test():
                for _ in range(5):
                    await track_group_activity(update, context)

            asyncio.run(run_test())
            self.assertEqual(bot.restrict_chat_member.await_count, 4)
            bot.ban_chat_member.assert_awaited_once_with(chat.id, user.id)
            bot.unban_chat_member.assert_awaited_once_with(
                chat.id, user.id, only_if_banned=True
            )
            with store.connect() as conn:
                violations = conn.execute(
                    """SELECT violations FROM group_violations
                       WHERE chat_id=? AND user_id=?""",
                    (chat.id, user.id),
                ).fetchone()[0]
            self.assertEqual(violations, 5)

    def test_role_aware_categorized_menus(self):
        regular_main = main_keyboard(False, False)
        admin_main = main_keyboard(True, False)
        regular_search = search_menu_keyboard(False)
        super_search = search_menu_keyboard(True)
        regular_main_data = {
            button.callback_data
            for row in regular_main.inline_keyboard for button in row
        }
        admin_main_data = {
            button.callback_data
            for row in admin_main.inline_keyboard for button in row
        }
        regular_search_data = {
            button.callback_data
            for row in regular_search.inline_keyboard for button in row
        }
        super_search_data = {
            button.callback_data
            for row in super_search.inline_keyboard for button in row
        }
        self.assertNotIn("nav:admin", regular_main_data)
        self.assertIn("nav:admin", admin_main_data)
        self.assertNotIn("searchstats:keywords:0", regular_search_data)
        self.assertIn("searchstats:keywords:0", super_search_data)
        self.assertNotIn("/searchstats", HELP_TEXT)
        self.assertNotIn("/addadmin", HELP_TEXT)

    def test_rejects_bad_checksum(self):
        with self.assertRaises(ValueError):
            validate_tron_address(USDT_TRC20_CONTRACT[:-1] + "x")

    def test_parse_okx_merchant_quote(self):
        quote = ChainService._merchant_quote(
            1,
            "buy",
            {
                "nickName": "Demo",
                "price": "7.12",
                "availableAmount": "1000",
                "quoteMinAmountPerOrder": "100",
                "quoteMaxAmountPerOrder": "5000",
                "paymentMethods": ["aliPay", "bank"],
                "completedRate": "0.998",
                "completedOrderQuantity": 42,
                "avgCompletedTime": 18,
            },
        )
        self.assertEqual(quote.price, Decimal("7.12"))
        self.assertEqual(quote.completion_rate, Decimal("99.800"))
        self.assertEqual(quote.payment_methods, ("支付宝", "银行卡"))

    def test_group_content_monitor(self):
        self.assertTrue(should_block_group_content("Visit Casino", ("casino",), False))
        self.assertTrue(should_block_group_content("https://example.com", (), True))
        self.assertFalse(should_block_group_content("normal group message", ("casino",), True))
        self.assertEqual(parse_group_trigger("888地址"), ("directory", "888"))
        self.assertEqual(parse_group_trigger(" Z0 "), ("rate", ""))
        self.assertIsNone(parse_group_trigger("普通群消息"))
        self.assertEqual(
            group_violation_reason("访问 https://example.com", ("违规词",)),
            "发送链接",
        )
        self.assertEqual(
            group_violation_reason("这里有违规词", ("违规词",)),
            "命中违规关键词：违规词",
        )
        self.assertIsNone(group_violation_reason("普通聊天", ("违规词",)))
        self.assertIsNone(parse_rich_submission_command("v8收录 图片和文字"))
        self.assertIsNone(parse_rich_submission_command("v8 搜录图片和文字"))
        self.assertEqual(
            parse_rich_submission_command("v8 搜录 图片和文字"),
            ("v8", "图片和文字"),
        )
        self.assertIsNone(parse_rich_submission_command("v8搜录 图片和文字"))

    def test_rich_entry_format_hides_internal_media_url(self):
        entry = Entry(
            1, "tgcontent://-100/20", "v8", "other", "图片说明", "approved",
            88, "alice", "", "2026-08-27", "2026-08-27", 0,
            content_text="图片说明", media_file_id="photo-id", media_type="photo",
        )
        rendered = entry_text(entry)
        self.assertIn("关键词：</b>v8", rendered)
        self.assertIn("内容：</b>图片说明", rendered)
        self.assertIn("附件：</b>图片", rendered)
        self.assertNotIn("tgcontent://", rendered)

    def test_stats_never_show_numeric_id_as_member_name(self):
        from tg_directory_bot.bot import group_stats_page

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.record_group_activity(
                -8008, "Names", "", "supergroup", 7429297786,
                "", "", messages=2,
            )
            text, _ = group_stats_page(store, -8008, 0, 1, False)
            self.assertIn(">群成员</a>", text)
            self.assertNotIn(">7429297786</a>", text)

    def test_parse_raffle_prize_tiers(self):
        self.assertEqual(
            parse_raffle_prizes("1*188RMB | 2*88RMB", 3, strict=True),
            [(1, "188RMB"), (2, "88RMB")],
        )
        with self.assertRaises(ValueError):
            parse_raffle_prizes("1*188RMB | 1*88RMB", 3, strict=True)

    def test_parse_trx_and_usdt_transactions(self):
        address = "TJmmqjb1DK9TTZbQXzRQ2AuA94z4gKAPFh"
        address_hex = "41608f8da72479edc7dd921e4c30bb7e7cddbe722e"
        self.assertEqual(tron_hex_to_base58(address_hex), address)
        trx = ChainService._parse_trx_transactions(address, {"data": [{
            "txID": "trx-id", "block_timestamp": 1000,
            "raw_data": {"contract": [{
                "type": "TransferContract",
                "parameter": {"value": {
                    "amount": 2500000,
                    "owner_address": address_hex,
                    "to_address": "41a614f803b6fd780986a42c78ec9c7f77e6ded13c",
                }},
            }]},
        }]})
        self.assertEqual(trx[0].direction, "转出")
        self.assertEqual(trx[0].amount, Decimal("2.5"))
        usdt = ChainService._parse_usdt_transactions(address, {"data": [{
            "transaction_id": "usdt-id", "block_timestamp": 2000,
            "from": USDT_TRC20_CONTRACT, "to": address, "value": "1230000",
            "token_info": {"address": USDT_TRC20_CONTRACT, "decimals": 6},
        }]})
        self.assertEqual(usdt[0].direction, "转入")
        self.assertEqual(usdt[0].amount, Decimal("1.23"))

    def test_public_entry_format_only_shows_keyword_and_address(self):
        entry = Entry(
            1, "https://example.com/", "888", "other", "", "approved",
            9, "demo", "", "2026-01-01 00:00:00", "2026-01-01 00:00:00", 0,
        )
        rendered = entry_text(entry)
        self.assertIn("关键词：</b>888", rendered)
        self.assertIn("地址：</b>https://example.com/", rendered)
        self.assertNotIn("分类", rendered)

    def test_raffle_user_link_is_clickable_and_escaped(self):
        self.assertEqual(
            telegram_user_link(123456, "A&B"),
            '<a href="tg://user?id=123456">A&amp;B</a>',
        )

    def test_compact_okx_and_top_100_point_ranking(self):
        quotes = [SimpleNamespace(merchant=f"商户{i}", price=Decimal("6.69")) for i in range(10)]
        text = format_compact_quotes(quotes, quotes, "2026-08-28 12:00")
        self.assertIn("OKX P2P", text)
        self.assertIn("购买价格\n6.69  商户0", text)
        self.assertIn("出售价格\n6.69  商户0", text)
        self.assertIn("商户9", text)
        self.assertIn("抓取时间", text)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            for user_id in range(1, 101):
                store.adjust_points(
                    -1, user_id, 1, "test", 1, f"user{user_id}", f"Name {user_id}"
                )
            ranking, keyboard = point_ranking_page(store, -1, 9)
            self.assertIn("100.", ranking)
            self.assertIn('tg://user?id=', ranking)
            self.assertEqual(len(store.point_rankings(-1, 10, 90)), 10)
            self.assertIsNotNone(keyboard)

    def test_rate_view_has_top_ten_filters_directions_and_add_group(self):
        quotes = [
            SimpleNamespace(rank=index, merchant=f"商户{index}", price=Decimal("6.69"))
            for index in range(1, 11)
        ]
        captured_at = 1_788_227_200.0
        chain = SimpleNamespace(okx_p2p_snapshot=AsyncMock(return_value=(
            {("buy", "bank"): quotes}, captured_at
        )))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            "chain": chain,
            "bot_username": "demo_bot",
            "tron_direction_emoji_ids": {},
        }))
        text, keyboard = asyncio.run(rate_result_view(context, "buy", "bank"))
        chain.okx_p2p_snapshot.assert_awaited_once_with()
        self.assertIn("商户10", text)
        self.assertIn("🏦 <b>OKX USDT/CNY", text)
        self.assertIn("<b>6.69 CNY</b>", text)
        self.assertNotIn("1. ", text)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        callbacks = {button.callback_data for button in buttons if button.callback_data}
        self.assertIn("rate:buy:wechat", callbacks)
        self.assertIn("rate:sell:bank", callbacks)
        self.assertTrue(any(button.url and "startgroup=true" in button.url for button in buttons))
        self.assertFalse(any("富贵娱乐" in button.text for button in buttons))

    def test_okx_all_buttons_share_one_sixty_second_snapshot(self):
        service = ChainService(SimpleNamespace())
        quote = SimpleNamespace(rank=1, merchant="商户", price=Decimal("6.69"))
        service.okx_p2p_quotes = AsyncMock(return_value=[quote])

        async def run():
            first, first_time = await service.okx_p2p_snapshot()
            second, second_time = await service.okx_p2p_snapshot()
            return first, first_time, second, second_time

        first, first_time, second, second_time = asyncio.run(run())
        self.assertIs(first, second)
        self.assertEqual(first_time, second_time)
        self.assertEqual(service.okx_p2p_quotes.await_count, 6)
        self.assertEqual(len(first), 6)

    def test_tron_monitor_alerts_delayed_outgoing_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "", "", "500", ["old"], 7, True,
            )
            with store.connect() as conn:
                conn.execute(
                    """UPDATE tron_monitors
                       SET started_at=DATETIME('now','-3 minutes')
                       WHERE owner_id=7"""
                )
            delayed = TronTransaction(
                "delayed-out", int(time.time() * 1000) - 120_000,
                "转出", "USDT", Decimal("5"), "TReceiver",
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("495")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(delayed,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(
                chat_id=7, message_id=99
            )))
            context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={
                "store": store, "chain": chain, "bot_username": "example_bot",
            }))
            asyncio.run(poll_tron_monitors(context))
            bot.send_message.assert_awaited_once()
            self.assertIn("检测到新的USDT支出", bot.send_message.await_args.args[1])

    def test_private_exact_keyword_is_counted_as_private_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            store.add_submission(
                Submission("https://example.com", "V8", "other", ""),
                7, "owner", "approved",
            )
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=88, username="alice", full_name="Alice"),
                effective_chat=SimpleNamespace(id=88, type=ChatType.PRIVATE),
            )
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"store": store}), user_data={}
            )
            self.assertTrue(asyncio.run(private_keyword_reply(update, context, "V8")))
            event = store.list_search_events()[0]
            self.assertEqual((event["chat_type"], event["source"]), (
                ChatType.PRIVATE, "private_exact_keyword",
            ))
            message.reply_text.assert_awaited_once()

    def test_capped_rangetotal_is_not_treated_as_real_hot_wallet_count(self):
        self.assertEqual(
            trusted_tronscan_transfer_count({"rangeTotal": 70}, 20), 70
        )
        self.assertEqual(
            trusted_tronscan_transfer_count(
                {"rangeTotal": 10000, "token_transfers": []}, 20
            ),
            0,
        )
        self.assertEqual(
            trusted_tronscan_transfer_count(
                {"rangeTotal": 10000, "token_transfers": [{"hash": "a"}]}, 20
            ),
            1,
        )
        self.assertEqual(
            trusted_tronscan_transfer_count(
                {"rangeTotal": 10000, "token_transfers": [{}] * 20}, 20
            ),
            10001,
        )
        self.assertEqual(
            trusted_tronscan_transfer_count({"data": [{}], "code": 200}, 20),
            1,
        )

    def test_parse_usdt_without_token_info_still_keeps_the_transfer(self):
        address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
        parsed = ChainService._parse_usdt_transactions(address, {"data": [{
            "transaction_id": "no-token-info",
            "block_timestamp": 2000,
            "from": "T" + "b" * 33,
            "to": address,
            "value": "1000000",
        }]})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].amount, Decimal("1"))
        self.assertEqual(parsed[0].asset, "USDT")

    def test_history_get_returns_403_without_retrying_like_rate_limit(self):
        class Response:
            status_code = 403
            headers = {}

        class Client:
            def __init__(self):
                self.calls = 0

            async def get(self, *args, **kwargs):
                self.calls += 1
                return Response()

        client = Client()
        response = asyncio.run(ChainService._history_get(
            client, "https://grid.invalid/v1", {}, {}
        ))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.calls, 1)

    def test_failed_alert_send_does_not_consume_the_transaction_hash(self):
        from telegram.error import TelegramError

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            address = "TLKamg9Ph2s2E9ZRJyj4bpRZr9VjDwe6mR"
            store.upsert_tron_monitor(
                7, address, "usdt", "", "", "500", ["old"], 7, True,
            )
            with store.connect() as conn:
                conn.execute(
                    """UPDATE tron_monitors
                       SET started_at=DATETIME('now','-3 minutes')
                       WHERE owner_id=7"""
                )
            fresh = TronTransaction(
                "fresh-tx", int(time.time() * 1000) - 30_000,
                "转入", "USDT", Decimal("8"), "TSender",
            )
            chain = SimpleNamespace(
                tron_monitor_balance=AsyncMock(return_value=TronBalance(
                    address, True, Decimal("1"), Decimal("508")
                )),
                tron_monitor_transactions=AsyncMock(return_value=(fresh,)),
                transaction_with_block=AsyncMock(side_effect=lambda item: item),
            )
            chain.tron_verified_balance = AsyncMock(
                side_effect=lambda address, **kw: chain.tron_monitor_balance.return_value
            )
            bot = SimpleNamespace(send_message=AsyncMock(
                side_effect=TelegramError("blocked")
            ))
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"store": store, "chain": chain}),
            )
            asyncio.run(poll_tron_monitors(context))
            saved = store.list_tron_monitors(7)[0]
            seen = json.loads(saved["seen_tx_ids"])
            self.assertNotIn("fresh-tx", seen)
            self.assertIn("old", seen)
            self.assertTrue(saved["last_error"])




    def test_lottery_poll_window_only_near_draw(self):
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=8))
        # fc3d draws daily 21:15
        noon = datetime(2026, 9, 5, 12, 0, tzinfo=tz)
        self.assertIsNone(lottery_poll_window("fc3d", noon))
        near = datetime(2026, 9, 5, 21, 14, tzinfo=tz)
        self.assertEqual(
            lottery_poll_window("fc3d", near),
            datetime(2026, 9, 5, 21, 15, tzinfo=tz),
        )
        after = datetime(2026, 9, 5, 21, 40, tzinfo=tz)
        self.assertEqual(
            lottery_poll_window("fc3d", after),
            datetime(2026, 9, 5, 21, 15, tzinfo=tz),
        )
        late = datetime(2026, 9, 5, 22, 10, tzinfo=tz)
        self.assertIsNone(lottery_poll_window("fc3d", late))


class DiceBetTest(unittest.TestCase):
    def test_parse_dice_bet_accepts_pure_phrases(self):
        cases = {
            "大3": ("大", Decimal("3")),
            "小5": ("小", Decimal("5")),
            "单10": ("单", Decimal("10")),
            "双2": ("双", Decimal("2")),
            "大 3": ("大", Decimal("3")),
            "小：5": ("小", Decimal("5")),
            "单:10": ("单", Decimal("10")),
            "双-2": ("双", Decimal("2")),
            "大－100": ("大", Decimal("100")),
            "大1.5": ("大", Decimal("1.5")),
            "小 2.25": ("小", Decimal("2.25")),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_dice_bet(text), expected, text)

    def test_parse_dice_bet_rejects_junk_and_limits(self):
        for text in (
            "大3额外", "押大3", "大", "3", "大大3", "大0", "大100001",
            "大 3 分", "小--5", "", "hello", "大1.234", "大.",
        ):
            self.assertIsNone(parse_dice_bet(text), text)

    def test_dice_side_matched_rules(self):
        for value in (4, 5, 6):
            self.assertTrue(dice_side_matched("大", value))
            self.assertFalse(dice_side_matched("小", value))
        for value in (1, 2, 3):
            self.assertTrue(dice_side_matched("小", value))
            self.assertFalse(dice_side_matched("大", value))
        for value in (1, 3, 5):
            self.assertTrue(dice_side_matched("单", value))
            self.assertFalse(dice_side_matched("双", value))
        for value in (2, 4, 6):
            self.assertTrue(dice_side_matched("双", value))
            self.assertFalse(dice_side_matched("单", value))
        self.assertEqual(dice_value_tags(5), "大单")
        self.assertEqual(dice_value_tags(2), "小双")

    def test_dice_daily_schedule_supports_daytime_and_overnight(self):
        daytime = {
            "dice_schedule_enabled": 1,
            "dice_open_time": "09:00",
            "dice_close_time": "18:00",
        }
        self.assertTrue(dice_schedule_is_open(daytime, "12:00"))
        self.assertFalse(dice_schedule_is_open(daytime, "20:00"))
        overnight = dict(daytime, dice_open_time="22:00", dice_close_time="06:00")
        self.assertTrue(dice_schedule_is_open(overnight, "23:30"))
        self.assertTrue(dice_schedule_is_open(overnight, "05:59"))
        self.assertFalse(dice_schedule_is_open(overnight, "12:00"))

    def test_help_text_mentions_dice_bets(self):
        self.assertIn("大3", HELP_TEXT)
        self.assertIn("双2", HELP_TEXT)
        self.assertIn("游戏记录", HELP_TEXT)

    def test_point_dice_bet_reply_win_and_guards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -4242, 77
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(chat_id, user_id, 20, "seed", 1, "alice", "Alice")
            self.assertTrue(store.points_config(chat_id)["dice_enabled"])

            dice_msg = SimpleNamespace(dice=SimpleNamespace(value=6))
            message = SimpleNamespace(
                reply_dice=AsyncMock(return_value=dice_msg),
                reply_text=AsyncMock(),
                message_id=9,
            )
            user = SimpleNamespace(id=user_id, username="alice", full_name="Alice")
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=user,
                effective_message=message,
            )
            context = SimpleNamespace(
                user_data={},
                application=SimpleNamespace(bot_data={"store": store}),
            )
            asyncio.run(point_dice_bet_reply(update, context, "大", 5))
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 25)
            self.assertIn("赢 +5", message.reply_text.await_args.args[0])
            self.assertIn("骰子点数：6", message.reply_text.await_args.args[0])
            self.assertEqual(store.count_point_game_records(chat_id, user_id), 1)
            game = store.point_game_records(chat_id, user_id, 1, 0)[0]
            self.assertEqual(game["game_type"], "dice")
            self.assertEqual(int(game["is_win"]), 1)
            self.assertEqual(int(game["delta"]), 5)
            self.assertIn("赔率2000", game["detail"])
            self.assertIn("反10", game["detail"])

            message.reply_dice.reset_mock()
            message.reply_text.reset_mock()
            dice_msg.dice.value = 1
            asyncio.run(point_dice_bet_reply(update, context, "大", 5))
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 20)
            self.assertIn("输 -5", message.reply_text.await_args.args[0])

            message.reply_dice.reset_mock()
            message.reply_text.reset_mock()
            asyncio.run(point_dice_bet_reply(update, context, "大", 100))
            message.reply_dice.assert_not_awaited()
            self.assertIn("积分不足", message.reply_text.await_args.args[0])

            store.set_dice_min_bet(chat_id, 10, 1)
            message.reply_dice.reset_mock()
            message.reply_text.reset_mock()
            asyncio.run(point_dice_bet_reply(update, context, "大", 5))
            message.reply_dice.assert_not_awaited()
            self.assertIn("每次至少需要 10 积分", message.reply_text.await_args.args[0])

            store.set_points_balance(chat_id, user_id, 0, 1)
            message.reply_text.reset_mock()
            asyncio.run(point_dice_bet_reply(update, context, "小", 1))
            message.reply_dice.assert_not_awaited()
            self.assertIn("0分或负分不能玩骰子", message.reply_text.await_args.args[0])

    def test_group_keyword_reply_handles_dice_bet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -5151, 88
            store.set_points_enabled(chat_id, True, 1)
            store.adjust_points(chat_id, user_id, 10, "seed", 1, "bob", "Bob")
            dice_msg = SimpleNamespace(dice=SimpleNamespace(value=3))
            message = SimpleNamespace(
                text="小3",
                reply_to_message=None,
                forward_origin=None,
                reply_dice=AsyncMock(return_value=dice_msg),
                reply_text=AsyncMock(),
                message_id=3,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(
                    id=user_id, username="bob", full_name="Bob"
                ),
                effective_message=message,
            )
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data={"store": store}),
            )
            with patch(
                "tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)
            ), patch(
                "tg_directory_bot.bot.schedule_group_trigger_cleanup"
            ) as cleanup:
                asyncio.run(group_keyword_reply(update, context))
            message.reply_dice.assert_awaited_once()
            cleanup.assert_not_called()
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 13)
            self.assertIn("赢 +3", message.reply_text.await_args.args[0])

            # menu_mode should skip dice handling
            message.text = "大1"
            message.reply_dice.reset_mock()
            message.reply_text.reset_mock()
            context.user_data["menu_mode"] = "points_gift"
            with patch(
                "tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)
            ), patch(
                "tg_directory_bot.bot.schedule_group_trigger_cleanup"
            ):
                asyncio.run(group_keyword_reply(update, context))
            message.reply_dice.assert_not_awaited()
            message.reply_text.assert_not_awaited()

    def test_group_keyword_reply_ignores_disabled_features(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -7171, 77
            store.set_points_enabled(chat_id, False, 1)
            message = SimpleNamespace(
                text="大3",
                reply_to_message=None,
                forward_origin=None,
                reply_dice=AsyncMock(),
                reply_text=AsyncMock(),
                message_id=9,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(
                    id=user_id, username="u", full_name="U"
                ),
                effective_message=message,
            )
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data={"store": store}),
            )
            with patch(
                "tg_directory_bot.bot.guard", new=AsyncMock(return_value=True)
            ), patch(
                "tg_directory_bot.bot.schedule_group_trigger_cleanup"
            ) as cleanup:
                asyncio.run(group_keyword_reply(update, context))
            message.reply_dice.assert_not_awaited()
            message.reply_text.assert_not_awaited()
            cleanup.assert_not_called()

            message.text = "签到"
            asyncio.run(group_keyword_reply(update, context))
            message.reply_text.assert_not_awaited()

            message.text = "开奖"
            asyncio.run(group_keyword_reply(update, context))
            message.reply_text.assert_not_awaited()

    def test_point_dice_bet_blocked_when_dice_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -6161, 99
            store.set_points_enabled(chat_id, True, 1)
            store.set_dice_enabled(chat_id, False, 1)
            store.adjust_points(chat_id, user_id, 10, "seed", 1, "c", "C")
            message = SimpleNamespace(
                reply_dice=AsyncMock(),
                reply_text=AsyncMock(),
                message_id=1,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=SimpleNamespace(
                    id=user_id, username="c", full_name="C"
                ),
                effective_message=message,
            )
            context = SimpleNamespace(
                user_data={},
                application=SimpleNamespace(bot_data={"store": store}),
            )
            asyncio.run(point_dice_bet_reply(update, context, "大", 1))
            message.reply_dice.assert_not_awaited()
            self.assertIn("骰子游戏尚未开启", message.reply_text.await_args.args[0])
            self.assertEqual(store.count_point_game_records(chat_id, user_id), 0)

    def test_parse_dice_odds_input_formats(self):
        self.assertEqual(parse_dice_odds_input("1950"), 1950)
        self.assertEqual(parse_dice_odds_input("1.95"), 1950)
        self.assertEqual(parse_dice_odds_input("1,95"), 1950)
        self.assertEqual(parse_dice_odds_input("1.7"), 1700)
        self.assertEqual(parse_dice_odds_input("2.0"), 2000)
        self.assertEqual(parse_dice_odds_input("2000"), 2000)
        with self.assertRaises(ValueError):
            parse_dice_odds_input("1699")
        with self.assertRaises(ValueError):
            parse_dice_odds_input("2001")
        with self.assertRaises(ValueError):
            parse_dice_odds_input("1.69")
        with self.assertRaises(ValueError):
            parse_dice_odds_input("2.01")
        with self.assertRaises(ValueError):
            parse_dice_odds_input("abc")
        with self.assertRaisesRegex(ValueError, r"1\.7-2\.0"):
            parse_dice_odds_input("3")

    def test_points_menu_keyboard_dice_odds_button_permission(self):
        without = points_menu_keyboard(can_manage=True, enabled=True)
        without_cbs = {
            button.callback_data
            for row in without.inline_keyboard for button in row
            if button.callback_data
        }
        self.assertNotIn("points:set:diceodds", without_cbs)
        with_perm = points_menu_keyboard(
            can_manage=True, enabled=True, can_manage_dice_odds=True,
        )
        with_cbs = {
            button.callback_data
            for row in with_perm.inline_keyboard for button in row
            if button.callback_data
        }
        self.assertIn("points:dice:menu", with_cbs)
        self.assertNotIn("points:set:diceodds", with_cbs)
        labels = {
            button.text
            for row in with_perm.inline_keyboard for button in row
        }
        self.assertIn("🎲 骰子设置", labels)


    def test_invite_source_cycle_redeem_draw_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -9401, 701
            store.set_points_enabled(chat_id, True, 1)
            store.set_point_draw_config(chat_id, True, 5, 1.0, 1)
            # Cycle 1: earn invite points → redeem notes
            store.adjust_points(
                chat_id, user_id, 20, "邀请成员 1 首次进群", 0, "u", "U"
            )
            self.assertTrue(store.user_has_invite_point_source(chat_id, user_id))
            gift1 = store.add_point_gift(chat_id, "礼品1", 10, 10, 1)
            rid1, _, bal = store.redeem_point_gift(
                chat_id, user_id, gift1, "u", "U"
            )
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 20, 0)}
            self.assertEqual(str(rows[rid1]["note"]), "积分来源邀请他人")
            self.assertEqual(bal, 10)
            self.assertTrue(store.user_has_invite_point_source(chat_id, user_id))

            # Spend remaining to 0 (<=5) → cycle clears
            store.adjust_points(chat_id, user_id, -10, "花光", 1, "u", "U")
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 0)
            self.assertFalse(store.user_has_invite_point_source(chat_id, user_id))

            # New cycle without invite → no note
            store.adjust_points(chat_id, user_id, 15, "签到", 0, "u", "U")
            self.assertFalse(store.user_has_invite_point_source(chat_id, user_id))
            gift2 = store.add_point_gift(chat_id, "礼品2", 5, 10, 1)
            rid2, _, _ = store.redeem_point_gift(chat_id, user_id, gift2, "u", "U")
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 20, 0)}
            self.assertEqual(str(rows[rid2]["note"] or ""), "")

            # Earn invite again → note again; draw path too
            store.adjust_points(
                chat_id, user_id, 30, "邀请成员 2 首次进群", 0, "u", "U"
            )
            self.assertTrue(store.user_has_invite_point_source(chat_id, user_id))
            gift3 = store.add_point_gift(chat_id, "抽奖礼", 5, 5, 1)
            draw = store.draw_point_gift(chat_id, user_id, gift3, "u", "U")
            self.assertTrue(draw["is_winner"])
            games = store.point_game_records(chat_id, user_id, 5, 0)
            self.assertTrue(
                any("积分来源邀请他人" in str(g["detail"]) for g in games)
            )
            rows = {int(r["id"]): r for r in store.point_redemption_rows(chat_id, 20, 0)}
            self.assertEqual(
                str(rows[int(draw["redemption_id"])]["note"]), "积分来源邀请他人"
            )

            text, _ = point_records_page(store, chat_id, user_id, "redeems", 0)
            self.assertIn("备注：积分来源邀请他人", text)
            self.assertIn("已兑换", text)

            # Historical joins alone must NOT set cycle flag (need real link for FK)
            other = 702
            link_id = store.save_invite_link(chat_id, other, "https://t.me/+histother")
            store.record_invite_join(chat_id, 703, other, int(link_id), 25, "x", "X")
            self.assertFalse(store.user_has_invite_point_source(chat_id, other))


    def test_parse_dice_toggle_keyword_and_permission(self):
        self.assertIs(parse_dice_toggle_keyword("开启骰子"), True)
        self.assertIs(parse_dice_toggle_keyword("关闭骰子"), False)
        self.assertIsNone(parse_dice_toggle_keyword("开启 骰子"))
        self.assertIsNone(parse_dice_toggle_keyword("大3"))

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, admin_id, other_id = -9601, 11, 22
            store.set_group_admin_permissions(chat_id, admin_id, {"points"}, 1)
            store.set_points_enabled(chat_id, True, 1)
            store.set_dice_enabled(chat_id, True, 1)
            context = SimpleNamespace(
                user_data={},
                application=SimpleNamespace(bot_data={
                    "store": store,
                    "config": SimpleNamespace(
                        admin_ids=set(), super_admin_ids=set(), developer_ids=set(),
                        is_clone=False,
                    ),
                }),
            )
            self.assertTrue(can_toggle_group_dice(context, chat_id, admin_id))
            self.assertFalse(can_toggle_group_dice(context, chat_id, other_id))

            message = SimpleNamespace(reply_text=AsyncMock(), message_id=9)
            user = SimpleNamespace(id=admin_id, username="a", full_name="Admin")
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=user,
                effective_message=message,
            )
            asyncio.run(apply_group_dice_toggle(update, context, False))
            self.assertFalse(bool(store.points_config(chat_id)["dice_enabled"]))
            message.reply_text.assert_awaited()
            self.assertIn("关闭", message.reply_text.await_args.args[0])

            message2 = SimpleNamespace(reply_text=AsyncMock(), message_id=10)
            other = SimpleNamespace(id=other_id, username="o", full_name="Other")
            update2 = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=other,
                effective_message=message2,
            )
            asyncio.run(apply_group_dice_toggle(update2, context, True))
            self.assertFalse(bool(store.points_config(chat_id)["dice_enabled"]))
            self.assertIn("权限", message2.reply_text.await_args.args[0])

    def test_invite_member_query_self_vs_other_permission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, owner_id, admin_id, stranger_id = -9701, 101, 102, 103
            store.update_invite_config(chat_id, 1, enabled=True)
            store.set_group_admin_permissions(chat_id, admin_id, {"invite"}, 1)
            store.save_invite_link(
                chat_id, owner_id, "https://t.me/+ownerlink", "", "owner", "Owner"
            )
            store.adjust_points(chat_id, owner_id, 12, "邀请成员 x", 0, "owner", "Owner")
            overview = invite_member_overview(store, chat_id, owner_id)
            self.assertIn("https://t.me/+ownerlink", overview)
            self.assertIn("当前积分：12", overview)

            def make_update(user_id, username, text):
                message = SimpleNamespace(
                    reply_text=AsyncMock(),
                    message_id=1,
                    entities=(),
                    caption_entities=(),
                    reply_to_message=None,
                    text=text,
                )
                return SimpleNamespace(
                    effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                    effective_user=SimpleNamespace(
                        id=user_id, username=username, full_name=username
                    ),
                    effective_message=message,
                ), message

            context = SimpleNamespace(
                user_data={"menu_mode": "invite_member_query"},
                application=SimpleNamespace(bot_data={
                    "store": store,
                    "config": SimpleNamespace(
                        admin_ids=set(), super_admin_ids=set(), developer_ids=set(),
                        is_clone=False,
                    ),
                }),
            )
            update, message = make_update(owner_id, "owner", str(owner_id))
            ok = asyncio.run(handle_invite_member_query_input(update, context, str(owner_id)))
            self.assertTrue(ok)
            self.assertIn("邀请链接查询", message.reply_text.await_args.args[0])
            self.assertNotIn("menu_mode", context.user_data)

            context.user_data["menu_mode"] = "invite_member_query"
            update, message = make_update(stranger_id, "stranger", str(owner_id))
            ok = asyncio.run(handle_invite_member_query_input(update, context, str(owner_id)))
            self.assertTrue(ok)
            self.assertEqual(
                message.reply_text.await_args.args[0], "只能查询自己的邀请记录。"
            )

            context.user_data["menu_mode"] = "invite_member_query"
            update, message = make_update(admin_id, "admin", "@owner")
            ok = asyncio.run(handle_invite_member_query_input(update, context, "@owner"))
            self.assertTrue(ok)
            self.assertIn("https://t.me/+ownerlink", message.reply_text.await_args.args[0])

    def test_point_dice_bet_reply_with_custom_odds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -9191, 55
            store.set_points_enabled(chat_id, True, 1)
            store.set_dice_odds(chat_id, 1950, 1)
            store.adjust_points(chat_id, user_id, 2000, "seed", 1, "d", "D")
            dice_msg = SimpleNamespace(dice=SimpleNamespace(value=6))
            message = SimpleNamespace(
                reply_dice=AsyncMock(return_value=dice_msg),
                reply_text=AsyncMock(),
                message_id=2,
            )
            user = SimpleNamespace(id=user_id, username="d", full_name="D")
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
                effective_user=user,
                effective_message=message,
            )
            context = SimpleNamespace(
                user_data={},
                application=SimpleNamespace(bot_data={"store": store}),
            )
            asyncio.run(point_dice_bet_reply(update, context, "大", 1000))
            # payout 1950, delta +950
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 2950)
            reply = message.reply_text.await_args.args[0]
            self.assertIn("赢 +950", reply)
            self.assertIn("赔率1950", reply)
            self.assertIn("反1950", reply)
            game = store.point_game_records(chat_id, user_id, 1, 0)[0]
            self.assertEqual(int(game["delta"]), 950)
            self.assertIn("赔率1950", game["detail"])
            self.assertIn("反1950", game["detail"])

            message.reply_dice.reset_mock()
            message.reply_text.reset_mock()
            dice_msg.dice.value = 1
            asyncio.run(point_dice_bet_reply(update, context, "大", 1000))
            self.assertEqual(store.point_account(chat_id, user_id)["balance"], 1950)
            self.assertIn("输 -1000", message.reply_text.await_args.args[0])

    def test_point_game_records_page_shows_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DirectoryStore(Path(temp_dir) / "db.sqlite3")
            store.init()
            chat_id, user_id = -7171, 11
            store.add_point_game_record(
                chat_id, user_id, "dice",
                side="大", dice_value=6, stake=3, delta=3,
                balance_after=100, is_win=True,
                detail="押大3 · 点数6(大双)",
            )
            store.add_point_game_record(
                chat_id, user_id, "draw",
                stake=10, delta=-10, balance_after=90, is_win=False,
                detail="未中奖",
            )
            text, keyboard = point_game_records_page(store, chat_id, user_id)
            self.assertIn("游戏记录", text)
            self.assertIn("骰子", text)
            self.assertIn("积分抽奖", text)
            self.assertIn("未中奖", text)
            self.assertTrue(any(
                (button.callback_data or "").startswith("points:games:")
                for row in keyboard.inline_keyboard for button in row
            ) or any(
                (button.callback_data or "") == "group:points"
                for row in keyboard.inline_keyboard for button in row
            ))


if __name__ == "__main__":
    unittest.main()
