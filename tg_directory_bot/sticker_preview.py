"""贴纸包预览图：把贴纸缩略图拼成深色背景的网格（默认 5 列，最多 30 张）。

依赖 Pillow；视频贴纸优先用 ffmpeg 抽第一帧（没有 ffmpeg 时用缩略图），
动态 .tgs 贴纸使用 Telegram 提供的缩略图。任何一张失败都会跳过，不会中断。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
from typing import Any, Iterable

try:  # Pillow 缺失时预览功能自动关闭，不影响贴纸包封装
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only without Pillow
    Image = None  # type: ignore[assignment]

GRID_COLUMNS = 5
GRID_MAX_STICKERS = 30
CELL_SIZE = 160
CELL_PADDING = 14
BACKGROUND = (23, 24, 28)
DOWNLOAD_CONCURRENCY = 5
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024


def preview_available() -> bool:
    return Image is not None


def build_grid(images: Iterable[Any], columns: int = GRID_COLUMNS,
               cell: int = CELL_SIZE, padding: int = CELL_PADDING) -> bytes:
    """Return PNG bytes of the images laid out in a grid on a dark background."""
    if Image is None:
        raise RuntimeError("Pillow 未安装")
    items = [image for image in images if image is not None][:GRID_MAX_STICKERS]
    if not items:
        raise ValueError("没有可用的预览图片")
    columns = max(1, min(columns, len(items)))
    rows = (len(items) + columns - 1) // columns
    width = columns * cell + (columns + 1) * padding
    height = rows * cell + (rows + 1) * padding
    canvas = Image.new("RGBA", (width, height), BACKGROUND + (255,))
    for index, image in enumerate(items):
        picture = image.convert("RGBA")
        picture.thumbnail((cell, cell), Image.LANCZOS)
        row, column = divmod(index, columns)
        x = padding + column * (cell + padding) + (cell - picture.width) // 2
        y = padding + row * (cell + padding) + (cell - picture.height) // 2
        canvas.alpha_composite(picture, (x, y))
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def open_image(data: bytes) -> Any | None:
    if Image is None or not data:
        return None
    try:
        image = Image.open(io.BytesIO(bytes(data)))
        image.seek(0)
        image.load()
        return image
    except Exception:  # noqa: BLE001 - 任意坏图都跳过
        return None


def ffmpeg_first_frame(data: bytes, timeout: float = 15.0) -> Any | None:
    """Extract the first frame of a .webm video sticker (keeps transparency)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or Image is None or not data:
        return None
    import subprocess

    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "in.webm")
        target = os.path.join(folder, "out.png")
        with open(source, "wb") as handle:
            handle.write(bytes(data))
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-c:v", "libvpx-vp9", "-i", source, "-frames:v", "1", target,
        ]
        for attempt in (command, [part for part in command if part not in {"-c:v", "libvpx-vp9"}]):
            try:
                subprocess.run(attempt, check=True, timeout=timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(target):
                with open(target, "rb") as handle:
                    return open_image(handle.read())
    return None


async def _download(bot: Any, file_id: str | None) -> bytes | None:
    if not file_id:
        return None
    try:
        telegram_file = await asyncio.wait_for(bot.get_file(file_id), timeout=20)
        size = getattr(telegram_file, "file_size", None) or 0
        if size and size > MAX_DOWNLOAD_BYTES:
            return None
        data = await asyncio.wait_for(telegram_file.download_as_bytearray(), timeout=30)
        return bytes(data)
    except Exception as exc:  # noqa: BLE001 - 单张失败跳过
        logging.debug("Sticker preview download failed: %s", exc)
        return None


def _thumbnail_id(sticker: Any) -> str | None:
    thumb = getattr(sticker, "thumbnail", None)
    return getattr(thumb, "file_id", None) if thumb else None


async def sticker_preview_image(bot: Any, sticker: Any) -> Any | None:
    """Best-effort still image for one sticker; None when unavailable."""
    if getattr(sticker, "is_video", False):
        if shutil.which("ffmpeg"):
            data = await _download(bot, sticker.file_id)
            if data:
                frame = await asyncio.to_thread(ffmpeg_first_frame, data)
                if frame is not None:
                    return frame
        return open_image(await _download(bot, _thumbnail_id(sticker)) or b"")
    if getattr(sticker, "is_animated", False):
        # .tgs 渲染依赖较重，直接使用 Telegram 生成的缩略图
        return open_image(await _download(bot, _thumbnail_id(sticker)) or b"")
    image = open_image(await _download(bot, sticker.file_id) or b"")
    if image is None:
        image = open_image(await _download(bot, _thumbnail_id(sticker)) or b"")
    return image


async def build_sticker_preview(bot: Any, stickers: Iterable[Any],
                                limit: int = GRID_MAX_STICKERS) -> bytes | None:
    """PNG grid preview of up to `limit` stickers, or None if nothing worked."""
    if Image is None:
        return None
    chosen = list(stickers)[:limit]
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def one(sticker: Any) -> Any | None:
        async with semaphore:
            try:
                return await sticker_preview_image(bot, sticker)
            except Exception:  # noqa: BLE001
                return None

    images = await asyncio.gather(*(one(sticker) for sticker in chosen))
    images = [image for image in images if image is not None]
    if not images:
        return None
    try:
        return await asyncio.to_thread(build_grid, images)
    except Exception:  # noqa: BLE001
        logging.exception("Sticker preview grid failed")
        return None
