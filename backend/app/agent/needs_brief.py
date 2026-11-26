"""Collect requirements for hosting or protecting business email, across customer turns."""

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from app.agent.catalog_selection import Selection


@dataclass
class Brief:
    goal: str = "mail"
    files: bool = False
    people: int | None = None
    addresses: int | None = None
    servers: int | None = None
    mail_system: str = ""
    file_system: str = ""
    gateway: bool | None = None
    buyer: str = ""
    organization: str = ""
    purchase: str = ""
    months: int | None = None
    perpetual: bool = False
    cloud_only: bool = False
    budget_kopecks: int | None = None
    russian: bool = False
    delivery: str = ""
    task: str = ""
    platform: str = ""


MAIL = r"почт\w*|ящик\w*|e-?mail"
PROTECT = r"защи[тщ]\w*|защит\w*|фильтр\w*|антиспам|антивирус|спам\w*"
MAILBOX = (
    r"(?:завести|заводим|созда\w*|развер\w*|нов\w*|нужны|поднять|организовать)"
    r"\W+(?:\w+\W+){0,3}(?:почт\w*|ящик\w*)|(?:почтовый|почтового) сервер"
)
FILES = r"файл\w*"
NUMBER = r"(\d{1,7}|один|одна|одной|одном|одного|два|две|три)"
NUMBERS = {"один": 1, "одна": 1, "одной": 1, "одном": 1, "одного": 1, "два": 2, "две": 2, "три": 3}
GATEWAY = r"(?:отдельн\w*|выделенн\w*)\s+(?:машин\w*|(?:windows|виндоус)(?:[- ](?:машин|сервер)\w*)?|сервер\w*)"
LINUX = r"linux|линукс|ubuntu|debian|centos|rhel|astra|suse"
MONEY_LIMIT = re.compile(
    r"(?:бюджет\w*|потолок|не дороже|до|лимит|не более)\s*(?:(?:теперь|до|составляет|не более)\s+)*[:—-]?\s*"
    r"(\d[\d ]*(?:[,.]\d{1,3})?)\s*(тыс\w*\.?|к|млн\.?)?\s*(?:₽|руб\w*|р\.|за лицензии|[.!?]?$)"
)


def initial_need(text: str) -> bool:
    s = text.casefold()
    return bool(
        (re.search(MAIL, s) or (re.search(FILES, s) and re.search(PROTECT, s)))
        and re.search(r"подобра\w*|подберите|выб\w*|нуж\w*|хотим|защит\w*|развер\w*|завести|организовать|посовет\w*", s)
    )


def open_intake(text: str) -> bool:
    if re.search(r"\b(?:подберите|посоветуйте|ищем|нужен|нужна|нужно)\b", text, re.I) and re.search(
        r"редакт\w*[^.!?]{0,40}pdf|pdf[^.!?]{0,40}редакт|резервн\w* коп|б[эе]кап", text, re.I
    ):
        return True
    return bool(
        re.search(
            r"помогите (?:подобрать|выбрать)|не зна(?:ю|ем),? что (?:нужно|выбрать)|"
            r"(?:подберите|нужно что-то|нужно решение) (?:ПО |софт |программу |решение )?для\b",
            text,
            re.I,
        )
    )


def count(text: str, unit: str) -> int | None:
    match = re.search(r"\b" + NUMBER + r"\s*(?:" + unit + r")\b", text)
    if not match:
        return None
    value = match[1]
    return int(value) if value.isdigit() else NUMBERS[value]


def update_brief(previous: Brief, text: str) -> Brief:
    b = replace(previous)
    s = " ".join(text.casefold().replace("ё", "е").split())
    qualifiers = Selection("").qualifiers(text, followup=True)
    if b.goal == "discovery":
        if re.search(r"резервн\w* коп|бэкап|бекап|backup", s):
            b.task = "backup"
        elif re.search(r"редакт\w*[^.!?]{0,40}pdf|pdf[^.!?]{0,40}редакт", s):
            b.task = "pdf"
        if "windows" in s:
            b.platform = "Windows"
        elif re.search(LINUX, s):
            b.platform = "Linux"
        elif "macos" in s:
            b.platform = "macOS"
    if b.goal == "discovery" and re.search(MAIL, s):
        b.goal = "mail"
    if re.search(PROTECT, s) and (re.search(MAIL, s) or b.goal == "mail"):
        b.goal = "protect"
    elif re.search(MAILBOX, s) or (b.goal == "mail" and re.search(r"(?:новые|создать|завести)\b", s)):
        b.goal = "mailboxes"
    if re.search(FILES, s):
        b.files = not bool(re.search(r"без защит\w* файл|файл\w* (?:уже защищены|не нужны|защищать не надо)", s))
        if re.search(r"только (?:защит\w* )?файл|почт\w* (?:уже защит\w*|защищать не надо)", s) or (
            previous.goal in {"mail", "discovery"} and re.search(PROTECT, s) and not re.search(MAIL, s)
        ):
            b.goal = "files"
    if re.search(r"только (?:защит\w* )?почт", s):
        b.goal, b.files = "protect", False
    people = count(s, r"пользовател\w*|человек\w*|сотрудник\w*|ящик\w*|мест\w*")
    if people is not None:
        b.people = people
    addresses = count(s, r"(?:почтов\w+ |рабочи\w+ )?(?:адрес\w*|ящик\w*)")
    total = re.search(r"(?:адрес\w*|ящик\w*)[ ,:—–-]+(?:всего\s+)?(\d+)\b", s)
    if total:
        addresses = int(total[1])
    elif re.search(r"(?:у каждого|по одному)\s+(?:один\s+)?(?:рабочи\w*\s+)?(?:адрес|ящик)", s):
        addresses = b.people
    if addresses is not None:
        b.addresses = addresses
    servers = count(s, r"файлов\w* сервер\w*|(?:windows|linux)[ -]server\w*|(?:windows|linux)[ -]сервер\w*")
    if servers is not None:
        b.servers = servers
    if previous.goal == "protect" and previous.gateway is None and re.fullmatch(r"(?:да|нет)[.! ]*", s):
        b.gateway = s.startswith("да")
    for clause in re.split(r"[.,;!?]|\s+а\s+", s):
        if re.search(GATEWAY, clause):
            if re.search(r"\b(?:без|нет|нельзя|недоступ\w*|не)\b", clause) or re.search(LINUX, clause):
                b.gateway = False
            else:
                b.gateway = True if re.search(r"windows|виндоус", clause) else None
        os_name = "Windows" if re.search(r"windows|виндоус", clause) else "Linux" if re.search(LINUX, clause) else ""
        if os_name and (re.search(FILES, clause) or ("server " in clause and b.files and not re.search(MAIL, clause))):
            version = re.search(r"windows server (20\d{2})(?:\s+(r2))?", clause)
            b.file_system = version[0] if version else os_name
        if "postfix" in clause:
            b.mail_system = "Postfix"
        elif "exchange" in clause:
            b.mail_system = "Exchange"
        elif re.search(r"облак\w*|облачн\w*", clause) and not re.search(r"без облак|не в облаке", clause):
            b.mail_system = "облако"
        elif (
            os_name
            and not re.search(FILES + "|" + GATEWAY, clause)
            and (b.goal == "mailboxes" or re.search(MAIL, clause))
        ):
            b.mail_system = os_name
    b.buyer = qualifiers.buyer or b.buyer
    b.organization = qualifiers.organization or b.organization
    b.purchase = qualifiers.kind or (
        "Полная версия" if re.search(r"первая покупка|первичн\w* лицензи", s) else b.purchase
    )
    b.months = qualifiers.months if qualifiers.months is not None else b.months
    if re.search(r"бессроч|навсегда", s):
        b.perpetual = not bool(re.search(r"не нужна бессроч|бессроч\w* (?:не нужна|не обязательно)", s))
    elif qualifiers.months:
        b.perpetual = False
    if re.search(r"(?:только|исключительно|требу\w*|нуж\w*)\s+(?:исключительно |только )?(?:в )?обла[кч]", s):
        b.cloud_only = True
    elif re.search(r"(?:размещ\w*|установ\w*) у нас|только локально|сво[её]м сервере", s):
        b.cloud_only = False
    budget = next(reversed(list(MONEY_LIMIT.finditer(s))), None)
    if budget:
        amount = budget[1].replace(" ", "").replace(",", ".")
        if "." in amount and len(amount.split(".")[1]) == 3 and not budget[2]:
            amount = amount.replace(".", "")
        factor = 1_000_000 if (budget[2] or "").startswith("млн") else 1000 if budget[2] else 1
        b.budget_kopecks = int(Decimal(amount) * factor * 100)
    elif re.search(r"без ограничени\w* (?:по бюджету|бюджета)|бюджет (?:не ограничен|любой)", s):
        b.budget_kopecks = None
    if re.search(r"российск\w*|отечественн\w*|реестр\w*", s):
        b.russian = not bool(re.search(r"(?:не обязател\w*|не нужен|не требуется)", s))
    if re.search(r"электронн\w* (?:поставк|доставк)|электронно", s):
        b.delivery = "электрон"
    elif re.search(r"коробочн\w*|физическ\w* поставк", s):
        b.delivery = "короб"
    return b


def questions(b: Brief) -> list[str]:
    if b.goal == "discovery":
        if b.task:
            return [] if b.platform else ["На какой операционной системе должно работать решение?"]
        return ["Какую задачу должно решить ПО, где оно будет работать и для скольких пользователей?"]
    if b.goal == "mail":
        return ["Вы хотите завести новые почтовые ящики или защитить уже работающую почту?"]
    missing = []
    if b.goal == "mailboxes":
        if not b.people:
            missing.append("Сколько пользователей будут работать с почтой?")
        if not b.mail_system:
            missing.append("Где хотите разместить почту и какая ОС у сервера?")
    else:
        if b.goal == "protect" and (not b.mail_system or not b.addresses):
            missing.append("Какая почтовая система используется и сколько адресов предстоит защищать?")
        if b.files and (not b.servers or not b.file_system):
            missing.append("На скольких файловых серверах хранятся данные и какая у них ОС?")
        if b.goal == "protect" and b.gateway is None and not b.russian:
            missing.append("Можно выделить отдельную Windows-машину для почтового шлюза?")
    if not b.buyer or not b.purchase:
        missing.append("На кого оформляется лицензия и это первая покупка или продление?")
    if b.buyer == "Юрлицо" and not b.organization:
        missing.append("Организация коммерческая, государственная, учебная или некоммерческая?")
    if missing and b.goal in {"protect", "files"} and b.budget_kopecks is None:
        missing.append("Есть ли предел бюджета на лицензии?")
    return missing
