from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import InvalidToken, TelegramError

from .config import Config
from .storage import DirectoryStore


class CloneManager:
    def __init__(self, config: Config, store: DirectoryStore):
        self.config = config
        self.store = store
        self.processes: dict[int, subprocess.Popen] = {}
        material = (config.session_secret or config.bot_token).encode("utf-8")
        self.key = hashlib.sha256(material).digest()
        self.project_root = Path(__file__).resolve().parent.parent

    def encrypt(self, token: str) -> str:
        nonce = secrets.token_bytes(16)
        raw = token.encode("utf-8")
        stream = self._stream(nonce, len(raw))
        encrypted = bytes(left ^ right for left, right in zip(raw, stream))
        tag = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + tag + encrypted).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value.encode("ascii"))
            nonce, tag, encrypted = payload[:16], payload[16:48], payload[48:]
            expected = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError
            stream = self._stream(nonce, len(encrypted))
            return bytes(left ^ right for left, right in zip(encrypted, stream)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("克隆令牌无法解密，请重新提交 Token") from exc

    def _stream(self, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(self.key + nonce + counter.to_bytes(4, "big")).digest())
            counter += 1
        return bytes(output[:length])

    async def request(self, owner_id: int, token: str) -> tuple[int, str]:
        token = token.strip()
        if ":" not in token or len(token) < 30:
            raise ValueError("Bot Token 格式不正确")
        try:
            async with Bot(token) as bot:
                me = await bot.get_me()
        except (TelegramError, InvalidToken) as exc:
            raise ValueError(f"Bot Token 验证失败：{exc}") from exc
        clone_id = self.store.save_bot_clone_request(
            owner_id, int(me.id), me.username or "", self.encrypt(token)
        )
        current = self.processes.pop(clone_id, None)
        if current and current.poll() is None:
            current.terminate()
        return clone_id, me.username or str(me.id)

    async def approve(self, clone_id: int, developer_id: int) -> tuple[int, str]:
        row = self.store.bot_clone(clone_id)
        if not row or row["status"] != "pending":
            raise ValueError("该克隆申请不存在或已经审核")
        token = self.decrypt(str(row["token_cipher"]))
        if not self.store.review_bot_clone(clone_id, True, developer_id):
            raise ValueError("该克隆申请已经审核")
        process = self.start_process(clone_id, int(row["owner_id"]), token)
        await asyncio.sleep(1)
        if process.poll() is not None:
            error = "克隆进程启动失败，请检查 Token 是否已在其他服务器运行"
            self.store.set_bot_clone_error(clone_id, error)
            raise ValueError(error)
        return int(row["owner_id"]), str(row["bot_username"] or row["bot_id"])

    def reject(self, clone_id: int, developer_id: int) -> int:
        row = self.store.bot_clone(clone_id)
        if not row or not self.store.review_bot_clone(clone_id, False, developer_id):
            raise ValueError("该克隆申请不存在或已经审核")
        return int(row["owner_id"])

    def start_process(
        self, clone_id: int, owner_id: int, token: str
    ) -> subprocess.Popen:
        current = self.processes.get(clone_id)
        if current and current.poll() is None:
            current.terminate()
        env = os.environ.copy()
        env.update(
            BOT_TOKEN=token,
            ADMIN_IDS=str(owner_id),
            SUPER_ADMIN_IDS="",
            DEVELOPER_IDS="",
            IS_CLONE="1",
            DB_PATH=str(self.project_root / "data" / "clones" / f"clone-{clone_id}.sqlite3"),
            WEB_PORT="0",
        )
        process = subprocess.Popen(
            [sys.executable, str(self.project_root / "run.py")],
            cwd=self.project_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes[clone_id] = process
        self.store.set_bot_clone_error(clone_id, "")
        return process

    def start_all(self) -> None:
        for row in self.store.enabled_bot_clones():
            clone_id = int(row["id"])
            try:
                token = self.decrypt(str(row["token_cipher"]))
                self.start_process(clone_id, int(row["owner_id"]), token)
            except (ValueError, OSError) as exc:
                self.store.set_bot_clone_error(clone_id, str(exc))

    def stop_all(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        self.processes.clear()
