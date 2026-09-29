from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from telegram import Bot
from telegram.error import InvalidToken, TelegramError

from .config import Config
from .storage import DirectoryStore


DEFAULT_CLONE_STORAGE_QUOTA = 2 * 1024 * 1024 * 1024


def token_bot_id(token: str) -> int:
    head = str(token or "").split(":", 1)[0].strip()
    return int(head) if head.isdigit() else 0


class CloneManager:
    """Clone registry + process control.

    The mother bot owns every clone process (any depth); the registry lives in
    the mother's database. A child bot builds a manager pointed at the
    mother's database with ``manage_processes=False``: it can only file
    requests (parent = itself); approval and processes stay on the mother.
    """

    def __init__(
        self, config: Config, store: DirectoryStore,
        manage_processes: bool | None = None,
    ):
        self.config = config
        self.store = store
        self.manage_processes = (
            (not config.is_clone) if manage_processes is None else manage_processes
        )
        self.parent_clone_id = int(config.clone_id or 0) if config.is_clone else 0
        self.processes: dict[int, subprocess.Popen] = {}
        if config.clone_cipher_key:
            self.key = bytes.fromhex(config.clone_cipher_key)
        else:
            material = (config.session_secret or config.bot_token).encode("utf-8")
            self.key = hashlib.sha256(material).digest()
        self.project_root = Path(__file__).resolve().parent.parent
        self.clones_dir = self.project_root / "data" / "clones"
        self.storage_quota = DEFAULT_CLONE_STORAGE_QUOTA
        self.developer_ids_provider = None

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

    def mother_bot_id(self) -> int:
        if self.config.is_clone:
            return int(self.config.mother_bot_id or 0)
        return token_bot_id(self.config.bot_token)

    def forbidden_bot_ids(self) -> set[int]:
        """Bots that may not be re-registered under this bot (avoid cycles)."""
        ids = {self.mother_bot_id(), token_bot_id(self.config.bot_token)}
        for row in self.store.bot_clone_ancestors(self.parent_clone_id):
            ids.add(int(row["bot_id"]))
        ids.discard(0)
        return ids

    async def request(
        self, owner_id: int, token: str, owner_name: str = "",
    ) -> tuple[int, str]:
        token = token.strip()
        if ":" not in token or len(token) < 30:
            raise ValueError("Bot Token 格式不正确")
        try:
            async with Bot(token) as bot:
                me = await bot.get_me()
        except (TelegramError, InvalidToken) as exc:
            raise ValueError(f"Bot Token 验证失败：{exc}") from exc
        if int(me.id) in self.forbidden_bot_ids():
            raise ValueError("不能克隆当前机器人或它的上级机器人")
        clone_id = self.store.save_bot_clone_request(
            owner_id, int(me.id), me.username or "", self.encrypt(token),
            parent_clone_id=self.parent_clone_id,
            notified=self.manage_processes, owner_name=owner_name,
        )
        if self.manage_processes:
            current = self.processes.pop(clone_id, None)
            if current and current.poll() is None:
                current.terminate()
        return clone_id, me.username or str(me.id)

    async def approve(self, clone_id: int, developer_id: int) -> tuple[int, str]:
        if not self.manage_processes:
            raise ValueError("克隆审核只能在母机器人进行")
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
        if not self.manage_processes:
            raise ValueError("克隆审核只能在母机器人进行")
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
        env.update(self.child_env(clone_id, owner_id, token))
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

    def clone_db_path(self, clone_id: int) -> Path:
        return self.clones_dir / f"clone-{int(clone_id)}.sqlite3"

    def developer_ids(self) -> set[int]:
        if self.developer_ids_provider:
            try:
                return {int(item) for item in self.developer_ids_provider()}
            except Exception:
                pass
        ids = set(self.config.developer_ids)
        try:
            ids.update(
                int(row["user_id"]) for row in self.store.list_bot_admins()
                if row["role"] == "developer"
            )
        except Exception:
            pass
        if not ids and not self.config.is_clone:
            ids = set(self.config.super_admin_ids)
        return ids

    def child_env(self, clone_id: int, owner_id: int, token: str) -> dict[str, str]:
        developers = sorted(self.developer_ids())
        return {
            "BOT_TOKEN": token,
            "ADMIN_IDS": str(owner_id),
            "SUPER_ADMIN_IDS": "",
            # 开发者在所有子机器人上自动拥有全部管理权限
            "DEVELOPER_IDS": ",".join(str(item) for item in developers),
            "IS_CLONE": "1",
            "CLONE_ID": str(int(clone_id)),
            "DB_PATH": str(self.clone_db_path(clone_id)),
            "MOTHER_DB_PATH": str(Path(self.store.db_path).resolve()),
            "MOTHER_BOT_ID": str(self.mother_bot_id()),
            "CLONE_CIPHER_KEY": self.key.hex(),
            "STORAGE_QUOTA_BYTES": str(int(self.storage_quota)),
            "WEB_PORT": "0",
        }

    def is_running(self, clone_id: int) -> bool:
        process = self.processes.get(int(clone_id))
        return bool(process and process.poll() is None)

    def storage_bytes(self, clone_id: int) -> int:
        base = str(self.clone_db_path(clone_id))
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(base + suffix)
            except OSError:
                pass
        return total

    def stop(self, clone_id: int) -> None:
        process = self.processes.pop(int(clone_id), None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def delete_tree(self, clone_id: int) -> list[int]:
        """Delete a child bot and all of its descendants (cascade).

        Processes are stopped, registry rows removed and each bot's database
        is moved to data/clones/deleted/ (archived, not destroyed).
        """
        if not self.manage_processes:
            raise ValueError("只能在母机器人删除子机器人")
        root = self.store.bot_clone(clone_id)
        if not root:
            raise ValueError("该子机器人不存在或已删除")
        ids = [int(root["id"])] + [
            int(row["id"]) for row in self.store.bot_clone_descendants(clone_id)
        ]
        for item in ids:
            self.stop(item)
        self.store.delete_bot_clones(ids)
        archive = self.clones_dir / "deleted"
        stamp = time.strftime("%Y%m%d%H%M%S")
        for item in ids:
            base = self.clone_db_path(item)
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(base) + suffix)
                if not source.exists():
                    continue
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                    source.rename(archive / f"{source.name}.{stamp}")
                except OSError:
                    pass
        return ids

    def sync_requests(self) -> list:
        """Mother-side: stop bots whose token was re-submitted and return
        requests filed on child bots that still need a review notice."""
        if not self.manage_processes:
            return []
        for clone_id in self.store.pending_bot_clone_ids():
            if self.is_running(clone_id):
                self.stop(clone_id)
        rows = self.store.unnotified_bot_clone_requests()
        for row in rows:
            self.store.mark_bot_clone_notified(int(row["id"]))
        return rows

    def start_all(self) -> None:
        if not self.manage_processes:
            return
        for row in self.store.enabled_bot_clones():
            clone_id = int(row["id"])
            try:
                token = self.decrypt(str(row["token_cipher"]))
                self.start_process(clone_id, int(row["owner_id"]), token)
            except (ValueError, OSError) as exc:
                self.store.set_bot_clone_error(clone_id, str(exc))

    def stop_all(self) -> None:
        if not self.manage_processes:
            return
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        self.processes.clear()
