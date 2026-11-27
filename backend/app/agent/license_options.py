"""Resolve customer-named license variants against the catalog before choosing an SKU."""

import re
import sqlite3

from app.agent.catalog_selection import MDAEMON, SEATS, Selection, named_request, select
from app.agent.needs_catalog import money
from app.agent.quote_calculation import Item, catalog_lines
from app.core.errors import ApiError
from app.core.llm import LLMError


class SelectionInputError(ValueError):
    """A tool argument that cannot be verified against the customer's request."""

    def __init__(self, code, correction, question):
        super().__init__(correction)
        self.code = code
        self.question = question


def license_selection(name, seats, turns):
    """Only bind a label and seat count grounded in the buyer's messages."""
    pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
    anchor = next((i for i in reversed(range(len(turns))) if pattern.search(turns[i])), None)
    if anchor is None:
        raise SelectionInputError(
            "name",
            "Передай название дословно из сообщений покупателя.",
            "Как точно называется нужный продукт или какая задача у него должна быть?",
        )
    scope = turns[anchor:]
    selection = named_request(name) if MDAEMON.search(name) else Selection(name)
    for turn in scope:
        selection = selection.qualifiers(turn, followup=True)
    if seats is not None:
        counts = {int(n) for n in SEATS.findall(scope[-1])}
        for group in re.finditer(r"\b(\d+(?:\s*(?:,|и|или)\s*\d+)+)\s+(?:мест|пользовател)", scope[-1], re.I):
            counts.update(int(n) for n in re.findall(r"\d+", group[1]))
        if seats != selection.seats and seats not in counts:
            raise SelectionInputError(
                "capacity",
                "Вместимость должна быть указана покупателем для этой позиции.",
                "На сколько пользователей нужна лицензия для выбранного продукта?",
            )
        selection.seats = seats
    return selection


def resolve_options(index, variants, turns):
    selections = [license_selection(item.name, item.seats, turns) for item in variants]
    # Different products may have different quantities. Only attribute a count to a
    # product when that count is attached to its name in the same clause.
    names = {item.name.casefold() for item in variants}
    if len(names) > 1:
        for turn in turns:
            terms = [Selection("").qualifiers(part, followup=False) for part in re.split(r"[;,]|\s+и\s+", turn)]
            if any(len({getattr(t, key) for t in terms if getattr(t, key)}) > 1 for key in ("kind", "buyer", "months")):
                raise SelectionInputError(
                    "terms",
                    "Разные условия покупки: уточни или подбери каждую позицию отдельно.",
                    "Какие позиции покупаете впервые, а какие продлеваете?",
                )
    if len(names) > 1 and any(len(set(SEATS.findall(turn))) > 1 for turn in turns):
        for item, selection in zip(variants, selections, strict=True):
            clauses = (part for turn in turns for part in re.split(r"[;,]|\s+и\s+", turn, flags=re.I))
            counts = {
                int(n) for part in clauses if item.name.casefold() in part.casefold() for n in SEATS.findall(part)
            }
            if len(counts) != 1 or (item.seats is not None and item.seats not in counts):
                raise SelectionInputError(
                    "line_capacity",
                    "Уточни отдельно число мест для каждой позиции.",
                    "Сколько пользователей требуется для каждой позиции комплекта?",
                )
            selection.seats = counts.pop()
    if len({(s.name.casefold(), s.seats) for s in selections}) != len(selections):
        raise SelectionInputError(
            "duplicate",
            "Повтор позиции с теми же условиями.",
            "Какие разные позиции или варианты вместимости нужно сравнить?",
        )
    try:
        hits = [select(index, s) for s in selections]
    except sqlite3.Error as exc:
        raise LLMError("catalog_unavailable") from exc
    if any(hit.evidence.error == "catalog_index_missing" for hit in hits):
        raise LLMError("catalog_unavailable")
    return hits


def quote_preview(settings, hits, turns):
    """Price a provisional basket at one sale unit per line using T16's actual document policy."""
    if any(len(hit.products) != 1 for hit in hits) or re.search(r"\b(?:или|сравн\w*)\b", turns[-1], re.I):
        return None
    items = [Item(offerId=hit.products[0].offerId, packages=1) for hit in hits]
    if len({item.offerId for item in items}) != len(items):
        return None
    try:
        details = catalog_lines(settings.catalog_index_path, items, settings.data_dir / "config/quote_terms.json")
    except ApiError:
        return None
    if any(
        line.unit is None
        or line.reason
        or line.unit != hit.products[0].unitPriceKopecks
        or line.product.source.url != hit.products[0].source.url
        for line, hit in zip(details, hits, strict=True)
    ):
        return None

    lines = [f"{line.product.name} (арт. {line.product.offerId}) — {money(line.unit)}." for line in details]
    lines.append(
        f"При покупке по одной единице каждой позиции сумма по каталогу — {money(sum(d.unit for d in details))}."
    )
    return "\n".join(lines)
