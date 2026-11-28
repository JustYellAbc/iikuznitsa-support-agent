"""Resolve customer-named license variants against the catalog before choosing an SKU."""

import re
import sqlite3
from contextlib import closing

from app.agent.catalog_selection import COUNT_WORDS, MDAEMON, SEATS, USER_UNIT, WORD, Selection, named_request, select
from app.agent.handoff import discovery_context
from app.agent.needs_catalog import money
from app.agent.quote_calculation import Item, catalog_lines
from app.core.errors import ApiError
from app.core.llm import LLMError

CAPACITY_LIST = re.compile(rf"\b(\d+(?:\s*(?:,|и|или)\s*\d+)+)\s+(?:мест|{USER_UNIT})", re.I)
DURATION = re.compile(r"\b\d+\s*(?:мес(?:яц\w*)?|год\w*|лет)\b", re.I)
# Keys, licenses or pieces count packages, not the capacity certified for the card.
PACKAGES = re.compile(
    rf"\b(\d{{1,6}}|один|одн(?:а|у|ой|ого))\s+{COUNT_WORDS}(?:ключ\w*|лицензи\w*|шт\b|штук\w*|экземпляр\w*)", re.I
)
# Only resume automatically when the entire reply supplies purchase fields.
# Mixed requests (new product, exclusions, documents, etc.) go through the model.
FIELD_REPLY = re.compile(
    r"(?:это|мы|на|для|и|или|покупа(?:ет|ем)|бер[её]м|впервые|первая|новая|покупка|"
    r"коммерческ(?:ое|ая|ой)|юрлицо|компани[яию]|организаци[яию]|физлицо|"
    r"электронная|коробочная|поставка|месяц(?:а|ев)?|год|лет|"
    rf"покажите|сравнения|вариант(?:ы|а)?|{USER_UNIT}|мест(?:о|а)?|\d{{1,6}})",
    re.I,
)


# Only an unambiguous change to a single line may bypass tool choice.
RESIZE_UNIT = re.compile(rf"\b([1-9]\d{{0,5}})\s+(?:{USER_UNIT}|мест(?:о|а)?)\b", re.I)
RESIZE_WORDS = set(
    "а и на для теперь тогда если нужно надо сколько будет стоить стоит как какая какова "
    "цена стоимость какой срок получить оформить запросить кп коммерческое предложение".split()
)
ADDED_KEY = re.compile(
    r"(?:а\s+)?ещ[её]\s+нужен\s+(?:один|1)\s+ключ\s+((?a:[a-z][a-z0-9-]*(?: +[a-z0-9][a-z0-9-]*)*))"
    r"(?: +для +ИП)?[.!]?",
    re.I,
)
SPOKEN_SEATS = (
    dict.fromkeys(("один", "одна", "одного", "одну"), 1)
    | {word: n for n, word in enumerate("две три четыре пять шесть семь восемь девять десять".split(), 2)}
    | {"два": 2}
)


def resized_seats(text):
    matches = list(RESIZE_UNIT.finditer(text))
    if len(matches) != 1:
        return None
    rest = text[: matches[0].start()] + " " + text[matches[0].end() :]
    words = re.findall(r"\w+|[^\w\s.,!?]", rest.casefold())
    return int(matches[0][1]) if all(w in RESIZE_WORDS for w in words) else None


def shown_family_name(history, card):
    family = card.name.partition(" (")[0].casefold()
    for message in reversed(history[-16:]):
        if message.role == "client":
            product = named_request(message.text)
            if (
                product
                and key_words(product.name)
                and re.search(r"(?<!\w)" + re.escape(product.name) + "$", family, re.I)
            ):
                return product.name
    return None


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


# Numbers, editions, licensing terms and company suffixes do not identify a product family.
GENERIC_WORD = re.compile(
    r"для|на|с|и|в|по|for|the|and|of|with|год\w*|лет|мес\w*|пользовател\w*|users?|лиценз\w*|licen[cs]\w*|"
    r"ключ\w*|подписк\w*|обновлен\w*|продлен\w*|электронн\w*|pro|professional|standard|edition|home|business|"
    r"enterprise|plus|basic|premium|ultimate|server|suite|version|верси\w*|ltd|inc|llc|gmbh|corp\w*|technologies"
)


def key_words(name):
    return {
        w for w in WORD.findall(name.casefold()) if len(w) > 1 and not w.isdigit() and not GENERIC_WORD.fullmatch(w)
    }


def shown_label(name, turn, previous, names):
    """The buyer's word for a shown card: a distinctive word the variant name shares with that card."""
    cards = [p.name for p in previous if name_pattern(name).search(p.name)]
    words = key_words(name) & set().union(*map(key_words, cards))
    words -= set().union(*(key_words(other) for other in names if other != name))
    found = (name_pattern(word).search(turn) for word in sorted(words, key=len, reverse=True))
    return next((m[0] for m in found if m), None)


def vendor_trimmed(index, name, turns):
    """The buyer's wording when the model prefixed it with the vendor of a catalog card containing the name."""
    rests = [name[m.start() :] for m in list(re.finditer(r"\S+", name))[1:]]
    rests = [r for r in rests if key_words(r) and any(name_pattern(r).search(t) for t in turns)]
    if not rests or not index.is_file():
        return None
    expr = " AND ".join(f'name:"{w}"' for w in WORD.findall(name.casefold()))
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        cards = db.execute(
            "SELECT o.name, o.vendor FROM products JOIN offers o ON o.rowid=products.rowid "
            "WHERE products MATCH ? LIMIT 50",
            (expr,),
        ).fetchall()
    for rest in rests:
        prefix = key_words(name[: -len(rest)])
        if any(name_pattern(name).search(card) and prefix <= key_words(vendor or "") for card, vendor in cards):
            return rest
    return None


def grounded_label(index, name, turns, previous, names):
    """Return the buyer's wording that grounds a variant and the name to look up in the catalog."""
    if any(name_pattern(name).search(t) for t in turns):
        return name, name
    rest = vendor_trimmed(index, name, turns)
    if rest:
        return rest, rest
    label = next(filter(None, (shown_label(name, t, previous, names) for t in reversed(turns))), None)
    return (label, name) if label else (name, name)


def license_dialog_context(history, text):
    """Resolve a short numeric answer against the unit in the question that elicited it."""
    turns = discovery_context(history, text, max_turns=8)
    exchanges = history[len(history) - 2 * (len(turns) - 1) :]
    for i, question in enumerate(exchanges[1::2], 1):
        count = re.fullmatch(r"(?:на\s+)?([1-9]\d{0,5}|[а-яё]+)[.!]?", turns[i].strip().casefold())
        if count and re.search(r"(?:на\s+)?сколько\s+(?:пользовател|рабочих мест|мест\b)", question.text, re.I):
            if question.text.count("?") == 1 and " или " not in question.text.casefold():
                value = count[1] if count[1].isdigit() else SPOKEN_SEATS.get(count[1])
                if value:
                    line = re.search(r"нужна лицензия «([^»]+)»\?", question.text)
                    if line and not any(name_pattern(line[1]).search(t) for t in turns[:i]):
                        continue
                    turns[i] = (f"{line[1]} — " if line else "") + f"{value} пользователей"
    return turns


def kind_before_name(history, turns, variants):
    """Carry a buyer's operation into their answer to a request to identify that license."""
    if len(variants) != 1:
        return ""
    label = name_pattern(variants[0].name)
    exchanges = history[len(history) - 2 * (len(turns) - 1) :]
    for i, agent in enumerate(exchanges[1::2], 1):
        if not label.search(turns[i]):
            continue
        question = agent.text.casefold()
        if "назван" in question or "артикул" in question:
            kind = Selection("").qualifiers(turns[i - 1], followup=True).kind
            if kind and Selection("").qualifiers(question, followup=True).kind == kind:
                return kind
        break
    return ""


def shown_product_turns(history, text, variants, previous):
    """A change of terms after a shown card may omit the product name the buyer used earlier."""
    names = [v.name for v in variants]
    shown = [
        n for n in names if not name_pattern(n).search(text) and any(name_pattern(n).search(p.name) for p in previous)
    ]
    if not shown:
        return [text]

    def mentions(turn):
        return any(name_pattern(n).search(turn) or shown_label(n, turn, previous, names) for n in shown)

    earlier = [m.text for m in history if m.role == "client"][-8:]
    start = next((i for i in reversed(range(len(earlier))) if mentions(earlier[i])), None)
    return [text] if start is None else [*earlier[start:], text]


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


def license_selection(name, seats, turns, *, lookup=None):
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

    product = lookup or name
    selection = named_request(product) if MDAEMON.search(product) else Selection(product)
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
            packages = {int(n) if n.isdigit() else 1 for n in PACKAGES.findall(" ".join(scope))}
            if not counts and seats in packages:
                return selection
            raise SelectionInputError(
                "capacity",
                "Вместимость должна быть указана покупателем для этой позиции. Если он назвал число покупаемых "
                "лицензий, упаковок или сотрудников компании, оставь seats пустым и сохрани их количество в контексте.",
                "На сколько пользователей нужна лицензия для выбранного продукта?",
            )
        selection.seats = seats
    return selection


def resolve_options(index, variants, turns, *, prior_kind="", previous=()):
    names = [v.name for v in variants]
    try:
        grounded = [grounded_label(index, v.name, turns, previous, names) for v in variants]
    except sqlite3.Error as exc:
        raise LLMError("catalog_unavailable") from exc
    labeled = [v.model_copy(update={"name": label}) for v, (label, _) in zip(variants, grounded, strict=True)]
    selections = []
    for item, (_, lookup) in zip(labeled, grounded, strict=True):
        try:
            scope = scoped_turns(item.name, labeled, turns)
            selections.append(license_selection(item.name, item.seats, scope, lookup=lookup))
        except SelectionInputError as exc:
            if exc.code == "capacity" and len(labeled) > 1:
                exc.question = f"На сколько пользователей нужна лицензия «{item.name}»?"
            raise
    if prior_kind and len(selections) == 1 and not selections[0].kind:
        selections[0].kind = prior_kind
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
