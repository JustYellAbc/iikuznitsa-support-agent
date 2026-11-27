#!/usr/bin/env python3
"""Доля реплик покупателей из открытой части T02 длиннее лимита чата (T33).

Читает только записи из списка открытой выборки: dev_ids.json из eval/split_chats.py
или командный список T02 (split dev, ID строками, source_sha256 архива). Строки с
другими ID пропускаются до разбора текста, holdout не открывается. Длина считается как
в чате и на сервере (app/core/prefilter.py). Печатает только агрегаты в JSON: тексты,
ID и ПДн не выводятся и не сохраняются.

    python scripts/message_lengths.py --archive "data/raw/чаты поддержки.zip" \\
        --dev-ids ~/.local/share/iikuznitsa-eval-private/dev_ids.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.core.prefilter import MAX_MESSAGE_CODEPOINTS, message_length  # noqa: E402
from eval.split_chats import (  # noqa: E402
    DEFAULT_ARCHIVE,
    LINE_RE,
    MANIFEST_SPLIT_VALUES,
    compute_sha256,
    default_holdout_dir,
)


def dev_lengths(archive: Path, dev_ids: set[int]) -> list[int]:
    """Длины реплик только для dev-ID. Чужие строки отбрасываются по ID до чтения текста."""
    lengths: dict[int, int] = {}
    with zipfile.ZipFile(archive) as zf:
        for member in sorted(zf.namelist()):
            if not member.endswith(".txt"):
                continue
            for line in zf.read(member).decode("utf-8").splitlines():
                match = LINE_RE.match(line)
                if match is None or int(match[1]) not in dev_ids:
                    continue
                lengths[int(match[1])] = message_length(match[4].strip())
    if missing := len(dev_ids - lengths.keys()):
        raise ValueError(f"В архиве не найдено {missing} ID из открытой выборки")
    return list(lengths.values())


def summarize(lengths: list[int], limit: int = MAX_MESSAGE_CODEPOINTS) -> dict:
    ordered = sorted(lengths)
    over = sum(n > limit for n in ordered)
    return {
        "split": "open_dev",
        "limit_codepoints": limit,
        "checked": len(ordered),
        "over_limit": over,
        "over_limit_pct": round(100 * over / len(ordered), 2),
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],  # nearest-rank
        "max": ordered[-1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, default=ROOT / DEFAULT_ARCHIVE)
    parser.add_argument("--dev-ids", type=Path, default=default_holdout_dir() / "dev_ids.json")
    args = parser.parse_args(argv)
    payload = json.loads(args.dev_ids.read_text(encoding="utf-8"))
    if MANIFEST_SPLIT_VALUES.get(str(payload.get("split", "")).strip().lower()) != "dev":
        raise SystemExit("--dev-ids должен указывать на список открытой выборки (split dev или open_dev)")
    expected = payload.get("archive_sha256") or payload.get("source_sha256")
    if expected and compute_sha256(args.archive) != expected:
        raise SystemExit("Список открытой выборки относится к другой версии архива")
    dev_ids = {int(str(chat_id).strip().lstrip("#")) for chat_id in payload["ids"]}
    print(json.dumps(summarize(dev_lengths(args.archive, dev_ids)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
