"""Route requests to the existing operator queue. Product facts come from the catalog."""

import re
from dataclasses import dataclass
from typing import Literal

from app.contracts.schemas import Message
from app.contracts.t04 import HandoffReason, Product

ActionType = Literal["message", "request_operator"]
SearchFinding = Literal["partner_required", "insufficient_evidence"]
OFFER_HELP = "В источнике нет подтверждённых сведений. Подключить оператора, чтобы он уточнил ваш вопрос?"
CONFIRMED = "Передал вопрос оператору. Он увидит переписку и ответит в этом чате."

# Match a request for a human, not every mention of a manager or a telecom operator.
HUMAN = r"(?:оператор|менеджер|специалист|человек|сотрудник)\w*"
CALL = r"(?:позов\w*|позва\w*|подключ\w*|соедин\w*|переключ\w*|приглас\w*|связа\w*|поговор\w*|переда\w*)"
HUMAN_REQUEST = re.compile(
    rf"\b(?:{CALL}|нужен|нужна|хочу|можно)(?:\s+(?:меня|мне|нас|нам|пожалуйста|на|к|с|со|жив\w*|ваш\w*)){{0,4}}"
    rf"\s+{HUMAN}\b(?!\s+связи)"
)
HUMAN_DECLINED = re.compile(
    rf"\b(?:без\s+{HUMAN}|не\s+(?:(?:надо|нужно|хочу)\s+)?{CALL}|не\s+нужен\s+{HUMAN}|{HUMAN}\s+не\s+нужен)"
)
AFFIRMATIVE = re.compile(
    r"(?:да|давайте|согласен|согласна|конечно|подключайте)(?:[, .!]+(?:пожалуйста|давайте|подключайте))?[.! ]*"
)
PRICE = re.compile(r"\b(?:цен[а-яё]*|стоимост\w*|стоит|стоят|прайс|поч[её]м)\b")
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


def asks_for_operator(text: str) -> bool:
    return not HUMAN_DECLINED.search(text) and bool(
        HUMAN_REQUEST.search(text) or re.fullmatch(rf"{HUMAN}[.!? ]*", text)
    )


def bare_request(text: str) -> bool:
    return asks_for_operator(text) and bool(
        re.fullmatch(rf"(?:пожалуйста[, ]+)?(?:{CALL}\s+(?:мне\s+|с\s+)?)?{HUMAN}(?:[, ]+пожалуйста)?[?!. ]*", text)
    )


def handoff_reason(text: str) -> HandoffReason | None:
    if TENDER.search(text) and not NO_TENDER.search(text):
        return "tender"
    if EXTRA_DOCUMENTS.search(text) or SPECIAL.search(text):
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
                and not AFFIRMATIVE.fullmatch(m.text.casefold())
            ):
                return m.text
    return text


@dataclass(frozen=True)
class Routing:
    reason: HandoffReason
    question: str
    offer_help: bool = False


def route_action(
    history: list[Message],
    text: str,
    products: list[Product],
    *,
    action_type: ActionType = "message",
    finding: SearchFinding | None = None,
) -> Routing | None:
    """Route a T04 action using the server's selected products and verified search outcome."""
    body = " ".join(text.casefold().split())
    if action_type == "request_operator":
        return Routing(
            "customer_request", open_question(history, text or "Покупатель просит подключить оператора.", True)
        )
    reason = handoff_reason(body)
    if reason:
        return Routing(reason, open_question(history, text, reason == "customer_request" and bare_request(body)))
    if history and history[-1].role == "agent" and history[-1].text == OFFER_HELP and AFFIRMATIVE.fullmatch(body):
        return Routing("insufficient_evidence", open_question(history, text, True))
    if finding == "partner_required":
        return Routing("outside_catalog", text)
    if len(products) == 1:
        p = products[0]
        if PRICE.search(body) and p.priceState == "on_request":
            return Routing("price_request", text)
        if p.priceState == "availability_check" and re.search(r"наличи|доступн|купить|поставк", body):
            return Routing("availability", text)
        if TERM.search(body) and p.licenseTerm is None:
            finding = "insufficient_evidence"
    if finding == "insufficient_evidence":
        return Routing("insufficient_evidence", text, offer_help=True)
    return None
