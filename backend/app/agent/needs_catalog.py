"""Match documented product families to current, purchasable rows in one T06 snapshot."""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.agent.catalog_selection import Result, result
from app.agent.handoff import OFFER_HELP
from app.agent.needs_brief import Brief, questions
from app.agent.needs_discovery import compare_catalog
from app.contracts.t04 import Product, Source
from app.services.price_policy import is_supported_currency


@dataclass(frozen=True)
class Family:
    name: str
    function: str
    source: str
    unit: str


MAILBOX = Family(
    "MDaemon Messaging Server",
    "Почтовый сервер для создания ящиков на Windows",
    "https://mdaemon.com/products/mdaemon-email-server",
    "упаковка",
)
GATEWAY = Family(
    "Security Gateway for Email Servers",
    "Фильтрация почты через отдельный шлюз на Windows",
    "https://mdaemon.com/pages/security-gateway",
    "упаковка",
)
POSTFIX = Family(
    "Kaspersky Security для почтовых серверов",
    "Фильтрация на почтовом сервере Postfix под Linux",
    "https://support.kaspersky.com/klms8/56825",
    "адрес",
)
FILES = Family(
    "Dr.Web Server Security Suite",
    "Антивирус для файлового сервера Windows",
    "https://products.drweb.ru/fileserver/win/",
    "сервер",
)
DRWEB_UNITS = Source(url="https://license.drweb.ru/sss/", label="Dr.Web: единица лицензирования серверов")
REGISTRY = Source(
    url="https://www.kaspersky.ru/small-to-medium-business-security/registry", label="Перечень российского ПО вендора"
)


def money(kopecks: int) -> str:
    rub, kop = divmod(kopecks, 100)
    return f"{rub:,}".replace(",", " ") + (f",{kop:02}" if kop else "") + " ₽"


@dataclass
class Line:
    family: Family
    product: Product
    quantity: int
    unit_price: int

    @property
    def total(self) -> int:
        return self.quantity * self.unit_price

    def display(self) -> str:
        return (
            f"{self.family.name} (арт. {self.product.offerId}): {self.quantity} × {money(self.unit_price)} "
            f"({self.family.unit}) = {money(self.total)}"
        )


def allowed_quantity(notes: str, units: int) -> bool:
    if not notes:
        return True
    limits = re.fullmatch(r"кол-во от (\d+)(?: до (\d+))? шт\.?", notes.strip(), re.I)
    return bool(limits and int(limits[1]) <= units and (not limits[2] or units <= int(limits[2])))


def find_line(db: sqlite3.Connection, family: Family, b: Brief, count: int) -> Line | None:
    rows = db.execute(
        """SELECT o.* FROM products JOIN offers o ON products.rowid=o.rowid
           WHERE products MATCH ? AND o.available=1 AND o.price_state='priced_available'""",
        (f'name:"{family.name}"',),
    )
    candidates = []
    for row in rows:
        if not is_supported_currency(row["currency"]):
            continue
        params, details = json.loads(row["params"]), json.loads(row["details"])
        name = row["name"].casefold()
        term = params.get("Срок действия", [])
        if not row["source"].startswith("https://") or term != [f"{b.months or 12} мес."]:
            continue
        if b.buyer not in params.get("Пользователь", []) or b.organization not in params.get("Тип организации", []):
            continue
        if b.delivery and not any(b.delivery in v.casefold() for v in params.get("Способ доставки", [])):
            continue
        required_kind = "Базовая" if family == FILES else "Полная версия"
        if b.purchase != "Полная версия" or required_kind not in params.get("Тип лицензии", []):
            continue
        platform = details.get("description", "").casefold()
        if family == POSTFIX:
            if (
                "базовая лицензия" not in name
                or "по количеству почтовых адресов" not in name
                or "linux" not in platform
            ):
                continue
        elif "windows" not in platform:
            continue
        if family == FILES and ("антивирус" not in name or b.file_system.casefold() not in platform):
            continue
        quantity = count
        capacities = [int(v) for v in params.get("К-во пользователей до", []) if v.isdecimal()]
        capacity = 0
        if family.unit == "упаковка":
            if len(capacities) != 1 or capacities[0] < count:
                continue
            capacity = capacities[0]
            lower = params.get("К-во пользователей от", [])
            if any(not v.isdecimal() or int(v) > count for v in lower):
                continue
            quantity = 1
        elif family == FILES and (capacities != [1] or not re.search(r"на 1 устройство\b", name)):
            continue
        notes = details.get("sales_notes", "")
        if (family == POSTFIX and not notes) or not allowed_quantity(notes, quantity):
            continue
        p = Product.from_catalog(row["id"], row["name"], row["source"], True, row["price"], term[0], row["name"])
        if p.unitPriceKopecks:
            line = Line(family, p, quantity, p.unitPriceKopecks)
            candidates.append((capacity, line.total, p.offerId, line))
    return min(candidates, key=lambda candidate: candidate[:3])[-1] if candidates else None


def recommend(index: Path, b: Brief, choice: str = "") -> Result:
    missing = questions(b)
    if missing:
        return result(" ".join(missing))
    if b.cloud_only or not index.is_file() or (b.goal != "discovery" and b.perpetual):
        return result(OFFER_HELP)
    if b.goal != "discovery" and b.purchase != "Полная версия":
        return result("Для продления укажите артикул текущей лицензии и действует ли подписка.")
    options = []
    gaps = []
    partial = []
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if b.goal == "discovery":
            return result(OFFER_HELP) if b.russian else compare_catalog(db, b, choice, money)
        if b.goal == "mailboxes":
            if b.mail_system == "Windows" and not b.russian:
                line = find_line(db, MAILBOX, b, b.people)
                if line:
                    options.append([line])
        elif b.goal == "files":
            line = find_line(db, FILES, b, b.servers)
            if line:
                options.append([line])
        else:
            mail = []
            if b.gateway and not b.russian and b.mail_system != "облако":
                mail.append(GATEWAY)
            if b.mail_system == "Postfix":
                mail.append(POSTFIX)
            file_line = None
            if b.files:
                file_line = find_line(db, FILES, b, b.servers)
                if file_line:
                    partial.append(file_line)
                else:
                    gaps.append(
                        "Для указанной файловой ОС, количества серверов и срока не подтверждена файловая лицензия."
                    )
            for family in mail:
                line = find_line(db, family, b, b.addresses)
                if not line:
                    gaps.append(f"Для {family.name} в каталоге нет подтверждённой применимой цены на это количество.")
                elif not b.files or file_line:
                    options.append([line, file_line] if b.files else [line])
                else:
                    partial.append(line)
            if not b.gateway and not b.russian:
                gaps.append("Для отдельного шлюза нужна Windows-машина.")
    if not options:
        return result(
            OFFER_HELP,
            sources=[
                Source(
                    url=FILES.source if b.files else MAILBOX.source if b.goal == "mailboxes" else GATEWAY.source,
                    label=" ".join(gaps) or "Для указанных условий подходящая поставка по каталогу не подтверждена.",
                ),
                *(
                    Source(url=line.product.source.url, label="Подтверждена только часть подбора: " + line.display())
                    for line in partial
                ),
            ],
        )
    if choice:
        options = [lines for lines in options if lines[0].family.name == choice]
        if not options:
            return result(OFFER_HELP)
    totals = [sum(line.total for line in lines) for lines in options]
    if b.budget_kopecks is not None:
        within = [lines for lines, total in zip(options, totals, strict=True) if total <= b.budget_kopecks]
        if not within:
            return result(
                f"Подтверждённые составы превышают бюджет {money(b.budget_kopecks)}. "
                f"Самый доступный стоит {money(min(totals))}. Какой предел можем рассмотреть?",
                sources=[line.product.source for line in options[totals.index(min(totals))]],
            )
        options = within
    products = {}
    sources = {}
    texts = []
    for i, lines in enumerate(options, 1):
        parts = []
        for line in lines:
            p, family = line.product, line.family
            products[p.offerId] = p
            sources[p.source.url] = p.source
            sources[family.source] = Source(url=family.source, label=family.function)
            if family == FILES:
                sources[DRWEB_UNITS.url] = DRWEB_UNITS
            parts.append(f"{family.function}. {line.display()}.")
        total = sum(line.total for line in lines)
        texts.append(f"Вариант {i}. " + " ".join(parts) + f" Итого за {b.months or 12} мес. — {money(total)}.")
    if any(lines[0].family == POSTFIX for lines in options):
        texts.append(
            "Для поставки Kaspersky уточните дистрибутив, версию Linux и версию почтового продукта. "
            "Руководство описывает интеграцию KLMS 8.2 с Postfix."
        )
        if b.russian:
            sources[REGISTRY.url] = REGISTRY
            texts.append(
                "У вендора есть перечень российского ПО. "
                "Реестровая запись для конкретной поставки требует отдельной проверки."
            )
    if b.goal == "mailboxes":
        texts.append(
            f"Выбрана упаковка для {b.people} пользователей. Способ поставки и ограничения указаны в карточке."
        )
    answer = result("\n\n".join(texts + gaps), "answer", products.values(), sources=sources.values())
    answer.evidence.data = [lines[0].family.name for lines in options]
    return answer
