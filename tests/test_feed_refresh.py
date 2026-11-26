"""T14: атомарное обновление фида, метаданные снимка, откат при битом фиде и статус в API."""

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from catalog.index import KNOWLEDGE

from app.config import get_settings
from app.services.catalog_snapshot import read_offer, read_snapshot
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


def write_feed(path: Path, date: str, offers=BASE) -> Path:
    root = ET.Element("yml_catalog", date=date)
    shop = ET.SubElement(root, "shop")
    ET.SubElement(ET.SubElement(shop, "categories"), "category", id="1").text = "Программы"
    items = ET.SubElement(shop, "offers")
    for offer_id, available, price, name in offers:
        offer = ET.SubElement(items, "offer", id=offer_id, available=available)
        ET.SubElement(offer, "url").text = f"https://store.softline.ru/test/-{offer_id}/"
        ET.SubElement(offer, "price").text = price
        ET.SubElement(offer, "currencyId").text = "RUR"
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
    """Индекс, собранный напрямую catalog/index.py build, заменяется без сверки с прежним снимком."""
    from catalog.index import build

    index = tmp_path / "catalog.sqlite3"
    build(write_feed(tmp_path / "v1.xml", "2026-11-25 10:00"), index, KNOWLEDGE)
    assert read_snapshot(index) is None
    refresh(write_feed(tmp_path / "v2.xml", "2026-11-26 10:00"), index, KNOWLEDGE, LIMITS)
    assert read_snapshot(index)["offers_count"] == 4


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
