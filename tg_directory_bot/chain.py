from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

import logging

import httpx

from .config import Config
from .tron_net import (
    BUSY_MESSAGE, COOLDOWN_HEADER, GovernedTransport, config_fallback_nodes,
    fresh_requests, governor_for, is_busy_error, strip_urls,
)

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
MIN_DISPLAY_TRANSFER = Decimal("0.1")
MIN_HISTORY_TRANSFER = Decimal("0.11")
TRONSCAN_RANGE_TOTAL_CAP = 10_000
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
logger = logging.getLogger(__name__)
HistoryProgress = Callable[[dict[str, object]], Awaitable[None]]


class ChainQueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TronTransaction:
    tx_id: str
    timestamp_ms: int
    direction: str
    asset: str
    amount: Decimal
    counterparty: str
    block_number: int = 0


@dataclass(frozen=True)
class TronBalance:
    address: str
    activated: bool
    trx: Decimal
    usdt: Decimal
    created_at_ms: int | None = None
    transactions: tuple[TronTransaction, ...] = ()
    transactions_error: str = ""
    energy_remaining: int = 0
    bandwidth_remaining: int = 0
    free_bandwidth_remaining: int = 0
    is_frozen: bool | None = None
    is_multisig: bool | None = None
    has_authorization: bool | None = None


@dataclass(frozen=True)
class UsdtRate:
    usd: Decimal
    cny: Decimal
    updated_at: int


@dataclass(frozen=True)
class MerchantQuote:
    rank: int
    direction: str
    merchant: str
    price: Decimal
    available_usdt: Decimal
    min_cny: Decimal
    max_cny: Decimal
    payment_methods: tuple[str, ...]
    completion_rate: Decimal
    orders: int
    avg_seconds: int


def validate_tron_address(address: str) -> str:
    address = address.strip()
    if len(address) != 34 or not address.startswith("T"):
        raise ValueError("波场地址应为以 T 开头的 34 位 Base58Check 地址")
    number = 0
    try:
        for char in address:
            number = number * 58 + BASE58_ALPHABET.index(char)
    except ValueError as exc:
        raise ValueError("地址包含无效字符") from exc
    raw = number.to_bytes(25, "big")
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if len(payload) != 21 or payload[0] != 0x41 or checksum != expected:
        raise ValueError("波场地址校验失败")
    return address


def tron_hex_to_base58(value: str) -> str:
    try:
        payload = bytes.fromhex(value)
    except ValueError:
        return value
    if len(payload) != 21 or payload[0] != 0x41:
        return value
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * leading_zeroes + (encoded or "1")


def tron_address_hex(address: str) -> str:
    """Base58 TRON address -> 41-prefixed hex (as returned when visible=false)."""
    value = 0
    for char in address:
        value = value * 58 + BASE58_ALPHABET.index(char)
    return value.to_bytes(25, "big")[:21].hex()


def tron_address_abi_parameter(address: str) -> str:
    address = validate_tron_address(address)
    number = 0
    for char in address:
        number = number * 58 + BASE58_ALPHABET.index(char)
    payload = number.to_bytes(25, "big")[:-4]
    return payload[1:].hex().rjust(64, "0")


def tron_usdt_blacklist_status(payload: dict) -> bool | None:
    result = payload.get("result") or {}
    values = payload.get("constant_result") or []
    if result.get("result") is not True or not values:
        return None
    try:
        return int(str(values[0]), 16) != 0
    except (TypeError, ValueError):
        return None


def blocksec_usdt_freeze_status(status_code: int, payload: dict | None) -> bool | None:
    """Translate BlockSec's checker response without guessing on service errors."""
    if status_code == 404:
        return False
    if status_code != 200 or not isinstance(payload, dict):
        return None
    value = payload.get("is_frozen")
    return value if isinstance(value, bool) else None


def tronscan_usdt_balance(account: dict) -> Decimal:
    """Real USDT quantity from a TronScan account payload.

    Only the official USDT contract counts (fake airdropped tokens may also be
    named "USDT"). TronScan's ``amount`` field in ``withPriceTokens`` is the
    token's value priced in TRX (quantity × tokenPriceInTrx), NOT the token
    quantity, so it must never be used as the balance. The raw ``balance``
    divided by ``tokenDecimal`` is authoritative; ``quantity`` is a fallback.
    """
    for key in ("withPriceTokens", "trc20token_balances", "tokens"):
        for token in account.get(key) or []:
            token_id = str(
                token.get("tokenId") or token.get("token_id")
                or token.get("contract_address") or ""
            )
            if token_id != USDT_TRC20_CONTRACT:
                continue
            raw = token.get("balance")
            if raw not in {None, ""}:
                decimals = token.get("tokenDecimal", token.get("decimals"))
                decimals = 6 if decimals in {None, ""} else max(0, int(decimals))
                return Decimal(str(raw)) / (Decimal(10) ** decimals)
            if token.get("quantity") not in {None, ""}:
                return Decimal(str(token["quantity"]))
            return Decimal("0")
    return Decimal("0")


def tronscan_row_is_usdt(row: dict) -> bool:
    """Reject transfer rows of look-alike tokens; accept rows without contract info."""
    token_info = row.get("tokenInfo") or {}
    contract = str(
        row.get("contract_address") or token_info.get("tokenId") or ""
    )
    return not contract or contract == USDT_TRC20_CONTRACT


def trongrid_v1_usdt_balance(account: dict) -> Decimal:
    for token in account.get("trc20") or []:
        if isinstance(token, dict) and USDT_TRC20_CONTRACT in token:
            return Decimal(str(token[USDT_TRC20_CONTRACT])) / Decimal(1_000_000)
    return Decimal("0")


def tron_account_is_multisig(account: dict, address: str = "") -> bool:
    permissions = [account.get("owner_permission") or {}] + list(
        account.get("active_permission") or []
    )
    for permission in permissions:
        if not isinstance(permission, dict) or not permission:
            continue
        keys = permission.get("keys") or []
        if int(permission.get("threshold") or 1) != 1 or len(keys) != 1:
            return True
        if address and keys:
            key_address = str(keys[0].get("address") or "")
            if key_address.startswith("41"):
                key_address = tron_hex_to_base58(key_address)
            if key_address and key_address != address:
                return True
    return False


def tron_has_contract_authorization(payload: dict | list) -> bool | None:
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict):
        return None
    if "total" in payload or "rangeTotal" in payload:
        return int(payload.get("total") or payload.get("rangeTotal") or 0) > 0
    for key in ("approve_list", "approves", "list"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return bool(rows)
    if "data" in payload:
        data = payload.get("data")
        if isinstance(data, list):
            return bool(data)
        if isinstance(data, dict):
            rows = data.get("list") or data.get("rows") or data.get("data")
            if isinstance(rows, list):
                return bool(rows)
            if "total" in data or "rangeTotal" in data:
                return int(data.get("total") or data.get("rangeTotal") or 0) > 0
    return None


def combine_tron_transfer_counts(parts: list[int] | tuple[int, ...], cap: int) -> int:
    """Do not treat TronScan's 10000 cap as a real total when summing assets."""
    known = [int(part) for part in parts if 0 <= int(part) < TRONSCAN_RANGE_TOTAL_CAP]
    uncertain = any(int(part) >= TRONSCAN_RANGE_TOTAL_CAP for part in parts)
    total = sum(known)
    if uncertain:
        return min(cap, total)
    return min(cap + 1, total)


def trusted_tronscan_transfer_count(payload: dict, page_limit: int) -> int:
    """TronScan's rangeTotal is hard-capped at 10000 and is not a real count."""
    if not isinstance(payload, dict):
        raise ValueError("接口未返回交易总数")
    rows = payload.get("token_transfers") or payload.get("data") or []
    if not isinstance(rows, list):
        rows = []
    range_total = int(payload.get("rangeTotal") or 0)
    total = int(payload.get("total") or payload.get("totalCount") or 0)
    for candidate in (range_total, total):
        if 0 < candidate < TRONSCAN_RANGE_TOTAL_CAP:
            return candidate
    if len(rows) < max(1, int(page_limit)):
        return len(rows)
    return TRONSCAN_RANGE_TOTAL_CAP + 1


class ChainService:
    def __init__(self, config: Config):
        self.config = config
        self._rate_cache: tuple[float, UsdtRate] | None = None
        self._okx_cache: dict[str, tuple[float, list[MerchantQuote]]] = {}
        self._okx_snapshot_cache: tuple[
            float, float, dict[tuple[str, str], list[MerchantQuote]]
        ] | None = None
        self._okx_snapshot_lock = asyncio.Lock()
        self._tron_block_cache: dict[str, int] = {}
        self._tron_history_cache: dict[
            tuple[str, str, int, int], tuple[float, tuple[TronTransaction, ...]]
        ] = {}
        self._tron_recent_fallback: dict[
            tuple[str, str], tuple[float, tuple[TronTransaction, ...]]
        ] = {}
        self._tron_balance_cache: dict[str, tuple[float, TronBalance]] = {}
        self._tron_count_cache: dict[
            tuple[str, str, int, int], tuple[float, int]
        ] = {}
        self._tron_balance_provider_preference: tuple[str, float] | None = None
        self._tron_history_provider_preference: dict[str, tuple[str, float]] = {}
        self._tron_authorization_cache: dict[str, tuple[float, bool]] = {}
        self._governor = governor_for(
            config, shared=bool(getattr(config, "db_path", None)),
        )
        self._known_head = 0

    def _http(self, **kwargs) -> httpx.AsyncClient:
        """httpx client whose TRON traffic goes through the shared governor
        (API-key rotation, cross-process rate limit, cooldown, cache)."""
        return httpx.AsyncClient(transport=GovernedTransport(self._governor), **kwargs)

    @property
    def governor(self):
        return self._governor

    def _node_bases(self, prefer_public: bool = False) -> tuple[str, ...]:
        trongrid = str(getattr(self.config, "trongrid_url", "https://api.trongrid.io"))
        fallbacks = [node for node in config_fallback_nodes(self.config) if node != trongrid]
        if (self._governor.keys and not prefer_public) or not fallbacks:
            return (trongrid, *fallbacks)
        # without an API key TronGrid allows ~1 QPS (and block scanning would
        # burn the key's daily quota): prefer the public nodes
        return (*fallbacks, trongrid)

    def _tronscan_headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": "TGDirectoryBot/2.0"}
        api_key = getattr(self.config, "tronscan_api_key", "")
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        return headers

    @staticmethod
    def _headers_without_api_key(headers: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in headers.items() if key != "TRON-PRO-API-KEY"}

    async def _tronscan_get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = dict(headers or self._tronscan_headers())
        response = await client.get(url, params=params, headers=headers)
        if response.status_code in {401, 403} and headers.get("TRON-PRO-API-KEY"):
            response = await client.get(
                url, params=params, headers=self._headers_without_api_key(headers),
            )
        return response

    async def _tron_authorization_status(
        self, address: str, client: httpx.AsyncClient | None = None,
    ) -> bool | None:
        cached = self._tron_authorization_cache.get(address)
        if cached and time.monotonic() - cached[0] <= 600:
            return cached[1]
        own_client = client is None
        client = client or self._http(timeout=8, follow_redirects=True)
        endpoint = (
            f"{getattr(self.config, 'tronscan_api_url', 'https://apilist.tronscanapi.com')}"
            "/api/account/approve/list"
        )
        headers = self._tronscan_headers()
        public_headers = self._headers_without_api_key(headers)
        params = {"address": address, "start": "0", "limit": "1"}
        try:
            try:
                response = await client.get(
                    endpoint, params=params, headers=public_headers,
                )
                if response.status_code in {401, 403, 429}:
                    response = await self._tronscan_get(
                        client, endpoint, params, headers,
                    )
                if response.status_code in {401, 403}:
                    return (
                        cached[1]
                        if cached and time.monotonic() - cached[0] <= 21600
                        else None
                    )
                response.raise_for_status()
                status = tron_has_contract_authorization(response.json())
                if status is not None:
                    self._tron_authorization_cache[address] = (
                        time.monotonic(), status
                    )
                    return status
            except (httpx.HTTPError, ValueError, TypeError):
                pass

            responses = await asyncio.gather(*(
                self._tronscan_get(
                    client, endpoint, {
                        "address": address, "start": "0", "limit": "1",
                        "type": approval_type,
                    }, headers,
                )
                for approval_type in ("project", "token")
            ), return_exceptions=True)
            statuses: list[bool] = []
            for candidate in responses:
                if isinstance(candidate, Exception):
                    continue
                try:
                    candidate.raise_for_status()
                    parsed = tron_has_contract_authorization(candidate.json())
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
                if parsed is not None:
                    statuses.append(parsed)
            if any(statuses):
                self._tron_authorization_cache[address] = (time.monotonic(), True)
                return True
            if len(statuses) == 2:
                self._tron_authorization_cache[address] = (time.monotonic(), False)
                return False
            return cached[1] if cached and time.monotonic() - cached[0] <= 21600 else None
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError):
            return cached[1] if cached and time.monotonic() - cached[0] <= 21600 else None
        finally:
            if own_client:
                await client.aclose()

    async def tron_balance(self, address: str) -> TronBalance:
        address = validate_tron_address(address)
        cached = self._tron_balance_cache.get(address)
        if cached and time.monotonic() - cached[0] <= 30:
            return cached[1]

        preferred = self._tron_balance_provider_preference
        if preferred and time.monotonic() - preferred[1] <= 1800:
            preferred_provider = {
                "TronGrid节点": self._trongrid_node_balance,
                "TronScan": self._tronscan_balance,
                "Tokenview": self._tokenview_balance,
            }.get(preferred[0])
            if preferred_provider is not None:
                try:
                    result = await asyncio.wait_for(
                        preferred_provider(address), timeout=7
                    )
                except (TimeoutError, ChainQueryError, httpx.HTTPError, ValueError):
                    self._tron_balance_provider_preference = None
                else:
                    self._tron_balance_cache[address] = (time.monotonic(), result)
                    return result

        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self.config.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key
        try:
            async with self._http(timeout=6) as client:
                response = await client.get(
                    f"{self.config.trongrid_url}/v1/accounts/{address}",
                    params={"only_confirmed": "true"}, headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                query_results = await asyncio.gather(
                    asyncio.wait_for(client.post(
                        f"{self.config.trongrid_url}/wallet/getaccountresource",
                        json={"address": address, "visible": True},
                        headers={**headers, "content-type": "application/json"},
                    ), timeout=2),
                    asyncio.wait_for(client.post(
                        f"{self.config.trongrid_url}/wallet/triggerconstantcontract",
                        json={
                            "owner_address": address,
                            "contract_address": USDT_TRC20_CONTRACT,
                            "function_selector": "isBlackListed(address)",
                            "parameter": tron_address_abi_parameter(address),
                            "visible": True,
                        },
                        headers={**headers, "content-type": "application/json"},
                    ), timeout=1.5),
                    asyncio.wait_for(
                        self._tron_authorization_status(address), timeout=5
                    ),
                    asyncio.wait_for(client.get(
                        "https://blocksec.com/usdt-freeze-checker/api/addresses/lookup",
                        params={"address": address, "chain_id": "-2"},
                        headers={"accept": "application/json"},
                    ), timeout=1.2),
                    return_exceptions=True,
                )
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            return await self._fallback_tron_balance(address, exc)
        data = payload.get("data") or []
        if not data:
            result = TronBalance(address, False, Decimal("0"), Decimal("0"))
            self._tron_balance_cache[address] = (time.monotonic(), result)
            return result
        account = data[0]
        trx = Decimal(str(account.get("balance", 0))) / Decimal(1_000_000)
        transaction_errors: list[str] = []
        resource_payload: dict = {}
        resource_response = query_results[0]
        if isinstance(resource_response, Exception):
            transaction_errors.append(str(resource_response))
        else:
            try:
                resource_response.raise_for_status()
                resource_payload = resource_response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                transaction_errors.append(str(exc))
        contract_frozen: bool | None = None
        blacklist_response = query_results[1]
        if isinstance(blacklist_response, Exception):
            transaction_errors.append(str(blacklist_response))
        else:
            try:
                blacklist_response.raise_for_status()
                contract_frozen = tron_usdt_blacklist_status(blacklist_response.json())
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                transaction_errors.append(str(exc))
        is_frozen: bool | None = None
        blocksec_response = query_results[3]
        if not isinstance(blocksec_response, Exception):
            try:
                blocksec_payload = (
                    blocksec_response.json() if blocksec_response.status_code == 200 else None
                )
                is_frozen = blocksec_usdt_freeze_status(
                    blocksec_response.status_code, blocksec_payload
                )
            except (ValueError, TypeError):
                is_frozen = None
        if is_frozen is None:
            is_frozen = contract_frozen
        has_authorization: bool | None = None
        authorization_status = query_results[2]
        if isinstance(authorization_status, bool):
            has_authorization = authorization_status
        if has_authorization is None:
            try:
                has_authorization = await asyncio.wait_for(
                    self._tron_authorization_status(address), timeout=5
                )
            except (TimeoutError, ChainQueryError, httpx.HTTPError, ValueError, TypeError):
                has_authorization = None
        energy_remaining = max(
            0, int(resource_payload.get("EnergyLimit") or 0)
            - int(resource_payload.get("EnergyUsed") or 0),
        )
        bandwidth_remaining = max(
            0, int(resource_payload.get("NetLimit") or 0)
            - int(resource_payload.get("NetUsed") or 0),
        )
        free_bandwidth_remaining = max(
            0, int(resource_payload.get("freeNetLimit") or 0)
            - int(resource_payload.get("freeNetUsed") or 0),
        )
        result = TronBalance(
            address=address,
            activated=True,
            trx=trx,
            usdt=trongrid_v1_usdt_balance(account),
            created_at_ms=int(account["create_time"]) if account.get("create_time") else None,
            transactions=(),
            transactions_error="；".join(transaction_errors)[:500],
            energy_remaining=energy_remaining,
            bandwidth_remaining=bandwidth_remaining,
            free_bandwidth_remaining=free_bandwidth_remaining,
            is_frozen=is_frozen,
            is_multisig=tron_account_is_multisig(account, address),
            has_authorization=has_authorization,
        )
        self._tron_balance_cache[address] = (time.monotonic(), result)
        self._tron_balance_provider_preference = ("TronGrid V1", time.monotonic())
        return result

    async def _fallback_tron_balance(
        self, address: str, primary_error: Exception,
    ) -> TronBalance:
        providers: list[tuple[str, Callable[[str], Awaitable[TronBalance]]]] = [
            ("TronGrid节点", self._trongrid_node_balance),
            ("TronScan", self._tronscan_balance),
        ]
        if getattr(self.config, "tokenview_api_key", ""):
            providers.append(("Tokenview", self._tokenview_balance))
        errors = [("TronGrid V1", primary_error)]

        async def query_provider(
            name: str, provider: Callable[[str], Awaitable[TronBalance]],
        ) -> tuple[str, TronBalance | Exception]:
            try:
                return name, await asyncio.wait_for(provider(address), timeout=8)
            except Exception as exc:
                return name, exc

        tasks = [
            asyncio.create_task(query_provider(name, provider))
            for name, provider in providers
        ]
        try:
            for task in asyncio.as_completed(tasks):
                name, value = await task
                if isinstance(value, Exception):
                    errors.append((name, value))
                    continue
                self._tron_balance_cache[address] = (time.monotonic(), value)
                self._tron_balance_provider_preference = (name, time.monotonic())
                return value
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        cached = self._tron_balance_cache.get(address)
        if cached and time.monotonic() - cached[0] <= 180:
            return replace(
                cached[1],
                transactions_error="实时接口暂时不可用，显示3分钟内的最近查询结果",
            )
        details = "；".join(
            f"{name}: {self._short_error(error)}" for name, error in errors
        )
        logger.warning("TRON balance providers failed for %s: %s", address, details[:700])
        if any(is_busy_error(self._short_error(error)) for _, error in errors):
            raise ChainQueryError(BUSY_MESSAGE)
        raise ChainQueryError("波场账户接口暂时不可用，请稍后重试")

    @staticmethod
    def _short_error(error: Exception) -> str:
        cause = error.__cause__ or error
        text = " ".join(str(cause).split()) or type(cause).__name__
        return text[:160]

    async def _trongrid_node_balance(self, address: str) -> TronBalance:
        """Use java-tron's native account APIs when TronGrid V1 is unavailable.

        Every call goes through ``_node_post`` (TronGrid first, then the public
        fallback nodes), so a TronGrid 429 does not break the fallback.
        """
        body = {"address": address, "visible": True}
        parameter = tron_address_abi_parameter(address)

        def constant(selector: str) -> dict:
            return {
                "owner_address": address, "contract_address": USDT_TRC20_CONTRACT,
                "function_selector": selector, "parameter": parameter, "visible": True,
            }

        try:
            async with self._http(timeout=12) as client:
                account = await self._node_post(client, "/wallet/getaccount", body)
                if account.get("Error"):
                    raise ValueError(str(account["Error"]))
                if not account.get("address"):
                    return TronBalance(address, False, Decimal("0"), Decimal("0"))
                resource_payload, usdt_payload, blacklist_payload, authorization_status = await asyncio.gather(
                    asyncio.wait_for(self._node_post(
                        client, "/wallet/getaccountresource", body,
                    ), timeout=4),
                    asyncio.wait_for(self._node_post(
                        client, "/wallet/triggerconstantcontract",
                        constant("balanceOf(address)"),
                    ), timeout=6),
                    asyncio.wait_for(self._node_post(
                        client, "/wallet/triggerconstantcontract",
                        constant("isBlackListed(address)"),
                    ), timeout=3),
                    asyncio.wait_for(
                        self._tron_authorization_status(address), timeout=5
                    ),
                    return_exceptions=True,
                )
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            if is_busy_error(exc):
                raise ChainQueryError(BUSY_MESSAGE) from exc
            raise ChainQueryError("TronGrid节点账户接口不可用") from exc

        resource: dict = resource_payload if isinstance(resource_payload, dict) else {}
        if isinstance(usdt_payload, dict):
            values = usdt_payload.get("constant_result") or []
            try:
                usdt = Decimal(int(str(values[0]), 16)) / Decimal(1_000_000)
            except (IndexError, ValueError, TypeError, InvalidOperation) as exc:
                raise ChainQueryError("USDT 合约余额查询失败") from exc
        else:
            # never report a made-up 0 USDT balance
            raise ChainQueryError("USDT 合约余额查询失败") from (
                usdt_payload if isinstance(usdt_payload, Exception) else None
            )
        is_frozen: bool | None = None
        if isinstance(blacklist_payload, dict):
            try:
                is_frozen = tron_usdt_blacklist_status(blacklist_payload)
            except (ValueError, TypeError):
                is_frozen = None
        return TronBalance(
            address=address,
            activated=True,
            trx=Decimal(str(account.get("balance") or 0)) / Decimal(1_000_000),
            usdt=usdt,
            created_at_ms=int(account.get("create_time") or 0) or None,
            energy_remaining=max(
                0, int(resource.get("EnergyLimit") or 0)
                - int(resource.get("EnergyUsed") or 0),
            ),
            bandwidth_remaining=max(
                0, int(resource.get("NetLimit") or 0)
                - int(resource.get("NetUsed") or 0),
            ),
            free_bandwidth_remaining=max(
                0, int(resource.get("freeNetLimit") or 0)
                - int(resource.get("freeNetUsed") or 0),
            ),
            is_frozen=is_frozen,
            is_multisig=tron_account_is_multisig(account, address),
            has_authorization=(
                authorization_status if isinstance(authorization_status, bool) else None
            ),
        )

    async def tron_monitor_balance(self, address: str) -> TronBalance:
        """Fetch only current balances for monitoring; never request transactions."""
        address = validate_tron_address(address)
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self.config.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key
        try:
            async with self._http(timeout=10) as client:
                response = await self._history_get(
                    client,
                    f"{self.config.trongrid_url}/v1/accounts/{address}",
                    {"only_confirmed": "true"}, headers,
                )
                response.raise_for_status()
                data = response.json().get("data") or []
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError):
            try:
                trx, usdt, activated = await self._node_balance_pair(address)
            except ChainQueryError:
                return await self._tronscan_monitor_balance(address)
            return TronBalance(address, activated, trx, usdt)
        if not data:
            return TronBalance(address, False, Decimal("0"), Decimal("0"))
        account = data[0]
        if account.get("address") and account.get("address") not in {
            address, tron_address_hex(address),
        }:
            raise ChainQueryError("余额接口返回了其他地址的数据")
        return TronBalance(
            address, True,
            Decimal(str(account.get("balance") or 0)) / Decimal(1_000_000),
            trongrid_v1_usdt_balance(account),
        )

    # ---- 监控播报专用：权威节点余额（不走任何缓存） ----------------------

    def _trongrid_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json", "content-type": "application/json",
            "cache-control": "no-cache",
        }
        api_key = getattr(self.config, "trongrid_api_key", "")
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        return headers

    async def _node_post(
        self, client: httpx.AsyncClient, path: str, body: dict,
        *, prefer_public: bool = False,
    ) -> dict:
        """POST a read-only full-node API.

        TronGrid (with API key) first, then the public fallback nodes
        (``TRON_FALLBACK_NODES``) on 429 / local cooldown / 5xx / network errors.
        """
        headers = self._trongrid_headers()
        last_error: Exception | None = None
        busy = False
        for round_index in range(2):
            for base in self._node_bases(prefer_public):
                try:
                    response = await client.post(f"{base}{path}", json=body, headers=headers)
                except httpx.RequestError as exc:
                    last_error = exc
                    continue
                status = getattr(response, "status_code", 200)
                if status in {401, 403, 429} or status >= 500:
                    busy = busy or status == 429
                    last_error = ChainQueryError(
                        BUSY_MESSAGE if status == 429 else f"节点接口HTTP {status}"
                    )
                    continue
                try:
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    raise ChainQueryError(f"节点接口异常：{strip_urls(str(exc))[:120]}") from exc
                if not isinstance(payload, dict):
                    raise ChainQueryError("节点接口返回格式错误")
                return payload
            if round_index == 0:
                await asyncio.sleep(0.8 + random.uniform(0, 0.3))
        if busy:
            raise ChainQueryError(BUSY_MESSAGE)
        raise ChainQueryError(f"节点接口不可用：{strip_urls(str(last_error))[:120]}")

    async def _node_balance_pair(self, address: str) -> tuple[Decimal, Decimal, bool]:
        """(TRX spendable, USDT, activated) for exactly ``address`` from the full node.

        TRX = getaccount.balance (sun / 1e6, excludes staked/frozen TRX);
        USDT = balanceOf on the official contract (6 decimals).
        """
        address = validate_tron_address(address)
        async with self._http(timeout=8) as client:
            account = await self._node_post(
                client, "/wallet/getaccount", {"address": address, "visible": True}
            )
            if account.get("Error"):
                raise ChainQueryError(str(account["Error"]))
            returned = str(account.get("address") or "")
            if not returned:
                return Decimal("0"), Decimal("0"), False
            if returned not in {address, tron_address_hex(address)}:
                raise ChainQueryError("节点返回了其他地址的数据")
            contract = await self._node_post(
                client, "/wallet/triggerconstantcontract", {
                    "owner_address": address,
                    "contract_address": USDT_TRC20_CONTRACT,
                    "function_selector": "balanceOf(address)",
                    "parameter": tron_address_abi_parameter(address),
                    "visible": True,
                },
            )
        values = contract.get("constant_result") or []
        result = contract.get("result") or {}
        if not values or (isinstance(result, dict) and result.get("result") is False):
            raise ChainQueryError("USDT 合约余额查询失败")
        try:
            usdt = Decimal(int(str(values[0]) or "0", 16)) / Decimal(1_000_000)
            trx = Decimal(str(account.get("balance") or 0)) / Decimal(1_000_000)
        except (ValueError, InvalidOperation) as exc:
            raise ChainQueryError("节点余额格式错误") from exc
        return trx, usdt, True

    async def _indexer_balance_pair(self, address: str) -> tuple[Decimal, Decimal, bool]:
        """Second, independent source (TronGrid V1 indexer) for cross-checking."""
        headers = self._trongrid_headers()
        headers.pop("content-type", None)
        async with self._http(timeout=8) as client:
            response = await self._history_get(
                client, f"{self.config.trongrid_url}/v1/accounts/{address}",
                {"only_confirmed": "false"}, headers,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
        if not data:
            return Decimal("0"), Decimal("0"), False
        account = data[0]
        if account.get("address") and account.get("address") not in {
            address, tron_address_hex(address),
        }:
            raise ChainQueryError("索引接口返回了其他地址的数据")
        return (
            Decimal(str(account.get("balance") or 0)) / Decimal(1_000_000),
            trongrid_v1_usdt_balance(account), True,
        )

    async def tron_head(self) -> tuple[int, int]:
        """(head block number, head block timestamp ms) via a ~0.5 KB request.

        ``/wallet/getblock {"detail": false}`` returns only the header; older
        nodes fall back to ``getnowblock`` (full block, much larger).
        """
        with fresh_requests():
            async with self._http(timeout=8) as client:
                try:
                    payload = await self._node_post(
                        client, "/wallet/getblock", {"detail": False}, prefer_public=True,
                    )
                    if not (payload.get("block_header") or {}).get("raw_data"):
                        raise ChainQueryError("getblock 未返回区块头")
                except ChainQueryError as exc:
                    if BUSY_MESSAGE in str(exc):
                        raise
                    payload = await self._node_post(
                        client, "/wallet/getnowblock", {}, prefer_public=True,
                    )
        header = (payload.get("block_header") or {}).get("raw_data") or {}
        number = int(header.get("number") or 0)
        if number > self._known_head:
            self._known_head = number
        return number, int(header.get("timestamp") or 0)

    async def _node_head_block(self) -> int:
        number, _ = await self.tron_head()
        return number

    async def tron_block(self, number: int) -> dict:
        """Full block ``number`` (visible=false: hex addresses, ~18% smaller).

        Returns {} when the block does not exist yet. Processed in memory only.
        """
        last_error: Exception | None = None
        with fresh_requests():
            async with self._http(timeout=15) as client:
                for base in self._node_bases(prefer_public=True):
                    try:
                        response = await client.post(
                            f"{base}/wallet/getblockbynum",
                            json={"num": int(number), "visible": False},
                        )
                        if response.status_code in {401, 403, 429} or response.status_code >= 500:
                            last_error = ChainQueryError(
                                BUSY_MESSAGE if response.status_code == 429
                                else f"HTTP {response.status_code}"
                            )
                            continue
                        response.raise_for_status()
                        payload = response.json()
                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        last_error = exc
                        continue
                    if isinstance(payload, dict) and payload.get("block_header"):
                        return payload
                    last_error = ChainQueryError(f"节点尚无区块 {number}")
        if is_busy_error(last_error):
            raise ChainQueryError(BUSY_MESSAGE)
        return {}

    async def tron_block_txinfo(self, number: int) -> list[dict]:
        """All transaction receipts/logs of block ``number`` (USDT Transfer logs).

        Nodes answer ``[]`` both for a block without transactions and for a
        block they do not have yet, so an empty answer is only accepted after
        the same node confirms the block header exists (never miss a block
        because one fallback node lags behind).
        """
        last_error: Exception | None = None
        with fresh_requests():
            async with self._http(timeout=15) as client:
                for base in self._node_bases(prefer_public=True):
                    try:
                        response = await client.post(
                            f"{base}/wallet/gettransactioninfobyblocknum",
                            json={"num": int(number)},
                        )
                        if response.status_code in {401, 403, 429} or response.status_code >= 500:
                            last_error = ChainQueryError(
                                BUSY_MESSAGE if response.status_code == 429
                                else f"HTTP {response.status_code}"
                            )
                            continue
                        response.raise_for_status()
                        payload = response.json()
                        if isinstance(payload, dict) and payload.get("Error"):
                            last_error = ChainQueryError("区块回执接口返回错误")
                            continue
                        rows = payload if isinstance(payload, list) else []
                        if rows:
                            return [item for item in rows if isinstance(item, dict)]
                        header = await client.post(
                            f"{base}/wallet/getblock",
                            json={"id_or_num": str(int(number)), "detail": False},
                        )
                        header.raise_for_status()
                        if (header.json() or {}).get("block_header"):
                            return []
                        last_error = ChainQueryError(f"节点尚无区块 {number}")
                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        last_error = exc
        if is_busy_error(last_error):
            raise ChainQueryError(BUSY_MESSAGE)
        raise ChainQueryError(f"区块回执接口不可用：{strip_urls(str(last_error))[:120]}")

    async def tron_usdt_events(self, block_number: int) -> list[dict]:
        """USDT Transfer events of one block from TronGrid's event server.

        ~4 KB gzip per block vs ~12 KB for gettransactioninfobyblocknum.
        Pages via ``meta.fingerprint``. An empty list may also mean "not
        indexed yet" — the scanner then falls back to the node receipts.
        """
        url = (
            f"{self.config.trongrid_url}/v1/contracts/{USDT_TRC20_CONTRACT}/events"
        )
        params = {
            "event_name": "Transfer", "block_number": str(int(block_number)),
            "limit": "200",
        }
        rows: list[dict] = []
        with fresh_requests():
            async with self._http(timeout=12) as client:
                for _page in range(20):
                    response = await self._history_get(
                        client, url, params, self._trongrid_headers(),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    data = payload.get("data") or []
                    rows.extend(item for item in data if isinstance(item, dict))
                    fingerprint = (payload.get("meta") or {}).get("fingerprint")
                    if not fingerprint or len(data) < 200:
                        return rows
                    params = dict(params, fingerprint=str(fingerprint))
        raise ChainQueryError("USDT 事件分页过多")

    async def tron_verified_balance(
        self, address: str, *, min_block: int = 0,
        attempts: int = 3, delay: float = 2.0,
    ) -> TronBalance:
        """Fresh, verified balance of exactly ``address`` for alerts.

        * Never cached; never shared between addresses.
        * Authoritative source: full node getaccount + USDT balanceOf.
        * Waits (short retries) until the node has reached ``min_block``
          (the block of the detected transaction).
        * Cross-checks with the TronGrid indexer when it answers; if they
          disagree, retries; after retries the node value is accepted only
          if two consecutive node readings agree.
        Raises ChainQueryError when the value cannot be verified, so callers
        can show 「余额获取中/暂不可用」 instead of a wrong number.
        """
        address = validate_tron_address(address)
        with fresh_requests():
            return await self._tron_verified_balance(address, min_block, attempts, delay)

    async def _tron_verified_balance(
        self, address: str, min_block: int, attempts: int, delay: float,
    ) -> TronBalance:
        last_error: Exception | None = None
        previous: tuple[Decimal, Decimal, bool] | None = None
        for attempt in range(max(1, attempts)):
            if attempt:
                await asyncio.sleep(delay)
            try:
                if min_block and min_block > self._known_head:
                    head = await self._node_head_block()
                    if head and head < min_block:
                        last_error = ChainQueryError("节点尚未同步到该交易区块")
                        continue
                primary = await self._node_balance_pair(address)
            except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
                last_error = exc
                continue
            try:
                secondary = await self._indexer_balance_pair(address)
            except (ChainQueryError, httpx.HTTPError, ValueError, TypeError):
                secondary = None
            if secondary is None or secondary[:2] == primary[:2] or previous == primary:
                return TronBalance(address, primary[2], primary[0], primary[1])
            previous = primary
            last_error = ChainQueryError("节点与索引余额不一致，等待确认")
        if is_busy_error(last_error):
            raise ChainQueryError(f"余额暂未核实：{BUSY_MESSAGE}")
        raise ChainQueryError(f"余额暂未核实：{strip_urls(str(last_error))[:120]}")

    async def _tronscan_monitor_balance(self, address: str) -> TronBalance:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()
        try:
            async with self._http(timeout=10, follow_redirects=True) as client:
                response = await self._history_get(
                    client,
                    f"{base_url}/api/accountv2",
                    {"address": address}, headers,
                )
                response.raise_for_status()
                account = response.json()
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("波场监控余额接口暂时不可用") from exc
        if account.get("address") and str(account.get("address")) != address:
            raise ChainQueryError("TronScan返回了其他地址的数据")
        activated = bool(account.get("activated", account.get("address")))
        if not activated:
            return TronBalance(address, False, Decimal("0"), Decimal("0"))
        trx = Decimal(str(account.get("balanceStr") or account.get("balance") or 0)) / Decimal(1_000_000)
        usdt = tronscan_usdt_balance(account)
        return TronBalance(address, True, trx, usdt)

    async def tron_monitor_transactions(
        self, address: str, assets: tuple[str, ...], since_ms: int,
    ) -> tuple[TronTransaction, ...]:
        """Fetch only transactions created since the monitor's last cursor."""
        address = validate_tron_address(address)
        selected = tuple(dict.fromkeys(asset.upper() for asset in assets))
        if not selected or any(asset not in {"TRX", "USDT"} for asset in selected):
            raise ValueError("监控币种无效")
        end_ms = int(time.time() * 1000) + 5_000
        since_ms = max(0, min(int(since_ms), end_ms))
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self.config.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key

        async def fetch_one(client: httpx.AsyncClient, asset: str) -> list[TronTransaction]:
            suffix = "transactions/trc20" if asset == "USDT" else "transactions"
            params = {
                "only_confirmed": "true", "limit": "200",
                "order_by": "block_timestamp,asc",
                "min_timestamp": str(since_ms), "max_timestamp": str(end_ms),
            }
            if asset == "USDT":
                params["contract_address"] = USDT_TRC20_CONTRACT
            response = await self._history_get(
                client,
                f"{self.config.trongrid_url}/v1/accounts/{address}/{suffix}",
                params, headers,
            )
            response.raise_for_status()
            payload = response.json()
            return (
                self._parse_usdt_transactions(address, payload)
                if asset == "USDT" else self._parse_trx_transactions(address, payload)
            )

        async def fetch_trongrid() -> tuple[TronTransaction, ...]:
            async with self._http(timeout=10) as client:
                batches = await asyncio.gather(
                    *(fetch_one(client, asset) for asset in selected)
                )
            return tuple(item for batch in batches for item in batch)

        tasks = [
            asyncio.create_task(fetch_trongrid()),
            asyncio.create_task(
                self._tronscan_monitor_transactions(address, selected, since_ms, end_ms)
            ),
            asyncio.create_task(
                self._tronscan_compat_monitor_transactions(
                    address, selected, since_ms, end_ms
                )
            ),
        ]
        done, pending = await asyncio.wait(tasks, timeout=8)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        provider_values: list[object] = []
        for task in tasks:
            if task.cancelled():
                provider_values.append(ChainQueryError("增量交易接口超时"))
                continue
            exc = task.exception() if task.done() else None
            provider_values.append(exc if exc is not None else task.result())
        successful = [
            value for value in provider_values if not isinstance(value, Exception)
        ]
        if not successful:
            optional_providers = []
            if getattr(self.config, "oklink_api_key", ""):
                optional_providers.append(self._oklink_transaction_history)
            if getattr(self.config, "tokenview_api_key", ""):
                optional_providers.append(self._tokenview_transaction_history)
            for provider in optional_providers:
                try:
                    fallback_rows = await asyncio.gather(*(
                        provider(address, asset, 200, 1)
                        for asset in selected
                    ))
                except ChainQueryError:
                    continue
                successful.append(tuple(
                    item for rows in fallback_rows for item in rows
                    if since_ms <= item.timestamp_ms <= end_ms
                ))
                break
        if not successful:
            errors = "；".join(str(value) for value in provider_values)
            logger.warning("TRON monitor transaction providers failed: %s", errors[:700])
            if any(is_busy_error(value) for value in provider_values):
                raise ChainQueryError(BUSY_MESSAGE)
            raise ChainQueryError("波场监控交易接口暂时不可用，稍后自动重试")
        merged: dict[str, TronTransaction] = {}
        for values in successful:
            for item in values:
                if item.tx_id:
                    merged[item.tx_id] = item
        transactions = list(merged.values())
        transactions = [
            item for item in transactions
            if since_ms <= item.timestamp_ms <= end_ms and item.tx_id
        ]
        transactions.sort(key=lambda item: item.timestamp_ms)
        return tuple(transactions)

    async def _tronscan_compat_monitor_transactions(
        self, address: str, assets: tuple[str, ...],
        since_ms: int, end_ms: int,
    ) -> tuple[TronTransaction, ...]:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()

        async def fetch_one(
            client: httpx.AsyncClient, asset: str,
        ) -> tuple[TronTransaction, ...]:
            endpoint = "transfer/trc20" if asset == "USDT" else "transfer/trx"
            params = {
                "address": address, "start": "0", "limit": "200",
                "direction": "0", "reverse": "true",
                "start_timestamp": str(since_ms),
                "end_timestamp": str(end_ms),
            }
            if asset == "USDT":
                params["trc20Id"] = USDT_TRC20_CONTRACT
            response = await self._history_get(
                client,
                f"{base_url}/api/{endpoint}", params=params, headers=headers,
            )
            response.raise_for_status()
            rows = response.json().get("data") or []
            results: list[TronTransaction] = []
            for row in rows:
                tx_id = str(row.get("hash") or row.get("transaction_id") or "")
                timestamp_ms = int(
                    row.get("block_timestamp") or row.get("block_ts") or 0
                )
                if not tx_id or not since_ms <= timestamp_ms <= end_ms:
                    continue
                if asset == "USDT" and not tronscan_row_is_usdt(row):
                    continue
                decimals = max(0, int(row.get("decimals") or 6))
                amount = Decimal(str(row.get("amount") or row.get("quant") or 0)) / (
                    Decimal(10) ** decimals
                )
                sender = str(row.get("from") or row.get("from_address") or "")
                recipient = str(row.get("to") or row.get("to_address") or "")
                direction = (
                    "转出" if sender == address else
                    "转入" if recipient == address else "相关"
                )
                results.append(TronTransaction(
                    tx_id, timestamp_ms, direction, asset, amount,
                    recipient if direction == "转出" else sender,
                    int(row.get("block") or row.get("block_number") or 0),
                ))
            return tuple(results)

        try:
            async with self._http(timeout=10, follow_redirects=True) as client:
                batches = await asyncio.gather(*(
                    fetch_one(client, asset) for asset in assets
                ))
        except (
            httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation
        ) as exc:
            raise ChainQueryError("波场监控兼容接口暂时不可用") from exc
        return tuple(item for batch in batches for item in batch)

    async def tron_monitor_latest_transactions(
        self, address: str, assets: tuple[str, ...],
    ) -> tuple[TronTransaction, ...]:
        """Fetch only the newest confirmed transaction per selected asset for bootstrap."""
        address = validate_tron_address(address)
        selected = tuple(dict.fromkeys(asset.upper() for asset in assets))
        if not selected or any(asset not in {"TRX", "USDT"} for asset in selected):
            raise ValueError("监控币种无效")
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self.config.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key

        async def fetch_one(client: httpx.AsyncClient, asset: str) -> list[TronTransaction]:
            suffix = "transactions/trc20" if asset == "USDT" else "transactions"
            params = {
                "only_confirmed": "true", "limit": "1",
                "order_by": "block_timestamp,desc",
            }
            if asset == "USDT":
                params["contract_address"] = USDT_TRC20_CONTRACT
            response = await client.get(
                f"{self.config.trongrid_url}/v1/accounts/{address}/{suffix}",
                params=params, headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            return (
                self._parse_usdt_transactions(address, payload)
                if asset == "USDT" else self._parse_trx_transactions(address, payload)
            )

        try:
            async with self._http(timeout=10) as client:
                batches = await asyncio.gather(
                    *(fetch_one(client, asset) for asset in selected)
                )
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return await self._tronscan_monitor_latest_transactions(address, selected)
        rows = [item for batch in batches for item in batch if item.tx_id]
        rows.sort(key=lambda item: (item.block_number, item.timestamp_ms, item.tx_id))
        return tuple(rows)

    async def _tronscan_monitor_latest_transactions(
        self, address: str, assets: tuple[str, ...],
    ) -> tuple[TronTransaction, ...]:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()

        async def fetch_one(client: httpx.AsyncClient, asset: str) -> list[TronTransaction]:
            endpoint = "token_trc20/transfers" if asset == "USDT" else "transfer"
            params = {
                "start": "0", "limit": "1", "direction": "all",
                "confirm": "0",
            }
            if asset == "USDT":
                params.update({
                    "relatedAddress": address,
                    "contract_address": USDT_TRC20_CONTRACT,
                })
            else:
                params.update({
                    "address": address, "token": "_", "sort": "-timestamp",
                    "count": "false",
                })
            response = await client.get(
                f"{base_url}/api/{endpoint}", params=params, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("token_transfers") if asset == "USDT" else payload.get("data")
            rows = rows or []
            results: list[TronTransaction] = []
            for row in rows[:1]:
                token_info = row.get("tokenInfo") or {}
                if asset == "USDT" and not tronscan_row_is_usdt(row):
                    continue
                if asset == "USDT":
                    tx_id = str(row.get("transaction_id") or "")
                    timestamp_ms = int(row.get("block_ts") or 0)
                    sender = str(row.get("from_address") or "")
                    recipient = str(row.get("to_address") or "")
                    raw_amount = row.get("quant") or 0
                else:
                    tx_id = str(row.get("transactionHash") or "")
                    timestamp_ms = int(row.get("timestamp") or 0)
                    sender = str(row.get("transferFromAddress") or "")
                    recipient = str(row.get("transferToAddress") or "")
                    raw_amount = row.get("amount") or 0
                decimals = max(0, int(token_info.get("tokenDecimal") or 6))
                direction = "转出" if sender == address else "转入" if recipient == address else "相关"
                results.append(TronTransaction(
                    tx_id, timestamp_ms, direction, asset,
                    Decimal(str(raw_amount)) / (Decimal(10) ** decimals),
                    recipient if direction == "转出" else sender,
                    int(row.get("block") or 0),
                ))
            return results

        try:
            async with self._http(timeout=10, follow_redirects=True) as client:
                batches = await asyncio.gather(
                    *(fetch_one(client, asset) for asset in assets)
                )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise ChainQueryError("波场监控基线接口暂时不可用") from exc
        rows = [item for batch in batches for item in batch if item.tx_id]
        rows.sort(key=lambda item: (item.block_number, item.timestamp_ms, item.tx_id))
        return tuple(rows)

    async def _tronscan_monitor_transactions(
        self, address: str, assets: tuple[str, ...],
        since_ms: int, end_ms: int,
    ) -> tuple[TronTransaction, ...]:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()

        async def fetch_one(client: httpx.AsyncClient, asset: str) -> list[TronTransaction]:
            endpoint = "token_trc20/transfers" if asset == "USDT" else "transfer"
            results: list[TronTransaction] = []
            for offset in (0,):
                params = {
                    "start": str(offset), "limit": "200", "direction": "all",
                    "confirm": "0", "start_timestamp": str(since_ms),
                    "end_timestamp": str(end_ms),
                }
                if asset == "USDT":
                    params.update({
                        "relatedAddress": address,
                        "contract_address": USDT_TRC20_CONTRACT,
                    })
                else:
                    params.update({
                        "address": address, "token": "_", "sort": "+timestamp",
                        "count": "true",
                    })
                response = await client.get(
                    f"{base_url}/api/{endpoint}", params=params, headers=headers
                )
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("token_transfers") if asset == "USDT" else payload.get("data")
                rows = rows or []
                for row in rows:
                    token_info = row.get("tokenInfo") or {}
                    if asset == "USDT" and not tronscan_row_is_usdt(row):
                        continue
                    if asset == "USDT":
                        tx_id = str(row.get("transaction_id") or "")
                        timestamp_ms = int(row.get("block_ts") or 0)
                        sender = str(row.get("from_address") or "")
                        recipient = str(row.get("to_address") or "")
                        raw_amount = row.get("quant") or 0
                    else:
                        if str(token_info.get("tokenId") or row.get("tokenName") or "_") != "_":
                            continue
                        tx_id = str(row.get("transactionHash") or "")
                        timestamp_ms = int(row.get("timestamp") or 0)
                        sender = str(row.get("transferFromAddress") or "")
                        recipient = str(row.get("transferToAddress") or "")
                        raw_amount = row.get("amount") or 0
                    decimals = max(0, int(token_info.get("tokenDecimal") or 6))
                    amount = Decimal(str(raw_amount)) / (Decimal(10) ** decimals)
                    direction = "转出" if sender == address else "转入" if recipient == address else "相关"
                    results.append(TronTransaction(
                        tx_id, timestamp_ms, direction, asset, amount,
                        recipient if direction == "转出" else sender,
                        int(row.get("block") or 0),
                    ))
            return results

        try:
            async with self._http(timeout=10, follow_redirects=True) as client:
                batches = await asyncio.gather(
                    *(fetch_one(client, asset) for asset in assets)
                )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise ChainQueryError("波场监控增量交易接口暂时不可用") from exc
        return tuple(item for batch in batches for item in batch)

    async def _tronscan_balance(self, address: str) -> TronBalance:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()
        account: dict | None = None
        last_error: Exception | None = None
        try:
            async with self._http(timeout=12, follow_redirects=True) as client:
                for endpoint in ("accountv2", "account"):
                    try:
                        response = await self._history_get(
                            client,
                            f"{base_url}/api/{endpoint}",
                            {"address": address}, headers,
                        )
                        response.raise_for_status()
                        candidate = response.json()
                        if isinstance(candidate, dict) and (
                            candidate.get("address") or candidate.get("activated") is False
                        ):
                            account = candidate
                            break
                    except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
                        last_error = exc
                if account is None:
                    raise ChainQueryError("TronScan账户接口不可用") from last_error
                authorization_status, blocksec_response = await asyncio.gather(
                    asyncio.wait_for(
                        self._tron_authorization_status(address), timeout=5
                    ),
                    asyncio.wait_for(client.get(
                        "https://blocksec.com/usdt-freeze-checker/api/addresses/lookup",
                        params={"address": address, "chain_id": "-2"},
                        headers={"accept": "application/json"},
                    ), timeout=1.2),
                    return_exceptions=True,
                )
        except ChainQueryError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("TronScan账户接口不可用") from exc

        if account.get("address") and str(account.get("address")) != address:
            raise ChainQueryError("TronScan返回了其他地址的数据")
        activated = bool(account.get("activated", account.get("address")))
        if not activated:
            return TronBalance(address, False, Decimal("0"), Decimal("0"))
        trx = Decimal(str(account.get("balanceStr") or account.get("balance") or 0)) / Decimal(1_000_000)
        usdt = tronscan_usdt_balance(account)
        bandwidth = account.get("bandwidth") or {}
        owner_permission = account.get("ownerPermission") or account.get("owner_permission") or {}
        active_permissions = account.get("activePermissions") or account.get("active_permission") or []
        permission_account = {
            "owner_permission": owner_permission,
            "active_permission": active_permissions,
        }
        has_authorization = (
            authorization_status if isinstance(authorization_status, bool) else None
        )
        is_frozen: bool | None = None
        if not isinstance(blocksec_response, Exception):
            try:
                payload = blocksec_response.json() if blocksec_response.status_code == 200 else None
                is_frozen = blocksec_usdt_freeze_status(blocksec_response.status_code, payload)
            except (ValueError, TypeError):
                is_frozen = None
        return TronBalance(
            address=address, activated=True, trx=trx, usdt=usdt,
            created_at_ms=int(account.get("date_created") or account.get("create_time") or 0) or None,
            transactions=(),
            transactions_error="",
            energy_remaining=max(0, int(
                bandwidth.get("energyRemaining")
                or account.get("totalEnergyRemaining") or 0
            )),
            bandwidth_remaining=max(0, int(
                bandwidth.get("netRemaining")
                or account.get("netRemainingCal") or 0
            )),
            free_bandwidth_remaining=max(0, int(
                bandwidth.get("freeNetRemaining")
                or int(bandwidth.get("freeNetLimit") or 0)
                - int(bandwidth.get("freeNetUsed") or 0)
            )),
            is_frozen=is_frozen,
            is_multisig=tron_account_is_multisig(permission_account, address),
            has_authorization=has_authorization,
        )

    async def _tronscan_latest_transactions(
        self, address: str, asset: str,
    ) -> tuple[TronTransaction, ...]:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()
        if asset == "USDT":
            endpoint = "token_trc20/transfers"
            params = {
                "relatedAddress": address,
                "contract_address": USDT_TRC20_CONTRACT,
                "start": "0", "limit": "50", "direction": "all", "confirm": "0",
            }
        else:
            endpoint = "transfer"
            params = {
                "address": address, "token": "_", "sort": "-timestamp",
                "count": "false", "start": "0", "limit": "50", "confirm": "0",
            }
        try:
            async with self._http(timeout=10, follow_redirects=True) as client:
                response = await self._history_get(
                    client, f"{base_url}/api/{endpoint}", params, headers
                )
                response.raise_for_status()
                payload = response.json()
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError(f"TronScan最近{asset}交易查询失败") from exc
        rows = payload.get("token_transfers") if asset == "USDT" else payload.get("data")
        transactions: list[TronTransaction] = []
        for row in rows or []:
            token_info = row.get("tokenInfo") or {}
            if asset == "USDT":
                tx_id = str(row.get("transaction_id") or "")
                timestamp_ms = int(row.get("block_ts") or 0)
                sender = str(row.get("from_address") or "")
                recipient = str(row.get("to_address") or "")
                raw_amount = row.get("quant") or 0
            else:
                if str(token_info.get("tokenId") or row.get("tokenName") or "_") != "_":
                    continue
                tx_id = str(row.get("transactionHash") or "")
                timestamp_ms = int(row.get("timestamp") or 0)
                sender = str(row.get("transferFromAddress") or "")
                recipient = str(row.get("transferToAddress") or "")
                raw_amount = row.get("amount") or 0
            decimals = max(0, int(token_info.get("tokenDecimal") or 6))
            amount = Decimal(str(raw_amount)) / (Decimal(10) ** decimals)
            if not tx_id or amount < MIN_DISPLAY_TRANSFER:
                continue
            direction = "转出" if sender == address else "转入" if recipient == address else "相关"
            transactions.append(TronTransaction(
                tx_id, timestamp_ms, direction, asset, amount,
                recipient if direction == "转出" else sender,
                int(row.get("block") or 0),
            ))
        return tuple(transactions)

    async def transaction_with_block(self, transaction: TronTransaction) -> TronTransaction:
        if transaction.block_number or not transaction.tx_id:
            return transaction
        block_number = self._tron_block_cache.get(transaction.tx_id, 0)
        if not block_number:
            try:
                async with self._http(timeout=8) as client:
                    for path in (
                        "/wallet/gettransactioninfobyid",
                        "/walletsolidity/gettransactioninfobyid",
                    ):
                        try:
                            payload = await self._node_post(
                                client, path, {"value": transaction.tx_id}
                            )
                        except ChainQueryError:
                            continue
                        block_number = int(payload.get("blockNumber") or 0)
                        if block_number:
                            break
            except (httpx.HTTPError, ValueError, TypeError):
                block_number = 0
            if not block_number:
                block_number = await self._tronscan_block_number(transaction.tx_id)
            if block_number:
                self._tron_block_cache[transaction.tx_id] = block_number
        return replace(transaction, block_number=block_number) if block_number else transaction

    async def _tronscan_block_number(self, tx_id: str) -> int:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        try:
            async with self._http(timeout=8, follow_redirects=True) as client:
                response = await self._history_get(
                    client, f"{base_url}/api/transaction-info",
                    {"hash": tx_id}, self._tronscan_headers(),
                )
                response.raise_for_status()
                return int(response.json().get("block") or 0)
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError):
            return 0

    async def tron_transaction_history(
        self, address: str, asset: str, max_records: int = 20_000,
        days: int = 0,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        address = validate_tron_address(address)
        asset = asset.upper()
        if asset not in {"TRX", "USDT"}:
            raise ValueError("交易币种只能是 TRX 或 USDT")
        max_records = max(10, min(int(max_records), 100_000))
        cache_key = (address, asset, max(0, int(days)), max_records)
        cached = self._tron_history_cache.get(cache_key)
        if cached and cached[1] and time.monotonic() - cached[0] < 300:
            if progress:
                await progress({
                    "provider": "缓存", "pages": 0,
                    "scanned_records": len(cached[1]),
                    "kept_records": len(cached[1]), "cached": True,
                })
            return cached[1][:max_records]
        if days > 0:
            cutoff_ms = int(time.time() * 1000) - days * 86400 * 1000
            for key, candidate in self._tron_history_cache.items():
                cached_address, cached_asset, cached_days, _ = key
                if (
                    cached_address == address and cached_asset == asset
                    and (cached_days == 0 or cached_days >= days)
                    and candidate[1] and time.monotonic() - candidate[0] < 300
                ):
                    result = tuple(
                        item for item in candidate[1]
                        if item.timestamp_ms >= cutoff_ms
                    )[:max_records]
                    if progress:
                        await progress({
                            "provider": "缓存", "pages": 0,
                            "scanned_records": len(result),
                            "kept_records": len(result), "cached": True,
                        })
                    return result
        errors: list[str] = []
        providers = [
            ("TronGrid", self._trongrid_transaction_history),
            ("TronScan", self._tronscan_official_transaction_history),
            ("TronScan兼容", self._tronscan_transaction_history),
        ]
        if getattr(self.config, "oklink_api_key", ""):
            providers.append(("OKLink", self._oklink_transaction_history))
        if getattr(self.config, "tokenview_api_key", ""):
            providers.append(("Tokenview", self._tokenview_transaction_history))
        preferred = self._tron_history_provider_preference.get(asset)
        if preferred and time.monotonic() - preferred[1] <= 1800:
            providers.sort(key=lambda item: item[0] != preferred[0])
        else:
            self._tron_history_provider_preference.pop(asset, None)
        successful_empty = False
        for provider_name, provider in providers:
            try:
                result = await provider(
                    address, asset, max_records, days, progress
                )
            except ChainQueryError as exc:
                errors.append(f"{provider_name}: {exc}")
                if progress:
                    await progress({
                        "provider": provider_name, "switching": True,
                        "error": str(exc), "pages": 0,
                        "scanned_records": 0, "kept_records": 0,
                    })
                continue
            if result:
                self._tron_history_provider_preference[asset] = (
                    provider_name, time.monotonic()
                )
                break
            successful_empty = True
            if progress:
                await progress({
                    "provider": provider_name, "switching": True,
                    "error": "接口返回空记录，继续查询备用接口", "pages": 0,
                    "scanned_records": 0, "kept_records": 0,
                })
        else:
            if successful_empty:
                return ()
            if cached and time.monotonic() - cached[0] < 21600:
                return cached[1][:max_records]
            raise ChainQueryError(
                "多个交易记录接口暂时不可用，请稍后重试"
            )
        self._tron_history_cache[cache_key] = (time.monotonic(), result)
        return result

    @staticmethod
    async def _history_get(
        client: httpx.AsyncClient, url: str,
        params: dict[str, str], headers: dict[str, str],
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= 2:
                    raise ChainQueryError(f"网络连接失败：{exc}") from exc
                await asyncio.sleep(min(4.0, 2 ** attempt + random.uniform(0, 0.5)))
                continue
            status_code = getattr(response, "status_code", 200)
            response_headers = getattr(response, "headers", {}) or {}
            if status_code == 429 and response_headers.get(COOLDOWN_HEADER):
                raise ChainQueryError(BUSY_MESSAGE)
            if status_code in {401, 403} and headers.get("TRON-PRO-API-KEY"):
                headers = ChainService._headers_without_api_key(headers)
                continue
            if status_code != 429:
                return response
            if attempt >= 2:
                break
            retry_after = 0.0
            try:
                retry_after = float(
                    (getattr(response, "headers", {}) or {}).get("Retry-After") or 0
                )
            except (TypeError, ValueError):
                retry_after = 0.0
            delay = retry_after or min(
                8.0, 2 ** (attempt + 1) + random.uniform(0, 0.5)
            )
            await asyncio.sleep(delay)
        if last_error:
            raise ChainQueryError(f"网络连接失败：{last_error}") from last_error
        raise ChainQueryError(BUSY_MESSAGE)

    async def _oklink_transaction_history(
        self, address: str, asset: str, max_records: int, days: int,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        api_key = getattr(self.config, "oklink_api_key", "")
        if not api_key:
            raise ChainQueryError("OKLink API Key 未配置")
        cutoff_ms = int(time.time() * 1000) - days * 86400 * 1000 if days else 0
        headers = {
            "accept": "application/json", "Ok-Access-Key": api_key,
            "user-agent": "TGDirectoryBot/2.0",
        }
        results: list[TronTransaction] = []
        try:
            async with self._http(timeout=15, follow_redirects=True) as client:
                for page in range(1, min(100, (max_records + 49) // 50) + 1):
                    params = {
                        "chainShortName": "TRON", "address": address,
                        "page": str(page), "limit": "50",
                    }
                    if asset == "USDT":
                        params.update({
                            "protocolType": "token_20",
                            "tokenContractAddress": USDT_TRC20_CONTRACT,
                        })
                    response = await self._history_get(
                        client,
                        "https://www.oklink.com/api/v5/explorer/address/transaction-list",
                        params, headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if str(payload.get("code")) != "0":
                        raise ValueError(str(payload.get("msg") or "接口返回错误"))
                    container = (payload.get("data") or [{}])[0]
                    rows = (
                        container.get("transactionLists")
                        or container.get("transactionList") or []
                    )
                    reached_cutoff = False
                    for row in rows:
                        tx_id = str(row.get("txId") or row.get("txid") or "")
                        timestamp_ms = int(row.get("transactionTime") or row.get("time") or 0)
                        if timestamp_ms and timestamp_ms < 10_000_000_000:
                            timestamp_ms *= 1000
                        if cutoff_ms and timestamp_ms < cutoff_ms:
                            reached_cutoff = True
                            continue
                        symbol = str(
                            row.get("transactionSymbol") or row.get("symbol") or "TRX"
                        ).upper()
                        if symbol != asset:
                            continue
                        sender = str(row.get("from") or row.get("fromAddress") or "")
                        recipient = str(row.get("to") or row.get("toAddress") or "")
                        direction = (
                            "转出" if sender == address else
                            "转入" if recipient == address else "相关"
                        )
                        amount = abs(Decimal(str(row.get("amount") or row.get("value") or 0)))
                        if not tx_id or amount < MIN_HISTORY_TRANSFER:
                            continue
                        results.append(TronTransaction(
                            tx_id, timestamp_ms, direction, asset, amount,
                            recipient if direction == "转出" else sender,
                            int(row.get("height") or row.get("blockHeight") or 0),
                        ))
                    if progress:
                        await progress({
                            "provider": "OKLink", "pages": page,
                            "scanned_records": page * len(rows),
                            "kept_records": len(results),
                        })
                    if not rows or reached_cutoff or len(results) >= max_records:
                        break
        except (
            httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation
        ) as exc:
            raise ChainQueryError("OKLink 查询未完成") from exc
        results.sort(key=lambda item: item.timestamp_ms, reverse=True)
        return tuple(dict.fromkeys(results))[:max_records]

    async def _tokenview_balance(self, address: str) -> TronBalance:
        api_key = getattr(self.config, "tokenview_api_key", "")
        if not api_key:
            raise ChainQueryError("Tokenview API Key 未配置")
        try:
            async with self._http(timeout=15, follow_redirects=True) as client:
                response = await client.post(
                    "https://services.tokenview.io/vipapi/trx/accounts/getaccount",
                    params={"apikey": api_key},
                    json={"address": address, "trc20only": 0, "visible": True},
                    headers={"accept": "application/json", "content-type": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("Tokenview账户接口不可用") from exc
        if int(payload.get("code") or 0) != 1:
            raise ChainQueryError(
                "Tokenview账户接口返回错误：" + str(payload.get("msg") or "未知错误")
            )
        account = payload.get("data") or {}
        if not isinstance(account, dict) or not account.get("address"):
            return TronBalance(address, False, Decimal("0"), Decimal("0"))
        usdt = Decimal("0")
        for token in account.get("trc20") or []:
            token_info = token.get("tokenInfo") or {}
            contract = str(token.get("hash") or token_info.get("h") or "")
            if contract != USDT_TRC20_CONTRACT:
                continue  # 只认官方合约，防止同名假 USDT
            decimals = max(0, int(token_info.get("d") or 6))
            usdt = Decimal(str(token.get("balance") or 0)) / (Decimal(10) ** decimals)
            break
        return TronBalance(
            address=address,
            activated=True,
            trx=Decimal(str(account.get("balance") or 0)) / Decimal(1_000_000),
            usdt=usdt,
            created_at_ms=int(account.get("create_time") or 0) or None,
            is_multisig=tron_account_is_multisig(account, address),
            is_frozen=None,
            has_authorization=None,
        )

    async def _tokenview_transaction_history(
        self, address: str, asset: str, max_records: int, days: int,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        api_key = getattr(self.config, "tokenview_api_key", "")
        if not api_key:
            raise ChainQueryError("Tokenview API Key 未配置")
        cutoff_ms = int(time.time() * 1000) - days * 86400 * 1000 if days else 0
        results: list[TronTransaction] = []
        try:
            async with self._http(timeout=15, follow_redirects=True) as client:
                for page in range(1, min(50, (max_records + 49) // 50) + 1):
                    endpoint = (
                        f"usdt/addresstxlist/{address}/{page}/50"
                        if asset == "USDT" else
                        f"address/trx/{address}/{page}/50"
                    )
                    response = await self._history_get(
                        client, f"https://services.tokenview.io/vipapi/{endpoint}",
                        {"apikey": api_key}, {"accept": "application/json"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if int(payload.get("code") or 0) != 1:
                        raise ValueError(str(payload.get("msg") or "接口返回错误"))
                    data = payload.get("data") or {}
                    rows = data.get("txs") if isinstance(data, dict) else data
                    rows = rows or []
                    reached_cutoff = False
                    for row in rows:
                        network = str(row.get("network") or "").upper()
                        if asset == "USDT" and network and network not in {"TRX", "TRON"}:
                            continue
                        timestamp_ms = int(row.get("time") or row.get("timestamp") or 0)
                        if timestamp_ms and timestamp_ms < 10_000_000_000:
                            timestamp_ms *= 1000
                        if cutoff_ms and timestamp_ms < cutoff_ms:
                            reached_cutoff = True
                            continue
                        sender = str(row.get("from") or "")
                        recipient = str(row.get("to") or "")
                        direction = (
                            "转出" if sender == address else
                            "转入" if recipient == address else "相关"
                        )
                        amount = abs(
                            Decimal(str(row.get("value") or row.get("amount") or 0))
                        ) / Decimal(1_000_000)
                        tx_id = str(row.get("txid") or row.get("hash") or "")
                        if not tx_id or amount < MIN_HISTORY_TRANSFER:
                            continue
                        results.append(TronTransaction(
                            tx_id, timestamp_ms, direction, asset, amount,
                            recipient if direction == "转出" else sender,
                            int(row.get("height") or row.get("block_no") or 0),
                        ))
                    if progress:
                        await progress({
                            "provider": "Tokenview", "pages": page,
                            "scanned_records": page * len(rows),
                            "kept_records": len(results),
                        })
                    if not rows or reached_cutoff or len(results) >= max_records:
                        break
        except (
            httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation
        ) as exc:
            raise ChainQueryError("Tokenview 查询未完成") from exc
        merged = {item.tx_id: item for item in results}
        return tuple(sorted(
            merged.values(), key=lambda item: item.timestamp_ms, reverse=True
        )[:max_records])

    async def _trongrid_transaction_history(
        self, address: str, asset: str, max_records: int, days: int,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        suffix = "transactions/trc20" if asset == "USDT" else "transactions"
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self.config.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key
        params = {
            "only_confirmed": "true", "limit": "200",
            "order_by": "block_timestamp,desc",
        }
        if asset == "USDT":
            params["contract_address"] = USDT_TRC20_CONTRACT
        transactions: list[TronTransaction] = []
        fingerprint = ""
        page_count = 0
        scanned_records = 0
        started_at = time.monotonic()
        end_ms = int(time.time() * 1000)
        cutoff_ms = (
            end_ms - days * 86400 * 1000
            if days > 0 else 0
        )
        if cutoff_ms:
            params["min_timestamp"] = str(cutoff_ms)
            params["max_timestamp"] = str(end_ms)
        try:
            async with self._http(timeout=15) as client:
                while True:
                    if fingerprint:
                        params["fingerprint"] = fingerprint
                    response = await self._history_get(
                        client,
                        f"{self.config.trongrid_url}/v1/accounts/{address}/{suffix}",
                        params, headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    raw_rows = payload.get("data") or []
                    page_count += 1
                    scanned_records += len(raw_rows)
                    batch = (
                        self._parse_usdt_transactions(address, payload)
                        if asset == "USDT" else self._parse_trx_transactions(address, payload)
                    )
                    transactions.extend(
                        item for item in batch
                        if item.amount >= MIN_HISTORY_TRANSFER
                        and (not cutoff_ms or item.timestamp_ms >= cutoff_ms)
                        and item.timestamp_ms <= end_ms
                    )
                    if progress:
                        await progress({
                            "provider": "TronGrid", "pages": page_count,
                            "scanned_records": scanned_records,
                            "kept_records": len(transactions),
                            "oldest_timestamp": min(
                                (int(row.get("block_timestamp") or 0) for row in raw_rows),
                                default=0,
                            ),
                            "large": (
                                page_count >= 3 or scanned_records >= 500
                                or time.monotonic() - started_at >= 3
                            ),
                        })
                    if len(transactions) >= max_records:
                        break
                    oldest_timestamp = min(
                        (int(row.get("block_timestamp") or 0) for row in raw_rows),
                        default=0,
                    )
                    if cutoff_ms and oldest_timestamp and oldest_timestamp < cutoff_ms:
                        break
                    next_fingerprint = str((payload.get("meta") or {}).get("fingerprint") or "")
                    if not raw_rows or not next_fingerprint or next_fingerprint == fingerprint:
                        break
                    fingerprint = next_fingerprint
                    await asyncio.sleep(0.15 if self.config.trongrid_api_key else 0.35)
        except ChainQueryError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ChainQueryError("TronGrid 查询未完成") from exc
        transactions.sort(key=lambda item: item.timestamp_ms, reverse=True)
        return tuple(transactions[:max_records])

    async def _tronscan_transaction_history(
        self, address: str, asset: str, max_records: int, days: int,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        endpoint = "transfer/trc20" if asset == "USDT" else "transfer/trx"
        end_ms = int(time.time() * 1000)
        cutoff_ms = (
            end_ms - days * 86400 * 1000 if days > 0 else 0
        )
        headers = self._tronscan_headers()
        transactions: list[TronTransaction] = []
        seen_ids: set[str] = set()
        completed = False
        page_count = 0
        scanned_records = 0
        started_at = time.monotonic()
        try:
            async with self._http(timeout=15, follow_redirects=True) as client:
                for start in range(0, max_records, 50):
                    params: dict[str, str] = {
                        "address": address, "start": str(start), "limit": "50",
                        "direction": "0", "reverse": "true",
                    }
                    if cutoff_ms:
                        params["start_timestamp"] = str(cutoff_ms)
                    if asset == "USDT":
                        params["trc20Id"] = USDT_TRC20_CONTRACT
                    response = await self._history_get(
                        client,
                        f"{getattr(self.config, 'tronscan_api_url', 'https://apilist.tronscanapi.com')}/api/{endpoint}",
                        params, headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if int(payload.get("code") or 0) not in {0, 200}:
                        raise ValueError(str(payload.get("message") or "接口返回错误"))
                    rows = payload.get("data") or []
                    page_count += 1
                    scanned_records += len(rows)
                    reached_cutoff = False
                    for row in rows:
                        tx_id = str(row.get("hash") or row.get("transaction_id") or "")
                        if not tx_id or tx_id in seen_ids:
                            continue
                        timestamp_ms = int(row.get("block_timestamp") or row.get("block_ts") or 0)
                        if cutoff_ms and timestamp_ms < cutoff_ms:
                            reached_cutoff = True
                            continue
                        decimals = max(0, int(row.get("decimals") or 6))
                        amount = Decimal(str(row.get("amount") or row.get("quant") or 0)) / (
                            Decimal(10) ** decimals
                        )
                        if amount < MIN_HISTORY_TRANSFER:
                            continue
                        sender = str(row.get("from") or row.get("from_address") or "")
                        recipient = str(row.get("to") or row.get("to_address") or "")
                        direction = "转出" if sender == address else "转入" if recipient == address else "相关"
                        counterparty = recipient if direction == "转出" else sender
                        transactions.append(TronTransaction(
                            tx_id, timestamp_ms, direction, asset, amount, counterparty,
                            int(row.get("block") or row.get("block_number") or 0),
                        ))
                        seen_ids.add(tx_id)
                    if progress:
                        await progress({
                            "provider": "TronScan兼容", "pages": page_count,
                            "scanned_records": scanned_records,
                            "kept_records": len(transactions),
                            "oldest_timestamp": min(
                                (int(row.get("block_timestamp") or row.get("block_ts") or 0)
                                 for row in rows), default=0,
                            ),
                            "large": (
                                page_count >= 3 or scanned_records >= 500
                                or time.monotonic() - started_at >= 3
                            ),
                        })
                    if len(rows) < 50 or reached_cutoff:
                        completed = True
                        break
                    await asyncio.sleep(0.15 if self.config.trongrid_api_key else 0.35)
        except ChainQueryError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise ChainQueryError("TronScan 查询未完成") from exc
        transactions.sort(key=lambda item: item.timestamp_ms, reverse=True)
        return tuple(transactions[:max_records])

    async def _tronscan_official_transaction_history(
        self, address: str, asset: str, max_records: int, days: int,
        progress: HistoryProgress | None = None,
    ) -> tuple[TronTransaction, ...]:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        endpoint = "token_trc20/transfers" if asset == "USDT" else "transfer"
        headers = self._tronscan_headers()
        end_ms = int(time.time() * 1000)
        cutoff_ms = end_ms - days * 86400 * 1000 if days > 0 else 0
        window_end = end_ms
        offset = 0
        page_count = 0
        scanned_records = 0
        started_at = time.monotonic()
        transactions: list[TronTransaction] = []
        seen_ids: set[str] = set()
        try:
            async with self._http(timeout=15, follow_redirects=True) as client:
                while True:
                    params: dict[str, str] = {
                        "start": str(offset), "limit": "50",
                        "start_timestamp": str(cutoff_ms) if cutoff_ms else "0",
                        "end_timestamp": str(window_end),
                        "direction": "all", "confirm": "0",
                    }
                    if asset == "USDT":
                        params.update({
                            "relatedAddress": address,
                            "contract_address": USDT_TRC20_CONTRACT,
                        })
                    else:
                        params.update({
                            "address": address, "sort": "-timestamp",
                            "count": "true", "token": "_",
                        })
                    response = await self._history_get(
                        client, f"{base_url}/api/{endpoint}", params, headers
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload.get("token_transfers") if asset == "USDT" else payload.get("data")
                    rows = rows or []
                    page_count += 1
                    scanned_records += len(rows)
                    oldest_timestamp = 0
                    for row in rows:
                        if asset == "USDT":
                            tx_id = str(row.get("transaction_id") or "")
                            timestamp_ms = int(row.get("block_ts") or 0)
                            sender = str(row.get("from_address") or "")
                            recipient = str(row.get("to_address") or "")
                            token_info = row.get("tokenInfo") or {}
                            decimals = max(0, int(token_info.get("tokenDecimal") or 6))
                            raw_amount = row.get("quant") or 0
                        else:
                            token_info = row.get("tokenInfo") or {}
                            if str(token_info.get("tokenId") or row.get("tokenName") or "_") != "_":
                                continue
                            tx_id = str(row.get("transactionHash") or "")
                            timestamp_ms = int(row.get("timestamp") or 0)
                            sender = str(row.get("transferFromAddress") or "")
                            recipient = str(row.get("transferToAddress") or "")
                            decimals = max(0, int(token_info.get("tokenDecimal") or 6))
                            raw_amount = row.get("amount") or 0
                        if timestamp_ms:
                            oldest_timestamp = (
                                min(oldest_timestamp, timestamp_ms)
                                if oldest_timestamp else timestamp_ms
                            )
                        if not tx_id or tx_id in seen_ids:
                            continue
                        amount = Decimal(str(raw_amount)) / (Decimal(10) ** decimals)
                        if amount < MIN_HISTORY_TRANSFER:
                            continue
                        direction = (
                            "转出" if sender == address else
                            "转入" if recipient == address else "相关"
                        )
                        transactions.append(TronTransaction(
                            tx_id, timestamp_ms, direction, asset, amount,
                            recipient if direction == "转出" else sender,
                            int(row.get("block") or 0),
                        ))
                        seen_ids.add(tx_id)
                    if progress:
                        await progress({
                            "provider": "TronScan", "pages": page_count,
                            "scanned_records": scanned_records,
                            "kept_records": len(transactions),
                            "oldest_timestamp": oldest_timestamp,
                            "large": (
                                page_count >= 3 or scanned_records >= 500
                                or time.monotonic() - started_at >= 3
                            ),
                        })
                    if not rows:
                        break
                    if len(transactions) >= max_records:
                        break
                    range_total = int(payload.get("rangeTotal") or payload.get("total") or 0)
                    offset += len(rows)
                    if len(rows) < 50 or (range_total and offset >= range_total):
                        break
                    if offset >= 9_950:
                        if not oldest_timestamp or oldest_timestamp >= window_end:
                            raise ChainQueryError("TronScan分页未能继续")
                        window_end = oldest_timestamp - 1
                        offset = 0
                    await asyncio.sleep(0.15 if self.config.trongrid_api_key else 0.35)
        except ChainQueryError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise ChainQueryError("TronScan官方接口查询未完成") from exc
        transactions.sort(key=lambda item: item.timestamp_ms, reverse=True)
        return tuple(transactions[:max_records])

    async def tron_transaction_count(
        self, address: str, asset: str, days: int, cap: int = 100_000,
    ) -> int:
        address = validate_tron_address(address)
        asset = asset.casefold()
        if asset not in {"trx", "usdt", "both"}:
            raise ValueError("交易币种无效")
        days = max(1, min(int(days), 365))
        cap = max(1, min(int(cap), 100_000))
        cache_key = (address, asset, days, cap)
        cached = self._tron_count_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 120:
            return cached[1]
        selected_assets = ["USDT", "TRX"] if asset == "both" else [asset.upper()]
        errors: list[str] = []
        providers = [
            ("TronScan", self._tronscan_transaction_count),
            ("TronScan兼容", self._tronscan_compat_transaction_count),
        ]
        if getattr(self.config, "trongrid_api_key", ""):
            providers.append(("TronGrid", self._trongrid_transaction_count))
        for provider_name, provider in providers:
            try:
                count = await asyncio.wait_for(
                    provider(address, selected_assets, days, cap), timeout=8
                )
            except ChainQueryError as exc:
                errors.append(f"{provider_name}: {exc}")
                continue
            except TimeoutError:
                errors.append(f"{provider_name}: 汇总接口超时")
                continue
            self._tron_count_cache[cache_key] = (time.monotonic(), count)
            return count
        raise ChainQueryError("交易次数汇总接口均不可用：" + "；".join(errors)[:500])

    async def _tronscan_transaction_count(
        self, address: str, assets: list[str], days: int, cap: int,
    ) -> int:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 86400 * 1000

        async def count_one(selected: str) -> int:
            if selected == "USDT":
                endpoint = "token_trc20/transfers"
                params = {
                    "relatedAddress": address,
                    "contract_address": USDT_TRC20_CONTRACT,
                    "start": "0", "limit": "20", "direction": "all",
                    "confirm": "0", "start_timestamp": str(start_ms),
                    "end_timestamp": str(now_ms),
                }
            else:
                endpoint = "transfer"
                params = {
                    "address": address, "token": "_", "sort": "-timestamp",
                    "count": "true", "start": "0", "limit": "20",
                    "direction": "all", "confirm": "0",
                    "start_timestamp": str(start_ms), "end_timestamp": str(now_ms),
                }
            async with self._http(timeout=10, follow_redirects=True) as client:
                response = await self._tronscan_get(
                    client, f"{base_url}/api/{endpoint}", params, headers,
                )
                response.raise_for_status()
                return trusted_tronscan_transfer_count(response.json(), 20)

        try:
            return combine_tron_transfer_counts(
                await asyncio.gather(*(count_one(item) for item in assets)), cap
            )
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("TronScan交易次数暂时无法统计") from exc

    async def _tronscan_compat_transaction_count(
        self, address: str, assets: list[str], days: int, cap: int,
    ) -> int:
        base_url = getattr(
            self.config, "tronscan_api_url", "https://apilist.tronscanapi.com"
        )
        headers = self._tronscan_headers()
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 86400 * 1000

        async def count_one(selected: str) -> int:
            endpoint = "transfer/trc20" if selected == "USDT" else "transfer/trx"
            params = {
                "address": address, "start": "0", "limit": "20",
                "direction": "0", "reverse": "true",
                "start_timestamp": str(start_ms), "end_timestamp": str(end_ms),
            }
            if selected == "USDT":
                params["trc20Id"] = USDT_TRC20_CONTRACT
            async with self._http(timeout=10, follow_redirects=True) as client:
                response = await self._tronscan_get(
                    client, f"{base_url}/api/{endpoint}", params, headers,
                )
                response.raise_for_status()
                return trusted_tronscan_transfer_count(response.json(), 20)

        try:
            return combine_tron_transfer_counts(
                await asyncio.gather(*(count_one(item) for item in assets)), cap
            )
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("TronScan兼容交易次数暂时无法统计") from exc

    async def _trongrid_transaction_count(
        self, address: str, assets: list[str], days: int, cap: int,
    ) -> int:
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if getattr(self.config, "trongrid_api_key", ""):
            headers["TRON-PRO-API-KEY"] = self.config.trongrid_api_key
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 86400 * 1000

        async def count_one(selected: str, remaining: int) -> int:
            suffix = "transactions/trc20" if selected == "USDT" else "transactions"
            params = {
                "only_confirmed": "true", "limit": "200",
                "order_by": "block_timestamp,desc",
                "min_timestamp": str(start_ms), "max_timestamp": str(end_ms),
            }
            if selected == "USDT":
                params["contract_address"] = USDT_TRC20_CONTRACT
            count = 0
            fingerprint = ""
            async with self._http(timeout=12) as client:
                while count <= remaining:
                    if fingerprint:
                        params["fingerprint"] = fingerprint
                    response = await self._history_get(
                        client,
                        f"{self.config.trongrid_url}/v1/accounts/{address}/{suffix}",
                        params, headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload.get("data") or []
                    count += len(rows)
                    if count > remaining or not rows:
                        break
                    fingerprint = str(
                        (payload.get("meta") or {}).get("fingerprint") or ""
                    )
                    if not fingerprint:
                        break
                    await asyncio.sleep(0.2)
            return count

        try:
            total = 0
            for selected in assets:
                total += await count_one(selected, cap - total)
                if total > cap:
                    return cap + 1
            return total
        except (ChainQueryError, httpx.HTTPError, ValueError, TypeError) as exc:
            raise ChainQueryError("TronGrid交易次数暂时无法统计") from exc

    async def tron_year_transaction_count(self, address: str, asset: str) -> int:
        return await self.tron_transaction_count(address, asset, 365, 100_000)

    @staticmethod
    def _parse_trx_transactions(address: str, payload: dict) -> list[TronTransaction]:
        transactions: list[TronTransaction] = []
        for row in payload.get("data") or []:
            contracts = (row.get("raw_data") or {}).get("contract") or []
            if not contracts or contracts[0].get("type") != "TransferContract":
                continue
            value = (contracts[0].get("parameter") or {}).get("value") or {}
            sender = tron_hex_to_base58(str(value.get("owner_address") or ""))
            recipient = tron_hex_to_base58(str(value.get("to_address") or ""))
            direction = "转出" if sender == address else "转入" if recipient == address else "相关"
            counterparty = recipient if direction == "转出" else sender
            transactions.append(
                TronTransaction(
                    tx_id=str(row.get("txID") or ""),
                    timestamp_ms=int(row.get("block_timestamp") or 0),
                    direction=direction,
                    asset="TRX",
                    amount=Decimal(str(value.get("amount") or 0)) / Decimal(1_000_000),
                    counterparty=counterparty,
                    block_number=int(row.get("blockNumber") or row.get("block_number") or 0),
                )
            )
        return transactions

    @staticmethod
    def _parse_usdt_transactions(address: str, payload: dict) -> list[TronTransaction]:
        transactions: list[TronTransaction] = []
        for row in payload.get("data") or []:
            token = row.get("token_info") or {}
            token_addr = str(token.get("address") or "")
            if token_addr and token_addr != USDT_TRC20_CONTRACT:
                continue
            sender = str(row.get("from") or "")
            recipient = str(row.get("to") or "")
            direction = "转出" if sender == address else "转入" if recipient == address else "相关"
            counterparty = recipient if direction == "转出" else sender
            decimals = int(token.get("decimals") or 6)
            if decimals < 0:
                decimals = 6
            transactions.append(
                TronTransaction(
                    tx_id=str(row.get("transaction_id") or ""),
                    timestamp_ms=int(row.get("block_timestamp") or 0),
                    direction=direction,
                    asset="USDT",
                    amount=Decimal(str(row.get("value") or 0)) / (Decimal(10) ** decimals),
                    counterparty=counterparty,
                    block_number=int(row.get("blockNumber") or row.get("block_number") or 0),
                )
            )
        return transactions

    async def usdt_rate(self) -> UsdtRate:
        now = time.monotonic()
        if self._rate_cache and now - self._rate_cache[0] < 30:
            return self._rate_cache[1]
        headers = {"accept": "application/json"}
        if self.config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self.config.coingecko_api_key
        try:
            async with self._http(timeout=12) as client:
                response = await client.get(
                    f"{self.config.coingecko_url}/simple/price",
                    params={
                        "ids": "tether",
                        "vs_currencies": "usd,cny",
                        "include_last_updated_at": "true",
                        "precision": "6",
                    },
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()["tether"]
                rate = UsdtRate(
                    usd=Decimal(str(data["usd"])),
                    cny=Decimal(str(data["cny"])),
                    updated_at=int(data.get("last_updated_at") or time.time()),
                )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ChainQueryError(f"汇率接口查询失败：{exc}") from exc
        self._rate_cache = (now, rate)
        return rate

    async def okx_p2p_quotes(
        self, direction: str, payment: str = "",
    ) -> list[MerchantQuote]:
        if direction not in {"buy", "sell"}:
            raise ValueError("direction must be buy or sell")
        payment_codes = {"": "", "bank": "bank", "wechat": "wxPay", "alipay": "aliPay"}
        if payment not in payment_codes:
            raise ValueError("payment must be bank, wechat or alipay")
        now = time.monotonic()
        cache_key = f"{direction}:{payment}"
        cached = self._okx_cache.get(cache_key)
        if cached and now - cached[0] < 60:
            return cached[1]
        # OKX side is the merchant's side, the reverse of the user's action.
        api_side = "sell" if direction == "buy" else "buy"
        result_key = api_side
        params = {
            "cryptoCurrency": "USDT",
            "fiatCurrency": "CNY",
            "side": api_side,
            "userType": "all",
            "showTrade": "false",
            "currentPage": "1",
            "numberPerPage": "50" if payment else "10",
        }
        if payment:
            params["paymentMethod"] = payment_codes[payment]
        try:
            async with self._http(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    self.config.okx_p2p_url,
                    params=params,
                    headers={"accept": "application/json", "user-agent": "Mozilla/5.0 TGDirectoryBot/2.0"},
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("code") != 0:
                raise ValueError(payload.get("msg") or f"OKX code {payload.get('code')}")
            rows = (payload.get("data") or {}).get(result_key) or []
            if payment:
                payment_code = payment_codes[payment]
                rows = [
                    row for row in rows
                    if payment_code in (row.get("paymentMethods") or [])
                ]
            quotes = [self._merchant_quote(index, direction, row) for index, row in enumerate(rows[:10], 1)]
            if not quotes:
                raise ValueError("当前没有可显示的商户广告")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ChainQueryError(f"OKX P2P 榜单查询失败：{exc}") from exc
        self._okx_cache[cache_key] = (now, quotes)
        return quotes

    async def okx_p2p_snapshot(
        self,
    ) -> tuple[dict[tuple[str, str], list[MerchantQuote]], float]:
        now = time.monotonic()
        cached = self._okx_snapshot_cache
        if cached and now - cached[0] < 60:
            return cached[2], cached[1]
        async with self._okx_snapshot_lock:
            now = time.monotonic()
            cached = self._okx_snapshot_cache
            if cached and now - cached[0] < 60:
                return cached[2], cached[1]
            keys = tuple(
                (direction, payment)
                for direction in ("buy", "sell")
                for payment in ("bank", "wechat", "alipay")
            )
            values = await asyncio.gather(*(
                self.okx_p2p_quotes(direction, payment)
                for direction, payment in keys
            ), return_exceptions=True)
            snapshot: dict[tuple[str, str], list[MerchantQuote]] = {}
            errors: list[str] = []
            for key, value in zip(keys, values):
                if isinstance(value, Exception):
                    errors.append(str(value))
                else:
                    snapshot[key] = value
            if not snapshot:
                raise ChainQueryError("OKX P2P 六组报价均读取失败：" + "；".join(errors))
            captured_at = time.time()
            self._okx_snapshot_cache = (now, captured_at, snapshot)
            return snapshot, captured_at

    @staticmethod
    def _merchant_quote(rank: int, direction: str, row: dict) -> MerchantQuote:
        payments = {
            "aliPay": "支付宝",
            "wxPay": "微信",
            "bank": "银行卡",
        }
        return MerchantQuote(
            rank=rank,
            direction=direction,
            merchant=str(row.get("nickName") or "未知商户"),
            price=Decimal(str(row.get("price") or 0)),
            available_usdt=Decimal(str(row.get("availableAmount") or 0)),
            min_cny=Decimal(str(row.get("quoteMinAmountPerOrder") or 0)),
            max_cny=Decimal(str(row.get("quoteMaxAmountPerOrder") or 0)),
            payment_methods=tuple(payments.get(str(item), str(item)) for item in (row.get("paymentMethods") or [])),
            completion_rate=Decimal(str(row.get("completedRate") or 0)) * Decimal(100),
            orders=int(row.get("completedOrderQuantity") or 0),
            avg_seconds=int(row.get("avgCompletedTime") or 0),
        )
