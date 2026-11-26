"""Verify the catalog records and reference amounts used by the T03 cases."""

import argparse
import hashlib
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal
from pathlib import Path


SNAPSHOT = "27eaec9e77942c61d58b3c04dcaa8991c707dc0fcb64a6442a7408d9908cb148"
# offer ID -> product group, user capacity, license type, package price in RUR
EXPECTED = {
    "19576": ("16940", "20", "Полная версия", 77556),
    "19587": ("16940", "20", "Продление", 38778),
    "19578": ("16940", "40", "Полная версия", 94341),
    "359841": ("17067", "20", "Полная версия", 32643),
    "359843": ("17067", "40", "Полная версия", 50933),
}


def check(actual, expected, label):
    if actual != expected:
        raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")


def verify(path):
    with path.open("rb") as source:
        check(hashlib.file_digest(source, "sha256").hexdigest(), SNAPSHOT, "snapshot")
    counts = Counter()
    prices = {}
    zero_control = False
    for _, offer in ET.iterparse(path):
        if offer.tag != "offer":
            continue
        identifier = offer.get("id")
        raw = offer.findtext("price")
        price = Decimal(raw) if raw and raw.strip() else None
        state = "missing" if price is None else "positive" if price > 0 else "zero" if price == 0 else "negative"
        counts[offer.get("available"), state] += 1
        if identifier in EXPECTED:
            check(identifier in prices, False, f"duplicate offer {identifier}")
            params = {p.get("name"): p.text for p in offer.findall("param")}
            actual = (offer.get("group_id"), params.get("К-во пользователей до"),
                      params.get("Тип лицензии"), price)
            check(actual, EXPECTED[identifier], identifier)
            check(offer.get("available"), "true", f"availability {identifier}")
            check(offer.findtext("currencyId"), "RUR", f"currency {identifier}")
            check(params.get("Срок действия"), "12 мес.", f"term {identifier}")
            check(offer.findtext("vendorCode"), "", f"vendor code {identifier}")
            check("Windows" in offer.findtext("description", ""), True, f"platform {identifier}")
            prices[identifier] = price
        if identifier == "504":
            check((offer.get("available"), price), ("false", Decimal(0)), "unavailable control")
            zero_control = True
        offer.clear()

    check(set(prices), set(EXPECTED), "case records")
    check(zero_control, True, "unavailable control present")
    check(counts, Counter({("true", "positive"): 44601, ("false", "positive"): 11170,
                           ("false", "zero"): 80269}), "availability and price distribution")
    original = prices["19576"] + prices["359841"]
    revised = prices["19578"] + prices["359843"]
    check((original, revised, revised - original), (110199, 145274, 35075), "quote revisions")
    print(f"Verified {sum(counts.values())} offers, {len(prices)} MDaemon records and offer 504.")
    print(f"Catalog amounts: {original} -> {revised} RUR (difference {revised - original}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    verify(parser.parse_args().catalog)
