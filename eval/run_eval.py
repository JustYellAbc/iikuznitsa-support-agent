"""Black-box acceptance evaluation runner against the T04 0.2.0 HTTP contract."""

from __future__ import annotations

import argparse
import http.cookiejar
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

try:
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - optional stdlib-only fallback
    yaml = None
    Draft202012Validator = None
    FormatChecker = None

DEFAULT_GOLDEN_OPEN = Path("eval/golden_open.json")
DEFAULT_SPLIT_SUMMARY = Path("eval/split_summary.json")
DEFAULT_CATALOG = Path("data/raw/Mindbox.yml.gz.xml")
DEFAULT_OUT = Path("data/private/eval_report")
T04_OPENAPI_CANDIDATES = (
    Path("contracts/t04/openapi.yaml"),
    Path("eval/t04_openapi.yaml"),
    Path(__file__).resolve().parent / "t04_openapi.yaml",
)

PRICE_IN_TEXT_RE = re.compile(
    r"(?<!\d)"
    r"(?P<num>\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)"
    r"(?:\s*(?P<mult>тыс\.?))?"
    r"\s*(?:₽|руб(?:л(?:я|ей))?\.?|р\.|RUR|RUB)(?!\w)",
    re.IGNORECASE,
)
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


@dataclass(frozen=True, slots=True)
class CatalogOffer:
    """Minimal catalog snapshot metadata used for price/availability assertions."""

    offer_id: str
    available: bool
    price_rub: Decimal
    license_term: str | None
    url: str


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
    errors: list[str] = field(default_factory=list)
    agent_responses: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    events_count: int = 0


def default_holdout_file() -> Path:
    """Resolve holdout_eval.json path from env or external holdout directory."""
    env_file = os.environ.get("EVAL_HOLDOUT_FILE")
    if env_file:
        return Path(env_file).expanduser()
    external = default_holdout_dir() / "holdout_eval.json"
    if external.is_file():
        return external
    legacy = Path("data/private/holdout_eval.json")
    return legacy if legacy.is_file() else external


def _normalize_decimal(value: str | int | float | Decimal) -> Decimal:
    text = str(value).replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal amount: {value!r}") from exc
    return dec.normalize()


def extract_prices_rub(text: str) -> list[Decimal]:
    """Extract currency-tagged ruble amounts (including 'тыс.', 'р.', '0,00', dot thousands)."""
    prices: list[Decimal] = []
    for match in PRICE_IN_TEXT_RE.finditer(text):
        amount = _normalize_decimal(match.group("num"))
        if match.group("mult"):
            amount = (amount * Decimal(1000)).normalize()
        prices.append(amount)
    return prices


class T04SchemaValidator:
    """JSON Schema validator backed by T04 0.2.0 OpenAPI components (same engine as contracts/t04/verify.py)."""

    def __init__(self, spec_path: Path | None = None) -> None:
        self._components: dict | None = None
        self._validators: dict[str, object] = {}
        self.spec_path: Path | None = None
        if yaml is None or Draft202012Validator is None or FormatChecker is None:
            return
        candidates = [spec_path] if spec_path is not None else list(T04_OPENAPI_CANDIDATES)
        for cand in candidates:
            if cand is not None and cand.is_file():
                spec = yaml.safe_load(cand.read_text(encoding="utf-8"))
                self._components = spec.get("components")
                self.spec_path = cand
                break

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
    """Return cached T04 0.2.0 OpenAPI schema validator."""
    global _DEFAULT_SCHEMA_VALIDATOR
    if _DEFAULT_SCHEMA_VALIDATOR is None:
        _DEFAULT_SCHEMA_VALIDATOR = T04SchemaValidator()
    return _DEFAULT_SCHEMA_VALIDATOR


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
                )
            elem.clear()
    return index


def _agent_messages(events: list[dict], *, purpose: str | None = None) -> list[dict]:
    result = []
    for event in events:
        data = event.get("data", {})
        if data.get("type") == "message" and data.get("author") == "agent":
            if purpose is None or data.get("purpose") == purpose:
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


def validate_contract_events(
    events: list[dict],
    conversation_id: str,
    schema_validator: T04SchemaValidator | None = None,
) -> list[str]:
    """Validate T04 0.2.0 OpenAPI schema and cross-event invariants on a collected journal."""
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
            quote = data.get("quote", {})
            lines = quote.get("lines", [])
            subtotal = sum(line.get("packages", 0) * (line.get("unitPriceKopecks") or 0) for line in lines)
            if subtotal != quote.get("knownSubtotalKopecks"):
                errors.append(
                    f"Quote {quote.get('quoteId')} subtotal {quote.get('knownSubtotalKopecks')} != {subtotal}"
                )
            if quote.get("state") != "needs_review" and quote.get("totalKopecks") != subtotal:
                errors.append(f"Quote {quote.get('quoteId')} total {quote.get('totalKopecks')} != {subtotal}")
    return errors


def validate_independent_cursors(
    incremental_events: list[dict],
    replay_events: list[dict],
    replay_next_after: int,
) -> list[str]:
    """Ensure a second independent reader starting at after=0 sees the exact same history."""
    errors: list[str] = []
    if replay_events != incremental_events:
        errors.append(
            f"Independent after=0 replay diverged from incremental reads "
            f"({len(replay_events)} vs {len(incremental_events)} events)"
        )
    if replay_next_after != len(incremental_events):
        errors.append(f"Independent replay nextAfter={replay_next_after} != {len(incremental_events)}")
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

    for msg in _agent_messages(events):
        for price in extract_prices_rub(msg.get("text", "")):
            if allowed_set is not None:
                if price not in allowed_set:
                    errors.append(f"Hallucinated or disallowed price in agent text: {price} RUB")
            elif catalog:
                known_prices = {offer.price_rub for offer in catalog.values() if offer.price_rub > 0}
                if price not in known_prices:
                    errors.append(f"Price {price} RUB in agent text not found in catalog index")

    for prod in _products_from_events(events):
        oid = str(prod.get("offerId", ""))
        kopecks = prod.get("unitPriceKopecks")
        if kopecks is None:
            continue
        price_rub = (Decimal(kopecks) / Decimal(100)).normalize()
        if oid in catalog and catalog[oid].price_rub != price_rub:
            errors.append(f"Product {oid} price mismatch: {price_rub} != XML {catalog[oid].price_rub}")
        if allowed_set is not None and price_rub not in allowed_set:
            errors.append(f"Product {oid} price {price_rub} not in allowed prices")

    for quote in _quotes_from_events(events):
        for line in quote.get("lines", []):
            oid = str(line.get("offerId", ""))
            kopecks = line.get("unitPriceKopecks")
            if kopecks is None:
                continue
            price_rub = (Decimal(kopecks) / Decimal(100)).normalize()
            if oid in catalog and catalog[oid].price_rub != price_rub:
                errors.append(f"Quote line {oid} price mismatch: {price_rub} != XML {catalog[oid].price_rub}")
        total_kopecks = quote.get("totalKopecks")
        if total_kopecks is not None and allowed_set is not None:
            total_rub = (Decimal(total_kopecks) / Decimal(100)).normalize()
            if total_rub not in allowed_set:
                errors.append(f"Quote total {total_rub} RUB not in allowed prices")
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
        if any(price == Decimal(0) for price in extract_prices_rub(text)) or ZERO_WORD_RE.search(text):
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
        if str(prod.get("offerId")) == offer_id:
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

    handoffs = _handoffs_from_events(events)
    if not handoffs:
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


def validate_case_expectations(
    events: list[dict],
    assertions: dict,
    catalog_index: dict[str, CatalogOffer] | None = None,
) -> list[str]:
    """Run all configured validators for a turn or full conversation."""
    errors: list[str] = []
    if not assertions:
        return errors

    allowed_prices = assertions.get("allowed_prices_rub")
    if allowed_prices is not None or assertions.get("check_catalog_prices"):
        errors.extend(validate_no_hallucinated_prices(events, allowed_prices, catalog_index))

    if assertions.get("anti_term_hallucination_offer_id"):
        errors.extend(validate_anti_term_hallucination(events, str(assertions["anti_term_hallucination_offer_id"])))

    if assertions.get("anti_zero_price_offer_id"):
        errors.extend(validate_anti_zero_price(events, str(assertions["anti_zero_price_offer_id"])))
    elif assertions.get("forbid_zero_price"):
        for msg in _agent_messages(events):
            if any(price == Decimal(0) for price in extract_prices_rub(msg.get("text", ""))):
                errors.append("Zero price (0 RUB) leaked in agent text")

    if assertions.get("unavailable_with_price_offer_id"):
        errors.extend(validate_unavailable_with_price(events, str(assertions["unavailable_with_price_offer_id"])))

    if assertions.get("require_sources_on_answer") or assertions.get("required_urls"):
        errors.extend(validate_sources_present(events, assertions.get("required_urls")))

    agent_msgs = _agent_messages(events)
    agent_text = "\n".join(m.get("text", "") for m in agent_msgs)
    observed_prices = set(extract_prices_rub(agent_text))
    for prod in _products_from_events(events):
        if prod.get("unitPriceKopecks") is not None:
            observed_prices.add((Decimal(prod["unitPriceKopecks"]) / Decimal(100)).normalize())
    for quote in _quotes_from_events(events):
        if quote.get("totalKopecks") is not None:
            observed_prices.add((Decimal(quote["totalKopecks"]) / Decimal(100)).normalize())

    for req_price in assertions.get("required_prices_rub", []):
        norm_req = _normalize_decimal(req_price)
        if norm_req not in observed_prices:
            errors.append(f"Expected price {norm_req} RUB not found in response")

    observed_offer_ids = {str(p.get("offerId")) for p in _products_from_events(events)}
    for quote in _quotes_from_events(events):
        observed_offer_ids.update(str(line.get("offerId")) for line in quote.get("lines", []))
    for exp_oid in assertions.get("expected_offer_ids", []):
        oid_str = str(exp_oid)
        in_structured = oid_str in observed_offer_ids
        in_text_boundary = bool(re.search(rf"(?<!\d){re.escape(oid_str)}(?!\d)", agent_text))
        if not in_structured and not in_text_boundary:
            errors.append(f"Expected offerId {oid_str} not surfaced in products, quotes, or text")

    if assertions.get("expect_clarification"):
        has_clarif = any(m.get("purpose") == "clarification" for m in agent_msgs)
        if not has_clarif:
            errors.append("Expected clarifying question with purpose='clarification' from agent")

    if assertions.get("expect_legal_entity_caveat"):
        if not LEGAL_ENTITY_CAVEAT_RE.search(agent_text) and not _handoffs_from_events(events):
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
    if assertions.get("expect_handoff"):
        if not handoffs:
            errors.append("Expected handoff event to operator")
        else:
            allowed_reasons = set(assertions.get("allowed_handoff_reasons", []))
            if allowed_reasons and not any(h.get("reason") in allowed_reasons for h in handoffs):
                errors.append(f"Handoff reason {[h.get('reason') for h in handoffs]} not in {sorted(allowed_reasons)}")
            if assertions.get("expect_empty_handoff_products") and not any(h.get("products") == [] for h in handoffs):
                errors.append("Expected handoff with empty products=[] on immediate operator request")

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

    expected_totals = assertions.get("expected_quote_totals_kopecks", [])
    if expected_totals:
        actual_totals = [q.get("totalKopecks") for q in _quotes_from_events(events)]
        for exp in expected_totals:
            if exp not in actual_totals:
                errors.append(f"Expected quote totalKopecks={exp} not found in {actual_totals}")

    expected_revisions = assertions.get("expected_quote_revisions_kopecks", [])
    if expected_revisions:
        actual_totals = [
            q.get("totalKopecks") for q in _quotes_from_events(events) if q.get("totalKopecks") is not None
        ]
        if not all(exp in actual_totals for exp in expected_revisions):
            errors.append(f"Expected quote totals {expected_revisions} in {actual_totals}")

    return errors


class T04HttpClient:
    """Minimal stdlib HTTP client implementing the T04 0.2.0 contract."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        cleaned = base_url.rstrip("/")
        if cleaned.endswith("/api/v1"):
            self.api_base = cleaned
            self.root_base = cleaned[: -len("/api/v1")] or cleaned
        else:
            self.root_base = cleaned
            self.api_base = f"{cleaned}/api/v1"
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return resp.status, payload, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return exc.code, payload, {k.lower(): v for k, v in exc.headers.items()}

    def create_conversation(self, key: str) -> str:
        status, payload, _ = self._request("POST", f"{self.api_base}/conversations", body={}, idempotency_key=key)
        if status != 201 or "conversationId" not in payload:
            raise RuntimeError(f"createConversation failed: HTTP {status} {payload}")
        return str(payload["conversationId"])

    def submit_action(self, conversation_id: str, body: dict, key: str) -> tuple[int, dict, dict[str, str]]:
        status, payload, headers = self._request(
            "POST",
            f"{self.api_base}/conversations/{urllib.parse.quote(conversation_id)}/actions",
            body=body,
            idempotency_key=key,
        )
        if status not in (200, 202):
            raise RuntimeError(f"submitAction failed: HTTP {status} {payload}")
        return status, payload, headers

    def read_events(self, conversation_id: str, after: int = 0) -> dict:
        url = f"{self.api_base}/conversations/{urllib.parse.quote(conversation_id)}/events?after={after}"
        status, payload, _ = self._request("GET", url)
        if status != 200:
            raise RuntimeError(f"readEvents failed: HTTP {status} {payload}")
        return payload

    def read_all_from(
        self,
        conversation_id: str,
        after: int = 0,
        schema_validator: T04SchemaValidator | None = None,
    ) -> tuple[list[dict], int]:
        validator = schema_validator if schema_validator is not None else get_schema_validator()
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

    def probe_today_spend_rub(self) -> float | None:
        for path in ("/health", "/api/telemetry/summary"):
            status, payload, _ = self._request("GET", f"{self.root_base}{path}")
            if status == 200 and isinstance(payload.get("today_spend_rub"), (int, float)):
                return float(payload["today_spend_rub"])
        return None


def _is_meaningful_event(event: dict) -> bool:
    data = event.get("data", {})
    dtype = data.get("type")
    if dtype in {"products", "handoff", "quote", "document", "action_failed"}:
        return True
    if dtype == "message":
        return data.get("author") in {"agent", "manager"} and data.get("purpose") != "greeting"
    return False


def _is_turn_done(action_events: list[dict], action_type: str) -> bool:
    if any(e.get("data", {}).get("type") == "action_failed" for e in action_events):
        return True
    if any(
        e.get("data", {}).get("type") == "message"
        and e.get("data", {}).get("author") in {"agent", "manager"}
        and e.get("data", {}).get("purpose") != "greeting"
        for e in action_events
    ):
        return True
    if action_type == "request_operator" and any(e.get("data", {}).get("type") == "handoff" for e in action_events):
        return True
    if action_type in {"confirm_quote", "request_document"} and any(
        e.get("data", {}).get("type") in {"quote", "document", "handoff"} for e in action_events
    ):
        return True
    return False


def run_case(
    client: T04HttpClient,
    case: dict,
    catalog_index: dict[str, CatalogOffer],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
    settle_seconds: float = 0.4,
    schema_validator: T04SchemaValidator | None = None,
) -> CaseResult:
    """Execute one evaluation scenario against a T04 HTTP endpoint."""
    validator = schema_validator if schema_validator is not None else get_schema_validator()
    case_id = str(case["id"])
    sanitize_outgoing = bool(case.get("sanitize_pii")) or case.get("suite") == "holdout"
    result = CaseResult(
        case_id=case_id,
        suite=str(case.get("suite", "open")),
        category=str(case.get("category", "")),
        passed=False,
        escalated=False,
        closed_by_agent=False,
    )
    try:
        conv_id = client.create_conversation(key=f"eval-conv-{case_id}-{uuid.uuid4().hex[:8]}")
        result.conversation_id = conv_id
        cursor = 0
        full_history: list[dict] = []
        turn_costs: list[float] = []

        for turn_idx, turn in enumerate(case.get("turns", []), start=1):
            action = dict(turn["action"])
            if sanitize_outgoing and action.get("type") == "message" and "text" in action:
                action["text"] = scrub_pii(str(action["text"]))
            action_key = f"eval-act-{case_id}-t{turn_idx}-{uuid.uuid4().hex[:8]}"
            t_start = time.perf_counter()
            status, payload, headers = client.submit_action(conv_id, action, key=action_key)
            if validator.active:
                expected_schema = "Document" if status == 200 else "Receipt"
                result.errors.extend(validator.validate(expected_schema, payload))

            header_cost: float | None = None
            if "x-llm-cost-rub" in headers:
                try:
                    header_cost = float(headers["x-llm-cost-rub"])
                except ValueError:
                    header_cost = None

            if status == 200:
                result.latency_ms.append((time.perf_counter() - t_start) * 1000.0)
                if header_cost is not None:
                    turn_costs.append(header_cost)
                continue

            action_id = str(payload["actionId"])
            action_type = str(action.get("type", ""))
            action_events: list[dict] = []
            first_meaningful_ms: float | None = None
            turn_ready = False
            last_event_at: float | None = None
            deadline = t_start + timeout

            while True:
                batch, cursor = client.read_all_from(conv_id, after=cursor, schema_validator=validator)
                now = time.perf_counter()
                if batch:
                    full_history.extend(batch)
                    last_event_at = now
                    for ev in batch:
                        if ev.get("actionId") == action_id:
                            action_events.append(ev)
                            if first_meaningful_ms is None and _is_meaningful_event(ev):
                                first_meaningful_ms = (now - t_start) * 1000.0
                    if _is_turn_done(action_events, action_type):
                        turn_ready = True

                if any(e.get("data", {}).get("type") == "action_failed" for e in action_events):
                    break
                if turn_ready and last_event_at is not None and (now - last_event_at) >= settle_seconds:
                    break
                if now >= deadline:
                    if not turn_ready:
                        result.timed_out = True
                        result.latency_ms.append((now - t_start) * 1000.0)
                        result.errors.append(
                            f"Turn {turn_idx} timed out after {timeout}s waiting for action {action_id}"
                        )
                    break

                remaining = max(0.0, deadline - now)
                if turn_ready and last_event_at is not None:
                    settle_left = max(0.0, settle_seconds - (now - last_event_at))
                    sleep_s = min(poll_interval, max(0.005, settle_left), remaining)
                else:
                    sleep_s = min(poll_interval, remaining)
                if sleep_s > 0:
                    time.sleep(sleep_s)

            if result.timed_out:
                break

            if first_meaningful_ms is None:
                first_meaningful_ms = (time.perf_counter() - t_start) * 1000.0
            result.latency_ms.append(first_meaningful_ms)
            if result.first_meaningful_latency_ms is None:
                result.first_meaningful_latency_ms = first_meaningful_ms

            # Deduplicate per-turn cost: prefer structured event cost_rub, else header X-LLM-Cost-Rub.
            event_costs = [
                float(ev["data"]["cost_rub"])
                for ev in action_events
                if isinstance(ev.get("data", {}).get("cost_rub"), (int, float))
            ]
            if event_costs:
                turn_costs.append(sum(event_costs))
            elif header_cost is not None:
                turn_costs.append(header_cost)

            turn_errors = validate_case_expectations(action_events, turn.get("assertions", {}), catalog_index)
            result.errors.extend(f"Turn {turn_idx}: {err}" for err in turn_errors)

        replay_events, replay_next_after = client.read_all_from(conv_id, after=0, schema_validator=validator)
        result.errors.extend(validate_independent_cursors(full_history, replay_events, replay_next_after))
        result.errors.extend(validate_contract_events(full_history, conv_id, schema_validator=validator))
        result.errors.extend(validate_case_expectations(full_history, case.get("assertions", {}), catalog_index))

        result.events_count = len(full_history)
        result.agent_responses = [str(m.get("text", "")) for m in _agent_messages(full_history)]
        handoffs = _handoffs_from_events(full_history)
        result.escalated = bool(handoffs)
        result.passed = len(result.errors) == 0
        # Failed scenarios are never credited as closed by the agent.
        result.closed_by_agent = result.passed and not result.escalated
        if turn_costs:
            result.cost_rub = round(sum(turn_costs), 6)
    except Exception as exc:
        result.errors.append(f"Runner error: {exc}")
        result.passed = False
        result.closed_by_agent = False
    return result


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
    suite_spend_delta_rub: float | None = None,
    category_weights: dict[str, float] | None = None,
) -> dict:
    """Aggregate pass/fail, escalation, latency p50/p95, and category-weighted cost metrics."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    escalated = sum(1 for r in results if r.escalated)
    closed_by_agent = sum(1 for r in results if r.passed and not r.escalated)
    timed_out = sum(1 for r in results if r.timed_out)

    all_latencies = [ms for r in results for ms in r.latency_ms]
    p50 = percentile(all_latencies, 50.0)
    p95 = percentile(all_latencies, 95.0)
    mean_lat = (sum(all_latencies) / len(all_latencies)) if all_latencies else None

    explicit_costs = [r.cost_rub for r in results if r.cost_rub is not None]
    avg_cost_rub: float | None = None
    projected_500_rub: float | None = None
    weighted_avg_cost_rub: float | None = None
    weighted_projected_500_rub: float | None = None

    if explicit_costs and len(explicit_costs) == total:
        avg_cost_rub = round(sum(explicit_costs) / total, 4)
        projected_500_rub = round(avg_cost_rub * 500.0, 2)
        if category_weights:
            by_cat: dict[str, list[float]] = {}
            for r in results:
                if r.cost_rub is not None and r.category in category_weights:
                    by_cat.setdefault(r.category, []).append(r.cost_rub)
            weight_sum = sum(category_weights[cat] for cat in by_cat)
            if weight_sum > 0:
                weighted_mean = (
                    sum((sum(vals) / len(vals)) * category_weights[cat] for cat, vals in by_cat.items()) / weight_sum
                )
                weighted_avg_cost_rub = round(weighted_mean, 4)
                weighted_projected_500_rub = round(weighted_mean * 500.0, 2)
    elif suite_spend_delta_rub is not None and total > 0:
        avg_cost_rub = round(max(0.0, suite_spend_delta_rub) / total, 4)
        projected_500_rub = round(avg_cost_rub * 500.0, 2)

    return {
        "schema_version": "1.1",
        "suite": suite,
        "mode": mode,
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": failed,
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
            "avg_cost_per_case_rub": avg_cost_rub,
            "projected_500_cases_per_day_rub": projected_500_rub,
            "weighted_avg_cost_per_case_rub": weighted_avg_cost_rub,
            "weighted_projected_500_cases_per_day_rub": weighted_projected_500_rub,
        },
        "cases": [
            {
                "id": r.case_id,
                "suite": r.suite,
                "category": r.category,
                "passed": r.passed,
                "escalated": r.escalated,
                "closed_by_agent": r.passed and not r.escalated,
                "timed_out": r.timed_out,
                "latency_ms": [round(x, 2) for x in r.latency_ms],
                "cost_rub": r.cost_rub,
                "events_count": r.events_count,
                "errors": r.errors,
                "agent_responses": r.agent_responses,
            }
            for r in results
        ],
    }


def format_markdown_report(summary: dict) -> str:
    """Render concise Markdown report including actual text of every failed response."""
    lat = summary["latency_ms"]
    econ = summary["economics"]
    avg_cost = econ["avg_cost_per_case_rub"]
    proj_500 = econ["projected_500_cases_per_day_rub"]
    w_proj = econ.get("weighted_projected_500_cases_per_day_rub")
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
        f"| Avg cost / 500-day proj (RUB) | {avg_cost} / {proj_500} (weighted 500-day: {w_proj}) |",
        "",
        "| Case ID | Status | Outcome | Latency (ms) | Errors |",
        "|---|---|---|---|---|",
    ]
    failed_items: list[dict] = []
    for item in summary["cases"]:
        status_str = "PASS" if item["passed"] else "FAIL"
        if item["escalated"]:
            outcome = "escalated"
        elif item["passed"]:
            outcome = "agent"
        else:
            outcome = "failed"
        lat_str = ", ".join(str(x) for x in item["latency_ms"]) or "-"
        err_str = "; ".join(item["errors"]) if item["errors"] else "-"
        lines.append(f"| {item['id']} | {status_str} | {outcome} | {lat_str} | {err_str} |")
        if not item["passed"]:
            failed_items.append(item)

    if failed_items:
        lines.extend(["", "## Failed Case Responses", ""])
        for item in failed_items:
            lines.append(f"### {item['id']}")
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
        json_path = out_base
        md_path = out_base.with_suffix(".md")
    elif out_base.suffix == ".md":
        md_path = out_base
        json_path = out_base.with_suffix(".json")
    else:
        json_path = Path(f"{out_base}.json")
        md_path = Path(f"{out_base}.md")

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


def _make_mock_t04_server() -> tuple[ThreadingHTTPServer, str]:
    """Spin up a minimal loopback T04 0.2.0 mock server for --mock-check."""
    lock = threading.Lock()
    state: dict[str, object] = {"seq_counter": 0, "conversations": {}}

    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status_code: int, payload: dict, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"status": "ok", "today_spend_rub": 1.25})
                return
            match = re.match(r"^/api/v1/conversations/([^/]+)/events$", parsed.path)
            if not match:
                self._send_json(404, {"code": "not_found", "message": "Not found"})
                return
            conv_id = urllib.parse.unquote(match.group(1))
            query = urllib.parse.parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            with lock:
                convs: dict = state["conversations"]  # type: ignore[assignment]
                if conv_id not in convs:
                    self._send_json(404, {"code": "not_found", "message": "Conversation not found"})
                    return
                events = convs[conv_id]["events"][after : after + 100]
                next_after = after + len(events)
                has_more = next_after < len(convs[conv_id]["events"])
            self._send_json(200, {"events": events, "nextAfter": next_after, "hasMore": has_more})

        def do_POST(self) -> None:  # noqa: N802
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
                        {
                            "seq": len(events) + 1,
                            "at": "2026-11-26T10:00:00.000Z",
                            "actionId": action_id,
                            "data": data,
                        }
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
                    push(
                        {
                            "type": "message",
                            "author": "customer",
                            "purpose": "input",
                            "text": text,
                            "sources": [],
                        }
                    )
                    if "61557" in text:
                        src = {"url": "https://store.softline.ru/sunrav/-61557/", "label": "Карточка Softline"}
                        push(
                            {
                                "type": "products",
                                "products": [
                                    {
                                        "offerId": "61557",
                                        "name": "SunRav Software BookOffice",
                                        "source": src,
                                        "available": True,
                                        "priceState": "listed",
                                        "unitPriceKopecks": 490000,
                                        "licenseTerm": None,
                                    }
                                ],
                            }
                        )
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
                        push(
                            {
                                "type": "products",
                                "products": [
                                    {
                                        "offerId": "504",
                                        "name": "Adobe InCopy CC",
                                        "source": src,
                                        "available": False,
                                        "priceState": "on_request",
                                        "unitPriceKopecks": None,
                                        "licenseTerm": "12 мес.",
                                    }
                                ],
                            }
                        )
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

            self._send_json(
                202,
                {"conversationId": conv_id, "actionId": action_id},
                extra_headers={"X-LLM-Cost-Rub": "0.12"},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}"


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise RuntimeError(f"Self-check failed: {detail}")


def run_mock_self_check(golden_open_path: Path) -> int:
    """Verify all validators catch synthetic errors and pass valid T04 mock traces."""
    catalog = load_catalog_index(golden_open_path)

    # 1. Negative checks on synthetic broken events
    bad_price_events = [
        {
            "seq": 1,
            "at": "2026-11-26T10:00:00Z",
            "actionId": "a1",
            "data": {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Лицензия стоит 5 555 ₽.",
                "sources": [{"url": "https://store.softline.ru/sunrav/-61557/", "label": "Softline"}],
            },
        }
    ]
    _require(validate_no_hallucinated_prices(bad_price_events, ["4900"], catalog), "price hallucination not caught")

    bad_term_events = [
        {
            "seq": 1,
            "at": "2026-11-26T10:00:00Z",
            "actionId": "a1",
            "data": {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "SunRav BookOffice стоит 4 900 ₽, срок действия 12 мес.",
                "sources": [{"url": "https://store.softline.ru/sunrav/-61557/", "label": "Softline"}],
            },
        }
    ]
    _require(validate_anti_term_hallucination(bad_term_events, "61557"), "term hallucination not caught")

    bad_zero_events = [
        {
            "seq": 1,
            "at": "2026-11-26T10:00:00Z",
            "actionId": "a1",
            "data": {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Adobe InCopy CC стоит 0,00 р.",
                "sources": [{"url": "https://store.softline.ru/adobe/65272676ba01a12-504/", "label": "Softline"}],
            },
        }
    ]
    _require(validate_anti_zero_price(bad_zero_events, "504"), "zero price leak not caught")

    bad_source_events = [
        {
            "seq": 1,
            "at": "2026-11-26T10:00:00Z",
            "actionId": "a1",
            "data": {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Цена 4 900 ₽.",
                "sources": [],
            },
        }
    ]
    _require(validate_sources_present(bad_source_events), "missing source not caught")

    # 2. Positive end-to-end check over loopback T04 HTTP server
    open_data = json.loads(golden_open_path.read_text(encoding="utf-8"))
    mock_case_ids = {"open-price-61557", "open-price-504", "open-robust-operator-button"}
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
    print("Mock self-check passed: all validators and T04 0.2.0 HTTP loop verified.")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Stand base URL (e.g. http://127.0.0.1:8000)")
    parser.add_argument("--suite", choices=["open", "holdout", "all"], default="open")
    parser.add_argument("--mode", choices=["mvp", "full"], default="mvp")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--mock-check", action="store_true", help="Run offline validator and mock T04 checks")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--golden-open", type=Path, default=DEFAULT_GOLDEN_OPEN)
    parser.add_argument("--holdout-file", type=Path, default=None)
    parser.add_argument("--split-summary", type=Path, default=DEFAULT_SPLIT_SUMMARY)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=0.4,
        help="Quiet period after a turn produces a meaningful event before closing the turn",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.mock_check:
        return run_mock_self_check(args.golden_open)

    if not args.base_url:
        raise SystemExit("--base-url is required unless --mock-check is specified")

    holdout_path = args.holdout_file if args.holdout_file is not None else default_holdout_file()
    cases = load_cases(
        suite=args.suite,
        mode=args.mode,
        golden_open_path=args.golden_open,
        holdout_path=holdout_path,
    )
    catalog = load_catalog_index(
        args.golden_open,
        args.catalog,
        load_full_xml=(args.suite in {"holdout", "all"}),
    )

    client = T04HttpClient(args.base_url, timeout=args.timeout)
    spend_before = client.probe_today_spend_rub()
    results = [
        run_case(
            client,
            case,
            catalog,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            settle_seconds=args.settle_seconds,
        )
        for case in cases
    ]
    spend_after = client.probe_today_spend_rub()
    spend_delta = (spend_after - spend_before) if spend_before is not None and spend_after is not None else None

    summary = summarize_results(
        results,
        suite=args.suite,
        mode=args.mode,
        suite_spend_delta_rub=spend_delta,
        category_weights=load_category_weights(args.split_summary),
    )
    json_path, md_path = write_reports(summary, args.out)
    print(format_markdown_report(summary))
    print(f"Reports written: {json_path}, {md_path}")
    return 0 if summary["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
