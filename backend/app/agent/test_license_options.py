import pytest

from app.agent.license_options import license_selection, quote_preview, resolve_options
from app.agent.test_catalog_selection import index as index
from app.agent.tool_agent import LicenseVariant
from app.config import get_settings


def test_separate_capacities_are_bound_to_the_customer_line(index):  # noqa: F811
    turns = ["Atlas Desk на 10 пользователей и Orion Mail на 40 пользователей"]
    items = [LicenseVariant(name="Atlas Desk", seats=40), LicenseVariant(name="Orion Mail", seats=10)]
    with pytest.raises(ValueError, match="число мест"):
        resolve_options(index, items, turns)


def test_unmentioned_product_or_capacity_cannot_be_substituted():
    turns = ["Нужен Northwind Editor на 15 пользователей"]
    with pytest.raises(ValueError, match="дословно"):
        license_selection("Northwind Pro", 15, turns)
    with pytest.raises(ValueError, match="Вместимость"):
        license_selection("Northwind Editor", 150, turns)


def test_quote_preview_uses_new_catalog_products_and_policy(index):  # noqa: F811
    inputs = ["Нужно КП на PaperDesk Pro для юрлица, новая покупка."]
    [hit] = resolve_options(index, [LicenseVariant(name="PaperDesk Pro")], inputs)
    body = quote_preview(get_settings(), [hit], inputs)
    assert "арт. desk" in body and "790 ₽" in body
    unpriced = ["Хочу купить Tool Suite для юрлица, новая покупка."]
    hits = resolve_options(index, [LicenseVariant(name="Tool Suite")], unpriced)
    assert not quote_preview(get_settings(), hits, unpriced)
    assert not quote_preview(get_settings(), [hit, hit], inputs)


def test_multiple_purchase_conditions_need_line_level_confirmation(index):  # noqa: F811
    names = [LicenseVariant(name="Atlas"), LicenseVariant(name="Orion")]
    with pytest.raises(ValueError, match="Разные условия"):
        resolve_options(index, names, ["Нужна новая лицензия Atlas и продление Orion."])
