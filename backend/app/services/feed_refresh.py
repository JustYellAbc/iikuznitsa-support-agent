"""Атомарное обновление каталога из XML-фида (T14).

Порядок: проверка файла -> сборка индекса T06 рядом с рабочим -> инварианты и сверка состояний цены ->
метаданные снимка -> self-check -> os.replace() -> self-check рабочего пути.
Любой сбой до подмены оставляет рабочий индекс как был. Прежний снимок лежит рядом (*.prev.sqlite3).
Читатели открывают индекс на каждый запрос, поэтому подмена проходит без остановки бэкенда.
"""

import fcntl
import hashlib
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from catalog import index as catalog_index

from app.services.catalog_snapshot import SNAPSHOT_TABLE, open_index, read_offer, read_snapshot
from app.services.price_policy import PRICE_STATES, T06_PRICE_STATES, assess, classify

# Эталоны трёх состояний цены в снимке от 25.11.2026: listed, on_request, availability_check
CONTROL_OFFERS = ("61557", "504", "48093")
# Дата выгрузки без часового пояса считается московской
FEED_TZ = ZoneInfo("Europe/Moscow")


class FeedRejected(Exception):
    """Фид или собранный из него индекс не прошли проверку. Рабочий каталог не изменён."""


@dataclass(frozen=True)
class Limits:
    min_offers: int = 1000
    max_drop: float = 0.5  # на какую долю новый снимок может быть меньше текущего по числу SKU
    max_missing_share: float = 0.01  # допустимая доля офферов без названия, ссылки, цены или available
    allow_older: bool = False


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def feed_time(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise FeedRejected(f"Некорректная дата выгрузки yml_catalog/@date: {raw!r}") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=FEED_TZ)


def inspect_index(path: Path, limits: Limits) -> dict:
    """Инварианты собранного индекса и счётчики трёх состояний цены по политике T14."""
    counts = dict.fromkeys(PRICE_STATES, 0)
    missing = mismatched = 0
    with open_index(path) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise FeedRejected("Собранный индекс повреждён (PRAGMA quick_check)")
        date = db.execute("SELECT snapshot FROM metadata").fetchone()["snapshot"]
        for row in db.execute("SELECT name,source,available,price,price_state FROM offers"):
            state = classify(row["available"], row["price"])
            counts[state] += 1
            mismatched += T06_PRICE_STATES.get(row["price_state"]) != state
            missing += not (row["name"] and row["source"]) or row["price"] is None or row["available"] is None
    feed_time(date)
    total = sum(counts.values())
    if total < limits.min_offers:
        raise FeedRejected(f"В фиде {total} офферов, нужно не меньше {limits.min_offers}")
    if missing > total * limits.max_missing_share:
        raise FeedRejected(f"{missing} из {total} офферов без названия, ссылки, цены или available")
    if mismatched:
        raise FeedRejected(f"У {mismatched} офферов состояние цены индекса T06 расходится с политикой T04")
    return {"feed_date": date, "offers_count": total, **counts}


def check_against_current(stats: dict, current: dict | None, limits: Limits) -> None:
    if current is None:
        return
    if stats["offers_count"] < current["offers_count"] * (1 - limits.max_drop):
        raise FeedRejected(
            f"SKU стало {stats['offers_count']} вместо {current['offers_count']}: "
            f"падение больше {limits.max_drop:.0%}, похоже на обрезанный фид"
        )
    if not limits.allow_older and feed_time(stats["feed_date"]) < feed_time(current["feed_date"]):
        raise FeedRejected(f"Выгрузка {stats['feed_date']} старше текущей {current['feed_date']} (--allow-older)")


def self_check(path: Path, expected: dict) -> dict:
    """Быстрая проверка индекса: метаданные, сводные счётчики, поиск и эталонные офферы."""
    if read_snapshot(path) != expected:
        raise FeedRejected(f"{path}: метаданные снимка не совпадают с ожидаемыми")
    by_state = dict.fromkeys(PRICE_STATES, 0)
    with open_index(path) as db:
        for state, count in db.execute("SELECT price_state, count(*) FROM offers GROUP BY price_state"):
            by_state[T06_PRICE_STATES.get(state, state)] = count
        sample = db.execute("SELECT name FROM offers ORDER BY rowid LIMIT 1").fetchone()["name"]
    if by_state != {state: expected[state] for state in PRICE_STATES}:
        raise FeedRejected(f"{path}: счётчики состояний цены {by_state} не совпадают со снимком")
    words = re.findall(r"[^\W_]+", sample)
    if not words or not catalog_index.search(path, words[0], limit=1):
        raise FeedRejected(f"{path}: полнотекстовый поиск не находит оффер {sample!r}")
    controls = {}
    for offer_id in CONTROL_OFFERS:
        if offer := read_offer(path, offer_id):
            a = assess(offer)
            controls[offer_id] = {
                "available": offer.available,
                "price": offer.price,
                "price_state": a.price_state,
                "show_price": a.show_price,
                "license_term": a.license_term,
                "allow_quote_draft": a.allow_quote_draft,
                "allow_auto_invoice": a.allow_auto_invoice,
                "handoff_reason": a.handoff_reason,
            }
    return {"search_probe": words[0], "controls": controls}


@contextmanager
def _exclusive(index: Path) -> Iterator[None]:
    with open(index.with_name(index.name + ".lock"), "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FeedRejected("Обновление каталога уже выполняется") from exc
        yield


def _copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
    except OSError:
        shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def refresh(feed: Path, index: Path, knowledge: Path, limits: Limits = Limits(), now: datetime | None = None) -> dict:
    if not feed.is_file() or feed.stat().st_size == 0:
        raise FeedRejected(f"Фид пуст или не найден: {feed}")
    with feed.open("rb") as stream:
        if stream.read(2) == b"\x1f\x8b":
            raise FeedRejected("Фид сжат gzip: распакуйте его в XML")
    index.parent.mkdir(parents=True, exist_ok=True)
    staging = index.with_name(f"{index.stem}.staging{index.suffix}")
    previous = index.with_name(f"{index.stem}.prev{index.suffix}")
    with _exclusive(index):
        try:
            current = read_snapshot(index)
        except sqlite3.Error:
            current = None  # повреждённый рабочий индекс не мешает собрать новый
        had_index = index.is_file()
        sha256, size = file_digest(feed)
        try:
            try:
                catalog_index.build(feed, staging, knowledge)
            except Exception as exc:
                raise FeedRejected(f"Фид не собран: {type(exc).__name__}: {exc}") from exc
            stats = inspect_index(staging, limits)
            check_against_current(stats, current, limits)
            snapshot = {
                **stats,
                "feed_sha256": sha256,
                "feed_name": feed.name,
                "feed_bytes": size,
                "refreshed_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            }
            with closing(sqlite3.connect(staging)) as db, db:
                db.executescript(SNAPSHOT_TABLE)
                db.execute(
                    "INSERT INTO feed_snapshot VALUES(:feed_date,:feed_sha256,:feed_name,:feed_bytes,:offers_count,"
                    ":listed,:availability_check,:on_request,:refreshed_at)",
                    snapshot,
                )
            self_check(staging, snapshot)
            if had_index:
                _copy_atomic(index, previous)
            os.replace(staging, index)
        finally:
            staging.unlink(missing_ok=True)
        try:
            report = self_check(index, snapshot)
        except Exception:
            if had_index:
                _copy_atomic(previous, index)
            raise
    return {"snapshot": snapshot, "previous": current, **report}
