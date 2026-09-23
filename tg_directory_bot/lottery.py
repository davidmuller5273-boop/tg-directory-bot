from __future__ import annotations

import asyncio
import base64
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx

from .config import Config


@dataclass(frozen=True)
class LotteryGame:
    code: str
    name: str
    source: str
    api_code: str
    secondary_count: int = 0


@dataclass(frozen=True)
class LotteryResult:
    source: str
    game_code: str
    game_name: str
    issue: str
    draw_time: str
    primary: tuple[str, ...]
    secondary: tuple[str, ...] = ()
    detail_url: str = ""
    next_draw_time: str = ""
    zodiac: tuple[str, ...] = ()
    wave: tuple[str, ...] = ()


class LotteryQueryError(RuntimeError):
    pass


LOTTERY_GAMES: dict[str, LotteryGame] = {
    "ssq": LotteryGame("ssq", "双色球", "cwl", "ssq", 1),
    "fc3d": LotteryGame("fc3d", "福彩3D", "cwl", "3d"),
    "qlc": LotteryGame("qlc", "七乐彩", "cwl", "qlc", 1),
    "kl8": LotteryGame("kl8", "快乐8", "cwl", "kl8"),
    "dlt": LotteryGame("dlt", "超级大乐透", "sport", "85", 2),
    "pl3": LotteryGame("pl3", "排列3", "sport", "35"),
    "pl5": LotteryGame("pl5", "排列5", "sport", "37"),
    "qxc": LotteryGame("qxc", "7星彩", "sport", "04"),
    "hklhc": LotteryGame("hklhc", "香港六合彩", "hkjc", "hk", 1),
    "macau_lhc": LotteryGame("macau_lhc", "澳门六合彩", "marksix6", "macau", 1),
    "new_macau_lhc": LotteryGame(
        "new_macau_lhc", "新澳六合彩", "macaujc", "newMacau", 1
    ),
}

LOTTERY_ALIASES = {
    "双色球": "ssq", "ssq": "ssq",
    "福彩3d": "fc3d", "3d": "fc3d", "fc3d": "fc3d",
    "七乐彩": "qlc", "qlc": "qlc",
    "快乐8": "kl8", "快乐八": "kl8", "kl8": "kl8",
    "大乐透": "dlt", "超级大乐透": "dlt", "dlt": "dlt",
    "排列3": "pl3", "排列三": "pl3", "pl3": "pl3",
    "排列5": "pl5", "排列五": "pl5", "pl5": "pl5",
    "7星彩": "qxc", "七星彩": "qxc", "qxc": "qxc",
    "香港六合彩": "hklhc", "香港彩": "hklhc", "港彩": "hklhc", "hklhc": "hklhc",
    "澳门六合彩": "macau_lhc", "澳门彩": "macau_lhc", "macau_lhc": "macau_lhc",
    "新澳六合彩": "new_macau_lhc", "新澳门六合彩": "new_macau_lhc",
    "新澳门彩": "new_macau_lhc", "new_macau_lhc": "new_macau_lhc",
    "福彩": "cwl", "体彩": "sport", "六合彩": "marksix",
    "全部": "all", "all": "all",
}

HKJC_GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"
HKJC_QUERY = (
    "\n        fragment lotteryDrawsFragment on LotteryDraw {\n    id\n    year\n    no\n"
    "    openDate\n    closeDate\n    drawDate\n    status\n    snowballCode\n"
    "    snowballName_en\n    snowballName_ch\n    lotteryPool {\n      sell\n      status\n"
    "      totalInvestment\n      jackpot\n      unitBet\n      estimatedPrize\n"
    "      derivedFirstPrizeDiv\n      lotteryPrizes {\n        type\n        winningUnit\n"
    "        dividend\n      }\n    }\n    drawResult {\n      drawnNo\n      xDrawnNo\n    }\n"
    "  }\n        query marksixResult($lastNDraw: Int, $startDate: String, $endDate: String, "
    "$drawType: LotteryDrawType) {\n            lotteryDraws(lastNDraw: $lastNDraw, "
    "startDate: $startDate, endDate: $endDate, drawType: $drawType) {\n"
    "              ...lotteryDrawsFragment\n            }\n        }\n    "
)
MARKSIX6_API_URL = "https://api3.marksix6.net/lottery_api.php"
MARKSIX6_HISTORY_URL = "https://api2.marksix6.net/"
MACAUJC_LATEST_URL = "https://macaumarksix.com/api/macaujc2.com"
MACAUJC_HISTORY_URL = "https://history.macaumarksix.com/history/macaujc2/y/{year}"

MARK_SIX_CODES = {"hklhc", "macau_lhc", "new_macau_lhc"}
THREE_DIGIT_CODES = {"fc3d", "pl3"}
MARK_SIX_RED = {1, 2, 7, 8, 12, 13, 18, 19, 23, 24, 29, 30, 34, 35, 40, 45, 46}
MARK_SIX_BLUE = {3, 4, 9, 10, 14, 15, 20, 25, 26, 31, 36, 37, 41, 42, 47, 48}
MARK_SIX_WAVE_EMOJI = {"red": "🔴", "blue": "🔵", "green": "🟢"}
CHINESE_ZODIAC = ("鼠", "牛", "虎", "兔", "龍", "蛇", "馬", "羊", "猴", "雞", "狗", "豬")

CWL_REFERERS = {
    "ssq": "https://www.cwl.gov.cn/ygkj/wqkjgg/ssq/",
    "fc3d": "https://www.cwl.gov.cn/ygkj/wqkjgg/fc3d/",
    "qlc": "https://www.cwl.gov.cn/ygkj/wqkjgg/qlc/",
    "kl8": "https://www.cwl.gov.cn/ygkj/wqkjgg/kl8/",
}

REALTIME_CODES = {
    "ssq": ("10039", "QuanGuoCai/getLotteryInfo.do"),
    "dlt": ("10040", "QuanGuoCai/getLotteryInfo.do"),
    "fc3d": ("10041", "QuanGuoCai/getLotteryInfo1.do"),
    "qlc": ("10042", "QuanGuoCai/getLotteryInfo.do"),
    "pl3": ("10043", "QuanGuoCai/getLotteryInfo1.do"),
    "pl5": ("10044", "QuanGuoCai/getLotteryInfo.do"),
    "qxc": ("10045", "QuanGuoCai/getLotteryInfo.do"),
}

HUINIAO_CODES = {
    "ssq": "ssq", "fc3d": "fcsd", "qlc": "qlc", "kl8": "klb",
    "dlt": "dlt", "pl3": "pls", "pl5": "plw", "qxc": "qxc",
}
HUINIAO_URL = "https://api.huiniao.top/interface/home/lotteryHistory"
PUBLIC_REPO_JSDELIVR = (
    "https://cdn.jsdelivr.net/gh/wenjinliuu/lottery-data-repo@main/"
    "public_data/draws/{code}.json"
)
PUBLIC_REPO_GITHUB_API = (
    "https://api.github.com/repos/wenjinliuu/lottery-data-repo/contents/"
    "public_data/draws/{code}.json"
)


def resolve_lottery_code(value: str) -> str | None:
    return LOTTERY_ALIASES.get(value.strip().casefold())


def resolve_lottery_history_keyword(value: str) -> str | None:
    text = value.strip().casefold().replace(" ", "")
    if not text.endswith("历史"):
        return None
    code = resolve_lottery_code(text[:-2])
    return code if code in LOTTERY_GAMES else None


def split_numbers(value: str) -> tuple[str, ...]:
    cleaned = value.replace(",", " ").replace("+", " ").replace("|", " ")
    return tuple(part.strip() for part in cleaned.split() if part.strip())


def mark_six_color(number: str) -> str:
    value = int(number)
    if value in MARK_SIX_RED:
        return "🔴"
    if value in MARK_SIX_BLUE:
        return "🔵"
    return "🟢"


def mark_six_zodiac(number: str, year: int) -> str:
    current_index = (year - 2020) % 12
    return CHINESE_ZODIAC[(current_index - (int(number) - 1)) % 12]


def mark_six_year(issue: str, draw_time: str) -> int:
    for value in (issue[:4], draw_time[:4]):
        if value.isdigit() and 2000 <= int(value) <= 2200:
            return int(value)
    return datetime.now(timezone.utc).year


def is_valid_mark_six_result(
    primary: tuple[str, ...] | list[str],
    secondary: tuple[str, ...] | list[str],
) -> bool:
    try:
        normalize_mark_six_numbers(primary, secondary)
        return True
    except ValueError:
        return False


def normalize_mark_six_numbers(
    primary: tuple[str, ...] | list[str] | None = None,
    secondary: tuple[str, ...] | list[str] | None = None,
    numbers: tuple[str, ...] | list[str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if numbers is not None:
        values = [str(item).strip() for item in numbers if str(item).strip()]
        if len(values) != 7:
            raise ValueError("六合彩须为6个正码+1个特码")
        primary_values = values[:6]
        secondary_values = values[6:]
    else:
        primary_values = [str(item).strip() for item in (primary or ()) if str(item).strip()]
        secondary_values = [str(item).strip() for item in (secondary or ()) if str(item).strip()]
    if len(primary_values) != 6 or len(secondary_values) != 1:
        raise ValueError("六合彩须为6个正码+1个特码")
    normalized: list[str] = []
    seen: set[int] = set()
    for raw in (*primary_values, *secondary_values):
        if not raw.isdigit():
            raise ValueError(f"六合彩号码非法：{raw}")
        value = int(raw)
        if value < 1 or value > 49:
            raise ValueError(f"六合彩号码超出1-49：{raw}")
        if value in seen:
            raise ValueError(f"六合彩号码重复：{value:02d}")
        seen.add(value)
        normalized.append(f"{value:02d}")
    return tuple(normalized[:6]), (normalized[6],)


def classify_three_digit(primary: tuple[str, ...] | list[str]) -> str:
    digits = [int(str(item).strip()) for item in primary]
    if len(digits) != 3:
        raise ValueError("三位彩须为3个号码")
    a, b, c = digits
    if a == b == c:
        return "豹子"
    unique = {a, b, c}
    if len(unique) == 3:
        ordered = sorted(digits)
        linear = ordered[0] + 1 == ordered[1] and ordered[1] + 1 == ordered[2]
        circular = unique in ({0, 8, 9}, {0, 1, 9})
        if linear or circular:
            return "顺子"
        return "组六"
    if len(unique) == 2:
        return "组三"
    return "组六"


def is_valid_three_digit_result(primary: tuple[str, ...] | list[str]) -> bool:
    try:
        normalize_three_digit_numbers(primary)
        return True
    except ValueError:
        return False


def normalize_three_digit_numbers(
    primary: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    values = [str(item).strip() for item in primary if str(item).strip() != ""]
    if len(values) != 3:
        raise ValueError("福彩3D/排列3须为恰好3个号码")
    normalized: list[str] = []
    for raw in values:
        if not raw.isdigit():
            raise ValueError(f"三位彩号码非法：{raw}")
        value = int(raw)
        if value < 0 or value > 9:
            raise ValueError(f"三位彩号码须为0-9：{raw}")
        normalized.append(str(value))
    return tuple(normalized)


def validate_lottery_numbers(game_code: str, primary: tuple[str, ...], secondary: tuple[str, ...]) -> None:
    if game_code in THREE_DIGIT_CODES:
        normalize_three_digit_numbers(primary)
        if secondary:
            raise ValueError("福彩3D/排列3不应包含特别号")
        return
    if game_code in MARK_SIX_CODES:
        normalize_mark_six_numbers(primary, secondary)
        return


def is_valid_lottery_result(result: LotteryResult) -> bool:
    try:
        validate_lottery_numbers(result.game_code, result.primary, result.secondary)
        return True
    except ValueError:
        return False


def normalize_lottery_result(result: LotteryResult) -> LotteryResult:
    if result.game_code in THREE_DIGIT_CODES:
        primary = normalize_three_digit_numbers(result.primary)
        if result.secondary:
            raise ValueError("福彩3D/排列3不应包含特别号")
        return LotteryResult(
            source=result.source, game_code=result.game_code, game_name=result.game_name,
            issue=result.issue, draw_time=result.draw_time, primary=primary,
            secondary=(), detail_url=result.detail_url, next_draw_time=result.next_draw_time,
            zodiac=result.zodiac, wave=result.wave,
        )
    if result.game_code in MARK_SIX_CODES:
        primary, secondary = normalize_mark_six_numbers(result.primary, result.secondary)
        return LotteryResult(
            source=result.source, game_code=result.game_code, game_name=result.game_name,
            issue=result.issue, draw_time=result.draw_time, primary=primary,
            secondary=secondary, detail_url=result.detail_url,
            next_draw_time=result.next_draw_time, zodiac=result.zodiac, wave=result.wave,
        )
    return result


def _join_mark_six_columns(cells: list[str]) -> str:
    if len(cells) != 7:
        raise ValueError("六合彩展示须为7列")
    # width-2 style cells joined by ASCII space; special ball after " + "
    return " ".join(cells[:6]) + " + " + cells[6]


def format_mark_six_numbers(
    issue: str,
    draw_time: str,
    primary: tuple[str, ...],
    secondary: tuple[str, ...],
    zodiac: tuple[str, ...] = (),
    wave: tuple[str, ...] = (),
) -> str:
    year = mark_six_year(issue, draw_time)
    try:
        primary_n, secondary_n = normalize_mark_six_numbers(primary, secondary)
    except ValueError:
        # 历史缓存若含脏数据，仍尽量展示，避免查询页崩溃
        primary_n = tuple(f"{int(x):02d}" if str(x).isdigit() else str(x) for x in primary)
        secondary_n = tuple(f"{int(x):02d}" if str(x).isdigit() else str(x) for x in secondary)
        numbers = (*primary_n, *secondary_n)
        if len(numbers) != 7:
            return " ".join(numbers)
        number_cells = [str(n) for n in numbers]
        zodiac_cells = [
            mark_six_zodiac(n, year) if str(n).isdigit() else "?" for n in numbers
        ]
        color_cells = [
            mark_six_color(n) if str(n).isdigit() else "🟢" for n in numbers
        ]
        return "\n".join((
            _join_mark_six_columns(number_cells),
            _join_mark_six_columns(zodiac_cells),
            _join_mark_six_columns(color_cells),
        ))
    numbers = (*primary_n, *secondary_n)
    number_cells = [f"{int(number):02d}" for number in numbers]
    if len(zodiac) == 7:
        zodiac_cells = [str(item).strip() for item in zodiac]
    else:
        zodiac_cells = [mark_six_zodiac(number, year) for number in numbers]
    if len(wave) == 7:
        color_cells = []
        for item in wave:
            raw = str(item).strip()
            if raw in {"🔴", "🔵", "🟢"}:
                color_cells.append(raw)
                continue
            mapped = MARK_SIX_WAVE_EMOJI.get(raw.casefold())
            if mapped:
                color_cells.append(mapped)
            elif raw.isdigit():
                color_cells.append(mark_six_color(raw))
            else:
                color_cells.append("🟢")
    else:
        color_cells = [mark_six_color(number) for number in numbers]
    return "\n".join((
        _join_mark_six_columns(number_cells),
        _join_mark_six_columns(zodiac_cells),
        _join_mark_six_columns(color_cells),
    ))



def format_lottery_result(result: LotteryResult) -> str:
    source_names = {
        "cwl": "中国福彩网", "sport": "中国体彩网",
        "realtime168": "168开奖实时接口",
        "hkjc": "香港赛马会官方", "marksix6": "第三方 Marksix6（非澳门官方）",
        "macaujc": "macaujc.com（第三方，非澳门官方）",
        "huiniao": "慧鸟接口",
    }
    draw_time = result.draw_time or "数据源未提供具体时间"
    source_text = source_names.get(result.source, result.source)

    if result.game_code == "ssq":
        red = " ".join(f"{int(number):02d}" for number in result.primary)
        blue = " ".join(f"{int(number):02d}" for number in result.secondary)
        return (
            f"福彩双色球第:{result.issue}期开奖结果:\n"
            f"🔴{red}\n"
            f"🔵{blue}\n"
            f"开奖时间：{draw_time}\n"
            f"数据源：{source_text}"
        )

    if result.game_code in MARK_SIX_CODES:
        number_text = format_mark_six_numbers(
            result.issue, draw_time, result.primary, result.secondary,
            zodiac=result.zodiac, wave=result.wave,
        )
        return (
            f"{result.game_name} 第{result.issue}期\n"
            f"{number_text}\n"
            f"开奖时间：{draw_time}\n"
            f"数据源：{source_text}"
        )

    if result.game_code in THREE_DIGIT_CODES:
        try:
            digits = normalize_three_digit_numbers(result.primary)
            kind = classify_three_digit(digits)
            number_text = f"开奖号码：{' '.join(digits)}（{kind}）"
        except ValueError:
            number_text = f"开奖号码：{' '.join(result.primary)}"
        return (
            f"{result.game_name} 第{result.issue}期\n"
            f"{number_text}\n"
            f"开奖时间：{draw_time}\n"
            f"数据源：{source_text}"
        )

    numbers = " ".join(result.primary)
    if result.secondary:
        numbers += " + " + " ".join(result.secondary)
    return (
        f"{result.game_name} 第{result.issue}期\n"
        f"开奖号码：{numbers}\n"
        f"开奖时间：{draw_time}\n"
        f"数据源：{source_text}"
    )


class LotteryService:
    def __init__(self, config: Config):
        self.config = config
        self.next_draw_times: dict[str, str] = {}
        self.headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }

    async def latest(self, game_code: str, known_issue: str = "") -> LotteryResult:
        game = LOTTERY_GAMES.get(game_code)
        if not game:
            raise ValueError("未知彩种")
        providers = self.latest_providers(game)
        values = await asyncio.gather(*(
            asyncio.wait_for(provider, timeout=9)
            for provider in providers
        ), return_exceptions=True)
        results: list[LotteryResult] = []
        errors: list[str] = []
        for value in values:
            if isinstance(value, Exception):
                errors.append(str(value))
            else:
                results.append(value)
        if not results:
            raise LotteryQueryError(
                f"{game.name}多数据源均查询失败：" + "；".join(errors)
            )
        valid_results: list[LotteryResult] = []
        for result in results:
            if result.game_code in MARK_SIX_CODES or result.game_code in THREE_DIGIT_CODES:
                if not is_valid_lottery_result(result):
                    continue
                try:
                    valid_results.append(normalize_lottery_result(result))
                except ValueError:
                    continue
            else:
                valid_results.append(result)
        if not valid_results:
            raise LotteryQueryError(
                f"{game.name}多数据源均查询失败：" + "；".join(errors or ["结果校验未通过"])
            )
        newer = [
            result for result in valid_results
            if not known_issue or self._issue_key(result.issue) > self._issue_key(known_issue)
        ]
        candidates = newer or valid_results

        def _rank(result: LotteryResult) -> tuple:
            source_bonus = 1 if result.source == "macaujc" else 0
            return (self._issue_key(result.issue), source_bonus)

        return max(candidates, key=_rank)

    def latest_providers(self, game: LotteryGame) -> list:
        raw_base = getattr(
            self.config, "lottery_public_data_base_url",
            "https://raw.githubusercontent.com/wenjinliuu/lottery-data-repo/"
            "main/public_data",
        ).rstrip("/")
        providers = [
            self._latest_native(game),
            self._latest_huiniao(game),
            self._latest_public_url(
                game, f"{raw_base}/draws/{game.code}.json", "public-raw"
            ),
            self._latest_public_url(
                game, PUBLIC_REPO_JSDELIVR.format(code=game.code), "public-jsdelivr"
            ),
            self._latest_public_github(game),
        ]
        if game.code in REALTIME_CODES:
            providers.insert(0, self._latest_realtime(game))
        if game.code in {"hklhc", "new_macau_lhc"}:
            providers.append(self._latest_marksix6_backup(game))
        return providers

    @staticmethod
    def _issue_key(issue: str) -> tuple[int, ...]:
        values = tuple(int(value) for value in re.findall(r"\d+", str(issue)))
        return values or (0,)

    async def _latest_realtime(self, game: LotteryGame) -> LotteryResult:
        lot_code, endpoint = REALTIME_CODES[game.code]
        url = f"{self.config.lottery_realtime_url}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(url, params={"lotCode": lot_code}, headers=self.headers)
                response.raise_for_status()
                payload = response.json()
            result = self.parse_realtime(game, payload, url)
            if result.next_draw_time:
                self.next_draw_times[game.code] = result.next_draw_time
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise LotteryQueryError(f"{game.name}实时接口查询失败：{exc}") from exc

    async def history(self, game_code: str, limit: int = 100) -> list[LotteryResult]:
        game = LOTTERY_GAMES.get(game_code)
        if not game:
            raise ValueError("未知彩种")
        count = max(1, min(limit, 100))
        if game.source == "cwl":
            return await self._history_cwl(game, count)
        if game.source == "sport":
            return await self._history_sport(game, count)
        if game.source == "hkjc":
            return await self._history_hkjc(game, count)
        if game.source == "macaujc":
            return await self._history_macaujc(game, count)
        return await self._history_marksix6(game, count)

    async def _history_macaujc(
        self, game: LotteryGame, limit: int
    ) -> list[LotteryResult]:
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        years = (beijing_now.year, beijing_now.year - 1)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                responses = await asyncio.gather(
                    client.get(MACAUJC_LATEST_URL, headers=self.headers),
                    *(client.get(
                        MACAUJC_HISTORY_URL.format(year=year), headers=self.headers
                    ) for year in years),
                )
                for response in responses:
                    response.raise_for_status()
            results = []
            results.extend(self.parse_macaujc_payload(game, responses[0].json()))
            for response in responses[1:]:
                results.extend(self.parse_macaujc_payload(game, response.json()))
            merged = {result.issue: result for result in results}
            if not merged:
                raise ValueError("接口返回缺少开奖记录")
            return sorted(
                merged.values(), key=lambda item: item.issue, reverse=True
            )[:limit]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(f"macaujc.com 新澳六合彩查询失败：{exc}") from exc

    async def _history_hkjc(self, game: LotteryGame, limit: int) -> list[LotteryResult]:
        headers = dict(self.headers)
        headers.update({
            "content-type": "application/json", "origin": "https://bet.hkjc.com",
            "referer": "https://bet.hkjc.com/",
        })
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.post(
                    HKJC_GRAPHQL_URL,
                    json={"query": HKJC_QUERY, "variables": {"lastNDraw": limit}},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("errors"):
                raise ValueError(str(payload["errors"]))
            results = self.parse_hkjc_history(game, payload)
            if not results:
                raise ValueError("官方返回缺少开奖记录")
            return results[:limit]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(f"香港赛马会 {game.name} 查询失败：{exc}") from exc

    async def _history_marksix6(
        self, game: LotteryGame, limit: int
    ) -> list[LotteryResult]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                latest_response, history_response = await asyncio.gather(
                    client.get(MARKSIX6_API_URL, params={"type": game.api_code}, headers=self.headers),
                    client.get(MARKSIX6_HISTORY_URL, headers=self.headers),
                )
                latest_response.raise_for_status()
                history_response.raise_for_status()
            latest = self.parse_marksix6_latest(game, latest_response.json())
            history = self.parse_marksix6_history_html(game, history_response.text)
            merged = {result.issue: result for result in history}
            merged[latest.issue] = latest
            return sorted(merged.values(), key=lambda item: item.issue, reverse=True)[:limit]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(
                f"第三方 {game.name} 查询失败（非澳门官方）：{exc}"
            ) from exc

    async def latest_all(
        self, game_codes: tuple[str, ...] | None = None,
        known_issues: dict[str, str] | None = None,
    ) -> tuple[list[LotteryResult], dict[str, str]]:
        codes = game_codes or tuple(LOTTERY_GAMES)
        known_issues = known_issues or {}
        values = await asyncio.gather(
            *(self.latest(code, known_issues.get(code, "")) for code in codes),
            return_exceptions=True,
        )
        results: list[LotteryResult] = []
        errors: dict[str, str] = {}
        for code, value in zip(codes, values):
            if isinstance(value, Exception):
                errors[code] = str(value)
            else:
                results.append(value)
        return results, errors

    async def _latest_cwl(self, game: LotteryGame) -> LotteryResult:
        return (await self._history_cwl(game, 1))[0]

    async def _latest_native(self, game: LotteryGame) -> LotteryResult:
        if game.source == "cwl":
            return await self._latest_cwl(game)
        if game.source == "sport":
            return await self._latest_sport(game)
        return (await self.history(game.code, 1))[0]

    async def _latest_marksix6_backup(self, game: LotteryGame) -> LotteryResult:
        return (await self._history_marksix6(game, 1))[0]

    async def _latest_huiniao(self, game: LotteryGame) -> LotteryResult:
        code = HUINIAO_CODES.get(game.code)
        if not code:
            raise LotteryQueryError(f"慧鸟接口暂不支持{game.name}")
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    HUINIAO_URL,
                    params={"type": code, "page": 1, "limit": 1},
                    headers=self.headers,
                )
                response.raise_for_status()
                payload = response.json()
            row = (payload.get("data") or {}).get("last") or {}
            keys = (
                "one", "two", "three", "four", "five", "six", "seven",
                "eight", "nine", "ten", "eleven", "twelve", "thirteen",
                "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
                "nineteen", "twenty",
            )
            raw_parts: list[str] = []
            for key in keys:
                value = str(row.get(key) or "").strip()
                if not value:
                    continue
                raw_parts.append(value)
            if game.code in THREE_DIGIT_CODES:
                # 3D/排列3：不要 zfill(2)；只取前 3 个有效数字位
                digits: list[str] = []
                for part in raw_parts:
                    if not part.isdigit():
                        continue
                    digits.append(str(int(part)))
                    if len(digits) == 3:
                        break
                numbers = tuple(digits)
                if len(numbers) != 3:
                    raise ValueError("慧鸟返回三位彩号码不完整")
                numbers = normalize_three_digit_numbers(numbers)
                primary, secondary = numbers, ()
            else:
                numbers = tuple(part.zfill(2) if part.isdigit() else part for part in raw_parts)
                if not row.get("code") or not numbers:
                    raise ValueError("接口返回缺少开奖号码")
                split_at = (
                    len(numbers) - game.secondary_count
                    if game.secondary_count else len(numbers)
                )
                primary, secondary = numbers[:split_at], numbers[split_at:]
            if not row.get("code"):
                raise ValueError("接口返回缺少开奖号码")
            result = LotteryResult(
                source="huiniao", game_code=game.code, game_name=game.name,
                issue=str(row["code"]),
                draw_time=str(row.get("open_time") or row.get("day") or ""),
                primary=primary, secondary=secondary,
                detail_url=HUINIAO_URL,
                next_draw_time=str(row.get("next_open_time") or ""),
            )
            return normalize_lottery_result(result) if (
                game.code in THREE_DIGIT_CODES or game.code in MARK_SIX_CODES
            ) else result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(f"慧鸟 {game.name} 查询失败：{exc}") from exc

    async def _latest_public_url(
        self, game: LotteryGame, url: str, source: str,
    ) -> LotteryResult:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                payload = response.json()
            results = self.parse_public_repo_history(game, payload, url)
            if not results:
                raise ValueError("接口返回缺少开奖记录")
            result = results[0]
            return LotteryResult(
                source=source, game_code=result.game_code,
                game_name=result.game_name, issue=result.issue,
                draw_time=result.draw_time, primary=result.primary,
                secondary=result.secondary, detail_url=result.detail_url,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(f"{source} {game.name} 查询失败：{exc}") from exc

    async def _latest_public_github(self, game: LotteryGame) -> LotteryResult:
        url = PUBLIC_REPO_GITHUB_API.format(code=game.code)
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                payload = response.json()
            encoded = str(payload.get("content") or "").replace("\n", "")
            decoded = base64.b64decode(encoded).decode("utf-8")
            import json
            rows = self.parse_public_repo_history(game, json.loads(decoded), url)
            if not rows:
                raise ValueError("接口返回缺少开奖记录")
            result = rows[0]
            return LotteryResult(
                source="public-github", game_code=result.game_code,
                game_name=result.game_name, issue=result.issue,
                draw_time=result.draw_time, primary=result.primary,
                secondary=result.secondary, detail_url=result.detail_url,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LotteryQueryError(f"GitHub {game.name} 查询失败：{exc}") from exc

    async def _latest_public_repo(self, game: LotteryGame) -> LotteryResult:
        return (await self._history_public_repo(game, 1))[0]

    async def _history_cwl(self, game: LotteryGame, limit: int) -> list[LotteryResult]:
        page_size = min(limit, 100)
        params = {
            "name": game.api_code,
            "issueCount": "",
            "issueStart": "",
            "issueEnd": "",
            "dayStart": "",
            "dayEnd": "",
            "pageNo": "1",
            "pageSize": str(page_size),
            "week": "",
            "systemType": "PC",
        }
        headers = dict(self.headers)
        headers["referer"] = CWL_REFERERS.get(game.code, "https://www.cwl.gov.cn/ygkj/wqkjgg/")
        try:
            results: list[LotteryResult] = []
            seen: set[str] = set()
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                await client.get(headers["referer"], headers=headers)
                for page_no in range(1, 1 + (limit + page_size - 1) // page_size):
                    params["pageNo"] = str(page_no)
                    response = await client.get(self.config.cwl_lottery_url, params=params, headers=headers)
                    response.raise_for_status()
                    page_results = self.parse_cwl_history(
                        game, response.json(), self.config.cwl_lottery_url
                    )
                    for result in page_results:
                        if result.issue not in seen:
                            seen.add(result.issue)
                            results.append(result)
                    if len(page_results) < page_size or len(results) >= limit:
                        break
            if not results:
                raise ValueError("官方返回缺少开奖记录")
            return results[:limit]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            try:
                return await self._history_public_repo(game, limit)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as fallback_exc:
                raise LotteryQueryError(
                    f"中国福彩网 {game.name} 查询失败：{exc}；备用源失败：{fallback_exc}"
                ) from exc

    async def _latest_sport(self, game: LotteryGame) -> LotteryResult:
        return (await self._history_sport(game, 1))[0]

    async def _history_sport(self, game: LotteryGame, limit: int) -> list[LotteryResult]:
        page_size = min(limit, 100)
        params = {
            "gameNo": game.api_code,
            "provinceId": "0",
            "pageSize": str(page_size),
            "isVerify": "1",
            "pageNo": "1",
        }
        headers = dict(self.headers)
        headers.update({
            "referer": "https://m.lottery.gov.cn/",
            "origin": "https://m.lottery.gov.cn",
            "user-agent": (
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
            ),
        })
        try:
            results: list[LotteryResult] = []
            seen: set[str] = set()
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                for page_no in range(1, 1 + (limit + page_size - 1) // page_size):
                    params["pageNo"] = str(page_no)
                    response = await client.get(
                        self.config.sporttery_lottery_url, params=params, headers=headers
                    )
                    response.raise_for_status()
                    page_results = self.parse_sport_history(game, response.json())
                    for result in page_results:
                        if result.issue not in seen:
                            seen.add(result.issue)
                            results.append(result)
                    if len(page_results) < page_size or len(results) >= limit:
                        break
            if not results:
                raise ValueError("官方返回缺少开奖记录")
            return results[:limit]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            try:
                return await self._history_public_repo(game, limit)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as fallback_exc:
                raise LotteryQueryError(
                    f"中国体彩网 {game.name} 查询失败：{exc}；备用源失败：{fallback_exc}"
                ) from exc

    async def _history_public_repo(self, game: LotteryGame, limit: int) -> list[LotteryResult]:
        url = f"{self.config.lottery_public_data_base_url}/draws/{game.code}.json"
        headers = dict(self.headers)
        headers["accept"] = "application/json"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            results = self.parse_public_repo_history(game, response.json(), url)
        if not results:
            raise ValueError("备用源返回缺少开奖记录")
        return results[:limit]

    @staticmethod
    def parse_realtime(
        game: LotteryGame, payload: dict, detail_url: str = ""
    ) -> LotteryResult:
        if int(payload.get("errorCode", -1)) != 0:
            raise ValueError(str(payload.get("message") or "接口返回错误"))
        row = payload["result"]["data"]
        numbers = split_numbers(str(row.get("preDrawCode") or ""))
        if not numbers:
            raise ValueError("实时接口返回缺少开奖号码")
        split_at = len(numbers) - game.secondary_count if game.secondary_count else len(numbers)
        result = LotteryResult(
            source="realtime168", game_code=game.code, game_name=game.name,
            issue=str(row["preDrawIssue"]),
            draw_time=str(row.get("preDrawTime") or ""),
            primary=numbers[:split_at], secondary=numbers[split_at:],
            detail_url=detail_url,
            next_draw_time=str(row.get("drawTime") or row.get("nextDrawTime") or ""),
        )
        if game.code in THREE_DIGIT_CODES or game.code in MARK_SIX_CODES:
            return normalize_lottery_result(result)
        return result

    @staticmethod
    def parse_cwl(game: LotteryGame, payload: dict, base_url: str = "https://www.cwl.gov.cn/") -> LotteryResult:
        row = payload["result"][0]
        primary = split_numbers(str(row.get("red") or ""))
        secondary = split_numbers(str(row.get("blue") or row.get("blue2") or ""))
        if not primary:
            raise ValueError("官方返回缺少开奖号码")
        detail = str(row.get("detailsLink") or "")
        result = LotteryResult(
            source="cwl",
            game_code=game.code,
            game_name=game.name,
            issue=str(row["code"]),
            draw_time=str(row["date"]),
            primary=primary,
            secondary=secondary,
            detail_url=urljoin(base_url, detail),
        )
        if game.code in THREE_DIGIT_CODES or game.code in MARK_SIX_CODES:
            return normalize_lottery_result(result)
        return result

    @staticmethod
    def parse_cwl_history(
        game: LotteryGame, payload: dict, base_url: str = "https://www.cwl.gov.cn/"
    ) -> list[LotteryResult]:
        return [
            LotteryService.parse_cwl(game, {"result": [row]}, base_url)
            for row in payload.get("result", [])
        ]

    @staticmethod
    def parse_sport(game: LotteryGame, payload: dict) -> LotteryResult:
        row = payload["value"]["list"][0]
        numbers = split_numbers(str(row["lotteryDrawResult"]))
        if not numbers:
            raise ValueError("官方返回缺少开奖号码")
        split_at = len(numbers) - game.secondary_count if game.secondary_count else len(numbers)
        result = LotteryResult(
            source="sport",
            game_code=game.code,
            game_name=game.name,
            issue=str(row["lotteryDrawNum"]),
            draw_time=str(row["lotteryDrawTime"]),
            primary=numbers[:split_at],
            secondary=numbers[split_at:],
        )
        if game.code in THREE_DIGIT_CODES or game.code in MARK_SIX_CODES:
            return normalize_lottery_result(result)
        return result

    @staticmethod
    def parse_sport_history(game: LotteryGame, payload: dict) -> list[LotteryResult]:
        rows = payload.get("value", {}).get("list", [])
        return [
            LotteryService.parse_sport(game, {"value": {"list": [row]}})
            for row in rows
        ]

    @staticmethod
    def parse_public_repo_history(
        game: LotteryGame, payload: dict, detail_url: str = ""
    ) -> list[LotteryResult]:
        results: list[LotteryResult] = []
        for row in payload.get("draws", []):
            numbers = split_numbers(str(row.get("number_raw") or ""))
            if not numbers:
                number_groups = row.get("numbers") or {}
                if isinstance(number_groups, dict):
                    flattened: list[str] = []
                    for key in ("red", "blue", "front", "back", "digits", "nums"):
                        value = number_groups.get(key)
                        if isinstance(value, list):
                            for item in value:
                                if str(item).isdigit():
                                    flattened.append(
                                        str(int(item))
                                        if game.code in THREE_DIGIT_CODES
                                        else f"{int(item):02d}"
                                    )
                                else:
                                    flattened.append(str(item))
                    numbers = tuple(flattened)
            if not numbers:
                continue
            split_at = len(numbers) - game.secondary_count if game.secondary_count else len(numbers)
            result = LotteryResult(
                source=game.source,
                game_code=game.code,
                game_name=game.name,
                issue=str(row["issue"]),
                draw_time=str(row.get("draw_date") or row.get("draw_time") or ""),
                primary=numbers[:split_at],
                secondary=numbers[split_at:],
                detail_url=detail_url,
            )
            if game.code in THREE_DIGIT_CODES or game.code in MARK_SIX_CODES:
                try:
                    result = normalize_lottery_result(result)
                except ValueError:
                    continue
            results.append(result)
        return results

    @staticmethod
    def parse_hkjc_history(game: LotteryGame, payload: dict) -> list[LotteryResult]:
        results: list[LotteryResult] = []
        for row in (payload.get("data") or {}).get("lotteryDraws") or []:
            draw = row.get("drawResult") or {}
            primary = tuple(f"{int(value):02d}" for value in draw.get("drawnNo") or [])
            extra = draw.get("xDrawnNo")
            if row.get("status") != "Result" or len(primary) != 6 or extra is None:
                continue
            try:
                primary_n, secondary_n = normalize_mark_six_numbers(
                    primary, (f"{int(extra):02d}",)
                )
            except ValueError:
                continue
            results.append(LotteryResult(
                source="hkjc", game_code=game.code, game_name=game.name,
                issue=f"{int(row['year']):04d}{int(row['no']):03d}",
                draw_time=str(row.get("drawDate") or "")[:10], primary=primary_n,
                secondary=secondary_n, detail_url="https://bet.hkjc.com/",
            ))
        return results

    @staticmethod
    def parse_marksix6_latest(game: LotteryGame, payload: dict) -> LotteryResult:
        numbers = tuple(str(value).strip() for value in payload.get("numbers") or [])
        if len(numbers) != 7:
            numbers = split_numbers(str(payload.get("openCode") or ""))
        primary, secondary = normalize_mark_six_numbers(numbers=numbers)
        return LotteryResult(
            source="marksix6", game_code=game.code, game_name=game.name,
            issue=str(payload["expect"]), draw_time=str(payload.get("openTime") or ""),
            primary=primary, secondary=secondary, detail_url=MARKSIX6_API_URL,
        )

    @staticmethod
    def parse_macaujc_payload(
        game: LotteryGame, payload: dict | list
    ) -> list[LotteryResult]:
        rows = payload if isinstance(payload, list) else payload.get("data") or []
        results: list[LotteryResult] = []
        for row in rows:
            numbers = split_numbers(str(row.get("openCode") or ""))
            if not row.get("expect"):
                continue
            try:
                primary, secondary = normalize_mark_six_numbers(numbers=numbers)
            except ValueError:
                continue
            zodiac_raw = [
                part.strip() for part in str(row.get("zodiac") or "").split(",")
                if part.strip()
            ]
            wave_raw = [
                part.strip() for part in str(row.get("wave") or "").split(",")
                if part.strip()
            ]
            zodiac = tuple(zodiac_raw) if len(zodiac_raw) == 7 else ()
            wave = tuple(wave_raw) if len(wave_raw) == 7 else ()
            results.append(LotteryResult(
                source="macaujc", game_code=game.code, game_name=game.name,
                issue=str(row["expect"]), draw_time=str(row.get("openTime") or ""),
                primary=primary, secondary=secondary, detail_url="https://macaujc.com/",
                zodiac=zodiac, wave=wave,
            ))
        return results

    @staticmethod
    def parse_marksix6_history_html(
        game: LotteryGame, page: str
    ) -> list[LotteryResult]:
        section_match = re.search(
            rf'<section class="card" id="{re.escape(game.api_code)}">([\s\S]*?)</section>',
            page,
        )
        if not section_match:
            return []
        results: list[LotteryResult] = []
        for block in re.findall(
            r'<div class="history-line">([\s\S]*?)</div>', section_match.group(1)
        ):
            issue_match = re.search(r'<span class="period">\s*([^<期]+)期', block)
            numbers = tuple(re.findall(r'<span class="ball-sm [^"]+">\s*(\d{1,2})\s*</span>', block))
            if not issue_match or len(numbers) != 7:
                continue
            try:
                primary, secondary = normalize_mark_six_numbers(numbers=numbers)
            except ValueError:
                continue
            results.append(LotteryResult(
                source="marksix6", game_code=game.code, game_name=game.name,
                issue=html.unescape(issue_match.group(1)).strip(), draw_time="",
                primary=primary, secondary=secondary, detail_url=MARKSIX6_HISTORY_URL,
            ))
        return results
