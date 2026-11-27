"""Check publication, independent buyer access and the actual T04 journal."""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.handoff import OFFER_HELP
from app.agent.knowledge import ARTICLE_PATH
from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say
from app.agent.test_handoff import publish_product
from app.config import get_settings
from app.core.errors import ApiError
from app.db import repository

PATCHES = "/api/v1/manager/kb/patches"
QUESTION = "Где настроить уведомления в Softline Store?"
ANSWER = "Откройте настройки профиля и выберите раздел уведомлений."


def source_case(api, *, resolve=True):
    cid = api.new_conversation()
    api.login()
    assert api.say(cid, "Позовите оператора, помогите с профилем").status_code == 202
    api.idle()
    [handoff] = [e["data"]["handoff"] for e in api.events(cid) if e["data"]["type"] == "handoff"]
    hid = handoff["handoffId"]
    if resolve:
        close(api, hid)
    return cid, hid


def close(api, hid):
    url = f"/api/v1/manager/handoffs/{hid}/resolution"
    result = api.call("manager", "POST", url, json={"type": "close", "reply": "Внутренняя деталь заявки 709."})
    assert result.status_code == 202, result.text


def propose(api, hid, *, question=QUESTION, answer=ANSWER, scope="public_faq"):
    data = {"question": question, "answer": answer, "scope": scope, "handoff_id": hid}
    result = api.call("manager", "POST", PATCHES, json=data)
    assert result.status_code == 201, result.text
    return result.json()["id"]


def ask_new_buyer(api, text):
    cid = api.new_conversation("second")
    assert api.say(cid, text, role="second").status_code == 202
    api.idle()
    return cid, api.events(cid, role="second")[-1]["data"]


def test_publish_and_verify_both_journals(stand_api, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://demo.example.org")
    get_settings.cache_clear()
    api = stand_api
    cid, hid = source_case(api)
    pid = propose(api, hid)
    url = ARTICLE_PATH + pid
    assert api.call("second", "GET", url).status_code == 404
    _, draft_result = ask_new_buyer(api, QUESTION)
    assert draft_result["text"] != ANSWER
    assert api.call("second", "POST", f"{PATCHES}/{pid}/approve").status_code == 403
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200

    new_cid, reply = ask_new_buyer(api, "Подскажите, пожалуйста, ГДЕ настроить уведомления в Softline Store?!")
    assert reply["author"] == "agent" and reply["text"] == ANSWER
    assert [source["url"] for source in reply["sources"]] == ["https://demo.example.org" + url]
    page = api.call("second", "GET", reply["sources"][0]["url"])
    assert page.status_code == 200 and page.text == f"{QUESTION}\n\n{ANSWER}"
    assert "text/plain" in page.headers["Content-Type"]
    assert api.events(cid, role="manager")[-1]["data"]["handoff"]["status"] == "resolved"
    trace_url = f"/api/v1/manager/conversations/{new_cid}/trace"
    assert api.call("manager", "GET", trace_url).status_code == 404
    assert api.say(new_cid, "Позовите оператора", role="second").status_code == 202
    api.idle()
    trace = api.call("manager", "GET", trace_url)
    assert trace.status_code == 200, trace.text
    [step] = trace.json()[0]["steps"]
    assert (step["tool"], step["source_ref"]) == ("published_knowledge", "kb-patch:" + pid)
    assert api.call("second", "GET", f"/api/v1/conversations/{cid}/events").status_code in (403, 404)
    assert ask_new_buyer(api, "Где нельзя настроить уведомления в Softline Store?")[1]["text"] != ANSWER


@pytest.mark.parametrize("case", ["rejected", "old_scope", "deal"])
def test_unpublished_material_is_private(stand_api, case):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    scope = {"old_scope": "general", "deal": "customer"}.get(case, "public_faq")
    pid = propose(api, hid, scope=scope)
    decision = "reject" if case == "rejected" else "approve"
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/{decision}").status_code == 200
    assert api.call("second", "GET", ARTICLE_PATH + pid).status_code == 404
    assert ask_new_buyer(api, QUESTION)[1]["text"] != ANSWER


def test_early_approval_is_retryable_after_close(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api, resolve=False)
    invalid = api.call("manager", "POST", PATCHES, json={"scope": "public_faq", "question": QUESTION, "answer": ANSWER})
    assert invalid.status_code == 422
    pid = propose(api, hid)
    approve = f"{PATCHES}/{pid}/approve"
    assert api.call("manager", "POST", approve).status_code == 409
    assert api.call("manager", "GET", PATCHES, params={"status": "proposed"}).json()[0]["id"] == pid
    assert api.call("second", "GET", ARTICLE_PATH + pid).status_code == 404
    close(api, hid)
    assert api.call("manager", "POST", approve).status_code == 200
    assert ask_new_buyer(api, QUESTION)[1]["text"] == ANSWER


def test_tools_use_new_article_on_first_question_and_explicit_recheck(stand_api, monkeypatch):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    question = "Как изменить адрес доставки?"
    pid = propose(api, hid, question=question)
    # The first buyer asked while the FAQ was still a draft.
    cid, draft = ask_new_buyer(api, question)
    assert draft["text"] != ANSWER
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    monkeypatch.setattr(get_settings(), "agent_engine", "tools")
    assert api.say(cid, "Проверь базу, там это есть", role="second").status_code == 202
    api.idle()
    retry = api.events(cid, role="second")[-1]["data"]
    assert retry["text"] == ANSWER and retry["sources"][0]["url"].endswith(ARTICLE_PATH + pid)
    _, first = ask_new_buyer(api, "Хеллоу, как мне поменять адрес доставки?")
    assert first["text"] == ANSWER and first["sources"] == retry["sources"]


def test_recheck_without_a_specific_question_asks_which(stand_api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "agent_engine", "tools")
    _, reply = ask_new_buyer(stand_api, "Проверьте базу знаний")
    assert reply["text"] == "Какой именно вопрос проверить в базе знаний?" and not reply["sources"]


def test_manager_can_recover_a_premature_approval_from_an_older_database(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api, resolve=False)
    pid = propose(api, hid)
    with closing(sqlite3.connect(get_settings().db_path)) as db:
        db.execute("UPDATE kb_patches SET status='approved', approved_by='manager' WHERE id=?", (pid,))
        db.execute(
            """INSERT INTO kb_articles(url,title,body,section,origin,created_at)
            VALUES (?,?,?,'public_faq','patch','2025-01-01')""",
            ("kb-patch:" + pid, QUESTION, ANSWER),
        )
        db.commit()
    close(api, hid)
    assert api.call("other", "GET", ARTICLE_PATH + pid).status_code == 404
    assert api.call("manager", "GET", "/api/v1/manager/kb/articles").json()[0]["status"] == "proposed"
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    assert ask_new_buyer(api, QUESTION)[1]["text"] == ANSWER


def test_concurrent_publication_has_one_winner(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid)

    async def run():
        return await asyncio.gather(
            *(repository.set_kb_patch_status(pid, "approved", "manager") for _ in range(2)), return_exceptions=True
        )

    outcomes = api.client.portal.call(run)
    assert sum(isinstance(o, ApiError) and o.status == 409 for o in outcomes) == 1
    with closing(sqlite3.connect(get_settings().db_path)) as db:
        assert db.execute("SELECT count(*) FROM kb_articles WHERE url=?", ("kb-patch:" + pid,)).fetchone()[0] == 1


def test_atomic_replacement_withdrawal_and_aliases(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    old = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{old}/approve").status_code == 200
    alias = "Каким способом поменять уведомления в магазине?"
    data = {
        "question": QUESTION,
        "answer": "Новый порядок.",
        "scope": "public_faq",
        "handoff_id": hid,
        "phrasings": [alias],
    }
    new = api.call("manager", "POST", PATCHES, json=data).json()["id"]
    assert api.call("manager", "POST", f"{PATCHES}/{new}/approve", params={"replaces": old}).status_code == 200
    assert api.call("other", "GET", ARTICLE_PATH + old).status_code == 404
    assert ask_new_buyer(api, alias)[1]["text"] == data["answer"]
    assert ask_new_buyer(api, "Как в Softline Store изменить настройки уведомлений?")[1]["text"] == data["answer"]
    assert ask_new_buyer(api, alias.replace("поменять", "не поменять"))[1]["text"] != data["answer"]
    third = propose(api, hid, answer="Следующая версия.")
    assert api.call("manager", "POST", f"{PATCHES}/{third}/approve", params={"replaces": old}).status_code == 409
    assert api.call("other", "GET", ARTICLE_PATH + third).status_code == 404
    assert api.call("second", "POST", f"{PATCHES}/{new}/withdraw").status_code == 403
    assert api.call("manager", "POST", f"{PATCHES}/{new}/withdraw").status_code == 204
    assert api.call("other", "GET", ARTICLE_PATH + new).status_code == 404
    assert ask_new_buyer(api, alias)[1]["text"] != data["answer"]
    audit = {r["id"]: r for r in api.call("manager", "GET", "/api/v1/manager/kb/articles").json()}
    assert audit[old]["replacement_id"] == new and audit[new]["decided_by"] == "manager"


def test_conflict_and_mandatory_handoff(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    tender = "Для закупки по 44-ФЗ нужна анкета поставщика."
    for question, answer in [(QUESTION, ANSWER), (QUESTION, "Другой порядок."), (tender, "Можно без проверки.")]:
        pid = propose(api, hid, question=question, answer=answer)
        assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    cid, conflict = ask_new_buyer(api, QUESTION)
    assert conflict["text"] == OFFER_HELP and not conflict["sources"]
    assert api.say(cid, "Да", role="second").status_code == 202
    api.idle()
    assert api.events(cid, role="second")[-1]["data"]["handoff"]["question"] == QUESTION
    _, result = ask_new_buyer(api, tender)
    assert result["handoff"]["reason"] == "tender"


def test_published_question_cannot_override_a_catalog_price_request(stand_api):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    question = "Сколько стоит эта лицензия?"
    pid = propose(api, hid, question=question, answer="Ошибочная фиксированная цена.")
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    cid = api.new_conversation()
    publish_product(api, cid, None)
    assert api.say(cid, question).status_code == 202
    api.idle()
    assert api.events(cid)[-1]["data"]["handoff"]["reason"] == "price_request"


def test_faq_keeps_prior_catalog_selection(stand_api, index):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    cid = api.new_conversation()
    _, initial, _ = say(api, cid, "Покупаем впервые MDaemon на 20 пользователей для юрлица")
    assert initial["products"][0]["offerId"] == "new20"
    assert say(api, cid, QUESTION)[-1]["text"] == ANSWER
    _, after, _ = say(api, cid, "Продлеваем, подписка действует.")
    assert after["products"][0]["offerId"] == "renew"


@pytest.mark.parametrize("question", ["Цена этой лицензии", "Назовите цену лицензии", "Цена этой лицензии?"])
def test_catalog_wins_over_published_amount(stand_api, index, question):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid, question=question, answer="Цена 7000 рублей.")
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    cid = api.new_conversation()
    say(api, cid, "Покупаем впервые MDaemon на 20 пользователей для юрлица")
    answer = say(api, cid, question)[-1]
    assert "1 234,56 ₽" in answer["text"] and "/store/new20" in answer["sources"][0]["url"]


@pytest.mark.parametrize(
    "question",
    [
        "Как поменять электронную почту в профиле?",
        "Как отправить запрос на смену адреса электронной почты?",
        "Куда отправить запрос в Softline Store?",
    ],
)
def test_approved_procedure_can_interrupt_discovery(stand_api, question):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid, question=question)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    cid = api.new_conversation()
    say(api, cid, "Не знаю, что выбрать")
    assert say(api, cid, question)[-1]["text"] == ANSWER


def pilot_draft(api, cid, question):
    say(api, cid, question)
    drafts = api.call("manager", "GET", "/api/v1/manager/drafts").json()
    return next(d for d in drafts if d["conversation_id"] == cid)


def send_draft(api, draft, text=None):
    return api.call(
        "manager", "POST", f"/api/v1/manager/drafts/{draft['id']}/send", json={} if text is None else {"text": text}
    )


def test_faq_between_sent_pilot_drafts_keeps_renewal_on_operator_card(stand_api, index):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    api.call("manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot"})
    cid = api.new_conversation()
    for q in ["Покупаем впервые MDaemon на 20 пользователей для юрлица", QUESTION, "Продлеваем, подписка действует."]:
        assert send_draft(api, pilot_draft(api, cid, q)).status_code == 200
    card = say(api, cid, "Позовите оператора")[-1]["handoff"]
    assert [(p["offerId"], p["unitPriceKopecks"]) for p in card["products"]] == [("renew", 87500)]


@pytest.mark.parametrize("decision", ["withdraw", "replace"])
@pytest.mark.parametrize("base", ["https://demo.example.org", "https://demo.example.org/support"])
def test_stale_pilot_draft_must_be_revised_before_delivery(stand_api, decision, base, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    get_settings.cache_clear()
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    api.call("manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot"})
    cid = api.new_conversation()
    draft = pilot_draft(api, cid, QUESTION)
    assert send_draft(api, draft, ANSWER).status_code == 200
    assert api.events(cid)[-1]["data"]["sources"]
    draft = pilot_draft(api, cid, QUESTION)
    if decision == "replace":
        new = propose(api, hid, answer="Другая процедура.")
        assert api.call("manager", "POST", f"{PATCHES}/{new}/approve", params={"replaces": pid}).status_code == 200
    else:
        assert api.call("manager", "POST", f"{PATCHES}/{pid}/withdraw").status_code == 204
    previous = api.events(cid)
    assert send_draft(api, draft).status_code == 409
    assert previous == api.events(cid)
    assert send_draft(api, draft, "Помогу изменить настройки вручную.").status_code == 200
    assert api.events(cid)[-1]["data"]["sources"] == []


def test_seat_answer_after_a_faq_cannot_become_an_article_number(stand_api, index):  # noqa: F811
    api = stand_api
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    with closing(sqlite3.connect(index)) as db:
        db.execute("UPDATE offers SET id='20' WHERE id='desk'")
        db.commit()
    cid = api.new_conversation()
    assert "На сколько пользователей" in say(api, cid, "Покупаем впервые MDaemon для юрлица")[-1]["text"]
    assert say(api, cid, QUESTION)[-1]["text"] == ANSWER
    _, cards, _ = say(api, cid, "20")
    assert [p["offerId"] for p in cards["products"]] == ["new20"]
