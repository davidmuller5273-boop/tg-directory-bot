from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


MAX_URL_LENGTH = 500
MAX_TITLE_LENGTH = 80
MAX_CATEGORY_LENGTH = 32
MAX_DESCRIPTION_LENGTH = 240


@dataclass(frozen=True)
class Submission:
    url: str
    title: str
    category: str
    description: str


def normalize_url(raw_url: str) -> str:
    value = raw_url.strip()
    if value.startswith("@") and len(value) > 1:
        value = "https://t.me/" + value[1:]
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("地址必须是 http:// 或 https:// 网址")
    if not parsed.netloc:
        raise ValueError("地址缺少域名")

    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    if not hostname or "." not in hostname:
        raise ValueError("地址域名不完整")

    port = parsed.port
    host = hostname
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    normalized = urlunparse(
        (
            parsed.scheme.casefold(),
            host,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )
    if len(normalized) > MAX_URL_LENGTH:
        raise ValueError(f"地址过长，最多 {MAX_URL_LENGTH} 个字符")
    return normalized


def parse_submission_payload(payload: str, categories: tuple[str, ...]) -> Submission:
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("Format: /submit https://example.com | Title | category | description")

    url = normalize_url(parts[0])
    title = parts[1][:MAX_TITLE_LENGTH].strip()
    category = parts[2].casefold()[:MAX_CATEGORY_LENGTH].strip() if len(parts) >= 3 and parts[2] else "other"
    description = parts[3][:MAX_DESCRIPTION_LENGTH].strip() if len(parts) >= 4 else ""

    if category not in categories:
        raise ValueError(f"Unknown category: {category}. Allowed: {', '.join(categories)}")
    if len(title) < 2:
        raise ValueError("Title must be at least 2 characters")
    return Submission(url=url, title=title, category=category, description=description)


def parse_keyword_address_payload(payload: str, categories: tuple[str, ...]) -> Submission:
    parts = payload.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("格式：关键词 地址，例如：888 https://example.com")
    keyword, raw_address = parts
    keyword = keyword.strip()[:MAX_TITLE_LENGTH]
    if not keyword:
        raise ValueError("关键词不能为空")
    category = "other" if "other" in categories else categories[-1]
    return Submission(
        url=normalize_url(raw_address),
        title=keyword,
        category=category,
        description="",
    )


def looks_like_keyword_address_payload(payload: str) -> bool:
    parts = payload.strip().split(maxsplit=1)
    if len(parts) != 2:
        return False
    address = parts[1].strip().casefold()
    return address.startswith(("http://", "https://", "www.", "t.me/", "telegram.me/", "@"))


def blocked_terms(submission: Submission, blocked_keywords: tuple[str, ...]) -> list[str]:
    haystack = " ".join(
        [submission.url, submission.title, submission.category, submission.description]
    ).casefold()
    return [keyword for keyword in blocked_keywords if keyword and keyword in haystack]
