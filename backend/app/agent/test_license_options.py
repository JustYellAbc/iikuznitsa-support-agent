from types import SimpleNamespace

import pytest

from app.agent.license_options import (
    alternate_proposals,
    followup_capacities,
    kind_before_name,
    license_dialog_context,
    license_selection,
    quote_preview,
    resolve_options,
    scoped_turns,
)
from app.agent.test_catalog_selection import index as index
from app.agent.tool_agent import LicenseVariant
from app.config import get_settings


def test_separate_capacities_are_bound_to_the_customer_line(index):  # noqa: F811
    turns = ["Atlas Desk на 10 пользователей и Orion Mail на 40 пользователей"]
    items = [LicenseVariant(name="Atlas Desk", seats=40), LicenseVariant(name="Orion Mail", seats=10)]
    with pytest.raises(ValueError, match="Вместимость|число мест"):
        resolve_options(index, items, turns)


def test_unmentioned_product_or_capacity_cannot_be_substituted():
    turns = ["Нужен Northwind Editor на 15 пользователей"]
    with pytest.raises(ValueError, match="дословно"):
        license_selection("Northwind Pro", 15, turns)
    with pytest.raises(ValueError, match="Вместимость"):
        license_selection("Northwind Editor", 150, turns)


def test_identifying_a_renewal_and_answering_its_seat_question_preserves_both_fields(index):  # noqa: F811
    history = [
        SimpleNamespace(role=role, text=body)
        for role, body in [
            ("client", "Давайте одну продлим"),
            ("agent", "Какой артикул или название продлеваемой лицензии?"),
            ("client", "MDaemon Messaging Server"),
            ("agent", "На сколько пользователей нужна лицензия?"),
        ]
    ]
    variants = [LicenseVariant(name="MDaemon Messaging Server", seats=20)]
    turns = license_dialog_context(history, "на 20")
    kind = kind_before_name(history, turns, variants)
    assert kind == "Продление"
    [hit] = resolve_options(index, variants, turns, prior_kind=kind)
    assert "ещё действует или уже истекла" in hit.text
    history[-1].text = "На сколько месяцев нужна лицензия?"
    assert license_dialog_context(history, "на 20")[-1] == "на 20"
    history[1].text = "Какой продукт теперь нужен?"
    assert not kind_before_name(history, turns, variants)


def test_quote_preview_uses_new_catalog_products_and_policy(index):  # noqa: F811
    inputs = ["Нужно КП на PaperDesk Pro для юрлица, новая покупка."]
    [hit] = resolve_options(index, [LicenseVariant(name="PaperDesk Pro")], inputs)
    body = quote_preview(get_settings(), [hit], inputs)
    assert "арт. desk" in body and "790 ₽" in body
    unpriced = ["Хочу купить Tool Suite для юрлица, новая покупка."]
    hits = resolve_options(index, [LicenseVariant(name="Tool Suite")], unpriced)
    assert not quote_preview(get_settings(), hits, unpriced)
    assert not quote_preview(get_settings(), [hit, hit], inputs)


def test_multiple_purchase_conditions_are_bound_to_each_line():
    variants = [LicenseVariant(name="Atlas"), LicenseVariant(name="Orion")]
    for turns in [
        ["Нужна новая лицензия Atlas на 10 пользователей и продление Orion на 40 пользователей."],
        ["Нужна новая лицензия Atlas на 10 пользователей.", "Добавьте продление Orion на 40 пользователей."],
    ]:
        items = [license_selection(v.name, v.seats, scoped_turns(v.name, variants, turns)) for v in variants]
        assert [(s.kind, s.seats) for s in items] == [("Полная версия", 10), ("Продление", 40)]


def test_excluded_product_and_unverified_platform_are_not_offered(index):  # noqa: F811
    with pytest.raises(ValueError, match="исключил"):
        resolve_options(
            index, [LicenseVariant(name="PaperDesk Pro")], ["Хочу другую программу. PaperDesk Pro не предлагайте."]
        )
    for condition in ("только Linux", "коробочная поставка"):
        [hit] = resolve_options(
            index, [LicenseVariant(name="PaperDesk Pro")], [f"Новая лицензия PaperDesk Pro для юрлица, {condition}."]
        )
        assert not hit.products and hit.purpose == "clarification"
    [hit] = resolve_options(
        index,
        [LicenseVariant(name="PaperDesk Pro")],
        ["Новая лицензия PaperDesk Pro для юрлица, электронная поставка."],
    )
    assert [p.offerId for p in hit.products] == ["desk"]


def test_alternative_quantities_do_not_become_a_basket():
    variants = [LicenseVariant(name="Atlas", seats=n) for n in (5, 20)]
    assert alternate_proposals(["Нужен расчёт Atlas на 5 и 20 пользователей."], variants)
    assert alternate_proposals(["Два отдельных КП на Atlas и Orion.", "Для юрлица."])


@pytest.mark.parametrize("terms", ["на 12 и 24 месяца", "на 12 месяцев и на 24 месяца"])
def test_duration_comparison_cannot_silently_select_first_term(terms):
    reply = f"Первая покупка. Покажите варианты {terms}."
    assert followup_capacities(reply) is None
    with pytest.raises(ValueError, match="несколько сроков"):
        license_selection("Atlas Desk", None, ["Нужен Atlas Desk для компании", reply])


@pytest.mark.parametrize("negative", ["только не Windows", "только не коробочная поставка", "Linux не подходит"])
def test_negative_technical_constraint_requires_clarification(negative):
    with pytest.raises(ValueError, match="исключил это условие"):
        license_selection("Nova Studio", 10, [f"Nova Studio на 10 пользователей, {negative}."])
