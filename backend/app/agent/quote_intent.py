"""Recognize requests to open the quote form for the current selection."""

import re

QUOTE = r"(?:кп|коммерческое предложение)"
REQUEST = re.compile(
    rf"(?:(?:эй|бля|да|давай(?:те)?|пожалуйста|тогда|ну|можно|хочу|нужно)[,!\s]+)*"
    rf"(?:(?:подготовь(?:те)?|подготовить|подготавливай(?:те)?|оформи(?:те)?|оформить|оформляй(?:те)?|"
    rf"сформируй(?:те)?|сформировать|пришли(?:те)?|сделай(?:те)?|составь(?:те)?)\s+)?{QUOTE}"
    rf"(?:\s+по (?:этим|выбранным) (?:товарам|позициям))?(?:,?\s+пожалуйста)?[.!? ]*",
    re.I,
)


def offers_quote(text: str) -> bool:
    question = text.strip().casefold()
    return bool(
        question.endswith("?")
        and re.search(rf"\b{QUOTE}\b", question)
        and re.search(
            r"(?:^|[.!]\s+)(?:(?:вам|хотите|могу|можем|давайте|предлагаю|чтобы я|я)\b[,\s]+)*"
            r"(?:оформить|подготовить|составить|сформировать|оформлю|подготовлю)\b",
            question,
        )
        and not re.search(r"\b(?:или|оператор\w*|менеджер\w*|отмен\w*|не)\b", question)
    )


def basket_resize(text):
    """An explicit instruction about the whole basket can be executed as a unit.
    Free-form qualifications require confirming the scope before changing a revision.
    """
    command = re.search(
        r"\b(?:пересчит(?:ай(?:те)?|ать)|измен(?:и(?:те)?|ить)|обнов(?:и(?:те)?|ить))\s+"
        r"(?:(?:этот|тот|весь|прежний|исходный)\s+(?:же\s+)?(?:состав|комплект|заказ)|"
        r"(?:все|обе)\s+(?:позиции|строки)|вс[её]\s+кп)\s+на\s+"
        r"([1-9]\d{0,4})\s+(?:пользовател(?:я|ей|ь)|мест(?:а|о)?|seats?)\b",
        text,
        re.I,
    )
    if not command:
        return None
    prefix = text[: command.start()].strip(" .,:;!").casefold()
    suffix = text[command.end() :].strip(" .!")
    if text.rstrip().endswith("?") or re.search(r"\bне\s*$", prefix):
        return None
    return int(command[1]), bool(prefix or suffix)
