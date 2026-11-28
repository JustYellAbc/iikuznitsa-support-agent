"""Named-product selection against the private SQLite index built by T06."""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from difflib import get_close_matches
from pathlib import Path

from app.agent.handoff import AFFIRMATIVE, OFFER_HELP, OPERATOR_QUESTION
from app.contracts.schemas import ToolResult
from app.contracts.t04 import HandoffReason, Product, Source
from app.services.price_policy import OfferAssessment, assess, is_supported_currency, snapshot_from_row

WORD = re.compile(r"[^\W_]+")
CANDIDATE_QUESTION = "Вам подходит именно этот вариант?"
MDAEMON = re.compile(r"\b(?:mda?e?mo?n|mdeamon|мд[еэ]й?мон|мдаймон|эмдеймон)\b", re.I)
# A pronoun or adjective may stand between a count and its unit: "5 таких ключей", "10 дополнительных мест".
COUNT_WORDS = r"(?:(?:так\w*|эт(?:их|от|у|а|и)|тех|же|нов\w*|дополнительн\w*|электронн\w*)\s+){0,2}"
USER_UNIT = r"(?:пользовател(?:ь|я|ей|ям|ях|ем|ями|и|ю)|пользоваетелй)\b"
SEATS = re.compile(
    rf"\b(\d{{1,9}})\s*[- ]?\s*{COUNT_WORDS}"
    rf"(?:{USER_UNIT}|пользоват|человек|сотрудник|рабочи[хе] мест|мест|users?|seats?)",
    re.I,
)
CODE = re.compile(r"\b(?:артикул(?:а|у|е|ом)?\b|offerid|sku|код(?:а|у|е|ом)? товара\b|id)\s*[:#№-]?\s*([\w.-]+)", re.I)
NAMED = re.compile(r"\b(?:ищу|найти|нуж(?:ен|на|но)|подберите|купить|купим|интересует|сколько стоит)\s+", re.I)
LATIN = re.compile(r"\b[a-z][a-z0-9.+-]*(?:\s+[a-z][a-z0-9.+-]*)*", re.I | re.ASCII)
STOP_NAME = re.compile(
    r"[,;!?]|\.\s|\s+(?:на|для|в редакции|по цене|лицензи\w*|купить|сколько|покупа\w*|продл\w*)\b", re.I
)
# A choice of terms describes the customer's question, not a selected catalog filter.
TERM_OPTION = r"(?:на\s+)?(?:год|\d{1,3}\s*(?:мес(?:яц(?:а|ев|ы)?)?\.?|года?|лет)|бессроч\w*|навсегда)"
TERM_ALTERNATIVES = re.compile(rf"\b{TERM_OPTION}\s+или\s+{TERM_OPTION}\b")
TERM_QUESTION = re.compile(
    r"\b(?:на\s+какой\s+срок|сколько\s+(?:лет|месяцев))\s+(?:(?:она|он|лицензия|подписка)\s+)?действует\b"
)
BUYER = "Пользователь"
KIND = "Тип лицензии"
CAPACITY = "К-во пользователей до"
LIMIT = 200
TRANSACTION_ONLY = re.compile(
    r"(?:продлени[еяюия]|продлить|нов(?:ая|ое|ую|ой|ые)|перв(?:ая|ую|ичной|ичную|ичная)|покупк\w*|лицензи\w*|подписк\w*|"
    r"действующ\w*|действует|активн\w*|просроч\w*|ист[её]к\w*|уже|ещ[её]|не|на|для)"
)


def confirmed_candidate(history, text):
    """Recover the proposed SKU only from the last sourced assistant confirmation."""
    if not history or history[-1].role != "agent" or not history[-1].text.endswith(CANDIDATE_QUESTION):
        return None
    if not re.fullmatch(r"(?:да(?:[, ]+подходит)?|подходит|бер[её]м|верно|всё верно)[.! ]*", text.strip(), re.I):
        return None
    prior = history[-1]
    ids = re.findall(r"\(артикул ([^()\n]{1,100})\)\.", prior.text)
    return (ids[0], prior.sources[0]) if len(ids) == len(prior.sources) == 1 else None


@dataclass
class Selection:
    name: str
    code: bool = False
    standard: bool = False
    seats: int | None = None
    buyer: str = ""
    kind: str = ""
    expired: bool | None = None
    perpetual: bool = False
    months: int | None = None
    organization: str = ""
    edition: str = ""
    group_id: str | None = None
    platforms: tuple[str, ...] = ()
    delivery: str = ""

    def qualifiers(self, text: str, *, followup: bool) -> "Selection":
        updated = replace(self)
        s = text.casefold()
        edition = re.search(r"\b(?:в редакции|редакция|редакцию)\s+[«\"]?([\w.-]+)", s)
        if edition and not (updated.standard and edition[1] in {"standard", "стандарт"}):
            updated.edition = edition[1].strip(".")
        count = SEATS.search(s)
        if count:
            updated.seats = int(count[1])
        elif followup and re.fullmatch(r"\s*\d{1,9}[.! ]*", s):
            updated.seats = int(s.strip(" .!"))
        if re.search(r"физ\s*лиц|физическ\w* лиц|частн\w* лиц|для себя", s):
            updated.buyer = "Физлицо"
        elif re.search(r"юр[. ]*лиц|юридическ\w* лиц|организаци|компани|предприяти|\bооо\b", s):
            updated.buyer = "Юрлицо"
        for pattern, category in (
            (r"некоммерческ|\bнко\b", "Некоммерческая"),
            (r"учебн|академич|образовательн", "Академическая"),
            (r"государственн|госучрежден|госорганизац", "Государственная"),
            (r"\bкоммерческ\w*\b(?!\s+предложени)", "Коммерческая"),
        ):
            if re.search(pattern, s):
                updated.organization = category
                break
        conditions = TERM_QUESTION.sub("", TERM_ALTERNATIVES.sub("", s))
        duration = re.search(r"\b(\d{1,3})\s*(мес(?:яц(?:а|ев|ы)?)?\.?|года?|лет)\b", conditions)
        if duration:
            updated.months = int(duration[1]) * (1 if duration[2].startswith("мес") else 12)
        elif re.search(r"\bна год\b", conditions):
            updated.months = 12
        if re.search(r"впервые|в первый раз|нов(?:ая|ую) (?:покупк|лицензи)|полная версия|перв(?:ая|ую) покупк", s):
            updated.kind = "Полная версия"
        elif re.search(r"продл(?:ить|ев|ен|ён|ять)|(?<!не )\bпродл(?:им|ю|и|ите)\b|продле", s):
            updated.kind = "Продление"
        elif followup and re.fullmatch(r"(?:новая|новую|первичная|первый раз)[.! ]*", s):
            updated.kind = "Полная версия"
        if re.search(r"\bне\s+(?:действует|активна)|\bнедействующ", conditions):
            updated.expired = True
        elif re.search(
            r"не (?:истек|истёк|истекла|просроч)|ещ[её] действ|\bдействует\b|действующ|активная", conditions
        ):
            updated.expired = False
        elif re.search(r"просроч|истек|истёк|закончилась|истекла", conditions):
            updated.expired = True
        updated.perpetual |= bool(re.search(r"навсегда|бессроч|после окончани", s))
        return updated


def named_request(text: str) -> Selection | None:
    code = CODE.search(text)
    if code or re.fullmatch(r"\d{3,12}", text.strip()):
        return Selection(code[1].rstrip(".") if code else text.strip(), code=True)
    match = MDAEMON.search(text)
    if match:
        suffix = text[match.end() :].strip(" ,.-")
        standard = bool(re.search(r"\b(?:standard|стандарт)\b", text, re.I))
        name = "MDaemon Messaging Server"
        for pattern, product in (
            (r"security\s*gateway|почтов\w* шлюз", "Security Gateway"),
            (r"outlook|коннектор|connector", "Connector for Outlook"),
            (r"activesync", "ActiveSync"),
            (r"antivirus|антивирус", "Antivirus"),
        ):
            if re.search(pattern, text, re.I):
                name = "MDaemon " + product
                break
        else:
            # An explicitly different edition/version must survive lookup.
            suffix = re.sub(r"^(?:messaging server|standard|стандарт)\b", "", suffix, flags=re.I).lstrip()
            tail = re.match(r"([a-z][a-z0-9.-]*|v?\d+\.\d+)\b", suffix, re.I | re.ASCII)
            if tail:
                name += " " + tail[1]
        return Selection(name, standard=standard)
    quoted = re.search(r'[«"]([^»"]{2,100})[»"]', text)
    intent = NAMED.search(text)
    request_name = text[intent.end() :] if intent else ""
    request_name = re.sub(
        r"^(?:(?:продление|покупка|покупку)\s+)?(?:лицензи[яюи]\s+)?(?:на|для)\s+"
        r"|^(?:продление|покупка|покупку)\s+(?:лицензи[ияю]\s+)?(?=[A-Za-z«\"])",
        "",
        request_name,
        flags=re.I,
    )
    head = STOP_NAME.split(request_name, maxsplit=1)[0].strip(" .") if intent else text.strip(" .")
    latin = LATIN.search(head)
    if quoted:
        name = quoted[1]
    elif re.search(r"[а-яё]-?\d", head, re.I) and len(head) <= 80:
        name = head
    elif latin:
        name = latin[0].strip(" .")
    elif intent:
        name = head
    elif re.fullmatch(r"[а-яё][а-яё-]{3,}", head, re.I) and not re.fullmatch(
        r"здравствуйте|привет|спасибо|оператор|менеджер|повтор|впервые|конечно|нет|согласен|помогите", head, re.I
    ):
        name = text.strip()
    else:
        return None
    if (
        not name
        or len(name) > 100
        or name.casefold()
        in {
            "по",
            "помощь",
            "счёт",
            "счет",
            "лицензия",
            "что-то",
            "программа",
            "решение",
            "почтовый сервер",
            "корпоративная почта",
            "антивирус",
            "защита почты",
        }
    ):
        return None
    return Selection(name)


def selection_for(
    text: str, previous: Selection | None, suggestion: str = "", options=(), *, awaiting_seats: bool = False
) -> Selection | None:
    found = named_request(text)
    if previous:
        updated = previous.qualifiers(text, followup=True)
        ordinal = re.fullmatch(
            r"(?:бер[её]м\s+)?(перв|втор|трет)[а-яё]*(?:\s+(?:вариант|позицию))?[.! ]*", text.strip().casefold()
        )
        if ordinal and options:
            i = ("перв", "втор", "трет").index(ordinal[1])
            if i < len(options):
                return replace(updated, name=options[i], code=True, group_id=None)
        if found and found.code and found.name in options:
            # A numeric option identifies a SKU, even when a previous enquiry included a seat count.
            return replace(previous.qualifiers(text, followup=False), name=found.name, code=True, group_id=None)
        if found and found.code:
            if awaiting_seats and text.strip().isdecimal() and len(text.strip()) <= 9:
                return updated
            return found.qualifiers(text, followup=False)
        if found and not found.code and all(TRANSACTION_ONLY.fullmatch(w) for w in WORD.findall(found.name.casefold())):
            return updated
        if found and SEATS.match(found.name):
            return updated
        if re.fullmatch(r"v\d+(?:\.\d+)*", text.strip(), re.I):
            return replace(updated, edition=text.strip().casefold())
        if suggestion and AFFIRMATIVE.fullmatch(text.strip().casefold()):
            return replace(updated, name=suggestion)
        if found is None or found.name.casefold() == previous.name.casefold():
            if updated != previous or found or re.fullmatch(r"\s*\d{1,9}[.! ]*", text):
                if found:
                    updated.standard |= found.standard
                return updated
        # A standalone answer such as «Компания» can resemble a Russian product name.
        if Selection("").qualifiers(text, followup=True) != Selection("") and not (
            MDAEMON.search(text) or LATIN.search(text) or CODE.search(text) or NAMED.search(text)
        ):
            return updated
    return found.qualifiers(text, followup=False) if found else None


@dataclass
class Result:
    text: str
    purpose: str
    products: list[Product]
    sources: list[Source]
    evidence: ToolResult
    suggestion: str = ""
    followup: Selection | None = None
    handoff_reason: HandoffReason | None = None
    awaiting_fields: bool = False


def result(text: str, purpose: str = "clarification", products=(), *, sources=()) -> Result:
    products = list(products)
    refs = list(sources) or [p.source for p in products]
    evidence = ToolResult(ok=bool(refs), source_ref=refs[0].url if refs else None, data=[p.offerId for p in products])
    return Result(text, purpose, products, refs, evidence)


def _lookup(db: sqlite3.Connection, selection: Selection) -> tuple[list, str]:
    fields = (
        "o.id,o.group_id,o.name,o.source,o.available,o.currency,o.price,o.price_state,o.params,o.edition,"
        "json_extract(o.details,'$.sales_notes') AS sales_notes"
    )
    if selection.code or re.fullmatch(r"(?=\S*\d)[a-zA-Z0-9.-]+", selection.name):
        rows = db.execute(
            f"SELECT {fields} FROM offers o WHERE o.id=? OR o.vendor_code=? LIMIT ?",
            (selection.name, selection.name, LIMIT + 1),
        )
        exact = rows.fetchall()
        if exact or selection.code:
            return exact, ""
    if selection.group_id:
        return db.execute(
            f"SELECT {fields} FROM offers o WHERE o.group_id=? ORDER BY o.id LIMIT ?",
            (selection.group_id, LIMIT + 1),
        ).fetchall(), ""
    words = WORD.findall(selection.name.casefold())
    if not words or len(words) > 12:
        return [], ""

    def candidates(tokens):
        expr = " AND ".join(f'name:"{token}"*' for token in tokens)
        if selection.edition:
            expr += "".join(f' AND "{token}"*' for token in WORD.findall(selection.edition))
        return db.execute(
            f"SELECT {fields} FROM products JOIN offers o ON o.rowid=products.rowid "
            "WHERE products MATCH ? ORDER BY bm25(products),o.id LIMIT ?",
            (expr, LIMIT + 1),
        ).fetchall()

    rows = candidates(words)
    # Prefix search also matches longer words ("pro" in PROMT); whole-word hits take precedence.
    whole = [r for r in rows if set(words) <= set(WORD.findall(r["name"].casefold()))]
    if whole and len(rows) <= LIMIT:
        return whole, ""
    if rows or len(words) != 1 or len(words[0]) < 5:
        return rows, ""
    # Category labels are the feed's own vocabulary. A fuzzy hit is an alternative, never an exact SKU.
    vocabulary = {
        w
        for row in db.execute("SELECT name FROM categories")
        for w in WORD.findall(row[0].casefold())
        if abs(len(w) - len(words[0])) <= 2 and w[:1] == words[0][:1]
    }
    near = get_close_matches(words[0], sorted(vocabulary), n=1, cutoff=0.80)
    return (candidates(near), near[0]) if near else ([], "")


def offer_assessment(row) -> OfferAssessment:
    return assess(snapshot_from_row(row))


def offer_product(row) -> Product:
    return offer_assessment(row).to_product()


def renewal_type_conflict(name, types):
    return types and "Продление" not in types and re.search(r"\bпродление\b", name, re.I)


def renewal_review(sources):
    titles = "; ".join(f"«{s.label}»" for s in sources)
    return (
        f"Для продления нашёл {titles}. В названии указано продление, а в поле «Тип лицензии» — другой тип. "
        "Нужно проверить, подойдёт ли этот вариант для вашей лицензии.\n\n" + OPERATOR_QUESTION
    )


def _followup_from_sku(selection: Selection, row: dict) -> Selection:
    """A direct article identifies both today's offer and the feed family for a later change of terms."""
    params = row["params"]

    def single(key: str) -> str:
        values = {v for v in params.get(key, []) if v.casefold() not in {"не определено", "не указано"}}
        return values.pop() if len(values) == 1 else ""

    count = single(CAPACITY)
    duration = re.fullmatch(r"(\d+)\s*мес\.?", single("Срок действия"), re.I)
    kind = selection.kind or single(KIND)
    expired = selection.expired
    if expired is None and kind == "Продление":
        expired = "просроч" in row["name"].casefold()
    return replace(
        selection,
        name=row["name"].split(" (", 1)[0] if row["group_id"] else selection.name,
        code=not bool(row["group_id"]),
        group_id=row["group_id"],
        seats=selection.seats if selection.seats is not None else (int(count) if count.isdecimal() else None),
        buyer=selection.buyer or single(BUYER),
        kind=kind,
        expired=expired,
        organization=selection.organization or single("Тип организации"),
        months=selection.months if selection.months is not None else (int(duration[1]) if duration else None),
        edition=selection.edition or (row["edition"] or "").casefold(),
    )


def select(index: Path, selection: Selection) -> Result:
    if not index.is_file():
        missing = result(OFFER_HELP)
        missing.evidence.error = "catalog_index_missing"
        return missing
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        records, correction = _lookup(db, selection)
    rows = []
    for record in records:
        row = dict(record)
        if row["source"].startswith("https://") and is_supported_currency(row["currency"]):
            row["params"] = json.loads(row["params"])
            rows.append(row)
    if not rows:
        return result(OFFER_HELP)
    source = Source(url=rows[0]["source"], label=rows[0]["name"])
    if len(records) > LIMIT:
        return result(
            f"По названию «{selection.name}» много вариантов. Уточните название редакции или артикул из карточки.",
            sources=[source],
        )

    def values(key):
        return {
            v for r in rows for v in r["params"].get(key, []) if v.casefold() not in {"не определено", "не указано"}
        }

    # Known constraints are applied before deciding what still needs clarification.
    buyer_restriction = None
    if selection.buyer:
        buyers = values(BUYER)
        if buyers and selection.buyer not in buyers:
            buyer_restriction = Source(
                url=source.url, label="В карточках указан тип покупателя: " + ", ".join(sorted(buyers))
            )
        rows = [r for r in rows if selection.buyer in r["params"].get(BUYER, [])]
    if selection.organization:
        rows = [r for r in rows if selection.organization in r["params"].get("Тип организации", [])]
    if selection.platforms:
        rows = [
            r
            for r in rows
            if all(
                any(
                    re.search(r"(?<!\w)" + re.escape(platform) + r"(?!\w)", v, re.I)
                    for v in r["params"].get("Платформа", [])
                )
                for platform in selection.platforms
            )
        ]
    if selection.delivery:
        rows = [
            r for r in rows if any(selection.delivery in v.casefold() for v in r["params"].get("Способ доставки", []))
        ]
    if selection.edition:
        rows = [
            r
            for r in rows
            if selection.edition == (r["edition"] or "").casefold()
            or selection.edition in WORD.findall(((r["edition"] or "") + " " + r["name"]).casefold())
        ]
    if selection.months is not None:
        rows = [
            r
            for r in rows
            if any(re.fullmatch(rf"{selection.months}\s*мес\.?", v, re.I) for v in r["params"].get("Срок действия", []))
        ]
    if selection.seats is not None:
        sized = [
            (int(v), r)
            for r in rows
            for v in r["params"].get(CAPACITY, [])
            if v.isdecimal()
            and int(v) >= selection.seats > 0
            and all(
                lb.isdecimal() and int(lb) <= selection.seats for lb in r["params"].get("К-во пользователей от", [])
            )
        ]
        capacity = {}
        for bound, row in sized:
            key = row["id"]
            capacity[key] = min(bound, capacity.get(key, bound))
        rows = [row for row in rows if row["id"] in capacity]
    if selection.kind == "Продление" and selection.expired is not None:
        rows = [r for r in rows if ("просроч" in r["name"].casefold()) == selection.expired]
    if selection.kind:
        if selection.kind == "Продление" and not correction and selection.kind not in values(KIND):
            conflicts = [
                Source(url=r["source"], label=r["name"])
                for r in rows
                if renewal_type_conflict(r["name"], r["params"].get(KIND))
            ][:2]
            if conflicts:
                return result(renewal_review(conflicts), sources=conflicts)
        rows = [r for r in rows if selection.kind in r["params"].get(KIND, [])]
    if rows and selection.seats is not None:
        smallest = min(capacity[r["id"]] for r in rows)
        rows = [r for r in rows if capacity[r["id"]] == smallest]
    if not rows:
        return result(OFFER_HELP, sources=[buyer_restriction] if buyer_restriction else [])
    source = Source(url=rows[0]["source"], label=rows[0]["name"])
    if correction:
        alternative = result(
            f"Точного совпадения нет. Ближайшее название в каталоге — «{correction}». Это нужное семейство?",
            sources=[source],
        )
        alternative.suggestion = correction
        return alternative
    prefix = (
        f"По коду «{selection.name}» нашёл позицию в каталоге. "
        if selection.code
        else (f"По названию нашёл в каталоге семейство «{selection.name}». ")
    )
    if selection.standard:
        prefix += "Карточки относятся к семейству Messaging Server. Соответствие редакции Standard не подтверждено. "
        prefix += "Для проверки совместимости укажите версию установленной лицензии. "
    questions = []
    if not selection.code:
        if selection.seats is None and len(values(CAPACITY)) > 1:
            questions.append("На сколько пользователей нужна лицензия?")
        if not selection.buyer and values(BUYER) and (len(rows) > 1 or selection.name.startswith("MDaemon")):
            questions.append("На кого оформляется лицензия — на организацию или физлицо?")
        if not selection.kind and {"Полная версия", "Продление"} <= values(KIND):
            questions.append("Покупаете впервые или продлеваете лицензию?")
        if not selection.organization and len(values("Тип организации")) > 1:
            questions.append(
                "Какой тип организации? В карточках есть варианты: "
                + ", ".join(sorted(values("Тип организации")))
                + "."
            )
        if selection.months is None and len(values("Срок действия")) > 1:
            questions.append(
                "Какой нужен срок лицензии? Варианты: " + ", ".join(sorted(values("Срок действия"))[:6]) + "."
            )
    if selection.kind == "Продление" and selection.expired is None:
        if len({"просроч" in r["name"].casefold() for r in rows}) > 1:
            questions.append("Подписка ещё действует или уже истекла?")
    if questions:
        pending = result(prefix + " ".join(questions), sources=[source])
        pending.awaiting_fields = True
        return pending
    products = [offer_product(r) for r in rows[:3]]
    if len(rows) != 1:
        options = "; ".join(f"{p.name} (ID {p.offerId})" for p in products)
        return result(
            prefix + f"Подходят несколько позиций: {options}. Уточните нужный вариант или его ID.", products=products
        )
    assessment = offer_assessment(rows[0])
    p = products[0]
    parts = [prefix + p.name.rstrip(".") + "."]
    if selection.kind:
        parts.append(f"Тип лицензии по карточке — «{selection.kind}».")
    if rows[0]["edition"]:
        parts.append(f"Редакция по карточке: {rows[0]['edition']}.")
    bundle = selection.name.startswith("MDaemon") and not rows[0]["sales_notes"]
    if selection.seats is not None and bundle:
        parts.append(f"Для {selection.seats} пользователей достаточно одной упаковки по вместимости из карточки.")
    if not assessment.show_price or p.unitPriceKopecks is None:
        parts.append("Цена по запросу.")
    else:
        rub, kop = divmod(p.unitPriceKopecks, 100)
        money = f"{rub:,}".replace(",", " ") + (f",{kop:02}" if kop else "")
        unit = " за упаковку" if bundle else ""
        parts.append(f"Цена по снимку каталога — {money} ₽{unit}.")
        if rows[0]["sales_notes"]:
            parts.append("Условия количества из карточки: " + rows[0]["sales_notes"])
    if assessment.price_state == "availability_check":
        parts.append("Наличие требует подтверждения.")
    elif p.available:
        parts.append("В снимке позиция отмечена как доступная.")
    if selection.perpetual:
        parts.append(f"Срок по карточке: {p.licenseTerm}." if p.licenseTerm else "Срок в карточке не указан.")
        parts.append("Право использования после окончания подписки требует проверки условий лицензии.")
    answer = result(" ".join(parts), "answer", products)
    if selection.code:
        answer.followup = _followup_from_sku(selection, rows[0])
    return answer
