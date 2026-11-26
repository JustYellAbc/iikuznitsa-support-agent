"""PII scrubbing helpers applied before sending real support transcripts to any stand/LLM."""

from __future__ import annotations

import re

IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
REQUISITE_RE = re.compile(
    r"\b(?:ИНН|КПП|ОГРН|ОГРНИП|БИК|ОКПО|р/с|к/с)\s*[:№]?\s*\d[\d\s-]{6,22}\d\b",
    re.IGNORECASE,
)
LONG_ID_NUM_RE = re.compile(r"(?<!\d)\d{10,15}(?!\d)")
LICENSE_KEY_RE = re.compile(r"\b[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,5}\b")
LICENSE_LABEL_RE = re.compile(
    r"(\b(?:номер\s+лицензи\w*|серийн\w*\s+номер|ключ\s+лицензи\w*|лицензия\s*№)\s*[:№]?\s*)"
    r"[A-Za-zА-Яа-я0-9._/-]{5,}",
    re.IGNORECASE,
)
ORG_RE = re.compile(
    r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|ФГБУ|ГБУ|МБУ|ФГУП|МУП|ФГАОУ|АНО|НКО|ТСЖ)\s+"
    r"(?:[«\"][^»\"\n]{2,80}[»\"]|(?!Софтлайн\b)[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9-]+){0,3})"
)
ADDRESS_RE = re.compile(
    r"(?:г\.\s*[А-ЯЁ][а-яё-]+,\s*)?"
    r"\b(?:ул\.|улица|пр-т|проспект|пер\.|переулок|шоссе|бульвар)\s+"
    r"[А-ЯЁа-яё0-9.\s/-]{3,45}",
    re.IGNORECASE,
)
NAME_INTRO_RE = re.compile(
    r"(\b(?:меня\s+зовут|с\s+уважением,?|контактное\s+лицо|фио)\s*[:,-]?\s+)"
    r"[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,}){0,2}",
    re.IGNORECASE,
)
PATRONYMIC_RE = re.compile(
    r"\b(?:[А-ЯЁ][а-яё]{2,}\s+)?[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:ович|евич|ич|овна|евна|ична)\b"
)
INITIALS_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.")
COMMON_FIRST_NAME_RE = re.compile(
    r"\b(?:Александр|Алексей|Анастасия|Андрей|Анна|Антон|Арт[её]м|Вадим|Валентин|Валерий|Василий|"
    r"Виктор|Виктория|Виталий|Владимир|Владислав|Вячеслав|Галина|Георгий|Григорий|Дарья|Денис|Дмитрий|"
    r"Евгений|Евгения|Егор|Екатерина|Елена|Иван|Игорь|Илья|Ирина|Кирилл|Константин|Ксения|Лариса|"
    r"Людмила|Максим|Маргарита|Марина|Мария|Михаил|Надежда|Наталья|Никита|Николай|Оксана|Олег|Ольга|"
    r"Павел|П[её]тр|Полина|Роман|Руслан|Светлана|Сергей|Станислав|Татьяна|Тимур|Ф[её]дор|Юлия|Юрий|Яна)"
    r"(?:а|у|ом|е|ой|и|ю|я|ей|ам|ах)?\b"
)


def scrub_pii(text: str) -> str:
    """Mask residual personal and organizational PII before external transmission."""
    cleaned = IPV4_RE.sub("[ip]", text)
    cleaned = EMAIL_RE.sub("[email]", cleaned)
    cleaned = PHONE_RE.sub("[тел]", cleaned)
    cleaned = REQUISITE_RE.sub("[реквизит]", cleaned)
    cleaned = LICENSE_KEY_RE.sub("[лицензия]", cleaned)
    cleaned = LICENSE_LABEL_RE.sub(r"\1[лицензия]", cleaned)
    cleaned = LONG_ID_NUM_RE.sub("[номер]", cleaned)
    cleaned = ORG_RE.sub("[организация]", cleaned)
    cleaned = ADDRESS_RE.sub("[адрес]", cleaned)
    cleaned = NAME_INTRO_RE.sub(r"\1[имя]", cleaned)
    cleaned = PATRONYMIC_RE.sub("[имя]", cleaned)
    cleaned = INITIALS_RE.sub("[имя]", cleaned)
    cleaned = COMMON_FIRST_NAME_RE.sub("[имя]", cleaned)
    return cleaned
