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
