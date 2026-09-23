from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(db_path: Path, out_dir: Path, keep: int = 30) -> tuple[Path, str]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = out_dir / f"directory-{stamp}.sqlite3"
    with closing(sqlite3.connect(db_path)) as source:
        with closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)
    checksum = sha256(backup_path)
    backup_path.with_suffix(backup_path.suffix + ".sha256").write_text(
        f"{checksum}  {backup_path.name}\n", encoding="utf-8"
    )
    if keep > 0:
        files = sorted(out_dir.glob("directory-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
            old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)
    return backup_path, checksum


def list_backups(out_dir: Path) -> list[dict[str, object]]:
    if not out_dir.exists():
        return []
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "modified": datetime.fromtimestamp(
                path.stat().st_mtime, ZoneInfo("Asia/Shanghai")
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "checksum": sha256(path),
        }
        for path in sorted(out_dir.glob("directory-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    ]
