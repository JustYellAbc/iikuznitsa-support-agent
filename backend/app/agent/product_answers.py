"""Answer product questions from the selected T06 offer and applicable store guidance."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from app.agent.catalog_selection import CODE, LATIN, MDAEMON, NAMED, Selection, named_request, offer_product, result
from app.agent.handoff import OFFER_HELP, PRICE
from app.contracts.t04 import Product, Source

TOPICS = {
    "term": r"\bсрок\b|срок[аи] (?:действия|лицензии|подписки)|на (?:сколько|год)|сколько (?:лет|месяц)|длительность",
    "updates": r"обновлен|что (?:точно )?входит",
    "rights": r"навсегда|бессроч|пользоваться после|после (?:окончания|истечения)|останется ли",
    "license": r"(?:какой|тип|модель|вид|какая|как)\s+(?:тип |модель )?лицензи|лицензировани|как лицензируется",
    "transfer": r"перенос\w* лиценз|перенести лиценз|переустанов\w* на друг|передать лицензию",
    "edition": r"верси[яюи]|редакци[яюи]",
    "compatibility": r"совместим|совпадает ли|аналог ли|(?:та|то) же|подойд[её]т ли",
    "delivery": (
        r"поставк|доставк|доставля|прид[её]т|получить (?:электронн\w* )?(?:лицензию|ключ)"
        r"|электронн\w* (?:ключ|почт)"
    ),
}
DELIVERY_TIME = re.compile(r"когда|как (?:скоро|быстро)|сколько (?:дн|час)|срок|сегодня|завтра")
COMPATIBILITY_TARGET = re.compile(r"совместим\w*\s+(?:ли\s+)?с\s+|подойд[её]т ли для\s+", re.I)
ORDER_STATUS = re.compile(r"мой заказ|моего заказа|по заказу|где мой|не приш[её]л|уже (?:заказал|оплатил)")
ELECTRONIC_ARTICLE = "Как получить электронный ключ и когда придёт лицензия"
BOX_ARTICLE = "Доставка оборудования и коробочного ПО"
DELIVERY_GUIDE = "https://store.softline.ru/delivery_info/"
UNKNOWN = {"не определено", "другое или отсутствует", "не указано", "нет данных", "мес."}


def topics_for(text: str, has_products: bool) -> set[str]:
    body = text.casefold()
    if not re.search(
        r"\?|\b(?:како\w*|какая|какие|как|что|есть ли|можно ли|сколько|когда|где|подскажите|навсегда|бессроч\w*)\b",
        body,
    ):
        return set()
    topics = {name for name, pattern in TOPICS.items() if re.search(pattern, body)}
    if "delivery" in topics and DELIVERY_TIME.search(body):
        topics.add("delivery_time")
        if not re.search(r"срок (?:лицензии|действия|подписки)", body):
            topics.discard("term")
    if ORDER_STATUS.search(body):
        topics.add("order_status")
    if "rights" in topics:
        topics.add("term")
    if topics and PRICE.search(body):
        topics.add("price")
    if (
        not has_products
        and topics <= {"delivery", "delivery_time"}
        and not (question_target(text) or re.search(r"товар|лицензи|ключ|коробочн|оборудовани|электронн", body))
    ):
        # A general logistics request with no item or delivery channel belongs to the store support flow.
        return set()
    return topics


def question_target(text: str) -> Selection | None:
    """A fact about a newly named product must never be taken from an earlier offer."""
    if CODE.search(text) or MDAEMON.search(text) or LATIN.search(text) or NAMED.search(text) or "«" in text:
        if COMPATIBILITY_TARGET.search(text) and not (CODE.search(text) or MDAEMON.search(text) or NAMED.search(text)):
            prefix = COMPATIBILITY_TARGET.split(text, maxsplit=1)[0]
            return named_request(prefix) if LATIN.search(prefix) else None
        target = named_request(text)
        if target and re.match(r"^(?:этот|эта|это|эту|этой|он|она|оно|его|е[её]|выбранн\w*)\b", target.name, re.I):
            return None
        return target
    target = re.search(r"\b(?:у|для|про)\s+(.+?)(?:[?,!]|$)", text, re.I)
    if target and not re.match(r"(?:него|не[её]|эт\w*|данн\w*|выбранн\w*|этого|этой|лицензии)\b", target[1], re.I):
        return named_request("Нужен " + target[1])
    return None


def delivery_article(db: sqlite3.Connection, title: str) -> tuple[str, Source] | None:
    row = db.execute(
        "SELECT body FROM knowledge WHERE title=? AND source=? LIMIT 1", (title, DELIVERY_GUIDE)
    ).fetchone()
    if not row or not row[0].strip():
        return None
    return row[0].strip(), Source(url=DELIVERY_GUIDE, label=title)


def answer_facts(index: Path, text: str, topics: set[str], products: list[Product]):
    if len(products) > 1:
        return result("По какой позиции проверить условия? Укажите артикул из показанных вариантов.")
    if not index.is_file():
        return result(OFFER_HELP)
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not products:
            if topics == {"delivery"}:
                title = (
                    ELECTRONIC_ARTICLE
                    if re.search(r"электронн", text, re.I)
                    else (BOX_ARTICLE if re.search(r"коробочн|оборудовани", text, re.I) else None)
                )
                article = delivery_article(db, title) if title else None
                if article:
                    body, ref = article
                    return result(body, "answer", sources=[ref])
            if "order_status" in topics:
                return result(OFFER_HELP)
            return result("Назовите товар или его артикул, чтобы я проверил условия по карточке.")
        p = products[0]
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id,name,params,available,price,currency,source,price_state FROM offers WHERE id=? AND source=?",
            (p.offerId, p.source.url),
        ).fetchone()
        if not row or row["currency"] not in {"RUB", "RUR"}:
            return result(OFFER_HELP)
        name, params = row["name"], json.loads(row["params"])
        p = offer_product({**row, "params": params})
        facts = []
        unknown = []
        refs = []

        def values(key):
            return ", ".join(v for v in params.get(key, []) if v.strip() and v.strip().casefold() not in UNKNOWN)

        def field(label, value):
            if value:
                facts.append(f"{label} — {value.rstrip('.')}.")
            else:
                unknown.append(label.lower())

        if "term" in topics:
            field("Срок по карточке", values("Срок действия"))
        if "updates" in topics:
            included = re.search(r"подписк[а-яё]* на обновления на \d+ (?:год[а-я]*|лет|месяц[а-я]*)", name, re.I)
            if included:
                facts.append("В наименовании указано: " + included[0] + ".")
            else:
                unknown.append("состав обновлений")
        if "rights" in topics:
            unknown.append("право использования после окончания подписки")
        if "transfer" in topics:
            unknown.append("право переноса лицензии")
        if "license" in topics:
            field(
                "Модель лицензирования" if "модель" in text.casefold() else "Тип лицензии",
                values("Модель лицензирования") if "модель" in text.casefold() else values("Тип лицензии"),
            )
        if "edition" in topics:
            field("Версия по карточке", values("Версия"))
        if "compatibility" in topics:
            unknown.append("совместимость с указанной конфигурацией")
        if "delivery" in topics:
            method = values("Способ доставки") or values("Тип поставки")
            field("Способ поставки", method)
            if method.casefold() in {"электронный", "электронная"}:
                article = delivery_article(db, ELECTRONIC_ARTICLE)
                if article:
                    body, ref = article
                    instruction = body.split(". ", 1)[0].rstrip(".") + "."
                    refs.append(Source(url=ref.url, label=instruction))
        if "delivery_time" in topics:
            unknown.append("точное время доставки")
        if "order_status" in topics:
            unknown.append("состояние конкретного заказа")
        if "price" in topics:
            if p.unitPriceKopecks is None:
                unknown.append("цена")
            else:
                rub, kop = divmod(p.unitPriceKopecks, 100)
                money = f"{rub:,}".replace(",", " ") + (f",{kop:02}" if kop else "")
                facts.append(f"Цена по снимку — {money} ₽.")
        if unknown:
            # The T09/T12 offer opens the operator button. Show the sourced partial answer
            # alongside it, even when one detail requires the manager.
            label = " ".join([*facts, "Требует уточнения: " + ", ".join(unknown) + "."])
            return result(OFFER_HELP, products=[p], sources=[Source(url=p.source.url, label=label), *refs])
        return result(" ".join([*facts, *(ref.label for ref in refs)]), "answer", [p], sources=[p.source, *refs])
