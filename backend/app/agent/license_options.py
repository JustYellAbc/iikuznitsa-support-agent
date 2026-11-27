"""Resolve customer-named license variants against the catalog before choosing an SKU."""

import re
import sqlite3

from app.agent.catalog_selection import MDAEMON, SEATS, Selection, named_request, select
from app.agent.needs_catalog import money
from app.agent.quote_calculation import Item, catalog_lines
from app.core.errors import ApiError
from app.core.llm import LLMError

CAPACITY_LIST = re.compile(r"\b(\d+(?:\s*(?:,|и|или)\s*\d+)+)\s+(?:мест|пользовател)", re.I)
DURATION = re.compile(r"\b\d+\s*(?:мес(?:яц\w*)?|год\w*|лет)\b", re.I)
# Only resume automatically when the entire reply supplies purchase fields.
# Mixed requests (new product, exclusions, documents, etc.) go through the model.
FIELD_REPLY = re.compile(
    r"(?:это|мы|на|для|и|или|покупа(?:ет|ем)|бер[её]м|впервые|первая|новая|покупка|"
    r"коммерческ(?:ое|ая|ой)|юрлицо|компани[яию]|организаци[яию]|физлицо|"
    r"электронная|коробочная|поставка|месяц(?:а|ев)?|год|лет|"
    r"покажите|сравнения|вариант(?:ы|а)?|пользовател(?:я|ей|ь)|мест(?:о|а)?|\d{1,6})",
    re.I,
)


def followup_capacities(text):
    """None delegates interpretation; [None] inherits capacity from the conversation."""
    words = re.findall(r"\w+|[^\w\s.,!]", text)
    if not words or not all(FIELD_REPLY.fullmatch(word) for word in words):
        return None
    if Selection("").qualifiers(text, followup=True) == Selection(""):
        return None
    if re.search(r"\bили\b", CAPACITY_LIST.sub("", text), re.I):
        return None
    if len(DURATION.findall(text)) > 1:
        return None
    counts = [int(n) for group in CAPACITY_LIST.finditer(text) for n in re.findall(r"\d+", group[1])]
    counts.extend(int(n) for n in SEATS.findall(text))
    if set(re.findall(r"\d+", text)) - set(re.findall(r"\d+(?=\s*(?:месяц|год|лет))", text)) - {str(n) for n in counts}:
        return None
    counts = list(dict.fromkeys(counts))
    return (counts or [None]) if len(counts) <= 5 and all(0 < n <= 1000000 for n in counts) else None


class SelectionInputError(ValueError):
    """A tool argument that cannot be verified against the customer's request."""

    def __init__(self, code, correction, question):
        super().__init__(correction)
        self.code = code
        self.question = question


def name_pattern(name):
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)


def scoped_turns(name, variants, turns):
    """Preserve shared basket requirements; keep line-specific edits with their named line."""
    names = sorted({v.name.casefold() for v in variants}, key=len, reverse=True)
    labels = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)", re.I)
    scope = []
    active = set()
    for turn in turns:
        matches = list(labels.finditer(turn))
        if not matches:
            shared = re.search(r"об(?:е|а|оих)|все(?:х)?|(?:этот|тот) же состав", turn, re.I)
            if name.casefold() in active or shared and scope:
                scope.append(turn)
            continue
        active = {m[0].casefold() for m in matches}
        if name.casefold() not in active:
            continue
        if len(active) == 1 or all(
            re.fullmatch(r"[\s,]*(?:и|или|плюс|\+)?[\s,]*", turn[left.end() : right.start()], re.I)
            for left, right in zip(matches, matches[1:], strict=False)
        ):
            scope.append(turn)
            continue
        clauses = re.split(r"\s+(?:и|а)\s+|;", turn, flags=re.I)
        for clause in clauses:
            if name_pattern(name).search(clause):
                if len(set(m[0].casefold() for m in labels.finditer(clause))) > 1:
                    raise SelectionInputError(
                        "line_capacity",
                        "Уточни число мест и условия каждой позиции.",
                        "Какие условия нужны для каждой позиции?",
                    )
                scope.append(clause)
    return scope


def attribute_values(pattern, text, question):
    matches = list(pattern.finditer(text))
    for m in matches:
        left = re.split(r"[,.;!?]", text[: m.start()])[-1]
        right = text[m.end() :]
        if re.search(r"\b(?:не|без|кроме|исключ\w*)(?:\s+\w+){0,3}\s*$", left, re.I) or re.match(
            r"^\s*[-—:]?\s*не\s+(?:нуж\w*|подход\w*|предлаг\w*|допуск\w*|интерес\w*)", right, re.I
        ):
            raise SelectionInputError(
                "excluded_attribute", "Покупатель исключил это условие. Уточни допустимое.", question
            )
    return [m[0].casefold() for m in matches]


def license_selection(name, seats, turns):
    """Only bind a label and seat count grounded in the buyer's messages."""
    pattern = name_pattern(name)
    anchor = next((i for i in reversed(range(len(turns))) if pattern.search(turns[i])), None)
    if anchor is None:
        raise SelectionInputError(
            "name",
            "Передай название дословно из сообщений покупателя.",
            "Как точно называется нужный продукт или какая задача у него должна быть?",
        )
    scope = turns
    for m in pattern.finditer(turns[anchor]):
        before, after = turns[anchor][: m.start()], turns[anchor][m.end() :]
        if re.search(
            r"(?:\bне(?:\s+(?:нуж\w*|предлаг\w*|интерес\w*|добавля\w*|включа\w*))?|кроме|без|исключ\w*)\s*$",
            before,
            re.I,
        ) or re.match(r"\s*[,—:-]?\s*не\s+(?:нуж\w*|предлаг\w*|интерес\w*|добавля\w*|включа\w*)", after, re.I):
            raise SelectionInputError(
                "excluded", "Покупатель исключил эту позицию.", "Какой продукт подобрать вместо исключённого?"
            )

    selection = named_request(name) if MDAEMON.search(name) else Selection(name)
    for turn in scope:
        selection = selection.qualifiers(turn, followup=True)
        technical_text = name_pattern(name).sub("", turn)
        if len(DURATION.findall(technical_text)) > 1 or re.search(
            r"\b\d+(?:\s*(?:,|и|или)\s*\d+)+\s*(?:месяц|год|лет)", technical_text, re.I
        ):
            raise SelectionInputError(
                "terms",
                "Запрошено несколько сроков. Уточни срок каждой позиции.",
                "На какой срок проверить лицензию сначала?",
            )
        platform = re.compile(r"\b(?:linux|windows(?:\s+(?:server\s+)?\d+(?:\s+r2)?)?|macos|android|ios)\b", re.I)
        delivery = re.compile(r"\b(?:электронн|коробочн)\w*\s+(?:поставк|доставк)\w*|\bкоробк\w*", re.I)
        systems = attribute_values(platform, technical_text, "Какая операционная система вам подходит?")
        formats = attribute_values(delivery, technical_text, "Какой способ поставки вам подходит?")
        if systems:
            selection.platforms = tuple(systems)
        if formats:
            types = {"электрон" if fmt.startswith("электрон") else "короб" for fmt in formats}
            if len(types) > 1:
                raise SelectionInputError("delivery", "Уточни способ поставки.", "Какой способ поставки нужен?")
            selection.delivery = types.pop()
    if seats is not None:
        counts = {int(n) for n in SEATS.findall(scope[-1])}
        for group in CAPACITY_LIST.finditer(scope[-1]):
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
    selections = [
        license_selection(item.name, item.seats, scoped_turns(item.name, variants, turns)) for item in variants
    ]
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


def alternate_proposals(turns, variants=()):
    names = [v.name.casefold() for v in variants]
    if len(names) != len(set(names)):
        return True
    document = r"(?:кп|коммерческ\w*\s+предложен\w*)"
    plural = r"(?:[2-9]|[1-9]\d+|два|две|три|четыре|пять|несколько|отдельн\w*)"
    separate = rf"{plural}\s+(?:отдельн\w*\s+)?{document}|{document}\s+(?:на|для|по)\s+кажд\w*"
    return any(
        re.search(rf"\b(?:{separate}|или|сравн\w*|вариант\w*|альтернатив\w*|по отдельности|раздельн\w*)\b", turn, re.I)
        for turn in turns
    )


def quote_preview(settings, hits, turns, variants=()):
    """Price a provisional basket at one sale unit per line using T16's actual document policy."""
    if any(len(hit.products) != 1 for hit in hits) or alternate_proposals(turns, variants):
        return None
    if len(hits) > 1 and len({hit.products[0].name.split(" (")[0].casefold() for hit in hits}) != len(hits):
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
