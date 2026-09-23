from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune_backups(out_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    backups = sorted(out_dir.glob("directory-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an online SQLite backup for the directory bot.")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/directory.sqlite3"))
    parser.add_argument("--out-dir", default="backups")
    parser.add_argument("--keep", type=int, default=30, help="How many backup files to keep.")
    args = parser.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    backup_path = out_dir / f"directory-{stamp}.sqlite3"

    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    checksum = sha256(backup_path)
    backup_path.with_suffix(backup_path.suffix + ".sha256").write_text(
        f"{checksum}  {backup_path.name}\n",
        encoding="utf-8",
    )
    prune_backups(out_dir, args.keep)
    print(f"backup={backup_path}")
    print(f"sha256={checksum}")


if __name__ == "__main__":
    main()
