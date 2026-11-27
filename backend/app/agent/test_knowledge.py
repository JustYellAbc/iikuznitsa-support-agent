"""Check publication, independent buyer access and the actual T04 journal."""

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


@pytest.mark.parametrize("case", ["rejected", "old_scope", "deal", "missing_handoff", "approved_before_close"])
def test_unpublished_material_is_private(stand_api, case):  # noqa: F811
    api = stand_api
    _, hid = source_case(api, resolve=case != "approved_before_close")
    scope = {"old_scope": "general", "deal": "customer"}.get(case, "public_faq")
    pid = propose(api, None if case == "missing_handoff" else hid, scope=scope)
    decision = "reject" if case == "rejected" else "approve"
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/{decision}").status_code == 200
    if case == "approved_before_close":
        close(api, hid)
    assert api.call("second", "GET", ARTICLE_PATH + pid).status_code == 404
    assert ask_new_buyer(api, QUESTION)[1]["text"] != ANSWER


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
