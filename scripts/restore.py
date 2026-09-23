from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(backup: Path) -> None:
    checksum_file = backup.with_suffix(backup.suffix + ".sha256")
    if not checksum_file.exists():
        return
    expected = checksum_file.read_text(encoding="utf-8").split()[0]
    actual = sha256(backup)
    if expected != actual:
        raise SystemExit(f"Checksum mismatch: expected {expected}, got {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore the directory SQLite database from a backup.")
    parser.add_argument("backup")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/directory.sqlite3"))
    parser.add_argument("--yes", action="store_true", help="Confirm overwrite.")
    args = parser.parse_args()

    if not args.yes:
        raise SystemExit("Refusing to overwrite without --yes")

    backup = Path(args.backup)
    db_path = Path(args.db)
    if not backup.exists():
        raise SystemExit(f"Backup not found: {backup}")
    verify_checksum(backup)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
        safety_copy = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
        shutil.copy2(db_path, safety_copy)
        print(f"safety_copy={safety_copy}")

    shutil.copy2(backup, db_path)
    print(f"restored={db_path}")


if __name__ == "__main__":
    main()
