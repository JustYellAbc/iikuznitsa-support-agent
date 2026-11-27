"""Обновить каталог стенда из нового XML-фида без простоя (T14).

Проверяет фид, собирает индекс T06 рядом с рабочим, сверяет состояния цены, пишет метаданные снимка
и атомарно подменяет $DATA_DIR/private/catalog.sqlite3. При ошибке рабочий каталог не меняется (код выхода 1).

  python scripts/refresh_feed.py data/raw/Mindbox.yml.gz.xml
  python scripts/refresh_feed.py https://example.org/feed.xml   # скачать в $DATA_DIR/raw/ и обновить
  docker compose exec backend python scripts/refresh_feed.py data/raw/Mindbox.yml.gz.xml
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from catalog.index import KNOWLEDGE  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.feed_refresh import FeedRejected, Limits, refresh  # noqa: E402


def download(url: str, directory: Path) -> tuple[Path, str]:
    """Скачивает фид в уникальный временный файл этого запуска, чтобы параллельные загрузки не мешали друг другу."""
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or "feed.xml"
    fd, raw_path = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".part")
    partial = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as out, urlopen(url, timeout=60) as response:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return partial, name


def main() -> int:
    settings = get_settings()
    private_knowledge = settings.data_dir / "private" / "knowledge.json"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("feed", help="путь к XML-фиду или http(s) URL")
    parser.add_argument("--index", type=Path, default=settings.catalog_index_path)
    parser.add_argument(
        "--knowledge",
        type=Path,
        default=private_knowledge if private_knowledge.is_file() else KNOWLEDGE,
        help="снимок справки (по умолчанию $DATA_DIR/private/knowledge.json, иначе catalog/knowledge.json)",
    )
    parser.add_argument("--min-offers", type=int, default=Limits.min_offers)
    parser.add_argument("--max-drop", type=float, default=Limits.max_drop, help="допустимое падение числа SKU")
    parser.add_argument("--allow-older", action="store_true", help="разрешить выгрузку старше текущей")
    args = parser.parse_args()

    downloaded: Path | None = None
    feed_name: str | None = None
    if args.feed.startswith(("http://", "https://")):
        downloaded, feed_name = download(args.feed, settings.data_dir / "raw")
    limits = Limits(min_offers=args.min_offers, max_drop=args.max_drop, allow_older=args.allow_older)
    try:
        try:
            report = refresh(downloaded or Path(args.feed), args.index, args.knowledge, limits, feed_name=feed_name)
        except FeedRejected as exc:
            print(f"Каталог не изменён: {exc}", file=sys.stderr)
            return 1
        if downloaded is not None and feed_name is not None:
            os.replace(downloaded, settings.data_dir / "raw" / feed_name)
            downloaded = None
    finally:
        if downloaded is not None:
            downloaded.unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
