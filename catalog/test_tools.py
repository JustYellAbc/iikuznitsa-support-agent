import os
from pathlib import Path

import pytest
from catalog.tools import get_offer, search_catalog, search_published_knowledge, search_store_knowledge
from tests.conftest import api, client, env  # noqa: F401

from app.agent.test_catalog_selection import index as index
from app.agent.test_knowledge import ANSWER, PATCHES, QUESTION, propose, source_case


def test_exact_and_search(index):  # noqa: F811
    hits = search_catalog(index, "MDaemon Standard на 20 мест", license_type="Полная версия")
    assert hits.ok and hits.source_ref == hits.data[0]["source"]
    assert hits.data[0]["offerId"] == "new20"
    card = get_offer(index, hits.data[0]["offerId"]).data[0]
    assert (card["version"], card["unitPriceKopecks"], card["seatLimit"]) == ("2026-01-01", 123456, ["20"])
    assert get_offer(index, "ask").data[0]["unitPriceKopecks"] is None
    assert get_offer(index, "verify").data[0]["available"] is False
    assert search_store_knowledge(index, "оплата").data[0]["version"]
    assert get_offer(index, "missing").data == []
    assert search_catalog(index, "!!!").source_ref is None
    assert not search_catalog(index, "MDaemon", limit=100).ok
    assert not get_offer(index.parent / "missing.db", "new20").ok


def test_only_published_answers(api):  # noqa: F811
    _, hid = source_case(api)
    pid = propose(api, hid)
    lookup = api.client.portal.call
    assert lookup(search_published_knowledge, QUESTION).data == []
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    hit = lookup(search_published_knowledge, QUESTION)
    assert hit.data[0]["body"] == ANSWER and hit.data[0]["version"] == pid
    assert hit.source_ref.endswith("/knowledge/" + pid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/withdraw").status_code == 204
    assert lookup(search_published_knowledge, QUESTION).data == []


@pytest.mark.skipif(not os.getenv("T31_CATALOG_INDEX"), reason="requires the private Softline store index")
def test_store_snapshot():
    path = Path(os.environ["T31_CATALOG_INDEX"])
    for sku, state in [("61557", "listed"), ("48093", "availability_check"), ("504", "on_request")]:
        hit = get_offer(path, sku)
        assert hit.ok and hit.source_ref and hit.data[0]["version"]
        assert hit.data[0]["priceState"] == state
    for seats, sku in [(20, "19576"), (5, "19574")]:
        hit = search_catalog(path, f"MDaemon Standard на {seats} мест", license_type="Полная версия")
        assert sku in {row["offerId"] for row in hit.data}
    pdf = search_catalog(path, "Нужен редактор PDF", by_need=True, limit=2)
    assert len(pdf.data) == 2 and all("pdf editor" in item["name"].lower() for item in pdf.data)
    assert search_store_knowledge(path, "оплата заказа").data
