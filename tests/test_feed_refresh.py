"""T14: атомарное обновление фида, метаданные снимка, откат при битом фиде и статус в API."""

import gzip
import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
from catalog.index import KNOWLEDGE

from app.config import get_settings
from app.services.catalog_snapshot import open_index, read_offer, read_snapshot
from app.services.feed_refresh import FeedRejected, Limits, refresh
from app.services.price_policy import assess
from tests.conftest import Api

ROOT = Path(__file__).resolve().parents[1]
LIMITS = Limits(min_offers=3)
BASE = [
    ("t1", "true", "100", "Тестовый офисный пакет"),
    ("t2", "false", "0", "Графический редактор"),
    ("t3", "false", "2500.50", "Серверная платформа"),
    ("t4", "true", "990", "Антивирус для почты"),
]


def write_feed(path: Path, date: str, offers=BASE, *, currency: str = "RUR", scheme: str = "https") -> Path:
    root = ET.Element("yml_catalog", date=date)
    shop = ET.SubElement(root, "shop")
    ET.SubElement(ET.SubElement(shop, "categories"), "category", id="1").text = "Программы"
    items = ET.SubElement(shop, "offers")
    for offer_id, available, price, name in offers:
        offer = ET.SubElement(items, "offer", id=offer_id, available=available)
        ET.SubElement(offer, "url").text = f"{scheme}://store.softline.ru/test/-{offer_id}/"
        ET.SubElement(offer, "price").text = price
        ET.SubElement(offer, "currencyId").text = currency
        ET.SubElement(offer, "categoryId").text = "1"
        ET.SubElement(offer, "name").text = name
        ET.SubElement(offer, "param", name="Срок действия").text = "Не определено"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def live(tmp_path):
    """Рабочий индекс на первом снимке."""
    index = tmp_path / "private" / "catalog.sqlite3"
    feed = write_feed(tmp_path / "v1.xml", "2026-11-25 10:00")
    refresh(feed, index, KNOWLEDGE, LIMITS, now=datetime(2026, 11, 25, 12, tzinfo=UTC))
    return index


def test_refresh_replaces_offer_and_snapshot(tmp_path, live):
    first = read_snapshot(live)
    assert first["feed_date"] == "2026-11-25 10:00"
    assert (first["offers_count"], first["listed"], first["availability_check"], first["on_request"]) == (4, 2, 1, 1)
    assert first["feed_sha256"] == digest(tmp_path / "v1.xml")
    assert assess(read_offer(live, "t1")).price_state == "listed"

    changed = [("t1", "false", "150", "Тестовый офисный пакет"), *BASE[1:]]
    feed = write_feed(tmp_path / "v2.xml", "2026-11-26 10:00", changed)
    report = refresh(feed, live, KNOWLEDGE, LIMITS, now=datetime(2026, 11, 26, 12, tzinfo=UTC))

    offer = read_offer(live, "t1")
    assert (offer.available, offer.price) == (False, "150")
    a = assess(offer)
    assert (a.price_state, a.unit_price_kopecks, a.allow_auto_invoice) == ("availability_check", 15000, False)
    snapshot = read_snapshot(live)
    assert snapshot == report["snapshot"]
    assert snapshot["feed_date"] == "2026-11-26 10:00"
    assert snapshot["feed_sha256"] == digest(feed) != first["feed_sha256"]
    assert snapshot["refreshed_at"] == "2026-11-26T12:00:00+00:00"
    assert (snapshot["listed"], snapshot["availability_check"], snapshot["on_request"]) == (1, 2, 1)
    assert report["previous"] == first
    # Прежний снимок сохранён рядом для ручного отката
    assert read_snapshot(live.with_name("catalog.prev.sqlite3")) == first
    assert report["search_probe"]
    assert live.stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in live.parent.iterdir()) == [
        "catalog.prev.sqlite3",
        "catalog.sqlite3",
        "catalog.sqlite3.lock",
    ]


BROKEN = {
    "truncated": lambda p: p.write_bytes(write_feed(p, "2026-11-26 10:00").read_bytes()[:-200]),
    "empty_file": lambda p: p.write_bytes(b""),
    "no_offers": lambda p: write_feed(p, "2026-11-26 10:00", []),
    "too_few": lambda p: write_feed(p, "2026-11-26 10:00", BASE[:2]),
    "no_date": lambda p: p.write_text("<yml_catalog><shop><offers/></shop></yml_catalog>", encoding="utf-8"),
    "bad_date": lambda p: write_feed(p, "вчера", BASE),
    "older": lambda p: write_feed(p, "2026-11-24 10:00", BASE),
    "gzip": lambda p: p.write_bytes(b"\x1f\x8b\x08\x00"),
    "not_catalog": lambda p: p.write_text("<rss><offers/></rss>", encoding="utf-8"),
}


@pytest.mark.parametrize("case", BROKEN)
def test_broken_feed_keeps_live_catalog(tmp_path, live, case):
    before = digest(live)
    feed = tmp_path / "bad.xml"
    BROKEN[case](feed)
    with pytest.raises(FeedRejected):
        refresh(feed, live, KNOWLEDGE, LIMITS)
    assert digest(live) == before
    assert assess(read_offer(live, "t1")).price_state == "listed"
    assert not [p for p in live.parent.iterdir() if "staging" in p.name or p.suffix == ".tmp"]


def test_sharp_drop_in_offers_is_rejected(tmp_path, live):
    feed = write_feed(tmp_path / "v2.xml", "2026-11-26 10:00", BASE[:3])
    with pytest.raises(FeedRejected, match="падение"):
        refresh(feed, live, KNOWLEDGE, Limits(min_offers=1, max_drop=0.2))
    assert read_snapshot(live)["offers_count"] == 4


def test_older_feed_allowed_explicitly(tmp_path, live):
    feed = write_feed(tmp_path / "old.xml", "2026-11-24 10:00")
    refresh(feed, live, KNOWLEDGE, Limits(min_offers=3, allow_older=True))
    assert read_snapshot(live)["feed_date"] == "2026-11-24 10:00"


def test_index_without_snapshot_metadata_is_upgraded(tmp_path):
    """Индекс T06 (без feed_snapshot) обновляется свежим валидным фидом с фиксацией fallback previous."""
    from catalog.index import build

    index = tmp_path / "catalog.sqlite3"
    build(write_feed(tmp_path / "v1.xml", "2026-11-25 10:00"), index, KNOWLEDGE)
    assert read_snapshot(index) is None
    report = refresh(write_feed(tmp_path / "v2.xml", "2026-11-26 10:00"), index, KNOWLEDGE, LIMITS)
    assert report["previous"] == {"feed_date": "2026-11-25 10:00", "offers_count": 4}
    assert read_snapshot(index)["offers_count"] == 4


def _bulk_offers(count: int) -> list[tuple[str, str, str, str]]:
    return [(f"sku-{i}", "true", "1000", f"Лицензия ПО {i}") for i in range(count)]


def _read_t06_state(index: Path) -> tuple[str, int]:
    with open_index(index) as db:
        snapshot = db.execute("SELECT snapshot FROM metadata").fetchone()["snapshot"]
        offers_count = db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    return snapshot, offers_count


def test_t06_index_rejects_older_feed(tmp_path):
    """Первое обновление поверх индекса T06 (без feed_snapshot) отклоняет более старую дату снимка."""
    from catalog.index import build

    index = tmp_path / "private" / "catalog.sqlite3"
    offers = _bulk_offers(3000)
    build(write_feed(tmp_path / "t06.xml", "2026-11-25 10:00", offers), index, KNOWLEDGE)
    assert read_snapshot(index) is None
    before = digest(index)

    older = write_feed(tmp_path / "older.xml", "2026-11-24 10:00", offers)
    with pytest.raises(FeedRejected, match="старше"):
        refresh(older, index, KNOWLEDGE, Limits())

    assert digest(index) == before
    assert read_snapshot(index) is None
    assert _read_t06_state(index) == ("2026-11-25 10:00", 3000)


def test_t06_index_rejects_sharp_drop_in_offers(tmp_path):
    """Первое обновление поверх индекса T06 (без feed_snapshot) отклоняет резкое падение числа SKU."""
    from catalog.index import build

    index = tmp_path / "private" / "catalog.sqlite3"
    offers = _bulk_offers(3000)
    build(write_feed(tmp_path / "t06.xml", "2026-11-25 10:00", offers), index, KNOWLEDGE)
    assert read_snapshot(index) is None
    before = digest(index)

    shrunk = write_feed(tmp_path / "shrunk.xml", "2026-11-26 10:00", offers[:1000])
    with pytest.raises(FeedRejected, match="падение"):
        refresh(shrunk, index, KNOWLEDGE, Limits())

    assert digest(index) == before
    assert read_snapshot(index) is None
    assert _read_t06_state(index) == ("2026-11-25 10:00", 3000)


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"currency": "USD", "scheme": "https"},
        {"currency": "RUR", "scheme": "http"},
    ],
)
def test_unusable_currency_or_http_feed_is_rejected(tmp_path, bad_kwargs):
    """Фид того же размера с 100% USD или 100% http://-ссылками отклоняется, рабочий индекс сохраняется."""
    index = tmp_path / "private" / "catalog.sqlite3"
    offers = _bulk_offers(1000)
    healthy = write_feed(tmp_path / "healthy.xml", "2026-11-25 10:00", offers)
    refresh(healthy, index, KNOWLEDGE, Limits())
    before = digest(index)
    before_snap = read_snapshot(index)

    broken = write_feed(tmp_path / "bad.xml", "2026-11-26 10:00", offers, **bad_kwargs)
    with pytest.raises(FeedRejected, match="пригодно"):
        refresh(broken, index, KNOWLEDGE, Limits())

    assert digest(index) == before
    assert read_snapshot(index) == before_snap
    assert read_offer(index, "sku-0").currency == "RUR"


def test_repeat_identical_snapshot_preserves_previous_backup(tmp_path):
    """A -> B -> повтор B не перезатирает catalog.prev.sqlite3 версией B (сохраняет A)."""
    index = tmp_path / "private" / "catalog.sqlite3"
    feed_a = write_feed(tmp_path / "a.xml", "2026-11-24 10:00", BASE)
    changed_b = [("t1", "true", "250", "Тестовый офисный пакет"), *BASE[1:]]
    feed_b = write_feed(tmp_path / "b.xml", "2026-11-25 10:00", changed_b)

    refresh(feed_a, index, KNOWLEDGE, LIMITS, now=datetime(2026, 11, 24, 12, tzinfo=UTC))
    snap_a = read_snapshot(index)
    refresh(feed_b, index, KNOWLEDGE, LIMITS, now=datetime(2026, 11, 25, 12, tzinfo=UTC))
    snap_b = read_snapshot(index)
    assert read_snapshot(index.with_name("catalog.prev.sqlite3")) == snap_a

    # Повтор идентичного фида B
    refresh(feed_b, index, KNOWLEDGE, LIMITS, now=datetime(2026, 11, 25, 13, tzinfo=UTC))
    assert read_snapshot(index)["feed_date"] == "2026-11-25 10:00"
    assert read_snapshot(index)["feed_sha256"] == snap_b["feed_sha256"]
    assert read_snapshot(index.with_name("catalog.prev.sqlite3")) == snap_a
    assert sorted(p.name for p in index.parent.iterdir()) == [
        "catalog.prev.sqlite3",
        "catalog.sqlite3",
        "catalog.sqlite3.lock",
    ]


def test_concurrent_feed_mutation_does_not_mismatch_hash_and_db(tmp_path, monkeypatch):
    """Подмена исходного XML между хешированием и build() не ломает соответствие SHA-256 и собранной БД."""
    from catalog import index as catalog_index

    index = tmp_path / "private" / "catalog.sqlite3"
    feed_path = write_feed(tmp_path / "feed.xml", "2026-11-25 10:00", BASE)
    sha_a = digest(feed_path)
    mutated = [("t1", "true", "999", "Тестовый офисный пакет"), *BASE[1:]]
    real_build = catalog_index.build
    seen_build_paths: list[Path] = []

    def mutating_build(src, dest, knowledge):
        seen_build_paths.append(src)
        # Имитируем конкурентную подмену исходного файла до запуска парсера T06
        write_feed(feed_path, "2026-11-26 10:00", mutated)
        return real_build(src, dest, knowledge)

    monkeypatch.setattr(catalog_index, "build", mutating_build)
    report = refresh(feed_path, index, KNOWLEDGE, LIMITS)

    assert seen_build_paths and seen_build_paths[0] != feed_path
    assert report["snapshot"]["feed_sha256"] == sha_a
    assert report["snapshot"]["feed_date"] == "2026-11-25 10:00"
    assert read_offer(index, "t1").price == "100"
    assert not [p for p in index.parent.iterdir() if p.name.startswith(".feed-import-")]


def test_cli_reports_and_exit_codes(tmp_path):
    index = tmp_path / "catalog.sqlite3"
    feed = write_feed(tmp_path / "feed.xml", "2026-11-25 10:00")
    cmd = [sys.executable, str(ROOT / "scripts" / "refresh_feed.py"), "--index", str(index), "--min-offers", "3"]
    ok = subprocess.run([*cmd, str(feed)], capture_output=True, text=True, cwd=tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert '"offers_count": 4' in ok.stdout
    feed.write_text("<yml_catalog", encoding="utf-8")
    bad = subprocess.run([*cmd, str(feed)], capture_output=True, text=True, cwd=tmp_path)
    assert bad.returncode == 1
    assert "Каталог не изменён" in bad.stderr
    assert read_snapshot(index)["offers_count"] == 4


def test_cli_accepts_gzip_or_xml_and_keeps_last_good_download_on_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    index = tmp_path / "catalog.sqlite3"
    xml = write_feed(tmp_path / "feed.xml", "2026-11-25 10:00").read_bytes()
    payload = xml

    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            if payload.startswith(b"\x1f\x8b"):
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(payload)

    with HTTPServer(("127.0.0.1", 0), Feed) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/Mindbox.yml.gz"
        cmd = [sys.executable, str(ROOT / "scripts/refresh_feed.py"), url, "--index", str(index), "--min-offers", "3"]
        archive = tmp_path / "data/raw/Mindbox.yml.gz"
        try:
            for payload in (xml, gzip.compress(xml)):
                run = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
                assert run.returncode == 0, run.stderr
                assert read_snapshot(index)["feed_sha256"] == hashlib.sha256(xml).hexdigest()
                assert read_snapshot(index)["feed_bytes"] == len(xml)
                assert archive.read_bytes() == payload
            good_archive = payload
            before = digest(index)
            payload = good_archive[:-5]
            bad = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
            assert bad.returncode == 1 and "gzip" in bad.stderr
            assert digest(index) == before and archive.read_bytes() == good_archive
            assert not list(archive.parent.glob("*.part"))
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("existing", [0, 1, 2])
def test_failed_published_check_restores_live_and_preserves_previous(tmp_path, monkeypatch, existing):
    from app.services import feed_refresh

    index = tmp_path / "catalog.sqlite3"
    prev = tmp_path / "catalog.prev.sqlite3"
    for day in range(1, existing + 1):
        refresh(write_feed(tmp_path / "feed.xml", f"2026-11-{day:02} 10:00"), index, KNOWLEDGE, LIMITS)
    live_before = digest(index) if index.exists() else None
    prev_before = digest(prev) if prev.exists() else None
    original = feed_refresh.self_check

    def fail_on_live(path, snapshot):
        if path == index:
            raise FeedRejected("final check failed")
        return original(path, snapshot)

    monkeypatch.setattr(feed_refresh, "self_check", fail_on_live)
    with pytest.raises(FeedRejected, match="final check failed"):
        refresh(write_feed(tmp_path / "feed.xml", "2026-11-03 10:00"), index, KNOWLEDGE, LIMITS)
    assert (digest(index) if index.exists() else None) == live_before
    assert (digest(prev) if prev.exists() else None) == prev_before
    assert not index.with_name("catalog.rollback.sqlite3").exists()


@pytest.mark.parametrize("currency", ["rub", "rUr"])
def test_refresh_currency_is_searchable_in_chat(tmp_path, api, currency):
    feed = write_feed(tmp_path / "feed.xml", "2026-11-25 10:00", currency=currency)
    refresh(feed, get_settings().catalog_index_path, KNOWLEDGE, LIMITS)
    cid = api.new_conversation()
    for text in ["Артикул t1", "Какая цена?"]:
        response = api.say(cid, text)
        assert response.status_code == 202
        api.idle()
        events = [e["data"] for e in api.events(cid) if e["actionId"] == response.json()["actionId"]]
        assert "100 ₽" in events[-1]["text"] and events[-1]["sources"][0]["url"].endswith("/-t1/")
        if text.startswith("Артикул"):
            assert events[-2]["products"][0]["unitPriceKopecks"] == 10000


def test_catalog_status_in_api(api: Api, client, tmp_path):
    assert client.get("/health").json()["offers_count"] == 0
    api.login()
    status = api.call("manager", "GET", "/api/v1/manager/catalog").json()
    assert status["snapshot"] is None
    assert status["availability_check"]["allow_auto_invoice"] is False

    refresh(write_feed(tmp_path / "feed.xml", "2026-11-25 10:00"), get_settings().catalog_index_path, KNOWLEDGE, LIMITS)
    assert client.get("/health").json()["offers_count"] == 4
    snapshot = api.call("manager", "GET", "/api/v1/manager/catalog").json()["snapshot"]
    assert (snapshot["listed"], snapshot["availability_check"], snapshot["on_request"]) == (2, 1, 1)
    assert snapshot["feed_sha256"] == digest(tmp_path / "feed.xml")
    assert api.call("customer", "GET", "/api/v1/manager/catalog").status_code == 401
