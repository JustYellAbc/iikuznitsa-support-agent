"""Customer-visible replies for complete, partial and conflicting catalog evidence."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env
from tests.test_t32_agent import provider as provider
from tests.test_t32_agent import tool

from app.agent.handoff import ASK_DETAIL, offers_operator
from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say
from app.agent.tool_agent import Answer, Evidence, checked_answer
from app.config import get_settings


def test_capabilities_do_not_replace_an_existing_selection(stand_api, provider, index):  # noqa: F811
    answers, requests = provider
    answers.extend([tool("get_offer", offer_id="new20"), tool("respond", kind="product", refs=["r1"])])
    cid = stand_api.new_conversation()
    say(stand_api, cid, "Покажите артикул new20")
    for question in ["Что ты умеешь?", "А ты сам чем можешь помочь?"]:
        answers.append(tool("respond", kind="capabilities"))
        reply = say(stand_api, cid, question)
        assert len(reply) >= 1 and "подключить менеджера" in reply[-1]["text"]
        assert not any(e["type"] in {"products", "handoff"} for e in reply)
    assert stand_api.say(cid, "Подготовить КП").headers["X-Chat-Action"] == "open-quote-form"
    assert len(requests) == 3


def test_capabilities_of_named_product_still_use_catalog(stand_api, provider, index):  # noqa: F811
    answers, requests = provider
    answers.extend([tool("get_offer", offer_id="new20"), tool("respond", kind="product", refs=["r1"])])
    reply = say(stand_api, stand_api.new_conversation(), "Что умеет MDaemon, артикул new20?")
    assert len(requests) == 2 and any(e["type"] == "products" for e in reply)


def test_order_tracking_reads_the_verified_guide_before_asking_for_a_product(stand_api, provider, index):  # noqa: F811
    body = "Откройте личный кабинет ( https://lk.softline.ru/ ). Найдите заказ в списке."
    with closing(sqlite3.connect(get_settings().catalog_index_path)) as db:
        db.execute(
            "INSERT INTO knowledge(title,body,source) VALUES(?,?,?)",
            ("Просмотр статуса заказа", body, "https://docs.softline.com/ru-ru/lk/view-statuses"),
        )
        db.commit()
    events = say(stand_api, stand_api.new_conversation(), "Где в личном кабинете посмотреть статус моего заказа?")
    assert events[-1]["sources"][0]["url"] == "https://lk.softline.ru/" and "Найдите заказ" in events[-1]["text"]
    assert not provider[1]


def test_team_size_and_named_product_are_kept_before_another_model_call(stand_api, provider):  # noqa: F811
    answers, requests = provider
    answers.append(tool("respond", kind="clarify", question="Какое решение нужно для вашей закупки?"))
    cid = stand_api.new_conversation()
    say(stand_api, cid, "Нужен полный комплект на 1000 человек")
    reply = say(stand_api, cid, "1С Бухгалтерия")
    assert len(requests) == 1 and "1000" in reply[-1]["text"] and reply[-1]["purpose"] == "clarification"
    reply = say(stand_api, cid, "Это всего по компании")
    assert len(requests) == 1 and "Сколько сотрудников" in reply[-1]["text"]


@pytest.mark.parametrize(
    ("source", "excerpt", "account_link"),
    [
        (
            "https://docs.softline.com/ru-ru/lk/view-statuses",
            "Откройте личный кабинет ( https://lk.softline.ru/ ).",
            True,
        ),
        ("https://docs.softline.com/ru-ru/lk/view-statuses", "Заказ отображается после оплаты.", False),
        ("https://other.example/ru-ru/lk/view-statuses", "Откройте личный кабинет.", False),
    ],
)
def test_account_action_and_reference_have_separate_links(source, excerpt, account_link):
    row = {"title": "Просмотр статуса заказа", "source": source, "body": excerpt + " https://lk.softline.ru/"}
    result = checked_answer(
        Answer(kind="knowledge", refs=["r1"], excerpt=excerpt), Evidence(rows={"r1": row}), [], "", []
    )
    assert [s.url for s in result[3]] == (["https://lk.softline.ru/", source] if account_link else [source])
    if account_link:
        assert result[0] == "Откройте личный кабинет." and result[3][0].label == "Открыть личный кабинет"


@pytest.mark.parametrize(("family", "choice"), [("1С", "1С Бухгалтерия"), ("Renga", "Renga Professional")])
def test_short_answers_continue_sizing_and_reach_manager_with_scope(family, choice):
    def msg(role, text):
        return SimpleNamespace(role=role, text=text)

    initial = f"Нужен комплект {family} на 150 человек"
    history = [msg("client", initial), msg("agent", "Какое решение нужно?")]
    handoff = Answer(kind="handoff", reason="outside_catalog")
    misses = Evidence(catalog_miss=True)
    scope, purpose, _, _, case = checked_answer(handoff, misses, history, choice, [])
    assert "150" in scope and purpose == "clarification" and case is None
    history.extend([msg("client", choice), msg("agent", scope)])
    fewer = checked_answer(handoff, misses, history, "Это всего в компании", [])
    assert "Сколько сотрудников" in fewer[0] and fewer[-1] is None
    direct = checked_answer(handoff, misses, history, "150 одновременных пользователей", [])
    assert direct[-1].reason == "outside_catalog" and initial in direct[-1].question and choice in direct[-1].question


def offer(sku, name, **details):
    return {
        "offerId": sku,
        "name": name,
        "source": f"https://example.org/products/{sku}",
        "priceState": "listed",
        "unitPriceKopecks": 120000,
        "available": True,
        "licenseTerm": None,
        **details,
    }


def reply(rows, *, text="Подскажите условия", history=(), kind="product", **kwargs):
    refs = {f"r{i}": row for i, row in enumerate(rows, start=1)}
    evidence = Evidence(rows=refs, detailed=set(refs))
    return checked_answer(Answer(kind=kind, refs=list(refs), **kwargs), evidence, history, text, [])


def test_selected_offer_with_sparse_attributes_can_be_presented_for_purchase():
    row = offer("a", "Design Suite Professional", edition=None, platform=None)
    body, purpose, cards, sources, case = reply([row], field="summary", text="Профессиональную, одну")
    assert "Design Suite Professional" in body and "1 200 ₽" in body
    assert purpose == "answer" and cards[0].source == sources[0] and case is None
    assert not offers_operator(body)


@pytest.mark.parametrize(
    ("field", "detail", "known", "missing"),
    [
        ("platform", "platform", ["Linux"], "Поддерживаемые платформы не указаны"),
        ("term", "licenseTerm", "12 месяцев", "Срок лицензии не указан"),
        ("delivery", "delivery", ["Электронная"], "Способ поставки не указан"),
        ("licenseType", "licenseType", ["Продление"], "Тип лицензии не указан"),
        ("edition", "edition", "Plus", "Редакция отдельно не указана"),
    ],
)
def test_unknown_attribute_is_scoped_to_its_sku(field, detail, known, missing):
    rows = [offer("a", "Design Suite", **{detail: known}), offer("b", "Mail Pro")]
    body, purpose, cards, sources, case = reply(rows, field=field)
    first, second = body.splitlines()[:2]
    assert (known if isinstance(known, str) else known[0]) in first and missing not in first
    assert "b" in second and missing in second and "1 200 ₽" in second
    assert offers_operator(body) and purpose == "clarification" and case is None
    assert [card.source for card in cards] == sources and len(sources) == 2


def test_official_name_can_answer_edition_but_cannot_confirm_platform_or_rights():
    row = offer("sku", "Design Suite Enterprise", edition=None, platform=None)
    body, purpose, *_ = reply([row], field="edition", title_edition="Enterprise")
    assert "«Enterprise»" in body and purpose == "answer" and not offers_operator(body)
    with pytest.raises(ValueError):
        reply([row], field="edition", title_edition="Enter")
    with pytest.raises(ValueError):
        reply([row], field="platform", title_edition="Enterprise")


def test_conflicting_edition_quotes_both_values_and_leaves_decision_open():
    row = offer("sku", "Design Suite Professional", edition="Standard")
    body, purpose, cards, sources, case = reply([row], field="edition", title_edition="Professional")
    assert "«Professional»" in body and "«Standard»" in body
    assert purpose == "clarification" and offers_operator(body) and case is None and cards[0].source == sources[0]


@pytest.mark.parametrize(
    ("options", "selection", "name", "ambiguous"),
    [
        ("Windows 10 Home или Windows 11 Home?", "хоум", "Windows 11 Home", True),
        ("Mail Suite Basic или Mail Suite Business?", "Mail Suite", "Mail Suite Basic", True),
        ("Нужна 1С:ЗУП ПРОФ или 1С:ЗУП КОРП?", "ЗУП?", "1С:ЗУП КОРП", True),
        ("На один год или на три года?", "три", "Редактор на один год", True),
        ("Mail Suite Basic или Mail Suite Business?", "Business", "Mail Suite Business", False),
        ("Какую редакцию: Home или Pro?", "Pro, одну", "Windows 11 Pro", False),
    ],
)
def test_only_distinguishing_choice_can_select_product(options, selection, name, ambiguous):
    row = offer("suite-1", name)
    body, purpose, cards, sources, case = reply(
        [row], field="summary", history=[SimpleNamespace(role="agent", text=options)], text=selection
    )
    assert sources[0].url == row["source"] and name in body and "1 200 ₽" in body and case is None
    if ambiguous:
        assert purpose == "clarification" and not cards and body.endswith("?") and not offers_operator(body)
    else:
        assert purpose == "answer" and cards[0].offerId == "suite-1"


def test_unjustified_model_handoff_answers_the_requested_attribute_of_the_verified_item():
    row = offer("sku", "Design Suite")
    body, purpose, cards, _, case = reply(
        [row], kind="handoff", text="Какие поддерживаемые платформы указаны для артикула sku?"
    )
    assert "Поддерживаемые платформы не указаны" in body and "1 200 ₽" in body and offers_operator(body)
    assert purpose == "clarification" and cards[0].offerId == "sku" and case is None


def test_missing_attribute_does_not_short_circuit_mandatory_route():
    row = offer("sku", "Design Suite")
    body, purpose, _, _, case = reply([row], field="term", text="Нужны тендерные документы и срок")
    assert "Срок лицензии не указан" in body and purpose == "notice" and case.reason == "tender"
    assert not offers_operator(body)


def test_repeated_unresolved_question_preserves_verified_product():
    row = offer("sku", "Design Suite")
    body, purpose, cards, _, case = reply(
        [row], kind="handoff", history=[SimpleNamespace(role="agent", text=ASK_DETAIL)]
    )
    assert offers_operator(body) and "Товар найден" in body and purpose == "clarification"
    assert cards[0].offerId == "sku" and case is None


@pytest.mark.usefixtures("index")
@pytest.mark.parametrize("confirmation", ["Да", "Подходит"])
def test_buyers_confirmation_rechecks_candidate_before_quote(stand_api, provider, confirmation):  # noqa: F811
    answers, requests = provider
    answers.extend(
        [
            tool("respond", kind="clarify", question="Вам нужен PaperDesk Pro или PaperDesk Basic?"),
            tool("search_catalog", query="PaperDesk Pro"),
            tool("get_offer", offer_id="desk"),
            tool("respond", kind="product", field="summary", refs=["r1"]),
        ]
    )
    cid = stand_api.new_conversation()
    say(stand_api, cid, "Нужна лицензия PaperDesk")
    pending = say(stand_api, cid, "PaperDesk")
    assert pending[-1]["purpose"] == "clarification" and "790 ₽" in pending[-1]["text"]
    assert not any(e["type"] in {"products", "handoff"} for e in pending)
    with closing(sqlite3.connect(get_settings().catalog_index_path)) as db:
        db.execute("UPDATE offers SET price='795' WHERE id='desk'")
        db.commit()
    selected = say(stand_api, cid, confirmation)
    assert [p["offerId"] for e in selected if e["type"] == "products" for p in e["products"]] == ["desk"]
    assert selected[-1]["purpose"] == "answer" and "795 ₽" in selected[-1]["text"] and len(requests) == 4
    assert stand_api.say(cid, "Подготовить КП").headers["X-Chat-Action"] == "open-quote-form"
