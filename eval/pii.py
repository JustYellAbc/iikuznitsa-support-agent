"""PII scrubbing applied before real support transcripts leave the machine (stand / external LLM).

The support archive is entirely lower-case, so every pattern here is case-insensitive and works on
lower-case Russian text: first names are matched by a dictionary with case forms, surnames and
patronymics only next to a recognised name, organisations by legal form (ООО, АО, ИП …) with
Softline catalog vendors excluded, licence keys in any case, and IPv4 addresses except software
versions such as ``astra linux 1.7.4.1``.
"""

from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "data/raw/Mindbox.yml.gz.xml"

_I = re.IGNORECASE

# ---------------------------------------------------------------------------
# First names with case forms
# ---------------------------------------------------------------------------

# Base forms (nominative). Words that are also common nouns or product names in this domain
# («астра», «аврора», «алиса», «марта», «лира», «слава», «любовь», «роза» …) are intentionally absent.
_MALE_NAMES = (
    "александр алексей анатолий андрей антон аркадий арсений артём артур богдан борис вадим валентин "
    "валерий василий виктор виталий владимир владислав всеволод вячеслав геннадий георгий герман глеб "
    "григорий давид даниил данил денис дмитрий евгений егор захар иван игорь илья иннокентий кирилл "
    "константин леонид максим марат матвей михаил назар никита николай олег павел пётр платон ринат "
    "роман ростислав руслан рустам святослав семён сергей станислав степан тимофей тимур фёдор филипп "
    "эдуард эльдар юрий ярослав"
).split()
_FEMALE_NAMES = (
    "айгуль алевтина александра алёна алина алла альбина анастасия ангелина анжела анна антонина "
    "валентина валерия варвара вера вероника виктория галина гульнара дарья диана евгения екатерина "
    "елена елизавета жанна зинаида зоя инна ирина карина кира кристина ксения лариса лидия лилия "
    "людмила маргарита марина мария милана надежда наталия наталья нина оксана олеся ольга полина "
    "раиса регина светлана софия софья тамара татьяна ульяна эльвира юлия яна"
).split()
_DIMINUTIVES = (
    "аня ваня вася вова даша дима женя ира катя коля костя ксюша лена леша лёша лиза маша миша настя "
    "наташа оля паша петя саша серёжа таня толя юля юра"
).split()

_SIBILANT_OR_VELAR = "гкхжшщч"


def _name_forms(base: str) -> set[str]:
    """Generate the six-case forms of a Russian first name (regular paradigms only)."""
    forms = {base}
    if base.endswith("ий"):
        stem = base[:-2]
        forms |= {stem + e for e in ("ия", "ию", "ием", "ии")}
    elif base.endswith("ия"):
        stem = base[:-2]
        forms |= {stem + e for e in ("ии", "ию", "ией", "иею")}
    elif base.endswith("ья"):
        stem = base[:-2]
        forms |= {stem + e for e in ("ьи", "ье", "ью", "ьей", "ьею")}
    elif base.endswith(("й", "ь")):
        stem = base[:-1]
        forms |= {stem + e for e in ("я", "ю", "ем", "е")}
    elif base.endswith("а"):
        stem = base[:-1]
        gen = "и" if stem and stem[-1] in _SIBILANT_OR_VELAR else "ы"
        forms |= {stem + e for e in (gen, "е", "у", "ой", "ою", "ей")}
    elif base.endswith("я"):
        stem = base[:-1]
        forms |= {stem + e for e in ("и", "е", "ю", "ей", "ею")}
    else:
        forms |= {base + e for e in ("а", "у", "ом", "е", "ым")}
    with_e = {f.replace("ё", "е") for f in forms if "ё" in f}
    return forms | with_e


NAME_FORMS: frozenset[str] = frozenset(
    form for base in (*_MALE_NAMES, *_FEMALE_NAMES, *_DIMINUTIVES) for form in _name_forms(base)
)
_NAME_ALT = "|".join(sorted(NAME_FORMS, key=len, reverse=True))
# Not followed by a hyphen: keeps product names such as «роман-газета» intact.
FIRST_NAME_RE = re.compile(rf"(?<![\w-])(?:{_NAME_ALT})(?![\w-])", _I)

PATRONYMIC_PART = (
    r"[а-яё]{2,}(?:ович|евич|ьич)(?:а|у|ем|е)?"
    r"|[а-яё]{2,}(?:овн|евн|ичн|иничн)(?:а|ы|е|у|ой|ою)"
)
SURNAME_PART = (
    r"[а-яё]{2,}(?:ов|ев|ёв|ин|ын|ова|ева|ёва|ина|ына|ский|цкий|ская|цкая|енко|ук|юк|ян|дзе|швили)"
    r"(?:а|у|ым|ой|ом|е|ую|ого|ому|ой)?"
)
NAME_WITH_PATRONYMIC_RE = re.compile(rf"\[имя\](?:\s+(?:{PATRONYMIC_PART}))(?![\w-])", _I)
NAME_THEN_SURNAME_RE = re.compile(rf"\[имя\]\s+(?:{SURNAME_PART})(?![\w-])", _I)
SURNAME_THEN_NAME_RE = re.compile(rf"(?<![\w-])(?:{SURNAME_PART})\s+\[имя\]", _I)
_NON_NAME_INITIALS = r"(?!(?:т\.\s?[екдпчн]|и\.\s?о|н\.\s?в|с\.\s?г|у\.\s?е)\.)"
INITIALS_RE = re.compile(
    rf"(?<![\w-])(?:{SURNAME_PART})\s+{_NON_NAME_INITIALS}[а-яё]\.\s?[а-яё]\.(?![\w])"
    rf"|(?<![\w.]){_NON_NAME_INITIALS}[а-яё]\.\s?[а-яё]\.\s?(?:{SURNAME_PART})(?![\w-])",
    _I,
)
NAME_INTRO_RE = re.compile(
    r"(\b(?:меня\s+зовут|зовут\s+меня|с\s+уважением,?|контактное\s+лицо|ответственн\w*\s+лицо|"
    r"фио(?:\s+контактного\s+лица)?|имя\s+и\s+фамилия)\s*[:,-]?\s*)"
    r"([а-яёa-z]{2,}(?:-[а-яёa-z]{2,})?)",
    _I,
)

# ---------------------------------------------------------------------------
# Organisations (legal form + name), with Softline vendors excluded
# ---------------------------------------------------------------------------

LEGAL_FORMS = (
    "ооо оао зао пао нао ао ип фгбу фгбоу фгаоу фгуп гбу гбоу гку гауз гбуз мбу мбоу маоу мадоу мбдоу "
    "моу муп гуп ано нко тсж снт чоу нпо нпп нпф"
).split()
_ORG_STOP = (
    r"(?:хоч\w*|нуж\w*|прос\w*|интерес\w*|для|на|в|по|и|или|из|от|с|к|о|об|у|за|не|бы|ли|нам|мы|"
    r"купит\w*|приобр\w*|оплат\w*|выстав\w*|счет\w*|счёт\w*|кп|лиценз\w*|подскаж\w*|скаж\w*|цен\w*)"
)
_ORG_WORD = rf"(?!{_ORG_STOP}(?![\w-]))[\w][\w.&+-]*"
ORG_RE = re.compile(
    rf"(?<![\w-])(?:{'|'.join(LEGAL_FORMS)})\.?\s+"
    r"(?P<name>"
    r"«(?:[^«»\n]|«[^«»\n]*»){1,80}»"
    r"|«[^»\n]{1,80}»"
    r"|[\"“„](?:[^\"“”„\n]){1,80}[\"”“]"
    r"|'[^'\n]{1,60}'"
    rf"|{_ORG_WORD}(?:\s+{_ORG_WORD})?"
    r")",
    _I,
)

# Vendors always protected even without the catalog file on disk.
CORE_VENDOR_STEMS = (
    "касперск",
    "kaspersky",
    "1с",
    "1c",
    "софтлайн",
    "softline",
    "microsoft",
    "майкрософт",
    "р7",
    "мойофис",
    "аскон",
    "криптопро",
    "доктор веб",
    "код безопасности",
    "эшелон",
    "базальт",
    "группа астра",
    "ред софт",
    "яндекс",
)
_LEGAL_FORM_PREFIX_RE = re.compile(rf"^(?:(?:{'|'.join(LEGAL_FORMS)})\.?\s+)+", _I)
_QUOTES_RE = re.compile(r"[«»\"“”„']")


def _normalize_org_name(name: str) -> str:
    cleaned = _QUOTES_RE.sub(" ", name.casefold().replace("ё", "е"))
    cleaned = _LEGAL_FORM_PREFIX_RE.sub("", cleaned.strip())
    return re.sub(r"\s+", " ", cleaned).strip(" .,")


@functools.lru_cache(maxsize=4)
def catalog_vendor_names(catalog_path: Path = DEFAULT_CATALOG) -> frozenset[str]:
    """Normalised vendor names from the Softline catalog snapshot (empty if the file is absent)."""
    names: set[str] = set()
    if not catalog_path.is_file():
        return frozenset()
    for _, elem in ET.iterparse(catalog_path):
        if elem.tag == "offer":
            vendor = (elem.findtext("vendor") or "").strip()
            if vendor:
                norm = _normalize_org_name(vendor)
                if len(norm) >= 2:
                    names.add(norm)
            elem.clear()
    return frozenset(names)


def is_vendor_name(name: str, vendors: frozenset[str] | None = None) -> bool:
    """True when an organisation name refers to a Softline catalog vendor (e.g. «лаборатория касперского»)."""
    norm = _normalize_org_name(name)
    if not norm:
        return False
    if any(stem in norm for stem in CORE_VENDOR_STEMS):
        return True
    known = catalog_vendor_names() if vendors is None else vendors
    return norm in known


# ---------------------------------------------------------------------------
# Technical identifiers
# ---------------------------------------------------------------------------

IPV4_RE = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])"
)
VERSION_CONTEXT_RE = re.compile(
    r"(?:верси\w*|version|ver\.?|сборк\w*|build|релиз\w*|release|редакци\w*|обновлени\w*|патч\w*|patch|"
    r"\bv\.?)\s*[:№]?\s*$",
    _I,
)
SOFTWARE_WORD_RE = re.compile(
    r"(?:linux|линукс|windows|astra|астра|alt|альт|ред\s*ос|redos|ubuntu|debian|centos|kaspersky|касперск\w*|"
    r"ksc|kes|office|офис|1с|1c|сканер-вс|postgres\w*|postgrespro|java|jdk|vipnet|континент|secret\s*net|"
    r"криптопро|csp|firefox|chrome|exchange|mdaemon|server|сервер\w*)\s*$",
    _I,
)
EMAIL_RE = re.compile(r"(?<![\w.%+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w-])", _I)
PHONE_RE = re.compile(r"(?<![\d+])(?:\+7|8|7)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
REQUISITE_RE = re.compile(
    r"\b(?:инн|кпп|огрн|огрнип|бик|окпо|р/с|к/с|р\.\s?с\.|к\.\s?с\.|расч[её]тный\s+сч[её]т)\s*[:№]?\s*\d[\d\s-]{6,22}\d(?!\d)",
    _I,
)
LONG_ID_NUM_RE = re.compile(r"(?<!\d)\d{10,15}(?!\d)")
LICENSE_KEY_RE = re.compile(
    r"(?<![\w-])(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])[a-z0-9]{4,6}(?:-[a-z0-9]{4,6}){2,7}(?![\w-])",
    _I,
)
LICENSE_LABEL_RE = re.compile(
    r"(\b(?:номер\s+лицензи\w*|серийн\w*\s+номер\w*|ключ\w*\s+(?:лицензи\w*|активаци\w*|продукта)|"
    r"лицензи\w*\s*№|лицензионн\w*\s+ключ\w*|license\s+key|serial(?:\s+number)?)\s*[:№]?\s*)"
    r"[a-zа-яё0-9._/-]{5,}",
    _I,
)
ADDRESS_RE = re.compile(
    r"(?:\bг\.\s*[а-яё-]{3,},?\s*)?"
    r"\b(?:ул\.|улица|пр-т|пр-кт|проспект|пер\.|переулок|шоссе|бульвар|б-р|наб\.|набережная|пл\.|площадь)\s+"
    r"[а-яё0-9.\s/-]{3,45}",
    _I,
)


def _mask_ip(match: re.Match[str]) -> str:
    left = match.string[max(0, match.start() - 40) : match.start()]
    first_octet = int(match.group(0).split(".", 1)[0])
    if VERSION_CONTEXT_RE.search(left):
        return match.group(0)
    if first_octet < 10 and SOFTWARE_WORD_RE.search(left):
        return match.group(0)
    return "[ip]"


def _mask_org(match: re.Match[str]) -> str:
    return match.group(0) if is_vendor_name(match.group("name")) else "[организация]"


def _mask_intro(match: re.Match[str]) -> str:
    return f"{match.group(1)}[имя]"


def scrub_pii(text: str) -> str:
    """Mask residual personal and organisational PII before external transmission."""
    cleaned = EMAIL_RE.sub("[email]", text)
    cleaned = IPV4_RE.sub(_mask_ip, cleaned)
    cleaned = PHONE_RE.sub("[тел]", cleaned)
    cleaned = REQUISITE_RE.sub("[реквизит]", cleaned)
    cleaned = LICENSE_LABEL_RE.sub(r"\1[лицензия]", cleaned)
    cleaned = LICENSE_KEY_RE.sub("[лицензия]", cleaned)
    cleaned = LONG_ID_NUM_RE.sub("[номер]", cleaned)
    cleaned = ORG_RE.sub(_mask_org, cleaned)
    cleaned = ADDRESS_RE.sub("[адрес]", cleaned)
    cleaned = NAME_INTRO_RE.sub(_mask_intro, cleaned)
    cleaned = FIRST_NAME_RE.sub("[имя]", cleaned)
    cleaned = INITIALS_RE.sub("[имя]", cleaned)
    for _ in range(2):  # «[имя] [имя] отчество», «фамилия [имя] отчество»
        cleaned = NAME_WITH_PATRONYMIC_RE.sub("[имя]", cleaned)
        cleaned = NAME_THEN_SURNAME_RE.sub("[имя]", cleaned)
        cleaned = SURNAME_THEN_NAME_RE.sub("[имя]", cleaned)
        cleaned = re.sub(r"\[имя\](?:\s+\[имя\])+", "[имя]", cleaned)
    return cleaned
