"""Black-box acceptance evaluation runner against the T04 0.3.0 HTTP contract (stand T05)."""

from __future__ import annotations

import argparse
import http.cookiejar
import http.cookies
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eval.pii import scrub_pii
from eval.split_chats import default_holdout_dir

try:  # Declared in eval/requirements.txt; absence is a hard error unless --no-schema-validation is given.
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - exercised only in broken environments
    yaml = None
    Draft202012Validator = None
    FormatChecker = None

# Repo-anchored defaults: the runner (and pytest, whose root conftest chdirs into tmp_path) may start anywhere.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN_OPEN = REPO_ROOT / "eval/golden_open.json"
DEFAULT_SPLIT_SUMMARY = REPO_ROOT / "eval/split_summary.json"
DEFAULT_CATALOG = REPO_ROOT / "data/raw/Mindbox.yml.gz.xml"
DEFAULT_OPEN_OUT = Path(".local/eval_reports/eval_report")
DEFAULT_SETTLE_SECONDS = 5.0
EXPECTED_T04_VERSION = "0.3.0"
T04_OPENAPI_CANDIDATES = (
    REPO_ROOT / "contracts/t04/openapi.yaml",
    REPO_ROOT / "eval/t04_openapi.yaml",
)
SESSION_COOKIE_NAME = "support_session"
# T05 returns the 20 most recent LLM calls in /api/v1/manager/telemetry (recent_calls).
TELEMETRY_RECENT_CALLS_LIMIT = 20
# Turn-level assertions that tell the runner a turn is complete (Б1).
COMPLETION_KEYS = (
    "expect_handoff",
    "expected_offer_ids",
    "expected_quote_totals_kopecks",
    "expected_quote_revisions_kopecks",
)

# A currency-tagged number token. Groups of digits are joined by exactly one space/nbsp or one '.'/','.
# The token must not start right after a digit, comma or dot, so «41,227 тыс.» or «1,670.94» can never be
# matched from the middle. parse_amount_rub() then reads it strictly and reports ambiguity as an error.
PRICE_IN_TEXT_RE = re.compile(
    r"(?<![\d,.])"
    r"(?P<num>\d+(?:(?:[   ]|[.,])\d+)*)"
    r"(?:\s*(?P<mult>тыс\.?|млн\.?))?"
    r"\s*(?:₽|руб(?:л(?:ь|я|ей))?\.?|р\.|RUR|RUB)(?!\w)",
    re.IGNORECASE,
)
_SPACES = "   "
LEGAL_ENTITY_CAVEAT_RE = re.compile(
    r"(?:юр\s*лиц|юридическ\w*\s+лиц|только\s+для\s+(?:организац|компан|юр)|"
    r"для\s+физ\w*\s+не\s+подход|ограничен\w*|оператор|менеджер|специалист)",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
CONCRETE_TERM_RE = re.compile(
    r"\b(?:12\s*мес\w*|1\s*год\w*|на\s+(?:1\s+|один\s+)?год|годов\w*|бессрочн\w*|навсегда|пожизненн\w*)\b",
    re.IGNORECASE,
)
UNCERTAINTY_RE = re.compile(
    r"(?:не\s+определ|не\s+указан|неизвест|нет\s+данн|отсутству|уточн|"
    r"нельзя\s+утвержда|не\s+подтверж|специалист|оператор|менеджер|или\b|\?)",
    re.IGNORECASE,
)
ZERO_WORD_RE = re.compile(r"\bбесплатн\w*\b", re.IGNORECASE)
AVAILABILITY_CAVEAT_RE = re.compile(
    r"(?:недоступ|нет\s+в\s+наличи|под\s+заказ|провер\w*\s+доступ|уточн\w*\s+доступ|"
    r"подтвер\w*\s+постав|оператор|менеджер|требует\s+проверк)",
    re.IGNORECASE,
)
INSTANT_DELIVERY_RE = re.compile(
    r"(?:мгновенн\w*\s+постав|сразу\s+отгруз|доставим\s+сегодня|в\s+наличии\s+прямо\s+сейчас|"
    r"сч[её]т\s+уже\s+выставлен|можете\s+сразу\s+оплачивать)",
    re.IGNORECASE,
)
FAKE_STANDARD_CLAIM_RE = re.compile(
    r"(?:mdaemon\s+standard\s+(?:есть|доступ|стоит|в\s+каталоге)|"
    r"редакц\w*\s+standard\s+идентич|артикул\s+standard)",
    re.IGNORECASE,
)
DISCOUNT_PROMISE_RE = re.compile(
    r"(?:скидк\w*\s*50\s*%\s*(?:предоставлен|согласован|применен|оформлен|сдела)|"
    r"со\s+скидкой\s+50\s*%|предоставля\w*\s+скидк\w*\s+50)",
    re.IGNORECASE,
)
FAKE_REGISTRY_NUMBER_RE = re.compile(
    r"(?:запис\w*\s+в\s+реестре\s*(?:отечественного\s+по\s*)?(?:№|номер)?\s*\d{3,}|"
    r"реестр\w*\s*№\s*\d{3,})",
    re.IGNORECASE,
)
OPERATOR_HELP_OFFER_RE = re.compile(
    r"(?:(?:подключ\w*|переда\w*|позв\w*|обрат\w*|связ\w*).{0,35}(?:оператор|менеджер|специалист)|"
    r"(?:оператор|менеджер|специалист).{0,35}(?:помож|уточн|подключ))",
    re.IGNORECASE,
)


class PriceParseError(ValueError):
    """A currency amount in agent text that cannot be read unambiguously."""


class SchemaValidationUnavailableError(RuntimeError):
    """T04 schema validation was requested but PyYAML/jsonschema or the OpenAPI file is missing."""


@dataclass(frozen=True, slots=True)
class CatalogOffer:
    """Minimal catalog snapshot metadata used for price/availability assertions."""

    offer_id: str
    available: bool
    price_rub: Decimal
    license_term: str | None
    url: str
    name: str | None = None
    # Accepted spellings of the product name («SunRav BookOffice», «BookOffice»); empty = derived from name.
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class CostInfo:
    """LLM cost of one conversation as reported by the stand (never estimated by the runner)."""

    status: str  # "exact" | "unavailable"
    source: str | None = None  # "trace" | "telemetry"
    total_rub: float | None = None
    turns_rub: list[float] = field(default_factory=list)
    reason: str | None = None
    llm_calls: list[dict] | None = None
    trace_turns: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class CaseResult:
    """Outcome and telemetry collected for one evaluated scenario."""

    case_id: str
    suite: str
    category: str
    passed: bool
    escalated: bool
    closed_by_agent: bool
    timed_out: bool = False
    latency_ms: list[float] = field(default_factory=list)
    first_meaningful_latency_ms: float | None = None
    cost_rub: float | None = None
    cost_status: str = "unavailable"
    cost_source: str | None = None
    cost_reason: str | None = "no manager credentials (--manager-password / --session-cookie)"
    errors: list[str] = field(default_factory=list)
    agent_responses: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    events_count: int = 0
    turns: list[dict] = field(default_factory=list)
    public_id: str | None = None
    llm_calls: list[dict] | None = None


def default_holdout_file() -> Path:
    """Resolve holdout_eval.json from EVAL_HOLDOUT_FILE or the external holdout directory (no repo fallback)."""
    env_file = os.environ.get("EVAL_HOLDOUT_FILE")
    if env_file:
        return Path(env_file).expanduser()
    return default_holdout_dir() / "holdout_eval.json"


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


def _normalize_decimal(value: str | float | Decimal) -> Decimal:
    """Parse a reference amount from golden files («4900», «1670.94», «1 670,94»)."""
    text = str(value)
    for ch in _SPACES:
        text = text.replace(ch, "")
    try:
        return Decimal(text.replace(",", ".")).normalize()
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal amount: {value!r}") from exc


def _plain_int(part: str, raw: str) -> str:
    """Validate an integer part that may use space thousands grouping («1 523 654»)."""
    if any(ch in part for ch in _SPACES):
        if not re.fullmatch(r"\d{1,3}(?:[   ]\d{3})+", part):
            raise PriceParseError(f"ambiguous amount {raw!r}: irregular space grouping")
        for ch in _SPACES:
            part = part.replace(ch, "")
    if not part.isdigit():
        raise PriceParseError(f"ambiguous amount {raw!r}")
    return part


def parse_amount_rub(num: str, mult: str | None = None) -> Decimal:
    """Read one currency number strictly; raise PriceParseError instead of guessing or truncating.

    Accepted: «4 900», «4900,00», «1 670,94», «1.670,94», «1,670.94», «4.900» / «4,900» (thousands),
    «1.523.654», «41,2 тыс.», «41,227 тыс.» (= 41 227), «4,9 млн».
    Rejected: «1,670,94», «1.670.94», «12,3456», «1 670.940», «1 67 0», mixed groupings.
    """
    raw = num
    factor = Decimal(1)
    if mult:
        factor = Decimal(1000) if mult.lower().startswith("тыс") else Decimal(1_000_000)
    dots, commas = num.count("."), num.count(",")
    if dots and commas:
        dec_sep = "." if num.rfind(".") > num.rfind(",") else ","
        thou_sep = "," if dec_sep == "." else "."
        if num.count(dec_sep) != 1:
            raise PriceParseError(f"ambiguous amount {raw!r}: repeated decimal separator")
        int_part, frac = num.split(dec_sep)
        if any(ch in int_part for ch in _SPACES) or not re.fullmatch(
            rf"\d{{1,3}}(?:{re.escape(thou_sep)}\d{{3}})+", int_part
        ):
            raise PriceParseError(f"ambiguous amount {raw!r}: bad thousands grouping")
        if not (1 <= len(frac) <= 2 or (mult and frac.isdigit())):
            raise PriceParseError(f"ambiguous amount {raw!r}: bad fractional part")
        value = Decimal(f"{int_part.replace(thou_sep, '')}.{frac}")
    elif dots or commas:
        sep = "." if dots else ","
        count = dots or commas
        if count >= 2:
            if any(ch in num for ch in _SPACES) or not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+", num):
                raise PriceParseError(f"ambiguous amount {raw!r}: repeated separator")
            value = Decimal(num.replace(sep, ""))
        else:
            int_part, frac = num.split(sep)
            int_digits = _plain_int(int_part, raw)
            if 1 <= len(frac) <= 2 or (mult and len(frac) <= 3):
                value = Decimal(f"{int_digits}.{frac}")
            elif len(frac) == 3 and int_digits == int_part and len(int_digits) <= 3:
                value = Decimal(int_digits + frac)  # «4.900 ₽», «4,900 ₽»: thousands separator
            else:
                raise PriceParseError(f"ambiguous amount {raw!r}")
    else:
        value = Decimal(_plain_int(num, raw))
    return (value * factor).normalize()


def parse_prices_rub(text: str) -> tuple[list[Decimal], list[str]]:
    """Return (amounts, parse errors) for every currency-tagged number in text."""
    prices: list[Decimal] = []
    errors: list[str] = []
    for match in PRICE_IN_TEXT_RE.finditer(text):
        try:
            prices.append(parse_amount_rub(match.group("num"), match.group("mult")))
        except PriceParseError as exc:
            errors.append(f"Price parse error: {exc} in {match.group(0)!r}")
    return prices, errors


def extract_prices_rub(text: str) -> list[Decimal]:
    """Strict variant of parse_prices_rub(): raises PriceParseError on any ambiguous amount."""
    prices, errors = parse_prices_rub(text)
    if errors:
        raise PriceParseError("; ".join(errors))
    return prices


# ---------------------------------------------------------------------------
# T04 OpenAPI schema
# ---------------------------------------------------------------------------


class T04SchemaValidator:
    """JSON Schema validator backed by T04 OpenAPI components (same engine as contracts/t04/verify.py)."""

    def __init__(self, spec_path: Path | None = None, *, enabled: bool = True) -> None:
        self._components: dict | None = None
        self._validators: dict[str, object] = {}
        self.spec_path: Path | None = None
        self.spec_version: str | None = None
        self.enabled = enabled
        if not enabled:
            return
        if yaml is None or Draft202012Validator is None or FormatChecker is None:
            raise SchemaValidationUnavailableError(
                "PyYAML and jsonschema are required for T04 schema validation "
                "(pip install -r eval/requirements.txt) or pass --no-schema-validation explicitly"
            )
        candidates = [spec_path] if spec_path is not None else list(T04_OPENAPI_CANDIDATES)
        for cand in candidates:
            if cand is not None and cand.is_file():
                spec = yaml.safe_load(cand.read_text(encoding="utf-8"))
                self._components = spec.get("components")
                self.spec_version = str(spec.get("info", {}).get("version"))
                self.spec_path = cand
                break
        if self._components is None:
            raise SchemaValidationUnavailableError(
                f"T04 OpenAPI spec not found in {[str(c) for c in candidates]}; pass --no-schema-validation explicitly"
            )

    @property
    def active(self) -> bool:
        return self._components is not None

    def validate(self, schema_name: str, payload: object) -> list[str]:
        if not self.active or self._components is None:
            return []
        validator = self._validators.get(schema_name)
        if validator is None:
            schema = self._components["schemas"][schema_name]
            root = {"components": self._components, **schema}
            validator = Draft202012Validator(root, format_checker=FormatChecker())
            self._validators[schema_name] = validator
        errors: list[str] = []
        for err in validator.iter_errors(payload):  # type: ignore[attr-defined]
            loc = ".".join(str(part) for part in err.path) or "<root>"
            errors.append(f"OpenAPI {schema_name} schema violation at {loc}: {err.message}")
        return errors


_DEFAULT_SCHEMA_VALIDATOR: T04SchemaValidator | None = None


def get_schema_validator() -> T04SchemaValidator:
    """Return the cached T04 schema validator (raises SchemaValidationUnavailableError if it cannot be built)."""
    global _DEFAULT_SCHEMA_VALIDATOR
    if _DEFAULT_SCHEMA_VALIDATOR is None:
        _DEFAULT_SCHEMA_VALIDATOR = T04SchemaValidator()
    return _DEFAULT_SCHEMA_VALIDATOR


def set_schema_validator(validator: T04SchemaValidator) -> None:
    global _DEFAULT_SCHEMA_VALIDATOR
    _DEFAULT_SCHEMA_VALIDATOR = validator


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def load_catalog_index(
    golden_open_path: Path,
    catalog_path: Path | None = None,
    *,
    load_full_xml: bool = False,
) -> dict[str, CatalogOffer]:
    """Load reference SKU index from golden_open.json and optionally stream full XML."""
    index: dict[str, CatalogOffer] = {}
    if golden_open_path.is_file():
        data = json.loads(golden_open_path.read_text(encoding="utf-8"))
        for offer_id, item in data.get("reference_skus", {}).items():
            index[str(offer_id)] = CatalogOffer(
                offer_id=str(offer_id),
                available=bool(item["available"]),
                price_rub=_normalize_decimal(item["price_rub"]),
                license_term=item.get("contract_license_term"),
                url=str(item["url"]),
                name=item.get("name"),
                aliases=tuple(str(a) for a in item.get("aliases", [])),
            )

    if load_full_xml and catalog_path is not None and catalog_path.is_file():
        for _, elem in ET.iterparse(catalog_path):
            if elem.tag != "offer":
                continue
            oid = elem.get("id")
            if oid:
                raw_price = elem.findtext("price") or "0"
                term: str | None = None
                for param in elem.findall("param"):
                    if param.get("name") == "Срок действия":
                        raw_term = (param.text or "").strip()
                        term = None if raw_term in {"", "Не определено", "другое или отсутствует"} else raw_term
                        break
                index[oid] = CatalogOffer(
                    offer_id=oid,
                    available=(elem.get("available") == "true"),
                    price_rub=_normalize_decimal(raw_price),
                    license_term=term,
                    url=(elem.findtext("url") or "").strip(),
                    name=(elem.findtext("name") or "").strip() or None,
                    aliases=index[oid].aliases if oid in index else (),
                )
            elem.clear()
    return index


# ---------------------------------------------------------------------------
# Event helpers and validators
# ---------------------------------------------------------------------------


def _agent_messages(events: list[dict], *, purpose: str | None = None) -> list[dict]:
    result = []
    for event in events:
        data = event.get("data", {})
        is_agent = data.get("type") == "message" and data.get("author") == "agent"
        if is_agent and (purpose is None or data.get("purpose") == purpose):
            result.append(data)
    return result


def _products_from_events(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "products":
            items.extend(data.get("products", []))
    return items


def _handoffs_from_events(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "handoff" and isinstance(data.get("handoff"), dict):
            items.append(data["handoff"])
    return items


def _quotes_from_events(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "quote" and isinstance(data.get("quote"), dict):
            items.append(data["quote"])
    return items


def _documents_from_events(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "document" and isinstance(data.get("document"), dict):
            items.append(data["document"])
    return items


def _agent_text_prices(events: list[dict]) -> tuple[list[Decimal], list[str]]:
    prices: list[Decimal] = []
    errors: list[str] = []
    for msg in _agent_messages(events):
        found, errs = parse_prices_rub(str(msg.get("text", "")))
        prices.extend(found)
        errors.extend(errs)
    return prices, errors


def _surfaced_offer_ids(events: list[dict]) -> tuple[set[str], str]:
    ids = {str(p.get("offerId")) for p in _products_from_events(events)}
    for quote in _quotes_from_events(events):
        ids.update(str(line.get("offerId")) for line in quote.get("lines", []))
    text = "\n".join(str(m.get("text", "")) for m in _agent_messages(events))
    for message in _agent_messages(events):
        for source in message.get("sources", []):
            match = re.search(r"-(\d+)/?(?:[?#].*)?$", source.get("url", ""))
            if match:
                ids.add(match[1])
    return ids, text


def _offer_id_surfaced(oid: str, ids: set[str], text: str) -> bool:
    return oid in ids or bool(re.search(rf"(?<!\d){re.escape(oid)}(?!\d)", text))


def validate_contract_events(
    events: list[dict],
    conversation_id: str,
    schema_validator: T04SchemaValidator | None = None,
) -> list[str]:
    """Validate T04 OpenAPI schema and cross-event invariants on a collected journal."""
    errors: list[str] = []
    validator = schema_validator if schema_validator is not None else get_schema_validator()
    valid_types = {"message", "products", "handoff", "quote", "document", "action_failed"}
    for idx, event in enumerate(events, start=1):
        if validator.active:
            errors.extend(f"seq={event.get('seq')}: {msg}" for msg in validator.validate("Event", event))
        if event.get("seq") != idx:
            errors.append(f"Non-contiguous seq: expected {idx}, got {event.get('seq')!r}")
        if not event.get("at") or not event.get("actionId"):
            errors.append(f"Event seq={event.get('seq')} missing 'at' or 'actionId'")
        data = event.get("data")
        if not isinstance(data, dict) or data.get("type") not in valid_types:
            errors.append(f"Event seq={event.get('seq')} has invalid data.type")
            continue
        dtype = data["type"]
        if dtype == "message":
            for src in data.get("sources", []):
                if not str(src.get("url", "")).startswith("https://"):
                    errors.append(f"Message source URL must use https://, got {src.get('url')!r}")
        elif dtype == "products":
            for prod in data.get("products", []):
                url = str(prod.get("source", {}).get("url", ""))
                if not url.startswith("https://"):
                    errors.append(f"Product {prod.get('offerId')} source URL invalid: {url!r}")
                pstate = prod.get("priceState")
                avail = prod.get("available")
                kopecks = prod.get("unitPriceKopecks")
                if pstate == "listed" and (avail is not True or not isinstance(kopecks, int) or kopecks < 1):
                    errors.append(f"Product {prod.get('offerId')} violates listed invariant")
                elif pstate == "availability_check" and (
                    avail is not False or not isinstance(kopecks, int) or kopecks < 1
                ):
                    errors.append(f"Product {prod.get('offerId')} violates availability_check invariant")
                elif pstate == "on_request" and kopecks is not None:
                    errors.append(f"Product {prod.get('offerId')} violates on_request invariant")
        elif dtype == "handoff":
            handoff = data.get("handoff", {})
            if handoff.get("conversationId") != conversation_id:
                errors.append(
                    f"Handoff conversationId mismatch: {handoff.get('conversationId')!r} != {conversation_id!r}"
                )
        elif dtype == "quote":
            # Only the known subtotal is fixed by T04 (sum of priced lines). totalKopecks may include
            # taxes/terms, so the runner does not assume total == subtotal.
            quote = data.get("quote", {})
            lines = quote.get("lines", [])
            subtotal = sum(line.get("packages", 0) * (line.get("unitPriceKopecks") or 0) for line in lines)
            if subtotal != quote.get("knownSubtotalKopecks"):
                errors.append(
                    f"Quote {quote.get('quoteId')} subtotal {quote.get('knownSubtotalKopecks')} != {subtotal}"
                )
    return errors


def validate_independent_cursors(
    incremental_events: list[dict],
    replay_events: list[dict],
    replay_next_after: int,
    min_replay_len: int | None = None,
) -> list[str]:
    """A second reader starting at after=0 must see the incremental history.

    The replay is read after the incremental reader drained the journal (min_replay_len events), and the
    incremental reader then reads the tail once more, so the replay must be a prefix of the final history
    that is at least min_replay_len long.
    """
    errors: list[str] = []
    min_len = len(incremental_events) if min_replay_len is None else min_replay_len
    if (
        len(replay_events) < min_len
        or len(replay_events) > len(incremental_events)
        or replay_events != incremental_events[: len(replay_events)]
    ):
        errors.append(
            f"Independent after=0 replay diverged from incremental reads "
            f"({len(replay_events)} vs {len(incremental_events)} events)"
        )
    if replay_next_after != len(replay_events):
        errors.append(f"Independent replay nextAfter={replay_next_after} != {len(replay_events)}")
    return errors


def validate_no_hallucinated_prices(
    events: list[dict],
    allowed_prices_rub: list[str | int | float] | None,
    catalog_index: dict[str, CatalogOffer] | None = None,
) -> list[str]:
    """Check all text prices, Product prices, and Quote totals against ground truth."""
    errors: list[str] = []
    allowed_set = {_normalize_decimal(v) for v in allowed_prices_rub} if allowed_prices_rub is not None else None
    catalog = catalog_index or {}

    text_prices, _ = _agent_text_prices(events)
    known_prices = {offer.price_rub for offer in catalog.values() if offer.price_rub > 0}
    for price in text_prices:
        if allowed_set is not None:
            if price not in allowed_set:
                errors.append(f"Hallucinated or disallowed price in agent text: {price:f} RUB")
        elif catalog and price not in known_prices:
            errors.append(f"Price {price:f} RUB in agent text not found in catalog index")

    for prod in _products_from_events(events):
        oid = str(prod.get("offerId", ""))
        kopecks = prod.get("unitPriceKopecks")
        if kopecks is None:
            continue
        price_rub = (Decimal(kopecks) / Decimal(100)).normalize()
        if oid in catalog and catalog[oid].price_rub != price_rub:
            errors.append(f"Product {oid} price mismatch: {price_rub:f} != XML {catalog[oid].price_rub:f}")
        if allowed_set is not None and price_rub not in allowed_set:
            errors.append(f"Product {oid} price {price_rub:f} not in allowed prices")

    for quote in _quotes_from_events(events):
        for line in quote.get("lines", []):
            oid = str(line.get("offerId", ""))
            kopecks = line.get("unitPriceKopecks")
            if kopecks is None:
                continue
            price_rub = (Decimal(kopecks) / Decimal(100)).normalize()
            if oid in catalog and catalog[oid].price_rub != price_rub:
                errors.append(f"Quote line {oid} price mismatch: {price_rub:f} != XML {catalog[oid].price_rub:f}")
        total_kopecks = quote.get("totalKopecks")
        if total_kopecks is not None and allowed_set is not None:
            total_rub = (Decimal(total_kopecks) / Decimal(100)).normalize()
            if total_rub not in allowed_set:
                errors.append(f"Quote total {total_rub:f} RUB not in allowed prices")
    return errors


def validate_anti_term_hallucination(events: list[dict], offer_id: str = "61557") -> list[str]:
    """Verify SKU 61557 does not get a fabricated license duration."""
    errors: list[str] = []
    for prod in _products_from_events(events):
        if str(prod.get("offerId")) == offer_id and prod.get("licenseTerm") is not None:
            errors.append(f"Product {offer_id} has fabricated licenseTerm={prod.get('licenseTerm')!r}, expected null")

    for msg in _agent_messages(events):
        text = msg.get("text", "")
        for sentence in SENTENCE_SPLIT_RE.split(text):
            if CONCRETE_TERM_RE.search(sentence) and not UNCERTAINTY_RE.search(sentence):
                errors.append(f"Hallucinated license term for {offer_id} in sentence: {sentence.strip()!r}")
    return errors


def validate_anti_zero_price(events: list[dict], offer_id: str = "504") -> list[str]:
    """Verify zero-price SKU 504 never displays 0 RUB and escalates/marks on_request."""
    errors: list[str] = []
    for msg in _agent_messages(events):
        text = msg.get("text", "")
        prices, _ = parse_prices_rub(text)
        if any(price == Decimal(0) for price in prices) or ZERO_WORD_RE.search(text):
            errors.append(f"Zero/free price leaked for {offer_id} in agent text")

    matched_products = [p for p in _products_from_events(events) if str(p.get("offerId")) == offer_id]
    for prod in matched_products:
        if prod.get("priceState") != "on_request":
            errors.append(f"Product {offer_id} must have priceState='on_request', got {prod.get('priceState')!r}")
        if prod.get("unitPriceKopecks") is not None:
            errors.append(f"Product {offer_id} must have unitPriceKopecks=null, got {prod.get('unitPriceKopecks')!r}")
        if prod.get("available") is not False:
            errors.append(f"Product {offer_id} must have available=false")

    for quote in _quotes_from_events(events):
        for line in quote.get("lines", []):
            if str(line.get("offerId")) == offer_id and line.get("unitPriceKopecks") == 0:
                errors.append(f"Quote line {offer_id} leaked 0 kopecks")

    handoffs = _handoffs_from_events(events)
    if not handoffs:
        errors.append(f"Zero-price SKU {offer_id} must trigger a handoff to operator")
    elif not any(h.get("reason") in {"price_request", "availability"} for h in handoffs):
        errors.append(f"SKU {offer_id} handoff must have reason price_request/availability")
    return errors


def validate_unavailable_with_price(events: list[dict], offer_id: str = "48093") -> list[str]:
    """Verify unavailable SKU 48093 warns about availability, escalates, and blocks auto-invoice."""
    errors: list[str] = []
    for prod in _products_from_events(events):
        if str(prod.get("offerId")) != offer_id:
            continue
        if prod.get("available") is not False or prod.get("priceState") != "availability_check":
            errors.append(
                f"Product {offer_id} must have available=false and priceState='availability_check', "
                f"got available={prod.get('available')!r}, priceState={prod.get('priceState')!r}"
            )

    agent_text = "\n".join(msg.get("text", "") for msg in _agent_messages(events))
    if not AVAILABILITY_CAVEAT_RE.search(agent_text):
        errors.append(f"Agent response for unavailable SKU {offer_id} lacks availability caveat")
    if INSTANT_DELIVERY_RE.search(agent_text):
        errors.append(f"Agent promised instant delivery/invoice for unavailable SKU {offer_id}")

    if _documents_from_events(events):
        errors.append(f"Document issuance must be blocked when SKU {offer_id} has available=false")
    for quote in _quotes_from_events(events):
        if quote.get("state") in {"ready", "confirmed"}:
            errors.append(f"Quote state {quote.get('state')!r} must be blocked for unavailable SKU {offer_id}")

    if not _handoffs_from_events(events):
        errors.append(f"Unavailable SKU {offer_id} must trigger availability handoff")
    return errors


def validate_sources_present(events: list[dict], required_urls: list[str] | None = None) -> list[str]:
    """Verify factual agent answers carry source_ref URLs and required links."""
    errors: list[str] = []
    product_urls_by_action: dict[str, set[str]] = {}
    all_urls: set[str] = set()

    for event in events:
        data = event.get("data", {})
        if data.get("type") == "products":
            urls = {str(p.get("source", {}).get("url", "")) for p in data.get("products", [])}
            urls.discard("")
            product_urls_by_action.setdefault(str(event.get("actionId")), set()).update(urls)
            all_urls.update(urls)
        elif data.get("type") == "quote":
            for line in data.get("quote", {}).get("lines", []):
                url = str(line.get("source", {}).get("url", ""))
                if url:
                    all_urls.add(url)
        elif data.get("type") == "message" and data.get("author") == "agent":
            for src in data.get("sources", []):
                url = str(src.get("url", ""))
                if url:
                    all_urls.add(url)

    for event in events:
        data = event.get("data", {})
        if data.get("type") == "message" and data.get("author") == "agent" and data.get("purpose") == "answer":
            msg_urls = [str(s.get("url", "")) for s in data.get("sources", []) if s.get("url")]
            action_prod_urls = product_urls_by_action.get(str(event.get("actionId")), set())
            if not msg_urls and not action_prod_urls:
                errors.append(f"Agent answer seq={event.get('seq')} has no source URLs")

    if required_urls:
        agent_text = "\n".join(msg.get("text", "") for msg in _agent_messages(events))
        for req_url in required_urls:
            if req_url not in all_urls and req_url not in agent_text:
                errors.append(f"Missing required source URL: {req_url}")
    return errors


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_NAME_NOISE_RE = re.compile(r"[^\w]+|_+")
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{2,}")
# Words of a catalog name that do not identify the product (vendor legal forms, license wording).
_GENERIC_NAME_TOKENS = frozenset(
    {
        "software",
        "systems",
        "technologies",
        "technology",
        "ltd",
        "inc",
        "corp",
        "llc",
        "gmbh",
        "лицензия",
        "лицензии",
        "версия",
        "подписка",
        "для",
        "на",
    }
)
# Latin words that may sit next to an article in a correct answer without naming another product.
_NEUTRAL_LATIN_WORDS = frozenset(
    {"softline", "rub", "rur", "pdf", "url", "https", "http", "www", "store", "sku", "offerid", "vat", "nds"}
)


def _norm_name(text: str) -> str:
    """Case/ё-insensitive form with punctuation folded to single spaces («Dr.Web» → «dr web»)."""
    return " ".join(_NAME_NOISE_RE.sub(" ", text.casefold().replace("ё", "е")).split())


def offer_name_variants(offer: CatalogOffer) -> list[str]:
    """Normalized accepted spellings of a catalog product name.

    Explicit aliases from golden_open.json win; otherwise the distinctive words before «(» or «,» of the
    catalog name are used (vendor legal forms and license wording are not distinctive).
    """
    variants: list[str] = [_norm_name(a) for a in offer.aliases]
    if offer.name:
        variants.append(_norm_name(offer.name))
        core = re.split(r"[(,]", offer.name, maxsplit=1)[0]
        variants.append(_norm_name(core))
        if not offer.aliases:
            variants.extend(
                tok for tok in _norm_name(core).split() if len(tok) >= 3 and tok not in _GENERIC_NAME_TOKENS
            )
    seen: set[str] = set()
    return [v for v in variants if v and not (v in seen or seen.add(v))]


def _names_product(text: str, variants: list[str]) -> bool:
    """A variant occurs in text from a word start; inflected endings («Касперского») are allowed."""
    haystack = f" {_norm_name(text)}"
    return any(f" {variant}" in haystack for variant in variants)


def _offer_name_tokens(offer: CatalogOffer) -> set[str]:
    tokens: set[str] = set()
    for variant in [*(_norm_name(a) for a in offer.aliases), _norm_name(offer.name or "")]:
        tokens.update(variant.split())
    return tokens


def validate_product_names(
    events: list[dict],
    expected_offer_ids: list[str] | None,
    catalog_index: dict[str, CatalogOffer] | None,
) -> list[str]:
    """Catch a correct article/price/link attached to the wrong product name (P2, PR #8).

    * Product cards and quote lines of catalog SKUs must carry that SKU's product name.
    * Agent text that cites an expected offerId must not name another product on that line, and, unless a
      card or quote line for that offerId accompanies it, must name the expected product in the message.
    """
    errors: list[str] = []
    catalog = catalog_index or {}
    named = {oid: offer for oid, offer in catalog.items() if offer.name}

    carded: set[str] = set()
    items = [("Product", p) for p in _products_from_events(events)]
    items += [("Quote line", line) for q in _quotes_from_events(events) for line in q.get("lines", [])]
    for kind, item in items:
        oid = str(item.get("offerId", ""))
        offer = named.get(oid)
        if offer is None:
            continue
        name = str(item.get("name", ""))
        if not _names_product(name, offer_name_variants(offer)):
            errors.append(f"{kind} {oid} name {name!r} does not match catalog product {offer.name!r}")
        else:
            carded.add(oid)

    for oid in [str(o) for o in (expected_offer_ids or [])]:
        offer = named.get(oid)
        if offer is None:
            continue
        variants = offer_name_variants(offer)
        allowed_tokens = _offer_name_tokens(offer) | _NEUTRAL_LATIN_WORDS
        oid_re = re.compile(rf"(?<!\d){re.escape(oid)}(?!\d)")
        for msg in _agent_messages(events):
            if msg.get("purpose") == "greeting":
                continue
            raw_text = str(msg.get("text", ""))
            source_urls = " ".join(str(s.get("url", "")) for s in msg.get("sources", []) if isinstance(s, dict))
            # Match the same SKU identification rule as _offer_id_surfaced, including URLs in text or sources.
            cites_in_raw = bool(oid_re.search(raw_text))
            cites_in_sources = bool(oid_re.search(source_urls))
            if not (cites_in_raw or cites_in_sources):
                continue
            text = _URL_RE.sub(" ", raw_text)
            foreign_found = False
            for raw_line in raw_text.splitlines():
                clean_line = _URL_RE.sub(" ", raw_line)
                if (cites_in_raw and not oid_re.search(raw_line)) or _names_product(clean_line, variants):
                    continue
                foreign = [w for w in _LATIN_WORD_RE.findall(clean_line) if _norm_name(w) not in allowed_tokens]
                if foreign:
                    foreign_found = True
                    errors.append(
                        f"Agent text cites offerId {oid} with another product name {' '.join(foreign)!r}, "
                        f"catalog product is {offer.name!r}"
                    )
            if not foreign_found and oid not in carded and not _names_product(text, variants):
                errors.append(
                    f"Agent text cites offerId {oid} without its product name (catalog: {offer.name!r}) "
                    "and no product card confirms it"
                )
    return errors


_PRICE_ASSERTION_KEYS = (
    "allowed_prices_rub",
    "required_prices_rub",
    "check_catalog_prices",
    "forbid_zero_price",
    "anti_zero_price_offer_id",
)


def _quote_events_from_history(events: list[dict]) -> list[tuple[str | None, dict]]:
    items: list[tuple[str | None, dict]] = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "quote" and isinstance(data.get("quote"), dict):
            aid = event.get("actionId")
            items.append((str(aid) if aid is not None else None, data["quote"]))
    return items


def validate_quote_revisions(
    events: list[dict],
    *,
    expected_revisions_kopecks: list[int] | None = None,
    expected_steps: list[dict] | None = None,
    turn_action_ids: dict[int, str] | None = None,
) -> list[str]:
    """Verify quote recalculation keeps the same quoteId, strictly increases revision, and matches turn/lines/total."""
    steps: list[dict] = []
    if expected_steps:
        steps = list(expected_steps)
    elif expected_revisions_kopecks:
        steps = [{"total_kopecks": total} for total in expected_revisions_kopecks]
    if not steps:
        return []

    quote_events = _quote_events_from_history(events)
    by_quote_id: dict[str, list[tuple[str | None, dict]]] = {}
    for aid, quote in quote_events:
        qid = quote.get("quoteId")
        if isinstance(qid, str) and qid.strip():
            by_quote_id.setdefault(qid, []).append((aid, quote))

    for chain in by_quote_id.values():
        step_idx = 0
        prev_rev: int | None = None
        for aid, quote in chain:
            rev = quote.get("revision")
            if isinstance(rev, bool) or not isinstance(rev, int):
                continue
            step = steps[step_idx]
            if quote.get("totalKopecks") != step.get("total_kopecks"):
                continue
            if prev_rev is not None and rev <= prev_rev:
                continue
            req_offer_ids = step.get("offer_ids")
            if req_offer_ids:
                line_ids = {str(line.get("offerId")) for line in quote.get("lines", [])}
                if not {str(oid) for oid in req_offer_ids}.issubset(line_ids):
                    continue
            req_turn = step.get("turn")
            if req_turn is not None and turn_action_ids and int(req_turn) in turn_action_ids:
                if aid != turn_action_ids[int(req_turn)]:
                    continue
            prev_rev = rev
            step_idx += 1
            if step_idx == len(steps):
                return []

    observed = [
        (
            q.get("quoteId"),
            q.get("revision"),
            q.get("totalKopecks"),
            [str(line.get("offerId")) for line in q.get("lines", [])],
        )
        for _, q in quote_events
    ]
    return [
        f"Expected quote recalculation sequence {steps} on one quoteId with strictly increasing revision; "
        f"observed {observed}"
    ]


def validate_case_expectations(
    events: list[dict],
    assertions: dict,
    catalog_index: dict[str, CatalogOffer] | None = None,
    *,
    turn_action_ids: dict[int, str] | None = None,
) -> list[str]:
    """Run all configured validators for a turn or full conversation."""
    errors: list[str] = []
    if not assertions:
        return errors

    text_prices, price_parse_errors = _agent_text_prices(events)
    if any(key in assertions for key in _PRICE_ASSERTION_KEYS):
        errors.extend(price_parse_errors)

    allowed_prices = assertions.get("allowed_prices_rub")
    if allowed_prices is not None or assertions.get("check_catalog_prices"):
        errors.extend(validate_no_hallucinated_prices(events, allowed_prices, catalog_index))

    if assertions.get("anti_term_hallucination_offer_id"):
        errors.extend(validate_anti_term_hallucination(events, str(assertions["anti_term_hallucination_offer_id"])))

    if assertions.get("anti_zero_price_offer_id"):
        errors.extend(validate_anti_zero_price(events, str(assertions["anti_zero_price_offer_id"])))
    elif assertions.get("forbid_zero_price") and any(price == Decimal(0) for price in text_prices):
        errors.append("Zero price (0 RUB) leaked in agent text")

    if assertions.get("unavailable_with_price_offer_id"):
        errors.extend(validate_unavailable_with_price(events, str(assertions["unavailable_with_price_offer_id"])))

    if assertions.get("require_sources_on_answer") or assertions.get("required_urls"):
        errors.extend(validate_sources_present(events, assertions.get("required_urls")))

    agent_msgs = _agent_messages(events)
    agent_text = "\n".join(m.get("text", "") for m in agent_msgs)
    observed_prices = set(text_prices)
    for prod in _products_from_events(events):
        if prod.get("unitPriceKopecks") is not None:
            observed_prices.add((Decimal(prod["unitPriceKopecks"]) / Decimal(100)).normalize())
    for quote in _quotes_from_events(events):
        if quote.get("totalKopecks") is not None:
            observed_prices.add((Decimal(quote["totalKopecks"]) / Decimal(100)).normalize())

    for req_price in assertions.get("required_prices_rub", []):
        norm_req = _normalize_decimal(req_price)
        if norm_req not in observed_prices:
            errors.append(f"Expected price {norm_req:f} RUB not found in response")

    if assertions.get("expected_offer_ids") or assertions.get("check_catalog_prices"):
        errors.extend(validate_product_names(events, assertions.get("expected_offer_ids"), catalog_index))

    surfaced_ids, surfaced_text = _surfaced_offer_ids(events)
    for exp_oid in assertions.get("expected_offer_ids", []):
        if not _offer_id_surfaced(str(exp_oid), surfaced_ids, surfaced_text):
            errors.append(f"Expected offerId {exp_oid} not surfaced in products, quotes, or text")

    if assertions.get("require_unknown_term") and not any(
        re.search(r"срок|длительность|период действия", sentence, re.I)
        and re.search(r"не указан|не определ|неизвест|нет данных|уточн|не подтверж|не удалось", sentence, re.I)
        for sentence in SENTENCE_SPLIT_RE.split(agent_text)
    ):
        errors.append("Expected an explicit answer that the license term needs confirmation")

    has_clarification = any(m.get("purpose") == "clarification" for m in agent_msgs)
    if assertions.get("expect_clarification") and not has_clarification:
        errors.append("Expected clarifying question with purpose='clarification' from agent")

    if assertions.get("expect_operator_help_offer") and not OPERATOR_HELP_OFFER_RE.search(agent_text):
        errors.append("Expected agent to offer connecting an operator/specialist")

    for pattern in assertions.get("expected_text_patterns", []):
        if not re.search(str(pattern), agent_text, re.IGNORECASE):
            errors.append(f"Expected pattern {pattern!r} not found in agent text")

    has_caveat = bool(LEGAL_ENTITY_CAVEAT_RE.search(agent_text)) or bool(_handoffs_from_events(events))
    if assertions.get("expect_legal_entity_caveat") and not has_caveat:
        errors.append("Expected legal-entity restriction caveat or operator handoff for individual buyer")

    if assertions.get("forbid_standard_sku_claim") and FAKE_STANDARD_CLAIM_RE.search(agent_text):
        errors.append("Agent falsely claimed a literal 'MDaemon Standard' SKU exists")

    if assertions.get("forbid_discount_promise") and DISCOUNT_PROMISE_RE.search(agent_text):
        errors.append("Agent promised unauthorized 50% discount")

    if assertions.get("forbid_fake_registry_number") and FAKE_REGISTRY_NUMBER_RE.search(agent_text):
        errors.append("Agent invented a Russian software registry number")

    if assertions.get("forbid_document") and _documents_from_events(events):
        errors.append("Unexpected document event issued")

    handoffs = _handoffs_from_events(events)
    if assertions.get("forbid_handoff") and handoffs:
        errors.append(f"Unexpected handoff event(s): {[h.get('handoffId') for h in handoffs]}")

    if "expect_unique_open_handoff_count" in assertions:
        latest = {h["handoffId"]: h for h in handoffs if h.get("handoffId")}
        handoffs = [h for h in latest.values() if h.get("status") == "open"]

    if assertions.get("expect_handoff"):
        if not handoffs:
            errors.append("Expected handoff event to operator")
        else:
            allowed_reasons = set(assertions.get("allowed_handoff_reasons", []))
            if allowed_reasons and not any(h.get("reason") in allowed_reasons for h in handoffs):
                errors.append(f"Handoff reason {[h.get('reason') for h in handoffs]} not in {sorted(allowed_reasons)}")
            if assertions.get("expect_empty_handoff_products") and not any(h.get("products") == [] for h in handoffs):
                errors.append("Expected handoff with empty products=[] on immediate operator request")
            for exp_prod in assertions.get("expected_handoff_products", []):
                exp_oid = str(exp_prod["offer_id"])
                exp_kopecks = exp_prod.get("unit_price_kopecks")
                exp_url = exp_prod.get("url")
                matched_prod = any(
                    str(p.get("offerId")) == exp_oid
                    and (exp_kopecks is None or p.get("unitPriceKopecks") == exp_kopecks)
                    and (exp_url is None or (p.get("source") or {}).get("url") == exp_url)
                    for h in handoffs
                    for p in h.get("products", [])
                    if isinstance(p, dict)
                )
                if not matched_prod:
                    errors.append(
                        f"Expected handoff product offerId={exp_oid} "
                        f"(price={exp_kopecks}, url={exp_url}) not found in handoff.products"
                    )
            for pattern in assertions.get("expected_handoff_question_patterns", []):
                if not any(re.search(str(pattern), str(h.get("question", "")), re.IGNORECASE) for h in handoffs):
                    questions = [h.get("question") for h in handoffs]
                    errors.append(f"Expected handoff question matching {pattern!r}, got {questions}")

    if "expect_unique_open_handoff_count" in assertions:
        latest_status: dict[str, str] = {}
        for h in handoffs:
            hid = str(h.get("handoffId", ""))
            if hid:
                latest_status[hid] = str(h.get("status", ""))
        open_ids = [hid for hid, st in latest_status.items() if st == "open"]
        expected_cnt = int(assertions["expect_unique_open_handoff_count"])
        if len(open_ids) != expected_cnt:
            errors.append(f"Expected {expected_cnt} unique open handoff(s), got {len(open_ids)} ({open_ids})")

    quotes = _quotes_from_events(events)
    quote_totals = [q.get("totalKopecks") for q in quotes]
    for exp in assertions.get("expected_quote_totals_kopecks", []):
        if exp not in quote_totals:
            errors.append(f"Expected quote totalKopecks={exp} not found in {quote_totals}")

    expected_line_ids = [str(oid) for oid in assertions.get("expected_quote_line_offer_ids", [])]
    if expected_line_ids:
        req_ids = set(expected_line_ids)
        req_totals = set(assertions.get("expected_quote_totals_kopecks", []))
        matched_line_quote = any(
            req_ids.issubset({str(line.get("offerId")) for line in q.get("lines", [])})
            and (not req_totals or q.get("totalKopecks") in req_totals)
            for q in quotes
        )
        if not matched_line_quote:
            errors.append(f"Expected quote with line offerIds {sorted(req_ids)} not found")

    errors.extend(
        validate_quote_revisions(
            events,
            expected_revisions_kopecks=assertions.get("expected_quote_revisions_kopecks"),
            expected_steps=assertions.get("expected_quote_revision_steps"),
            turn_action_ids=turn_action_ids,
        )
    )

    return errors


# ---------------------------------------------------------------------------
# Turn completion (Б1)
# ---------------------------------------------------------------------------


def _is_meaningful_event(event: dict) -> bool:
    """First useful reaction for latency: anything but the customer echo, greetings and action_failed.

    action_failed closes the turn but is a failure, not a reaction, so it never enters the latency sample.
    """
    data = event.get("data", {})
    dtype = data.get("type")
    if dtype in {"products", "handoff", "quote", "document"}:
        return True
    if dtype == "message":
        return data.get("author") in {"agent", "manager"} and data.get("purpose") in {"answer", "clarification"}
    return False


def _turn_has_response(action_events: list[dict]) -> bool:
    """The action produced a reply: agent/manager text, quote, handoff, document or action_failed.

    quote and handoff count for every action type, including ``message`` (T04 c-docs: input → quote → handoff).
    A bare ``products`` card is not a reply: T04 c-price sends the card before the answer text.
    """
    for event in action_events:
        data = event.get("data", {})
        dtype = data.get("type")
        if dtype in {"quote", "handoff", "document", "action_failed"}:
            return True
        if dtype == "message" and _is_meaningful_event(event):
            return True
    return False


def turn_expectations_met(action_events: list[dict], assertions: dict) -> bool | None:
    """True/False when the turn declares completion expectations, None when it declares none."""
    keys = [key for key in COMPLETION_KEYS if assertions.get(key)]
    if not keys:
        return None
    if assertions.get("expect_handoff") and not _handoffs_from_events(action_events):
        return False
    if assertions.get("expected_offer_ids"):
        ids, text = _surfaced_offer_ids(action_events)
        if not all(_offer_id_surfaced(str(oid), ids, text) for oid in assertions["expected_offer_ids"]):
            return False
    totals = [q.get("totalKopecks") for q in _quotes_from_events(action_events)]
    for key in ("expected_quote_totals_kopecks", "expected_quote_revisions_kopecks"):
        if assertions.get(key) and not all(exp in totals for exp in assertions[key]):
            return False
    return True


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------


def _split_base(base_url: str) -> tuple[str, str]:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/api/v1"):
        return cleaned[: -len("/api/v1")] or cleaned, cleaned
    return cleaned, f"{cleaned}/api/v1"


def _http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, object, dict[str, str], list[str]]:
    """Return (status, json payload, lower-cased headers, raw Set-Cookie values)."""
    req_headers = {"Accept": "application/json", **(headers or {})}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            cookies = resp.headers.get_all("Set-Cookie") or []
            return (
                resp.status,
                (json.loads(raw) if raw else {}),
                {k.lower(): v for k, v in resp.headers.items()},
                cookies,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: object = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        cookies = exc.headers.get_all("Set-Cookie") or []
        return exc.code, payload, {k.lower(): v for k, v in exc.headers.items()}, cookies


class T04HttpClient:
    """Customer-side T04 0.3.0 client. The stand issues the customer support_session on createConversation."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        *,
        schema_validator: T04SchemaValidator | None = None,
        retry_budget_s: float | None = None,
    ) -> None:
        self.root_base, self.api_base = _split_base(base_url)
        self.timeout = timeout
        self.retry_budget_s = timeout if retry_budget_s is None else retry_budget_s
        self.schema_validator = schema_validator
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.rejections: list[str] = []

    def _validator(self) -> T04SchemaValidator:
        return self.schema_validator if self.schema_validator is not None else get_schema_validator()

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else None
        status, payload, resp_headers, _ = _http(
            method, url, body=body, headers=headers, timeout=self.timeout, opener=self.opener
        )
        return status, payload if isinstance(payload, dict) else {"raw": payload}, resp_headers

    def _post_with_retry(self, url: str, body: dict, key: str) -> tuple[int, dict, dict[str, str]]:
        """POST with T04 0.3.0 pre-acceptance rejections handled: 429 rate_limited and 409 agent_busy.

        Rejected commands do not occupy the key, so the same Idempotency-Key is reused on retry.
        """
        deadline = time.perf_counter() + self.retry_budget_s
        while True:
            status, payload, headers = self._request("POST", url, body=body, idempotency_key=key)
            code = payload.get("code")
            retryable = (status == 429 and code == "rate_limited") or (status == 409 and code == "agent_busy")
            if not retryable:
                return status, payload, headers
            validator = self._validator()
            if validator.active:
                schema_errs = validator.validate("Error", payload)
                if schema_errs:
                    raise RuntimeError("; ".join(schema_errs[:3]))
            self.rejections.append(str(code))
            delay = 0.5
            if status == 429:
                try:
                    delay = max(1.0, float(headers.get("retry-after", "1")))
                except ValueError:
                    delay = 1.0
            if time.perf_counter() + delay > deadline:
                raise RuntimeError(f"{code}: retry budget {self.retry_budget_s}s exhausted for {url}")
            time.sleep(delay)

    def create_conversation(self, key: str) -> str:
        status, payload, _ = self._post_with_retry(f"{self.api_base}/conversations", {}, key)
        if status != 201 or "conversationId" not in payload:
            raise RuntimeError(f"createConversation failed: HTTP {status} {payload}")
        return str(payload["conversationId"])

    def submit_action(self, conversation_id: str, body: dict, key: str) -> tuple[int, dict, dict[str, str]]:
        url = f"{self.api_base}/conversations/{urllib.parse.quote(conversation_id)}/actions"
        status, payload, headers = self._post_with_retry(url, body, key)
        if status not in (200, 202):
            raise RuntimeError(f"submitAction failed: HTTP {status} {payload}")
        return status, payload, headers

    def read_events(self, conversation_id: str, after: int = 0) -> dict:
        url = f"{self.api_base}/conversations/{urllib.parse.quote(conversation_id)}/events?after={after}"
        deadline = time.perf_counter() + self.retry_budget_s
        while True:
            status, payload, headers = self._request("GET", url)
            if status == 429 and payload.get("code") == "rate_limited":
                try:
                    delay = max(1.0, float(headers.get("retry-after", "1")))
                except ValueError:
                    delay = 1.0
                if time.perf_counter() + delay > deadline:
                    raise RuntimeError("readEvents: rate_limited beyond retry budget")
                time.sleep(delay)
                continue
            if status != 200:
                raise RuntimeError(f"readEvents failed: HTTP {status} {payload}")
            return payload

    def read_all_from(
        self,
        conversation_id: str,
        after: int = 0,
        schema_validator: T04SchemaValidator | None = None,
    ) -> tuple[list[dict], int]:
        validator = schema_validator if schema_validator is not None else self._validator()
        cursor = after
        collected: list[dict] = []
        while True:
            page = self.read_events(conversation_id, after=cursor)
            if validator.active:
                schema_errs = validator.validate("EventPage", page)
                if schema_errs:
                    raise RuntimeError("; ".join(schema_errs[:3]))
            page_events = page.get("events", [])
            next_after = int(page.get("nextAfter", cursor))
            expected_next = cursor + len(page_events)
            if next_after != expected_next:
                raise RuntimeError(f"Invalid nextAfter={next_after}, expected {expected_next}")
            collected.extend(page_events)
            cursor = next_after
            if not page.get("hasMore"):
                break
            if not page_events:
                raise RuntimeError("hasMore=true returned empty events page")
        return collected, cursor


class ManagerCostClient:
    """Manager session on stand T05 used only to read the exact LLM cost of eval conversations.

    Sources (T05 extensions listed in T04 0.3.0 README):
    1. GET /api/v1/manager/conversations/{id}/trace — per-turn total_cost_rub. T05 grants managers access
       only to conversations with a handoff or a pilot draft, otherwise 404.
    2. GET /api/v1/manager/telemetry — recent_calls (last 20) with session_id and cost_rub. Used only when the
       window provably covers the whole case: it overlaps the call ids seen before the case started.
    Otherwise the cost is reported as unavailable; nothing is estimated or double-counted.
    """

    def __init__(
        self,
        base_url: str,
        *,
        session_cookie: str | None = None,
        password: str | None = None,
        username: str = "manager",
        timeout: float = 30.0,
    ) -> None:
        self.root_base, self.api_base = _split_base(base_url)
        self.timeout = timeout
        self.username = username
        self._password = password
        self.session_cookie = session_cookie
        if not session_cookie and not password:
            raise ValueError("ManagerCostClient needs --session-cookie or --manager-password")

    def _get(self, path: str) -> tuple[int, object]:
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={self.session_cookie}"} if self.session_cookie else None
        status, payload, _, _ = _http("GET", f"{self.api_base}{path}", headers=headers, timeout=self.timeout)
        return status, payload

    def login(self) -> None:
        """POST /api/v1/auth/manager/login and keep the support_session cookie (skipped if a cookie was given)."""
        if self.session_cookie:
            return
        status, payload, _, set_cookies = _http(
            "POST",
            f"{self.api_base}/auth/manager/login",
            body={"username": self.username, "password": self._password},
            timeout=self.timeout,
        )
        if status != 200:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise RuntimeError(f"Manager login failed: HTTP {status} {code or ''}".strip())
        for header in set_cookies:
            jar = http.cookies.SimpleCookie()
            jar.load(header)
            if SESSION_COOKIE_NAME in jar:
                self.session_cookie = jar[SESSION_COOKIE_NAME].value
                break
        if not self.session_cookie:
            raise RuntimeError(f"Manager login returned no {SESSION_COOKIE_NAME} cookie")

    def _recent_calls(self) -> list[dict] | None:
        status, payload = self._get("/manager/telemetry")
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("recent_calls"), list):
            return None
        return [c for c in payload["recent_calls"] if isinstance(c, dict)]

    @staticmethod
    def _call_key(call: dict) -> tuple:
        if call.get("id") is not None:
            return ("id", call["id"])
        return ("ts", call.get("ts"), call.get("step"), call.get("session_id"))

    def snapshot(self) -> set[tuple] | None:
        """Keys of recent LLM calls before a case starts (telemetry fallback needs them)."""
        calls = self._recent_calls()
        return None if calls is None else {self._call_key(c) for c in calls}

    @staticmethod
    def _extract_trace_llm_calls(turns_payload: list[dict]) -> list[dict] | None:
        """Extract normalized LLM call records (compatible with T19) from T05 trace turns."""
        has_steps_field = any(isinstance(t.get("steps"), list) for t in turns_payload)
        calls: list[dict] = []
        for turn in turns_payload:
            steps = turn.get("steps")
            if not isinstance(steps, list):
                continue
            for st in steps:
                if not isinstance(st, dict):
                    continue
                tool = str(st.get("tool") or "")
                args = st.get("args") if isinstance(st.get("args"), dict) else {}
                tokens_in = int(st.get("tokens_in") or 0)
                tokens_out = int(st.get("tokens_out") or 0)
                if not tool.startswith("llm:") and tokens_in == 0 and tokens_out == 0:
                    continue
                model = args.get("model") or st.get("model")
                if not isinstance(model, str) or not model.strip():
                    continue
                calls.append(
                    {
                        "step": tool.removeprefix("llm:").strip() or "llm",
                        "model": model.strip(),
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "cost_rub": float(st.get("cost_rub", 0.0)),
                        "latency_ms": int(st.get("latency_ms", 0)),
                    }
                )
        if has_steps_field:
            return calls
        if all(float(t.get("total_cost_rub", 0.0)) == 0.0 for t in turns_payload):
            return []
        return None

    @staticmethod
    def _extract_telemetry_llm_calls(own_calls: list[dict]) -> list[dict]:
        """Extract normalized LLM call records (compatible with T19) from T05 telemetry calls."""
        records: list[dict] = []
        for call in reversed(own_calls):
            model = call.get("model")
            step = call.get("step")
            if not isinstance(model, str) or not model.strip() or not isinstance(step, str) or not step.strip():
                continue
            tokens_in = call.get("tokens_in")
            tokens_out = call.get("tokens_out")
            if isinstance(tokens_in, bool) or not isinstance(tokens_in, int) or tokens_in < 0:
                continue
            if isinstance(tokens_out, bool) or not isinstance(tokens_out, int) or tokens_out < 0:
                continue
            rec: dict = {
                "step": step.strip(),
                "model": model.strip(),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_rub": float(call.get("cost_rub", 0.0)),
            }
            if call.get("latency_ms") is not None:
                rec["latency_ms"] = int(call["latency_ms"])
            records.append(rec)
        return records

    def conversation_cost(self, conversation_id: str, baseline: set[tuple] | None) -> CostInfo:
        status, payload = self._get(f"/manager/conversations/{urllib.parse.quote(conversation_id)}/trace")
        if status == 200 and isinstance(payload, list):
            trace_turns = [t for t in payload if isinstance(t, dict)]
            turns = [float(t.get("total_cost_rub", 0.0)) for t in trace_turns]
            llm_calls = self._extract_trace_llm_calls(trace_turns)
            if llm_calls is None:
                # Minimal trace without steps (e.g. legacy mock): try matching telemetry calls for this session.
                calls = self._recent_calls()
                if calls is not None:
                    own_raw = [c for c in calls if c.get("session_id") == conversation_id]
                    extracted = self._extract_telemetry_llm_calls(own_raw)
                    if extracted or not own_raw:
                        llm_calls = extracted
            return CostInfo(
                "exact",
                "trace",
                round(sum(turns), 6),
                turns,
                llm_calls=llm_calls,
                trace_turns=trace_turns,
            )
        if status in (401, 403):
            return CostInfo("unavailable", reason=f"trace HTTP {status}: manager session rejected")
        calls = self._recent_calls()
        if calls is None:
            return CostInfo("unavailable", reason=f"trace HTTP {status}; telemetry unavailable")
        keys = {self._call_key(c) for c in calls}
        complete = len(calls) < TELEMETRY_RECENT_CALLS_LIMIT or bool(baseline and keys & baseline)
        if not complete:
            return CostInfo(
                "unavailable",
                reason=f"trace HTTP {status}; telemetry window ({len(calls)} calls) does not cover the case",
            )
        own_raw = [c for c in calls if c.get("session_id") == conversation_id]
        own = [float(c.get("cost_rub", 0.0)) for c in own_raw]
        llm_calls = self._extract_telemetry_llm_calls(own_raw)
        if own_raw and not llm_calls:
            # Minimal mock calls without model/tokens: avoid claiming 0 LLM calls when cost > 0.
            llm_calls = None
        return CostInfo("exact", "telemetry", round(sum(own), 6), own, llm_calls=llm_calls)


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------


def _has_confirmed_agent_resolution(history: list[dict], last_action_id: str | None = None) -> bool:
    """True only when the conversation ended with a confirmed final outcome from the agent itself.

    Clarification-only replies, notices/greetings, handoffs, action_failed, or manager replies are NOT
    autonomous agent closures.
    """
    for ev in history:
        data = ev.get("data", {})
        dtype = data.get("type")
        if dtype in {"handoff", "action_failed"}:
            return False
        if dtype == "message" and (
            data.get("author") == "manager"
            or (
                data.get("purpose") == "notice"
                and data.get("text") == "Готовлю ответ, оператор проверит его и пришлёт сюда."
            )
        ):
            return False
    target = [ev for ev in history if ev.get("actionId") == last_action_id] if last_action_id is not None else history
    quotes = _quotes_from_events(target)
    if quotes and (quotes[-1].get("state") not in {"ready", "confirmed"} or quotes[-1].get("totalKopecks") is None):
        return False
    for ev in target:
        data = ev.get("data", {})
        dtype = data.get("type")
        if dtype in {"quote", "document"}:
            return True
        if dtype == "message" and data.get("author") == "agent" and data.get("purpose") == "answer":
            return True
    return False


def _drain_until_quiet(
    client: T04HttpClient,
    conv_id: str,
    cursor: int,
    history: list[dict],
    *,
    last_event_at: float,
    settle_seconds: float,
    poll_interval: float,
    deadline: float,
    validator: T04SchemaValidator,
) -> tuple[int, float]:
    """Read the journal to its end and keep reading until it has been quiet for settle_seconds."""
    while True:
        batch, cursor = client.read_all_from(conv_id, after=cursor, schema_validator=validator)
        now = time.perf_counter()
        if batch:
            history.extend(batch)
            last_event_at = now
        if now - last_event_at >= settle_seconds or now >= deadline:
            return cursor, last_event_at
        time.sleep(max(0.005, min(poll_interval, settle_seconds - (now - last_event_at), deadline - now)))


def run_case(
    client: T04HttpClient,
    case: dict,
    catalog_index: dict[str, CatalogOffer],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    schema_validator: T04SchemaValidator | None = None,
    cost_client: ManagerCostClient | None = None,
) -> CaseResult:
    """Execute one evaluation scenario against a T04 HTTP endpoint.

    A turn is closed when (a) action_failed arrives, (b) the turn produced a reply and its completion
    expectations (expect_handoff / expected_offer_ids / expected_quote_*) are met, or (c) as a fallback the
    journal has been quiet for settle_seconds after a reply (or after the input of a turn marked
    ``allow_silent``). Turn and case assertions run only after the journal is read to its end.
    """
    validator = schema_validator if schema_validator is not None else get_schema_validator()
    case_id = str(case["id"])
    suite = str(case.get("suite", "open"))
    sanitize_outgoing = bool(case.get("sanitize_pii")) or suite == "holdout"
    # Holdout cases use a random technical alias for CLI output and never embed case_id in HTTP keys.
    public_id = f"holdout-alias-{uuid.uuid4().hex[:8]}" if suite == "holdout" else case_id
    result = CaseResult(
        case_id=case_id,
        suite=suite,
        category=str(case.get("category", "")),
        passed=False,
        escalated=False,
        closed_by_agent=False,
        public_id=public_id,
    )
    baseline = None
    try:
        if cost_client is not None:
            baseline = cost_client.snapshot()
        conv_id = client.create_conversation(key=f"eval-conv-{uuid.uuid4().hex}")
        result.conversation_id = conv_id
        cursor = 0
        history: list[dict] = []
        last_event_at = time.perf_counter()
        turn_actions: list[tuple[int, str | None, dict]] = []

        for turn_idx, turn in enumerate(case.get("turns", []), start=1):
            action = dict(turn["action"])
            assertions = turn.get("assertions", {})
            if sanitize_outgoing and action.get("type") == "message" and "text" in action:
                action["text"] = scrub_pii(str(action["text"]))
            action_key = f"eval-act-t{turn_idx}-{uuid.uuid4().hex}"
            # Latency is measured from the moment the customer presses «Send» (before the POST), while the
            # wait deadline and the quiet window start at the 202 ack so a slow POST cannot eat the window.
            t_send = time.perf_counter()
            status, payload, _ = client.submit_action(conv_id, action, key=action_key)
            t_ack = time.perf_counter()
            if validator.active:
                result.errors.extend(
                    f"Turn {turn_idx}: {err}"
                    for err in validator.validate("Document" if status == 200 else "Receipt", payload)
                )
            turn_info: dict = {
                "turn": turn_idx,
                "action_type": action.get("type"),
                "close": None,
                "latency_ms": None,
                "ack_ms": round((t_ack - t_send) * 1000.0, 2),
            }
            result.turns.append(turn_info)

            if status == 200:  # synchronous document re-issue, no journal events for this action
                turn_info.update(close="sync_document", latency_ms=round((time.perf_counter() - t_send) * 1000.0, 2))
                result.latency_ms.append(turn_info["latency_ms"])
                turn_actions.append((turn_idx, None, assertions))
                continue

            action_id = str(payload["actionId"])
            turn_info["action_id"] = action_id
            turn_actions.append((turn_idx, action_id, assertions))
            action_events = [ev for ev in history if ev.get("actionId") == action_id]
            first_meaningful_ms: float | None = None
            post_ack_ms: float | None = None
            deadline = t_ack + timeout
            allow_silent = bool(turn.get("allow_silent"))

            while True:
                batch, cursor = client.read_all_from(conv_id, after=cursor, schema_validator=validator)
                now = time.perf_counter()
                if batch:
                    history.extend(batch)
                    last_event_at = now
                    for ev in batch:
                        if ev.get("actionId") == action_id:
                            action_events.append(ev)
                            if first_meaningful_ms is None and _is_meaningful_event(ev):
                                first_meaningful_ms = (now - t_send) * 1000.0
                                post_ack_ms = (now - t_ack) * 1000.0
                responded = _turn_has_response(action_events)
                quiet_for = now - max(last_event_at, t_ack)
                if any(e.get("data", {}).get("type") == "action_failed" for e in action_events):
                    turn_info["close"] = "action_failed"
                    break
                if responded and turn_expectations_met(action_events, assertions):
                    turn_info["close"] = "expectations"
                    break
                if (responded or allow_silent) and quiet_for >= settle_seconds:
                    turn_info["close"] = "quiet" if responded else "quiet_silent"
                    break
                if now >= deadline:
                    turn_info["close"] = "timeout"
                    result.timed_out = True
                    result.errors.append(f"Turn {turn_idx} timed out after {timeout}s waiting for action {action_id}")
                    break
                wait = poll_interval
                if responded or allow_silent:
                    wait = min(wait, max(0.005, settle_seconds - quiet_for))
                time.sleep(max(0.0, min(wait, deadline - now)))

            if turn_info["close"] == "timeout":
                turn_info["latency_ms"] = round(first_meaningful_ms, 2) if first_meaningful_ms is not None else None
                break
            if first_meaningful_ms is not None and turn_info["close"] != "action_failed":
                turn_info["latency_ms"] = round(first_meaningful_ms, 2)
                turn_info["post_ack_latency_ms"] = round(post_ack_ms or 0.0, 2)
                result.latency_ms.append(first_meaningful_ms)
                if result.first_meaningful_latency_ms is None:
                    result.first_meaningful_latency_ms = first_meaningful_ms

        # Read the journal to its end (and until quiet) before any scenario check.
        cursor, last_event_at = _drain_until_quiet(
            client,
            conv_id,
            cursor,
            history,
            last_event_at=last_event_at,
            settle_seconds=0.0 if result.timed_out else settle_seconds,
            poll_interval=poll_interval,
            deadline=time.perf_counter() + timeout,
            validator=validator,
        )
        drained_len = len(history)
        replay_events, replay_next_after = client.read_all_from(conv_id, after=0, schema_validator=validator)
        tail, cursor = client.read_all_from(conv_id, after=cursor, schema_validator=validator)
        history.extend(tail)
        result.errors.extend(validate_independent_cursors(history, replay_events, replay_next_after, drained_len))
        result.errors.extend(validate_contract_events(history, conv_id, schema_validator=validator))

        # T04 action_failed = the action was not processed. No scenario expects it, so it always fails the case
        # (and excludes that turn from latency samples, even if action_failed arrived after a meaningful reply).
        turn_by_action = {aid: idx for idx, aid, _ in turn_actions if aid is not None}
        failed_action_ids: set[str] = set()
        for ev in history:
            data = ev.get("data", {})
            if data.get("type") != "action_failed":
                continue
            aid_str = str(ev.get("actionId"))
            failed_action_ids.add(aid_str)
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            idx = turn_by_action.get(aid_str)
            where = f"Turn {idx}" if idx is not None else f"actionId {ev.get('actionId')!r}"
            result.errors.append(
                f"{where}: action_failed {err.get('code', '<no code>')}: {err.get('message', '<no message>')}"
            )

        if failed_action_ids:
            for t_info in result.turns:
                if t_info.get("action_id") in failed_action_ids:
                    t_info["close"] = "action_failed"
                    t_info["latency_ms"] = None
                    t_info.pop("post_ack_latency_ms", None)
        result.latency_ms = [float(t["latency_ms"]) for t in result.turns if t.get("latency_ms") is not None]
        result.first_meaningful_latency_ms = next(
            (
                float(t["latency_ms"])
                for t in result.turns
                if t.get("latency_ms") is not None and t.get("close") != "timeout"
            ),
            None,
        )

        turn_action_ids: dict[int, str] = {}
        for turn_idx, action_id, assertions in turn_actions:
            if action_id is None:
                continue
            turn_action_ids[turn_idx] = action_id
            turn_events = [ev for ev in history if ev.get("actionId") == action_id]
            result.errors.extend(
                f"Turn {turn_idx}: {err}" for err in validate_case_expectations(turn_events, assertions, catalog_index)
            )
        result.errors.extend(
            validate_case_expectations(
                history, case.get("assertions", {}), catalog_index, turn_action_ids=turn_action_ids
            )
        )

        result.events_count = len(history)
        result.agent_responses = [str(m.get("text", "")) for m in _agent_messages(history)]
        result.escalated = bool(_handoffs_from_events(history))
        last_action_id = next((aid for _, aid, _ in reversed(turn_actions) if aid is not None), None)
        agent_resolved = _has_confirmed_agent_resolution(history, last_action_id)
        if cost_client is not None:
            cost = cost_client.conversation_cost(conv_id, baseline)
            result.cost_status, result.cost_source, result.cost_reason = cost.status, cost.source, cost.reason
            result.cost_rub = cost.total_rub
            result.llm_calls = cost.llm_calls
            for turn_info, trace_turn in zip(
                result.turns, cost.trace_turns if cost.source == "trace" else [], strict=False
            ):
                turn_info["trace_cost_rub"] = float(trace_turn.get("total_cost_rub", 0.0))
                if isinstance(trace_turn.get("steps"), list):
                    turn_info["steps"] = trace_turn["steps"]
    except Exception as exc:  # noqa: BLE001 - any stand/runner failure fails the case, never the suite
        agent_resolved = False
        result.errors.append(f"Runner error: {exc}")
    result.passed = not result.errors
    # Closed by agent only on a confirmed final agent outcome (never on clarification, manager reply, or failure).
    result.closed_by_agent = result.passed and not result.escalated and agent_resolved
    return result


# ---------------------------------------------------------------------------
# Metrics and reports
# ---------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float | None:
    """Compute linear-interpolated percentile (0..100) for a non-empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def load_category_weights(split_summary_path: Path = DEFAULT_SPLIT_SUMMARY) -> dict[str, float]:
    """Load real-world category weights from eval/split_summary.json."""
    if not split_summary_path.is_file():
        return {}
    data = json.loads(split_summary_path.read_text(encoding="utf-8"))
    total = int(data.get("total_records", 0))
    if total <= 0:
        return {}
    return {
        cat: int(info["total"]) / total
        for cat, info in data.get("categories", {}).items()
        if int(info.get("total", 0)) > 0
    }


def summarize_results(
    results: list[CaseResult],
    *,
    suite: str,
    mode: str,
    category_weights: dict[str, float] | None = None,
) -> dict:
    """Aggregate pass/fail, escalation, latency p50/p95 and cost metrics from exact stand costs only."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    escalated = sum(1 for r in results if r.escalated)
    closed_by_agent = sum(1 for r in results if r.passed and r.closed_by_agent and not r.escalated)
    timed_out = sum(1 for r in results if r.timed_out)

    all_latencies = [ms for r in results for ms in r.latency_ms]
    p50 = percentile(all_latencies, 50.0)
    p95 = percentile(all_latencies, 95.0)
    mean_lat = (sum(all_latencies) / len(all_latencies)) if all_latencies else None

    exact = [r for r in results if r.cost_status == "exact" and r.cost_rub is not None]
    if not exact:
        cost_status = "unavailable"
    elif len(exact) == total:
        cost_status = "exact"
    else:
        cost_status = "partial"
    avg_cost_rub = projected_500_rub = weighted_avg_cost_rub = weighted_projected_500_rub = None
    if exact:
        avg = sum(float(r.cost_rub or 0.0) for r in exact) / len(exact)
        avg_cost_rub = round(avg, 4)
        projected_500_rub = round(avg * 500.0, 2)
        if category_weights:
            by_cat: dict[str, list[float]] = {}
            for r in exact:
                if r.category in category_weights:
                    by_cat.setdefault(r.category, []).append(float(r.cost_rub or 0.0))
            weight_sum = sum(category_weights[cat] for cat in by_cat)
            if weight_sum > 0:
                weighted_mean = (
                    sum((sum(vals) / len(vals)) * category_weights[cat] for cat, vals in by_cat.items()) / weight_sum
                )
                weighted_avg_cost_rub = round(weighted_mean, 4)
                weighted_projected_500_rub = round(weighted_mean * 500.0, 2)
    unavailable_reasons = sorted({str(r.cost_reason) for r in results if r.cost_status != "exact" and r.cost_reason})

    holdout_alias_map = {(r.public_id or r.case_id): r.case_id for r in results if r.suite == "holdout"}
    summary: dict = {
        "schema_version": "1.3",
        "suite": suite,
        "mode": mode,
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": total - passed,
        "timed_out_cases": timed_out,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "closed_by_agent_count": closed_by_agent,
        "closed_by_agent_rate": round(closed_by_agent / total, 4) if total else 0.0,
        "escalated_count": escalated,
        "escalated_rate": round(escalated / total, 4) if total else 0.0,
        "latency_ms": {
            "samples": len(all_latencies),
            "mean": round(mean_lat, 2) if mean_lat is not None else None,
            "p50": round(p50, 2) if p50 is not None else None,
            "p95": round(p95, 2) if p95 is not None else None,
        },
        "economics": {
            "cost_status": cost_status,
            "cases_with_exact_cost": len(exact),
            "cost_sources": sorted({str(r.cost_source) for r in exact}),
            "unavailable_reasons": unavailable_reasons,
            "avg_cost_per_case_rub": avg_cost_rub,
            "projected_500_cases_half_year_rub": projected_500_rub,
            "weighted_avg_cost_per_case_rub": weighted_avg_cost_rub,
            "weighted_projected_500_cases_half_year_rub": weighted_projected_500_rub,
            "load_scenario_500_cases_per_day_rub": projected_500_rub,
            "weighted_load_scenario_500_cases_per_day_rub": weighted_projected_500_rub,
        },
        "cases": [
            {
                "id": r.case_id,
                "alias": r.public_id or r.case_id,
                "conversation_id": r.conversation_id,
                "suite": r.suite,
                "category": r.category,
                "passed": r.passed,
                "escalated": r.escalated,
                "closed_by_agent": bool(r.passed and r.closed_by_agent and not r.escalated),
                "timed_out": r.timed_out,
                "latency_ms": [round(x, 2) for x in r.latency_ms],
                "cost_rub": r.cost_rub,
                "cost_status": r.cost_status,
                "cost_source": r.cost_source,
                "llm_calls": r.llm_calls,
                "turns": r.turns,
                "events_count": r.events_count,
                "errors": r.errors,
                "agent_responses": r.agent_responses,
            }
            for r in results
        ],
    }
    if holdout_alias_map:
        summary["holdout_alias_map"] = holdout_alias_map
    return summary


def format_markdown_report(
    summary: dict,
    *,
    mask_holdout_ids: bool = False,
    hide_holdout_details: bool = False,
) -> str:
    """Render concise Markdown report including actual text of every failed response.

    When ``hide_holdout_details=True`` (used for console stdout), holdout runs print only aggregate
    metrics (without per-case response texts); full holdout details and alias mappings stay in the
    private report files inside the holdout directory.
    """
    lat = summary["latency_ms"]
    econ = summary["economics"]
    if econ["cost_status"] == "unavailable":
        cost_line = "unavailable (" + "; ".join(econ.get("unavailable_reasons") or ["no exact stand cost"]) + ")"
    else:
        cost_line = (
            f"{econ['avg_cost_per_case_rub']} / {econ['projected_500_cases_half_year_rub']} "
            f"(weighted 500 / half-year: {econ['weighted_projected_500_cases_half_year_rub']}; "
            f"separate load scenario 500/day: {econ['load_scenario_500_cases_per_day_rub']}; "
            f"{econ['cost_status']}, {econ['cases_with_exact_cost']}/{summary['total_cases']} cases, "
            f"source: {', '.join(econ['cost_sources'])})"
        )
    lines = [
        f"# Eval Report ({summary['suite']} / {summary['mode']})",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total cases | {summary['total_cases']} |",
        f"| Passed / Failed | {summary['passed_cases']} / {summary['failed_cases']} ({summary['pass_rate']:.1%}) |",
        f"| Closed by agent | {summary['closed_by_agent_count']} ({summary['closed_by_agent_rate']:.1%}) |",
        f"| Escalated | {summary['escalated_count']} ({summary['escalated_rate']:.1%}) |",
        f"| Timed out | {summary.get('timed_out_cases', 0)} |",
        f"| Latency p50 / p95 (ms) | {lat['p50']} / {lat['p95']} (samples={lat['samples']}) |",
        f"| Avg cost / 500 cases per half-year proj (RUB) | {cost_line} |",
    ]
    if hide_holdout_details and summary.get("suite") == "holdout":
        lines.extend(
            [
                "",
                "_Holdout per-case details and failed responses are saved only in the private report._",
            ]
        )
        return "\n".join(lines) + "\n"

    if hide_holdout_details and summary.get("suite") == "all":
        private = [item for item in summary["cases"] if item.get("suite") == "holdout"]
        lines.append(f"\nHoldout: {sum(1 for item in private if item['passed'])}/{len(private)} passed.")

    lines.extend(
        [
            "",
            "| Case ID | Status | Outcome | Latency (ms) | Cost (RUB) | Errors |",
            "|---|---|---|---|---|---|",
        ]
    )
    failed_items: list[tuple[str, dict]] = []
    for item in summary["cases"]:
        is_holdout = item.get("suite") == "holdout"
        if hide_holdout_details and is_holdout:
            continue
        display_id = str(item.get("alias") or "<holdout>") if mask_holdout_ids and is_holdout else str(item["id"])
        status_str = "PASS" if item["passed"] else "FAIL"
        if item["escalated"]:
            outcome = "escalated"
        elif item["passed"] and item.get("closed_by_agent"):
            outcome = "agent"
        elif item["passed"]:
            outcome = "unresolved"
        else:
            outcome = "failed"
        lat_str = ", ".join(str(x) for x in item["latency_ms"]) or "-"
        cost_str = str(item["cost_rub"]) if item.get("cost_status") == "exact" else "unavailable"
        err_str = "; ".join(item["errors"]) if item["errors"] else "-"
        lines.append(f"| {display_id} | {status_str} | {outcome} | {lat_str} | {cost_str} | {err_str} |")
        if not item["passed"] and not (hide_holdout_details and is_holdout):
            failed_items.append((display_id, item))

    if failed_items:
        lines.extend(["", "## Failed Case Responses", ""])
        for display_id, item in failed_items:
            lines.append(f"### {display_id}")
            lines.append(f"- **Errors:** {'; '.join(item['errors'])}")
            responses = item.get("agent_responses") or []
            if responses:
                for idx, resp_text in enumerate(responses, start=1):
                    lines.append(f"- **Agent response {idx}:** {resp_text}")
            else:
                lines.append("- **Agent response:** <no agent message captured>")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(summary: dict, out_base: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown evaluation reports to the requested prefix/path."""
    if out_base.suffix == ".json":
        json_path, md_path = out_base, out_base.with_suffix(".md")
    elif out_base.suffix == ".md":
        md_path, json_path = out_base, out_base.with_suffix(".json")
    else:
        json_path, md_path = Path(f"{out_base}.json"), Path(f"{out_base}.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(format_markdown_report(summary), encoding="utf-8")
    return json_path, md_path


def load_cases(
    *,
    suite: str,
    mode: str,
    golden_open_path: Path,
    holdout_path: Path,
) -> list[dict]:
    """Load and mode-filter cases for the requested suite."""
    cases: list[dict] = []
    if suite in {"open", "all"}:
        open_data = json.loads(golden_open_path.read_text(encoding="utf-8"))
        cases.extend(open_data.get("cases", []))
    if suite in {"holdout", "all"}:
        if not holdout_path.is_file():
            raise FileNotFoundError(f"Holdout suite file not found: {holdout_path}")
        holdout_data = json.loads(holdout_path.read_text(encoding="utf-8"))
        cases.extend(holdout_data.get("cases", []))
    return [case for case in cases if mode in case.get("modes", ["mvp", "full"])]


# ---------------------------------------------------------------------------
# Offline self-check (--mock-check)
# ---------------------------------------------------------------------------


def _make_mock_t04_server() -> tuple[ThreadingHTTPServer, str]:
    """Spin up a minimal loopback T04 mock server for --mock-check (not a full contract implementation)."""
    lock = threading.Lock()
    state: dict[str, object] = {"seq_counter": 0, "conversations": {}}

    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status_code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            match = re.match(r"^/api/v1/conversations/([^/]+)/events$", parsed.path)
            if not match:
                self._send_json(404, {"code": "not_found", "message": "Not found"})
                return
            conv_id = urllib.parse.unquote(match.group(1))
            after = int(urllib.parse.parse_qs(parsed.query).get("after", ["0"])[0])
            with lock:
                convs: dict = state["conversations"]  # type: ignore[assignment]
                if conv_id not in convs:
                    self._send_json(404, {"code": "not_found", "message": "Conversation not found"})
                    return
                events = convs[conv_id]["events"][after : after + 100]
                next_after = after + len(events)
                has_more = next_after < len(convs[conv_id]["events"])
            self._send_json(200, {"events": events, "nextAfter": next_after, "hasMore": has_more})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw) if raw else {}
            if parsed.path == "/api/v1/conversations":
                with lock:
                    state["seq_counter"] = int(state["seq_counter"]) + 1
                    conv_id = f"mock-c-{state['seq_counter']}"
                    convs: dict = state["conversations"]  # type: ignore[assignment]
                    convs[conv_id] = {"events": [], "turn": 0}
                self._send_json(201, {"conversationId": conv_id})
                return

            match = re.match(r"^/api/v1/conversations/([^/]+)/actions$", parsed.path)
            if not match:
                self._send_json(404, {"code": "not_found", "message": "Not found"})
                return
            conv_id = urllib.parse.unquote(match.group(1))
            with lock:
                convs = state["conversations"]  # type: ignore[assignment]
                if conv_id not in convs:
                    self._send_json(404, {"code": "not_found", "message": "Conversation not found"})
                    return
                conv = convs[conv_id]
                conv["turn"] += 1
                action_id = f"a-{conv['turn']}"
                events: list[dict] = conv["events"]

                def push(data: dict) -> None:
                    events.append(
                        {"seq": len(events) + 1, "at": "2026-11-26T10:00:00.000Z", "actionId": action_id, "data": data}
                    )

                if body.get("type") == "request_operator":
                    push(
                        {
                            "type": "handoff",
                            "handoff": {
                                "handoffId": f"h-{conv_id}-{conv['turn']}",
                                "conversationId": conv_id,
                                "reason": "customer_request",
                                "question": "Запрос оператора",
                                "products": [],
                                "status": "open",
                            },
                        }
                    )
                else:
                    text = str(body.get("text", ""))
                    push({"type": "message", "author": "customer", "purpose": "input", "text": text, "sources": []})
                    if "61557" in text:
                        src = {"url": "https://store.softline.ru/sunrav/-61557/", "label": "Карточка Softline"}
                        product = {
                            "offerId": "61557",
                            "name": "SunRav Software BookOffice",
                            "source": src,
                            "available": True,
                            "priceState": "listed",
                            "unitPriceKopecks": 490000,
                            "licenseTerm": None,
                        }
                        push({"type": "products", "products": [product]})
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "answer",
                                "text": (
                                    "Цена 4 900 ₽ по снимку; срок действия в источнике не указан, "
                                    "нужна проверка специалиста."
                                ),
                                "sources": [src],
                            }
                        )
                    elif "504" in text:
                        src = {
                            "url": "https://store.softline.ru/adobe/65272676ba01a12-504/",
                            "label": "Карточка Softline",
                        }
                        product = {
                            "offerId": "504",
                            "name": "Adobe InCopy CC",
                            "source": src,
                            "available": False,
                            "priceState": "on_request",
                            "unitPriceKopecks": None,
                            "licenseTerm": "12 мес.",
                        }
                        push({"type": "products", "products": [product]})
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "answer",
                                "text": "Цена по запросу. Передал вопрос менеджеру.",
                                "sources": [src],
                            }
                        )
                        push(
                            {
                                "type": "handoff",
                                "handoff": {
                                    "handoffId": f"h-{conv_id}-{conv['turn']}",
                                    "conversationId": conv_id,
                                    "reason": "price_request",
                                    "question": "Запросить цену Adobe InCopy CC",
                                    "products": [product],
                                    "status": "open",
                                },
                            }
                        )
                    elif "HyperQuantum" in text:
                        conv["offered_help"] = True
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "clarification",
                                "text": (
                                    "В источнике нет подтверждённых сведений. "
                                    "Подключить оператора, чтобы он уточнил ваш вопрос?"
                                ),
                                "sources": [],
                            }
                        )
                    elif conv.get("offered_help") and "подключайте" in text.lower():
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "notice",
                                "text": "Передал вопрос оператору. Он увидит переписку и ответит в этом чате.",
                                "sources": [],
                            }
                        )
                        push(
                            {
                                "type": "handoff",
                                "handoff": {
                                    "handoffId": f"h-{conv_id}",
                                    "conversationId": conv_id,
                                    "reason": "insufficient_evidence",
                                    "question": "HyperQuantum Nebula ERP 2026",
                                    "products": [],
                                    "status": "open",
                                },
                            }
                        )
                    elif "Мдеймон" in text:
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "clarification",
                                "text": (
                                    "По названию нашёл в каталоге семейство «MDaemon Messaging Server». "
                                    "На кого оформляется лицензия — на организацию или физлицо? "
                                    "Покупаете впервые или продлеваете лицензию?"
                                ),
                                "sources": [],
                            }
                        )
                    elif "Коммерческая компания, покупаем впервые" in text:
                        src = {
                            "url": "https://store.softline.ru/alt-n-technologies-ltd/-19576/",
                            "label": "Карточка Softline",
                        }
                        product = {
                            "offerId": "19576",
                            "name": (
                                "MDaemon Technologies, Ltd. MDaemon Messaging Server "
                                "(лицензия с подпиской на обновления на 1 год), 20 пользователей"
                            ),
                            "source": src,
                            "available": True,
                            "priceState": "listed",
                            "unitPriceKopecks": 7755600,
                            "licenseTerm": "12 мес.",
                        }
                        push({"type": "products", "products": [product]})
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "answer",
                                "text": "MDaemon Messaging Server на 20 пользователей (новая покупка) стоит 77 556 ₽.",
                                "sources": [src],
                            }
                        )
                    elif text.strip().lower() == "позовите оператора":
                        if conv["turn"] == 1:
                            push(
                                {
                                    "type": "message",
                                    "author": "agent",
                                    "purpose": "clarification",
                                    "text": "Подскажите, с каким вопросом нужна помощь?",
                                    "sources": [],
                                }
                            )
                        else:
                            push(
                                {
                                    "type": "message",
                                    "author": "agent",
                                    "purpose": "notice",
                                    "text": "Передал вопрос оператору. Он увидит переписку и ответит в этом чате.",
                                    "sources": [],
                                }
                            )
                            push(
                                {
                                    "type": "handoff",
                                    "handoff": {
                                        "handoffId": f"h-{conv_id}",
                                        "conversationId": conv_id,
                                        "reason": "customer_request",
                                        "question": "Позовите оператора",
                                        "products": [],
                                        "status": "open",
                                    },
                                }
                            )
                    else:
                        push(
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "clarification",
                                "text": "Уточните параметры лицензии и количество пользователей?",
                                "sources": [],
                            }
                        )

            self._send_json(202, {"conversationId": conv_id, "actionId": action_id})

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}"


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise RuntimeError(f"Self-check failed: {detail}")


def _msg_event(text: str, url: str) -> list[dict]:
    return [
        {
            "seq": 1,
            "at": "2026-11-26T10:00:00Z",
            "actionId": "a1",
            "data": {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": text,
                "sources": [{"url": url, "label": "Softline"}] if url else [],
            },
        }
    ]


def run_mock_self_check(golden_open_path: Path) -> int:
    """Verify validators catch synthetic errors and pass valid traces over a loopback T04 mock.

    Covers 6 of the open scenarios end-to-end; the full 16-case HTTP loop lives in tests/eval.
    """
    catalog = load_catalog_index(golden_open_path)
    url_61557 = "https://store.softline.ru/sunrav/-61557/"
    url_504 = "https://store.softline.ru/adobe/65272676ba01a12-504/"

    _require(
        validate_no_hallucinated_prices(_msg_event("Лицензия стоит 5 555 ₽.", url_61557), ["4900"], catalog),
        "price hallucination not caught",
    )
    _require(
        validate_anti_term_hallucination(
            _msg_event("SunRav BookOffice стоит 4 900 ₽, срок действия 12 мес.", url_61557)
        ),
        "term hallucination not caught",
    )
    _require(validate_anti_zero_price(_msg_event("Adobe InCopy CC стоит 0,00 р.", url_504), "504"), "zero price leak")
    _require(validate_sources_present(_msg_event("Цена 4 900 ₽.", "")), "missing source not caught")
    _require(
        validate_case_expectations(_msg_event("Итого 41,227,5 ₽", url_61557), {"allowed_prices_rub": ["41227"]}),
        "ambiguous amount not reported",
    )

    open_data = json.loads(golden_open_path.read_text(encoding="utf-8"))
    mock_case_ids = {
        "open-price-61557",
        "open-price-504",
        "open-robust-operator-button",
        "open-robust-unknown-product",
        "open-typo-mdaemon",
        "open-robust-operator-twice",
    }
    cases = [c for c in open_data.get("cases", []) if c["id"] in mock_case_ids]

    server, base_url = _make_mock_t04_server()
    try:
        client = T04HttpClient(base_url, timeout=5.0)
        results = [
            run_case(client, case, catalog, timeout=5.0, poll_interval=0.02, settle_seconds=0.05) for case in cases
        ]
    finally:
        server.shutdown()
        server.server_close()

    summary = summarize_results(
        results, suite="mock-check", mode="mvp", category_weights=load_category_weights(DEFAULT_SPLIT_SUMMARY)
    )
    print(format_markdown_report(summary))
    if summary["failed_cases"] > 0:
        return 1
    spec = get_schema_validator().spec_version or "schema validation disabled"
    print(f"Mock self-check passed: validators and T04 ({spec}) HTTP loop verified.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Stand base URL (e.g. https://stand.example or http://127.0.0.1:8000)")
    parser.add_argument("--suite", choices=["open", "holdout", "all"], default="open")
    parser.add_argument("--mode", choices=["mvp", "full"], default="mvp")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Report prefix (default: .local/eval_reports/ for open, <holdout dir>/reports/ for holdout/all)",
    )
    parser.add_argument("--mock-check", action="store_true", help="Run offline validator and mock T04 checks")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--golden-open", type=Path, default=DEFAULT_GOLDEN_OPEN)
    parser.add_argument("--holdout-file", type=Path, default=None)
    parser.add_argument("--split-summary", type=Path, default=DEFAULT_SPLIT_SUMMARY)
    parser.add_argument("--openapi", type=Path, default=None, help="T04 OpenAPI file (default: contracts/t04, eval/)")
    parser.add_argument(
        "--no-schema-validation",
        action="store_true",
        help="Explicitly skip T04 OpenAPI validation (otherwise missing PyYAML/jsonschema is an error)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="Fallback: close a turn after the journal is quiet this long (expectations close it earlier)",
    )
    parser.add_argument(
        "--session-cookie",
        default=None,
        help="Existing manager support_session cookie value for cost reads (env: EVAL_SESSION_COOKIE)",
    )
    parser.add_argument(
        "--manager-password",
        default=None,
        help="Manager password for POST /api/v1/auth/manager/login (prefer env: EVAL_MANAGER_PASSWORD)",
    )
    parser.add_argument("--manager-user", default=None, help="Manager login (env: EVAL_MANAGER_USER, default manager)")
    return parser


def _default_out(suite: str) -> Path:
    if suite == "open":
        return DEFAULT_OPEN_OUT
    return default_holdout_dir() / "reports" / "eval_report"


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        validator = T04SchemaValidator(args.openapi, enabled=not args.no_schema_validation)
    except SchemaValidationUnavailableError as exc:
        raise SystemExit(f"error: {exc}") from exc
    set_schema_validator(validator)
    if validator.active and validator.spec_version != EXPECTED_T04_VERSION:
        print(
            f"warning: T04 spec {validator.spec_path} is {validator.spec_version}, "
            f"runner targets {EXPECTED_T04_VERSION}"
        )
    if not validator.active:
        print("warning: T04 OpenAPI schema validation disabled by --no-schema-validation")

    if args.mock_check:
        return run_mock_self_check(args.golden_open)

    if not args.base_url:
        raise SystemExit("--base-url is required unless --mock-check is specified")

    session_cookie = args.session_cookie or os.environ.get("EVAL_SESSION_COOKIE") or None
    manager_password = args.manager_password or os.environ.get("EVAL_MANAGER_PASSWORD") or None
    manager_user = args.manager_user or os.environ.get("EVAL_MANAGER_USER") or "manager"
    cost_client: ManagerCostClient | None = None
    if session_cookie or manager_password:
        cost_client = ManagerCostClient(
            args.base_url,
            session_cookie=session_cookie,
            password=manager_password,
            username=manager_user,
            timeout=args.timeout,
        )
        try:
            cost_client.login()
        except RuntimeError as exc:
            raise SystemExit(f"error: {exc}") from exc
    else:
        print("note: no manager credentials; LLM cost will be reported as unavailable")

    holdout_path = args.holdout_file if args.holdout_file is not None else default_holdout_file()
    cases = load_cases(suite=args.suite, mode=args.mode, golden_open_path=args.golden_open, holdout_path=holdout_path)
    catalog = load_catalog_index(args.golden_open, args.catalog, load_full_xml=(args.suite in {"holdout", "all"}))

    client = T04HttpClient(args.base_url, timeout=args.timeout, schema_validator=validator)
    results = [
        run_case(
            client,
            case,
            catalog,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            settle_seconds=args.settle_seconds,
            schema_validator=validator,
            cost_client=cost_client,
        )
        for case in cases
    ]
    summary = summarize_results(
        results, suite=args.suite, mode=args.mode, category_weights=load_category_weights(args.split_summary)
    )
    summary["t04_spec_version"] = validator.spec_version
    summary["pre_acceptance_rejections"] = {
        code: client.rejections.count(code) for code in sorted(set(client.rejections))
    }
    json_path, md_path = write_reports(summary, args.out if args.out is not None else _default_out(args.suite))
    print(format_markdown_report(summary, mask_holdout_ids=True, hide_holdout_details=True))
    print(f"Reports written: {json_path}, {md_path}")
    return 0 if summary["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
