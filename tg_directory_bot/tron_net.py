"""TRON API request governor.

Every TronGrid / full-node / TronScan request made by ``ChainService`` goes
through :class:`GovernedTransport`, which provides:

* multi API-key rotation (``TRONGRID_API_KEYS``) with per-key cooldown on 429,
  and the ``TRON-PRO-API-KEY`` header injected on *all* TronGrid calls
  (``/v1/*``, ``/wallet/*``, ``/walletsolidity/*``); the key is never sent to
  third-party fallback nodes;
* a token-bucket rate limiter per upstream that is shared by the mother bot and
  all clone processes (state in a small locked JSON file), so N processes never
  multiply the request rate;
* a shared cooldown after 429 (honouring ``Retry-After`` / "suspended for N s"),
  during which requests fail fast locally instead of hammering the API;
* request coalescing + a short TTL cache for identical read requests
  (bypassed with :func:`fresh_requests` for alert verification / block scans).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import httpx

try:  # pragma: no cover - always available on Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

BUSY_MESSAGE = "波场接口繁忙，稍后自动重试"
COOLDOWN_HEADER = "x-tron-local-cooldown"
API_KEY_HEADER = "TRON-PRO-API-KEY"
DEFAULT_TRONGRID_URL = "https://api.trongrid.io"
DEFAULT_TRONSCAN_URL = "https://apilist.tronscanapi.com"
# Verified live (getaccount / triggerconstantcontract / getblockbynum /
# gettransactioninfobyblocknum all answer) on 2026-10-04.
DEFAULT_FALLBACK_NODES = ("https://api.tronstack.io", "https://tron-rpc.publicnode.com")
CACHEABLE_POST_SUFFIXES = (
    "/wallet/getaccount", "/wallet/getaccountresource",
    "/wallet/triggerconstantcontract", "/wallet/gettransactioninfobyid",
    "/walletsolidity/gettransactioninfobyid",
)
MAX_QUEUE_SECONDS = 12.0
_fresh: contextvars.ContextVar[bool] = contextvars.ContextVar("tron_fresh", default=False)
_ALL_KEYS_COOLING = object()


@contextmanager
def fresh_requests() -> Iterator[None]:
    """Bypass the TTL cache / coalescing (still rate-limited)."""
    token = _fresh.set(True)
    try:
        yield
    finally:
        _fresh.reset(token)


def parse_api_keys(single: str = "", multi: str = "") -> tuple[str, ...]:
    keys: list[str] = []
    for raw in [*(multi or "").replace(";", ",").replace("\n", ",").split(","), single or ""]:
        key = raw.strip()
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def mask_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 10:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def _fingerprint(key: str | None) -> str:
    if not key:
        return "-"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def retry_after_seconds(headers, body: bytes | str = b"") -> float:
    try:
        value = float((headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return min(value, 300.0)
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body or "")
    match = re.search(r"suspended for (\d+(?:\.\d+)?) ?s", text)
    if match:
        return min(float(match.group(1)), 300.0)
    return 0.0


def is_busy_error(error: object) -> bool:
    text = str(error or "")
    return any(marker in text for marker in (
        "429", "Too Many Requests", "限流", BUSY_MESSAGE, "rate exceeded",
        "allowed_rps", "suspended",
    ))


def strip_urls(text: str) -> str:
    text = re.sub(r"\s*for url '[^']*'", "", str(text or ""))
    text = re.sub(r"https?://\S+", "", text)
    return " ".join(text.split())


class SharedState:
    """A tiny JSON document shared by all bot processes (flock-protected)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._memory: dict = {}

    @contextmanager
    def locked(self) -> Iterator[dict]:
        if self.path is None or fcntl is None:
            yield self._memory
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError:
            yield self._memory
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read()
                try:
                    state = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                yield state
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(state, separators=(",", ":")))
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class TronGovernor:
    def __init__(
        self, *, keys: tuple[str, ...] = (),
        trongrid_url: str = DEFAULT_TRONGRID_URL,
        tronscan_url: str = DEFAULT_TRONSCAN_URL,
        fallback_nodes: tuple[str, ...] = DEFAULT_FALLBACK_NODES,
        max_qps: float = 0.0, tronscan_qps: float = 3.0, fallback_qps: float = 5.0,
        cache_seconds: float = 5.0, state_path: Path | None = None,
    ):
        self.keys = tuple(keys)
        self.trongrid_hosts = {_host(trongrid_url), "api.trongrid.io"} - {""}
        self.tronscan_hosts = {_host(tronscan_url), "apilist.tronscanapi.com"} - {""}
        self.fallback_hosts = {_host(url) for url in fallback_nodes} - {""}
        default_qps = 8.0 * max(1, len(self.keys)) if self.keys else 1.0
        self.rates = {
            "trongrid": float(max_qps) if max_qps and max_qps > 0 else default_qps,
            "tronscan": max(0.2, float(tronscan_qps or 3.0)),
        }
        self.fallback_qps = max(0.2, float(fallback_qps or 5.0))
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.state = SharedState(state_path)
        self._cache: dict[tuple, tuple[float, int, list, bytes]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self.stats = {"requests": 0, "throttled": 0, "cache_hits": 0, "coalesced": 0}

    # ---- classification -------------------------------------------------
    def gate_for(self, host: str) -> str | None:
        host = (host or "").casefold()
        if host in self.trongrid_hosts:
            return "trongrid"
        if host in self.tronscan_hosts:
            return "tronscan"
        if host in self.fallback_hosts:
            return f"node:{host}"
        return None

    def rate_for(self, gate: str) -> float:
        return self.rates.get(gate, self.fallback_qps)

    # ---- limiter / cooldown ---------------------------------------------
    def cooldown_remaining(self, gate: str) -> float:
        with self.state.locked() as state:
            return max(0.0, float((state.get("cool") or {}).get(gate, 0)) - time.time())

    def _reserve(self, gate: str) -> float | None:
        """Take one token; returns seconds to wait, or None when cooling down."""
        rate = self.rate_for(gate)
        burst = max(1.0, rate)
        now = time.time()
        with self.state.locked() as state:
            cool = state.setdefault("cool", {})
            if float(cool.get(gate, 0)) > now:
                return None
            buckets = state.setdefault("buckets", {})
            tokens, stamp = buckets.get(gate) or [burst, now]
            stamp = min(float(stamp), now)
            tokens = min(burst, float(tokens) + (now - stamp) * rate) - 1.0
            wait = 0.0 if tokens >= 0 else -tokens / rate
            if wait > MAX_QUEUE_SECONDS:
                buckets[gate] = [tokens + 1.0, now]
                return None
            buckets[gate] = [tokens, now]
            return wait

    async def acquire(self, gate: str) -> bool:
        wait = self._reserve(gate)
        if wait is None:
            return False
        if wait > 0:
            await asyncio.sleep(wait)
        return True

    def pick_key(self, gate: str):
        if gate != "trongrid" or not self.keys:
            return None
        now = time.time()
        with self.state.locked() as state:
            key_state = state.setdefault("keys", {})
            usable = [
                key for key in self.keys
                if float((key_state.get(_fingerprint(key)) or {}).get("until", 0)) <= now
            ]
            if not usable:
                invalid = all(
                    (key_state.get(_fingerprint(key)) or {}).get("invalid")
                    for key in self.keys
                )
                # all keys rejected as invalid: degrade to keyless access
                return None if invalid else _ALL_KEYS_COOLING
            index = int(state.get("rr", 0)) % len(usable)
            state["rr"] = int(state.get("rr", 0)) + 1
            return usable[index]

    def report(
        self, gate: str, key: str | None, status: int,
        retry_after: float = 0.0, body: bytes = b"",
    ) -> bool:
        """Record an upstream answer; returns True if another key may be tried."""
        now = time.time()
        with self.state.locked() as state:
            strikes = state.setdefault("strikes", {})
            key_state = state.setdefault("keys", {})
            cool = state.setdefault("cool", {})
            fp = _fingerprint(key)
            if status not in {401, 403, 429}:
                strikes.pop(gate, None)
                if key and fp in key_state:
                    key_state[fp].pop("strikes", None)
                return False
            text = body.decode("utf-8", "ignore").casefold() if body else ""
            if key:
                entry = key_state.setdefault(fp, {})
                count = int(entry.get("strikes", 0)) + 1
                entry["strikes"] = count
                if status == 401 or "invalid" in text or "not exist" in text:
                    entry["invalid"] = True
                    entry["until"] = now + 600
                else:
                    delay = retry_after or min(60.0, 2.0 ** count)
                    entry["until"] = now + delay * random.uniform(1.0, 1.2)
                usable = [
                    k for k in self.keys
                    if float((key_state.get(_fingerprint(k)) or {}).get("until", 0)) <= now
                ]
                if usable:
                    return True
                if all((key_state.get(_fingerprint(k)) or {}).get("invalid") for k in self.keys):
                    return True  # retry keyless once
                cool[gate] = max(
                    float(cool.get(gate, 0)),
                    min(float((key_state.get(_fingerprint(k)) or {}).get("until", now))
                        for k in self.keys),
                )
                return False
            count = int(strikes.get(gate, 0)) + 1
            strikes[gate] = count
            delay = retry_after or min(60.0, 2.0 ** count)
            cool[gate] = max(float(cool.get(gate, 0)), now + delay * random.uniform(1.0, 1.2))
            return False

    # ---- cache ------------------------------------------------------------
    def cache_key(self, gate: str, request: httpx.Request) -> tuple | None:
        if self.cache_seconds <= 0 or _fresh.get():
            return None
        path = request.url.path
        if request.method == "GET":
            if gate == "tronscan" or path.startswith("/v1/"):
                return ("GET", str(request.url), b"")
            return None
        if request.method == "POST" and path.endswith(CACHEABLE_POST_SUFFIXES):
            return ("POST", str(request.url), bytes(request.content or b""))
        return None

    def cached(self, key: tuple) -> tuple[int, list, bytes] | None:
        item = self._cache.get(key)
        if not item:
            return None
        if time.monotonic() - item[0] > self.cache_seconds:
            self._cache.pop(key, None)
            return None
        return item[1], item[2], item[3]

    def store(self, key: tuple, status: int, headers: list, content: bytes) -> None:
        now = time.monotonic()
        if len(self._cache) > 512:
            for stale in [k for k, v in self._cache.items() if now - v[0] > self.cache_seconds]:
                self._cache.pop(stale, None)
            if len(self._cache) > 512:
                self._cache.clear()
        self._cache[key] = (now, status, headers, content)

    def describe(self) -> str:
        nodes = ",".join(sorted(self.fallback_hosts)) or "无"
        keys = ", ".join(mask_key(key) for key in self.keys) or "未配置(严格限流!)"
        return (
            f"TronGrid API keys={len(self.keys)} [{keys}] "
            f"trongrid_qps={self.rate_for('trongrid'):g} tronscan_qps={self.rate_for('tronscan'):g} "
            f"fallback_nodes={nodes} cache={self.cache_seconds:g}s "
            f"shared_state={self.state.path or 'memory'}"
        )


def _response_from(request: httpx.Request, status: int, headers: list, content: bytes) -> httpx.Response:
    return httpx.Response(status, headers=headers, content=content, request=request)


def _plain_headers(response: httpx.Response) -> list:
    skip = {"content-encoding", "content-length", "transfer-encoding"}
    return [(k, v) for k, v in response.headers.items() if k.casefold() not in skip]


def busy_response(request: httpx.Request, retry_after: float = 0.0) -> httpx.Response:
    headers = [(COOLDOWN_HEADER, "1"), ("content-type", "text/plain; charset=utf-8")]
    if retry_after > 0:
        headers.append(("Retry-After", str(max(1, int(retry_after + 0.999)))))
    return httpx.Response(429, headers=headers, content=BUSY_MESSAGE.encode(), request=request)


def _default_inner() -> httpx.AsyncBaseTransport:
    proxy = (
        os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        or os.getenv("ALL_PROXY") or os.getenv("all_proxy") or ""
    ).strip()
    if proxy:
        try:
            return httpx.AsyncHTTPTransport(proxy=proxy)
        except Exception:  # pragma: no cover - unsupported proxy scheme
            pass
    return httpx.AsyncHTTPTransport()


class GovernedTransport(httpx.AsyncBaseTransport):
    def __init__(self, governor: TronGovernor, inner: httpx.AsyncBaseTransport | None = None):
        self.governor = governor
        self.inner = inner or _default_inner()

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gate = self.governor.gate_for(request.url.host)
        if gate is None:
            return await self.inner.handle_async_request(request)
        key = self.governor.cache_key(gate, request)
        if key is None:
            return await self._send(gate, request, buffered=False)
        hit = self.governor.cached(key)
        if hit:
            self.governor.stats["cache_hits"] += 1
            return _response_from(request, *hit)
        pending = self.governor._inflight.get(key)
        if pending is not None:
            self.governor.stats["coalesced"] += 1
            status, headers, content = await asyncio.shield(pending)
            return _response_from(request, status, headers, content)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.governor._inflight[key] = future
        try:
            response = await self._send(gate, request, buffered=True)
            result = (response.status_code, _plain_headers(response), response.content)
            if response.status_code == 200:
                self.governor.store(key, *result)
            future.set_result(result)
            return _response_from(request, *result)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc if isinstance(exc, Exception) else httpx.TransportError("cancelled"))
                future.exception()  # mark retrieved
            raise
        finally:
            self.governor._inflight.pop(key, None)

    async def _send(self, gate: str, request: httpx.Request, *, buffered: bool) -> httpx.Response:
        governor = self.governor
        attempts = len(governor.keys) + 1 if gate == "trongrid" and governor.keys else 1
        for attempt in range(attempts):
            if not await governor.acquire(gate):
                governor.stats["throttled"] += 1
                return busy_response(request, governor.cooldown_remaining(gate))
            api_key = governor.pick_key(gate)
            if api_key is _ALL_KEYS_COOLING:
                governor.stats["throttled"] += 1
                return busy_response(request, governor.cooldown_remaining(gate))
            if gate == "trongrid" or gate.startswith("node:"):
                # never leak our TronGrid key to third-party fallback nodes
                if API_KEY_HEADER in request.headers:
                    del request.headers[API_KEY_HEADER]
                if api_key:
                    request.headers[API_KEY_HEADER] = api_key
            governor.stats["requests"] += 1
            response = await self.inner.handle_async_request(request)
            status = response.status_code
            if status in {401, 403, 429}:
                body = await response.aread()
                await response.aclose()
                again = governor.report(
                    gate, api_key, status, retry_after_seconds(response.headers, body), body,
                )
                if again and attempt < attempts - 1:
                    continue
                return _response_from(request, status, _plain_headers(response), body)
            governor.report(gate, api_key, status)
            if buffered:
                content = await response.aread()
                await response.aclose()
                return _response_from(request, status, _plain_headers(response), content)
            return response
        return busy_response(request)  # pragma: no cover


_GOVERNORS: dict[tuple, TronGovernor] = {}


def shared_state_path(config) -> Path | None:
    explicit = os.getenv("TRON_RATE_STATE_PATH", "").strip()
    if explicit:
        return Path(explicit)
    base = ""
    if getattr(config, "is_clone", False) and getattr(config, "mother_db_path", ""):
        base = str(config.mother_db_path)
    elif getattr(config, "db_path", None):
        base = str(config.db_path)
    if not base:
        return None
    return Path(base).resolve().parent / "tron-rate-state.json"


def config_keys(config) -> tuple[str, ...]:
    keys = tuple(getattr(config, "trongrid_api_keys", ()) or ())
    single = str(getattr(config, "trongrid_api_key", "") or "")
    return parse_api_keys(single, ",".join(keys))


def config_fallback_nodes(config) -> tuple[str, ...]:
    nodes = getattr(config, "tron_fallback_nodes", None)
    if nodes is None:
        nodes = DEFAULT_FALLBACK_NODES
    return tuple(str(node).strip().rstrip("/") for node in nodes if str(node).strip())


def governor_for(config, *, shared: bool = False) -> TronGovernor:
    keys = config_keys(config)
    state_path = shared_state_path(config) if shared else None
    signature = (
        keys, str(getattr(config, "trongrid_url", DEFAULT_TRONGRID_URL)),
        str(getattr(config, "tronscan_api_url", DEFAULT_TRONSCAN_URL)),
        config_fallback_nodes(config), float(getattr(config, "tron_max_qps", 0) or 0),
        float(getattr(config, "tronscan_max_qps", 3) or 3),
        float(getattr(config, "tron_cache_seconds", 5) or 0), str(state_path or ""),
    )
    governor = _GOVERNORS.get(signature)
    if governor is None:
        governor = TronGovernor(
            keys=keys, trongrid_url=signature[1], tronscan_url=signature[2],
            fallback_nodes=signature[3], max_qps=signature[4], tronscan_qps=signature[5],
            cache_seconds=signature[6], state_path=state_path,
        )
        _GOVERNORS[signature] = governor
    return governor
