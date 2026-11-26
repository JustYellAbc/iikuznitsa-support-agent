"""Answer product questions from the selected T06 offer and applicable store guidance."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from app.agent.catalog_selection import CODE, LATIN, MDAEMON, Selection, named_request, offer_product, result
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
PRODUCT_OBJECT = re.compile(
    r"(?=\b(?:у|про|для|по продукту|по товару|лицензи\w* на|сколько (?:стоит|стоят)|нужен|нужна)\s+"
    r"([а-яё][\w.-]*(?:\s+[\w.-]+){0,6}))",
    re.I,
)
GENERIC_WORDS = set(
    "и ли он она оно они мы мне нас вас него неё нее её ее его них меня себя всех мой моя моё мое мои моей "
    "моего моих моему моим моими мою кого чего как когда где сколько сама сам на от до по в с без за "
    "двух троих это эта эти эту этом этого одной одного".split()
)
# Inflections of common buyers, transactions and references to the selected item.
# Limit stems to actual endings so «Новатор» remains distinct from «новой лицензии».
GENERIC_NOUN = re.compile(
    r"(?:покупател|пользовател)(?:ь|я|ей|ям)|(?:сотрудник|товар|продукт|ключ)(?:а|у|ов|ей|и)?|"
    r"(?:компани|организаци|предприяти|лицензи|редакци|позици|верси)(?:я|е|ю|и|й|ям)|"
    r"(?:юр|физ)лиц(?:о|а|у)?|(?:покуп|подпис|достав|постав)к(?:а|у|и|е)|"
    r"(?:нов|действующ|физическ|юридическ|выбранн|текущ|электронн|данн|так|эт|ваш|наш|одн)"
    r"(?:ая|яя|ой|ей|ого|его|ом|ем|ую|юю|ые|ие|их|ых|а|е|и|о)?|"
    r"продлени(?:е|я|ю|и)|оформлени(?:е|я|ю|и)"
)


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
    if PRICE.search(body):
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
    subject = COMPATIBILITY_TARGET.split(text, maxsplit=1)[0]
    code = CODE.search(subject)
    if code:
        return Selection(code[1].rstrip("."), code=True)
    quoted = re.search(r'[«"]([^»"]{2,100})[»"]', subject)
    if quoted or MDAEMON.search(subject):
        return named_request(subject)
    # Customers type brands in lower case too. A change of subject supersedes the
    # prior SKU; buyer attributes and references to the current license do not.
    for match in PRODUCT_OBJECT.finditer(subject):
        name = re.split(r"\s+(?:и|на|для|како\w*|сколько)\b", match[1], maxsplit=1, flags=re.I)[0].strip(" .")
        word = name.casefold().split()[0] if name else ""
        if word and word not in GENERIC_WORDS and not GENERIC_NOUN.fullmatch(word):
            return Selection(name)
    latin = LATIN.search(subject)
    if latin:
        return Selection(latin[0].strip(" ."))
    return None


def delivery_article(db: sqlite3.Connection, title: str) -> tuple[str, Source] | None:
    row = db.execute(
        "SELECT body FROM knowledge WHERE title=? AND source=? LIMIT 1", (title, DELIVERY_GUIDE)
    ).fetchone()
    if not row or not row[0].strip():
        return None
    return row[0].strip(), Source(url=DELIVERY_GUIDE, label=title)


def current_offer(db: sqlite3.Connection, product: Product) -> dict | None:
    row = db.execute(
        "SELECT id,name,params,available,price,currency,source,price_state FROM offers WHERE id=? AND source=?",
        (product.offerId, product.source.url),
    ).fetchone()
    if row and row["currency"] in {"RUB", "RUR"}:
        return {**row, "params": json.loads(row["params"])}
    return None


def refresh_products(index: Path, products: list[Product]) -> list[Product]:
    if not products or not index.is_file():
        return products
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [offer_product(row) for p in products if (row := current_offer(db, p))]


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
        db.row_factory = sqlite3.Row
        row = current_offer(db, products[0])
        if not row:
            return result(OFFER_HELP)
        name, params = row["name"], row["params"]
        p = offer_product(row)
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
        if p.priceState == "availability_check":
            facts.append("Наличие требует подтверждения.")
        if unknown:
            # The T09/T12 offer opens the operator button. Show the sourced partial answer
            # alongside it, even when one detail requires the manager.
            label = " ".join([*facts, "Требует уточнения: " + ", ".join(unknown) + "."])
            return result(OFFER_HELP, products=[p], sources=[Source(url=p.source.url, label=label), *refs])
        return result(" ".join([*facts, *(ref.label for ref in refs)]), "answer", [p], sources=[p.source, *refs])
