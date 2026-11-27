"""Route requests to the existing operator queue. Product facts come from the catalog."""

import re
from dataclasses import dataclass
from typing import Literal

from app.contracts.schemas import Message
from app.contracts.t04 import HandoffReason, Product

ActionType = Literal["message", "request_operator"]
SearchFinding = Literal["partner_required", "insufficient_evidence"]
OPERATOR_QUESTION = "Подключить оператора, чтобы он уточнил ваш вопрос?"
OFFER_HELP = f"В источнике нет подтверждённых сведений. {OPERATOR_QUESTION}"
ASK_TOPIC = "Подскажите, с каким вопросом нужна помощь?"
CONFIRMED = "Передал вопрос оператору. Он увидит переписку и ответит в этом чате."

# Match a request for a human, not every mention of a manager or a telecom operator.
HUMAN = r"(?:оператор|менеджер|специалист|человек|сотрудник)\w*"
CALL = r"(?:позов\w*|позва\w*|подключ\w*|соедин\w*|переключ\w*|приглас\w*|связа\w*|поговор\w*|переда\w*)"
REQUEST_WORDS = (
    rf"(?:{CALL}|нужен|нужна|хочу|можно)(?:[ ,]+(?:меня|мне|нас|нам|пожалуйста|на|к|с|со|жив\w*|ваш\w*|"
    rf"пообщаться|поговорить|связаться|позвать|подключить)){{0,5}}[ ,]+{HUMAN}"
)
HUMAN_REQUEST = re.compile(rf"\b{REQUEST_WORDS}\b(?!\s+связи)", re.I)
COURTESY = r"(?:здравствуйте|здравствуй|добрый (?:день|вечер)|доброе утро|привет|пожалуйста|спасибо)"
BARE_REQUEST = re.compile(rf"(?:{COURTESY}[,.! ]+)*(?:{REQUEST_WORDS}|{HUMAN})(?:[,!?. ]+{COURTESY})*[,!?. ]*")
HUMAN_DECLINED = re.compile(
    rf"\b(?:без\s+{HUMAN}|не\s+(?:(?:надо|нужно|хочу)\s+)?{CALL}|не\s+нужен\s+{HUMAN}|{HUMAN}\s+не\s+нужен)"
)
AFFIRMATIVE = re.compile(
    r"(?:да|давайте|согласен|согласна|конечно|подключайте)(?:[, .!]+(?:пожалуйста|давайте|подключайте))?[.! ]*"
)
PRICE = re.compile(r"\b(?:цен[а-яё]*|стоимост\w*|стоит|стоят|прайс|поч[её]м)\b")
STOCK_ITEM = r"(?:товары?|продукты?|программ[аы]|программное обеспечение|по|лицензи[яи]|позици[яи]|артикул|он[аи]?)"
STOCK = re.compile(
    r"\b(?:наличи[еяюи]|наличием|в продаже)\b"
    r"|\bдоступ(?:ен|на|но|ны) ли\b(?:\s+(?:сейчас|сегодня|ещ[её]|уже))*\s*"
    r"(?=$|[?!.;,]|(?:(?:этот|эта|это|эти|выбранн\w+|данн\w+)\s+)?"
    rf"(?:{STOCK_ITEM}\b|для покупки\b|[a-z0-9]))"
    rf"|\b{STOCK_ITEM} доступ(?:ен|на|но|ны)\b"
    r"|\bдоступность (?:товара|продукта|лицензии|позиции)\b"
)
TERM = re.compile(r"срок\w*|сколько\s+(?:месяцев|лет)|бессроч\w*")
TENDER = re.compile(r"\b(?:тендер\w*|госзакуп\w*|закупк\w*\s+по\s+(?:44|223)|(?:44|223)[ -]?фз)\b")
NO_TENDER = re.compile(r"\b(?:не\s+тендер\w*|без\s+тендер\w*|тендер\w*\s+(?:нет|не\s+(?:будет|нужен)))\b")
EXTRA_DOCUMENTS = re.compile(
    r"(?:документ\w*|справк\w*|сертификат\w*).{0,65}"
    r"(?:вне\s+(?:store|магазина)|нет\s+в\s+(?:store|магазине)|для\s+(?:участия|аккредитации))"
    r"|нестандартн\w*\s+(?:пакет\s+)?документ|анкет\w*\s+поставщика|письмо\s+авторизации|гарантийно\w*\s+письмо"
)
PARTNER = re.compile(
    r"(?:через|запрос\w*\s+у|уточн\w*\s+у|заказ\w*\s+у)\s+партн[её]р|(?:нет|отсутствует)\s+в\s+(?:вашем\s+)?каталоге"
    r"|вне\s+каталога|в\s+(?:вашем\s+)?каталоге\s+(?:нет|отсутствует)"
)
SPECIAL = re.compile(
    r"(?:нуж\w*|просим|хотим|предостав\w*|оформ\w*|можно|по)\s+(?:(?:нам|ли|согласовать)\s+){0,2}(?:отсрочк\w*|постоплат\w*|индивидуальн\w*\s+скидк\w*)"
)


def offers_operator(text: str) -> bool:
    return text == OFFER_HELP or text.endswith("\n\n" + OPERATOR_QUESTION)


def asks_for_operator(text: str) -> bool:
    return not HUMAN_DECLINED.search(text) and bool(
        HUMAN_REQUEST.search(text) or re.fullmatch(rf"{HUMAN}[.!? ]*", text)
    )


def bare_request(text: str) -> bool:
    return bool(BARE_REQUEST.fullmatch(text))


def opening_chatter(text: str) -> bool:
    return bool(re.fullmatch(rf"{COURTESY}[,.!? ]*", text) or AFFIRMATIVE.fullmatch(text))


def handoff_reason(text: str) -> HandoffReason | None:
    if TENDER.search(text) and not NO_TENDER.search(text):
        return "tender"
    if (
        EXTRA_DOCUMENTS.search(text)
        or SPECIAL.search(text)
        or (
            re.search(r"\b(?:кп|коммерческ\w+ предложени\w*)\b", text)
            and re.search(r"\b(?:с\s+(?:постоплатой|отсрочкой|индивидуальной скидкой)|со\s+скидкой)\b", text)
        )
    ):
        return "special_terms"
    if PARTNER.search(text):
        return "outside_catalog"
    if re.search(r"\b(?:запросить цену|запросите цену|цена по запросу)\b", text):
        return "price_request"
    if asks_for_operator(text):
        return "customer_request"
    return None


def open_question(history: list[Message], text: str, generic: bool) -> str:
    if generic:
        for m in reversed(history):
            if m.role == "operator":
                break
            if (
                m.role == "client"
                and not bare_request(m.text.casefold())
                and not opening_chatter(m.text.strip().casefold())
            ):
                return m.text
    return text


@dataclass(frozen=True)
class Routing:
    reason: HandoffReason
    question: str
    clarification: str | None = None

    @property
    def offer_help(self) -> bool:
        return self.clarification == OFFER_HELP


def route_action(
    history: list[Message],
    text: str,
    products: list[Product],
    *,
    action_type: ActionType = "message",
    finding: SearchFinding | None = None,
    product_reason: HandoffReason | None = None,
) -> Routing | None:
    """Route a T04 action using the server's selected products and verified search outcome."""
    body = " ".join(text.casefold().split())
    if action_type == "request_operator":
        return Routing(
            "customer_request", open_question(history, text or "Покупатель просит подключить оператора.", True)
        )
    reason = handoff_reason(body)
    if reason == "customer_request" and bare_request(body):
        prior_requests = any(m.role == "client" and not opening_chatter(m.text.strip().casefold()) for m in history)
        if not prior_requests and not products and finding != "partner_required":
            return Routing(reason, text, clarification=ASK_TOPIC)
        return Routing(reason, open_question(history, text, True))
    if reason:
        return Routing(reason, text)
    if history and history[-1].role == "agent" and offers_operator(history[-1].text) and AFFIRMATIVE.fullmatch(body):
        return Routing("insufficient_evidence", open_question(history, text, True))
    if finding == "partner_required":
        return Routing("outside_catalog", text)
    if len(products) == 1:
        p = products[0]
        asks_availability = STOCK.search(body) or re.search(
            r"купить|поставк|достав|получить .*?ключ|получить ключ|электронн\w* ключ|прид[её]т", body
        )
        if asks_availability and (product_reason == "availability" or p.priceState == "availability_check"):
            return Routing("availability", text)
        if PRICE.search(body) and p.priceState == "on_request":
            return Routing("price_request", text)
        if asks_availability and not p.available:
            return Routing("availability", text)
        if TERM.search(body) and p.licenseTerm is None:
            finding = "insufficient_evidence"
    if finding == "insufficient_evidence":
        return Routing("insufficient_evidence", text, clarification=OFFER_HELP)
    return None
