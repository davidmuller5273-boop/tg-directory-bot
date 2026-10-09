"""Build tg_directory_bot/data/life_guide.json from a local clone of
https://github.com/eternity4719/HowToLiveBetter (text CC BY 4.0).

Usage: python scripts/build_life_guide.py /path/to/HowToLiveBetter

Only the title, 成本 / 说人话 / 证据等级 of each item and the intro of each
section are kept (excerpt); see ATTRIBUTION in the output.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tg_directory_bot" / "data" / "life_guide.json"
FIELDS = {"成本": "cost", "说人话": "plain", "证据等级": "grade"}


def clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_chapter(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    number = int(path.name.split("-", 1)[0])
    title = ""
    intro = ""
    items = []
    current = None
    for line in lines:
        if line.startswith("# ") and not title:
            title = clean(re.sub(r"^#\s*\d+\.\s*", "", line))
            continue
        heading = re.match(r"^###\s*(\d+)\.\s*(.+)$", line)
        if heading:
            current = {"n": int(heading.group(1)), "title": clean(heading.group(2))}
            items.append(current)
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None:
            if title and not intro and line.strip() and not line.startswith(("[", "#", "<", "**", "|")):
                intro = clean(line)
            continue
        field = re.match(r"^-\s*([^：:]+)[：:]\s*(.*)$", line)
        if field and field.group(1).strip() in FIELDS:
            current[FIELDS[field.group(1).strip()]] = clean(field.group(2))
    return {"id": number, "title": title, "intro": intro,
            "items": [item for item in items if item.get("plain") or item.get("cost")]}


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/HowToLiveBetter")
    chapters = [parse_chapter(p) for p in sorted((root / "book").glob("[0-9][0-9]-*.md"))]
    try:
        commit = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %cs"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        commit = ""
    data = {
        "source": "https://github.com/eternity4719/HowToLiveBetter",
        "web": "https://eternity4719.github.io/HowToLiveBetter/",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "version": commit,
        "chapters": chapters,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT}: {len(chapters)} chapters, {sum(len(c['items']) for c in chapters)} items")


if __name__ == "__main__":
    main()
