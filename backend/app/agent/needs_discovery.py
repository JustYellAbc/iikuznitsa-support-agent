"""Compare documented catalogue candidates when licensing units still need qualification."""

import json
import re
import sqlite3

from app.agent.catalog_selection import result
from app.agent.handoff import OFFER_HELP
from app.contracts.t04 import Product


def compare_catalog(db: sqlite3.Connection, brief, choice, money):
    query, purpose = {
        "pdf": ('name:"PDF Editor"', "редактирования PDF"),
        "backup": ("category:резервн* AND category:копирован*", "резервного копирования"),
    }[brief.task]
    # Only claim the category and platform. Licensing units and full project cost may differ by vendor.
    rows = db.execute(
        """SELECT o.name,o.id,o.vendor,o.params,o.source,o.price,o.details FROM products JOIN offers o
           ON o.rowid=products.rowid WHERE products MATCH ? AND o.available=1
           AND o.currency IN ('RUB','RUR') AND o.price_state='priced_available'
           ORDER BY CAST(o.price AS REAL),o.id""",
        (query,),
    )
    selected = []
    vendors = set()
    for row in rows:
        params, details = json.loads(row["params"]), json.loads(row["details"])
        if not row["source"].startswith("https://") or row["vendor"] in vendors or (choice and choice != row["id"]):
            continue
        if brief.platform.casefold() not in details.get("description", "").casefold():
            continue
        if brief.buyer and brief.buyer not in params.get("Пользователь", []):
            continue
        if brief.organization and brief.organization not in params.get("Тип организации", []):
            continue
        terms = params.get("Срок действия", [])
        if brief.months is not None and terms != [f"{brief.months} мес."]:
            continue
        if brief.perpetual and not any("бессроч" in t.casefold() for t in terms):
            continue
        if brief.delivery and not any(brief.delivery in v.casefold() for v in params.get("Способ доставки", [])):
            continue
        if (brief.purchase or "Полная версия") not in params.get("Тип лицензии", []):
            continue
        if re.search(r"продлен|обновлени|cross.grade|поддержк", row["name"], re.I):
            continue
        p = Product.from_catalog(
            row["id"],
            row["name"],
            row["source"],
            True,
            row["price"],
            terms[0] if len(terms) == 1 else None,
            row["name"],
        )
        if not p.unitPriceKopecks or (brief.budget_kopecks is not None and p.unitPriceKopecks > brief.budget_kopecks):
            continue
        selected.append((p, details.get("sales_notes", "")))
        vendors.add(row["vendor"])
        if len(selected) == 2:
            break
    if not selected:
        return result(OFFER_HELP)
    items = [f"По каталогу для {purpose} на {brief.platform} подходят для рассмотрения:"]
    for i, (p, notes) in enumerate(selected, 1):
        items.append(
            f"{i}. {p.name} (арт. {p.offerId}). Цена за единицу по карточке — {money(p.unitPriceKopecks)}. "
            + (f"Срок — {p.licenseTerm}. " if p.licenseTerm else "Срок требует уточнения. ")
            + (f"Условия количества: {notes}." if notes else "")
        )
    items.append("Для итоговой стоимости нужно проверить единицу лицензирования и количество по выбранной позиции.")
    products = [p for p, _ in selected]
    response = result("\n\n".join(items), "answer", products)
    response.evidence.data = [p.offerId for p in products]
    return response
