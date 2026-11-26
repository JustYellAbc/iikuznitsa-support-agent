import sqlite3
from contextlib import closing

from tests.conftest import env as env

from app.agent.catalog_selection import Selection, select, selection_for
from app.agent.test_catalog_selection import index as index


def test_direct_article_retains_its_feed_family_and_terms(index):  # noqa: F811
    with closing(sqlite3.connect(index)) as db:
        db.execute("UPDATE offers SET group_id='mail' WHERE id IN ('new20','new5','renew','late')")
        db.commit()
    initial = select(index, selection_for("Артикул new20", None))
    context = initial.followup
    assert (context.group_id, context.seats, context.buyer, context.kind, context.months) == (
        "mail",
        20,
        "Юрлицо",
        "Полная версия",
        12,
    )
    for text, expected in (
        ("А на 5 пользователей какая цена и срок?", ["new5"]),
        ("А если продлеваем действующую подписку, какая цена и срок?", ["renew"]),
        ("Оформляем на физлицо", []),
    ):
        changed = context.qualifiers(text, followup=True)
        assert [p.offerId for p in select(index, changed).products] == expected


def test_bare_number_disambiguates_seat_prompt_and_article():
    previous = Selection("MDaemon Messaging Server", buyer="Юрлицо", kind="Полная версия")
    changed = selection_for("100", previous, awaiting_seats=True)
    assert changed.name == previous.name and changed.seats == 100
    assert selection_for("504", previous) == Selection("504", code=True)
    assert selection_for("у артикула 61557", previous) == Selection("61557", code=True)
    chosen = selection_for("504", previous, options=["504"], awaiting_seats=True)
    assert chosen.name == "504" and chosen.code and chosen.seats is None and chosen.buyer == "Юрлицо"
