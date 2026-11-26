"""Verify the catalog records and reference amounts used by the T03 cases."""

import argparse
import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal
from pathlib import Path

SNAPSHOT = "27eaec9e77942c61d58b3c04dcaa8991c707dc0fcb64a6442a7408d9908cb148"
# offer ID -> product group, user capacity, license type, package price in RUR
MDAEMON = {
    "19574": ("16940", "5", "Полная версия", 47808),
    "19575": ("16940", "10", "Полная версия", 61698),
    "19576": ("16940", "20", "Полная версия", 77556),
    "19587": ("16940", "20", "Продление", 38778),
    "19578": ("16940", "40", "Полная версия", 94341),
    "359841": ("17067", "20", "Полная версия", 32643),
    "359843": ("17067", "40", "Полная версия", 50933),
}
STORE = "https://store.softline.ru/"
STANDARD = re.compile(r"\bstandard\b", re.IGNORECASE)
PRICE_CONTROLS = {
    # availability, raw price, raw license term, Store path
    "504": ("false", 0, "12 мес.", "adobe/65272676ba01a12-504/"),
    "48093": ("false", 1523654, "Не определено", "iceni-technology/-48093/"),
    "61557": ("true", 4900, "Не определено", "sunrav/-61557/"),
}
PROTECTION = {
    # price per sale unit, product family, name fragment, platform, Store path
    "19645": (34727, "17016", "Security Gateway for Email Servers", "Windows", "alt-n-technologies-ltd/-19645/"),
    "3850": (Decimal("1670.94"), "3707", "Security для почтовых серверов", "Debian", "kaspersky/kl4313raqfs-3850/"),
    "532803": (6500, "4092", "Dr.Web Server Security Suite", "Windows Server 2019", "drweb/lbs-ac-12m-1-a3-532803/"),
}


def check(actual, expected, label):
    if actual != expected:
        raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")


def check_param(offer, name, value):
    # Compare all values: multiple XML param elements can have the same name.
    values = [item.text for item in offer.findall("param") if item.get("name") == name]
    check(values, [value], f"{offer.get('id')} / {name}")


def price_of(offer):
    return Decimal(offer.findtext("price"))


def verify_mdaemon(offers):
    for identifier, (family, capacity, kind, price) in MDAEMON.items():
        offer = offers[identifier]
        check(offer.get("group_id"), family, f"family {identifier}")
        check(price_of(offer), price, f"price {identifier}")
        check(offer.get("available"), "true", f"availability {identifier}")
        check(offer.findtext("vendorCode"), "", f"vendor code {identifier}")
        check(offer.findtext("url"), STORE + f"alt-n-technologies-ltd/-{identifier}/", f"url {identifier}")
        check("Windows" in offer.findtext("description", ""), True, f"platform {identifier}")
        check_param(offer, "К-во пользователей до", capacity)
        check_param(offer, "Тип лицензии", kind)
        check_param(offer, "Срок действия", "12 мес.")
        check_param(offer, "Пользователь", "Юрлицо")
        check_param(offer, "Тип поставки", "электронная")
        if identifier in ("19574", "19575"):
            check_param(offer, "Версия", "другое или отсутствует")


def verify_price_controls(offers):
    for identifier, (available, raw_price, term, path) in PRICE_CONTROLS.items():
        offer = offers[identifier]
        check(offer.get("available"), available, f"availability {identifier}")
        check(price_of(offer), raw_price, f"price {identifier}")
        check(offer.findtext("url"), STORE + path, f"url {identifier}")
        check_param(offer, "Срок действия", term)


def verify_protection(offers):
    for identifier, (price, group, title, platform, path) in PROTECTION.items():
        offer = offers[identifier]
        check(price_of(offer), price, f"unit price {identifier}")
        check(offer.get("group_id"), group, f"group {identifier}")
        check(offer.get("available"), "true", f"availability {identifier}")
        check(title in offer.findtext("name", ""), True, f"title {identifier}")
        check(platform in offer.findtext("description", ""), True, f"platform {identifier}")
        check(offer.findtext("url"), STORE + path, f"url {identifier}")
        check_param(offer, "Пользователь", "Юрлицо")
        check_param(offer, "Срок действия", "12 мес.")

    gateway, mail, file_server = (offers[k] for k in ("19645", "3850", "532803"))
    check_param(gateway, "К-во пользователей до", "50")
    check_param(gateway, "Тип лицензии", "Полная версия")
    check("базовая лицензия" in mail.findtext("name"), True, "mail purchase type")
    check("по количеству почтовых адресов" in mail.findtext("name"), True, "mail unit")
    check(mail.findtext("sales_notes").strip(), "Кол-во от 50 до 99 шт.", "mail volume tier")
    check_param(mail, "Номер записи в реестре отечественного ПО", "другое или отсутствует")
    check("на 1 устройство" in file_server.findtext("name"), True, "file server package size")
    check_param(file_server, "Тип лицензии", "Базовая")
    check(price_of(gateway) + price_of(file_server), 41227, "gateway and file server")
    check(50 * price_of(mail) + price_of(file_server), 90047, "mail addresses and file server")


def verify(path):
    with path.open("rb") as source:
        check(hashlib.file_digest(source, "sha256").hexdigest(), SNAPSHOT, "snapshot")
    counts = Counter()
    ids = set(MDAEMON) | set(PRICE_CONTROLS) | set(PROTECTION)
    offers = {}
    family_count = 0
    standard_count = 0
    for _, offer in ET.iterparse(path):
        if offer.tag != "offer":
            continue
        identifier = offer.get("id")
        raw = offer.findtext("price")
        price = Decimal(raw) if raw and raw.strip() else None
        state = "missing" if price is None else "positive" if price > 0 else "zero" if price == 0 else "negative"
        counts[offer.get("available"), state] += 1
        name = offer.findtext("name", "")
        if "mdaemon" in name.casefold():
            family_count += 1
            if STANDARD.search(name) or STANDARD.search(offer.findtext("description", "")):
                standard_count += 1
        if identifier in ids:
            check(identifier in offers, False, f"duplicate offer {identifier}")
            check(offer.findtext("currencyId"), "RUR", f"currency {identifier}")
            offers[identifier] = offer
        else:
            offer.clear()

    check(set(offers), ids, "case records")
    check((family_count, standard_count), (510, 0), "MDaemon names and Standard in names/descriptions")
    check(
        counts,
        Counter({("true", "positive"): 44601, ("false", "positive"): 11170, ("false", "zero"): 80269}),
        "availability and price distribution",
    )
    verify_mdaemon(offers)
    verify_price_controls(offers)
    verify_protection(offers)
    original = price_of(offers["19576"]) + price_of(offers["359841"])
    revised = price_of(offers["19578"]) + price_of(offers["359843"])
    check((original, revised, revised - original), (110199, 145274, 35075), "quote revisions")
    print(f"Verified {sum(counts.values())} offers, {len(offers)} reference SKUs, 510 MDaemon names.")
    print("Mail and file protection configurations: 41227 and 90047 RUR.")
    print(f"Catalog amounts: {original} -> {revised} RUR (difference {revised - original}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    verify(parser.parse_args().catalog)
