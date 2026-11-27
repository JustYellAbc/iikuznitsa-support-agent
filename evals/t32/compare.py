"""Run identical synthetic buyer dialogs through a local T04 server; inspect the corresponding T08 trace."""

import argparse
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PUBLIC_QUESTION = "Где включить уведомления об обновлениях в тестовом кабинете Softline?"
PUBLIC_ANSWER = "Откройте настройки уведомлений, выберите «Обновления продуктов» и сохраните выбор."


def post(client, endpoint, payload=None):
    return client.post(endpoint, headers={"Idempotency-Key": str(uuid.uuid4())}, json=payload).raise_for_status().json()


def publish_example(url, password):
    with httpx.Client(base_url=url, trust_env=False) as buyer, httpx.Client(base_url=url, trust_env=False) as manager:
        post(manager, "auth/manager/login", {"username": "manager", "password": password})
        cid = post(buyer, "conversations")["conversationId"]
        post(buyer, f"conversations/{cid}/actions", {"type": "message", "text": "Позовите оператора по уведомлениям."})
        for _ in range(20):
            events = buyer.get(f"conversations/{cid}/events", params={"after": 0}).raise_for_status().json()["events"]
            handoffs = [e["data"]["handoff"] for e in events if e["data"]["type"] == "handoff"]
            if handoffs:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Setup case did not reach manager")
        hid = handoffs[0]["handoffId"]
        post(manager, f"manager/handoffs/{hid}/resolution", {"type": "close", "reply": PUBLIC_ANSWER})
        patch = post(
            manager,
            "manager/kb/patches",
            {
                "question": PUBLIC_QUESTION,
                "answer": PUBLIC_ANSWER,
                "scope": "public_faq",
                "handoff_id": hid,
            },
        )
        post(manager, f"manager/kb/patches/{patch['id']}/approve")


def cases():
    return [
        ("listed", "Подскажите цену по артикулу 61557.", "61557"),
        ("password", "Забыл пароль Softline ID. Как восстановить доступ?", "password"),
        ("request_price", "Сколько стоит артикул 504?", "price_request"),
        ("selection", "Сколько стоит артикул 19576 для компании? Покупаем впервые.", "19576"),
        ("change", "А если для той же компании нужно всего пять пользователей? Покупаем впервые.", "19574"),
        ("tender", "Закупка тендерная, нужны документы.", "tender"),
    ]


def correct(expected, text, products, handoffs, sources):
    reasons = [h["reason"] for h in handoffs]
    if expected in {"tender", "price_request"}:
        expected_ids = ["19574"] if expected == "tender" else ["504"]
        return reasons == [expected] and [p["offerId"] for p in handoffs[0]["products"]] == expected_ids
    if expected == "password":
        return (
            bool(sources)
            and not reasons
            and "https://login.softline.com/password" in text
            and "почт" in text.casefold()
        )
    prices = {"19576": "77556", "19574": "47808", "61557": "4900"}
    compact = "".join(text.replace("\xa0", " ").split())
    return products == [expected] and prices[expected] + "₽" in compact and bool(sources) and not reasons


def run(port, data_dir, published=False):
    rows = []
    url = f"http://127.0.0.1:{port}/api/v1/"
    scenario = cases()
    if published:
        publish_example(url, os.environ["OPERATOR_PASSWORD"])
        scenario.append(("published", PUBLIC_QUESTION, "published"))
    with (
        httpx.Client(base_url=url, trust_env=False) as client,
        sqlite3.connect(f"file:{data_dir}/app.db?mode=ro", uri=True) as db,
    ):
        for name, prompt, expected in scenario:
            key = {"Idempotency-Key": str(uuid.uuid4())}
            if name not in {"change", "tender"}:
                cid = client.post("conversations", headers=key).raise_for_status().json()["conversationId"]
            start = time.monotonic()
            action = (
                client.post(
                    f"conversations/{cid}/actions",
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    json={"type": "message", "text": prompt},
                )
                .raise_for_status()
                .json()["actionId"]
            )
            while time.monotonic() - start < 60:
                if db.execute(
                    "SELECT 1 FROM events WHERE conversation_id=? AND action_id=? "
                    "AND json_extract(data,'$.author')='agent'",
                    (cid, action),
                ).fetchone():
                    break
                time.sleep(0.06)
            else:
                raise TimeoutError(f"No reply for {name}")
            elapsed = round(time.monotonic() - start, 3)
            raw = client.get(f"conversations/{cid}/events", params={"after": 0}).raise_for_status().json()
            events = [e["data"] for e in raw["events"] if e["actionId"] == action]
            replies = [e for e in events if e.get("author") == "agent" and e.get("purpose") != "greeting"]
            products = [p["offerId"] for e in events if e["type"] == "products" for p in e["products"]]
            handoffs = [e["handoff"] for e in events if e["type"] == "handoff"]
            text = replies[-1]["text"]
            steps = json.loads(
                db.execute("SELECT steps FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()[
                    0
                ]
            )
            sources = replies[-1].get("sources", [])
            if expected == "published":
                article = urlsplit(sources[0]["url"]).path if sources else ""
                passed = (
                    not handoffs
                    and text == PUBLIC_ANSWER
                    and article.startswith("/api/v1/knowledge/")
                    and client.get(f"http://127.0.0.1:{port}{article}").raise_for_status().text.endswith(PUBLIC_ANSWER)
                )
            else:
                passed = correct(expected, text, products, handoffs, sources)
            row = dict(
                case=name,
                correct=passed,
                elapsed_s=elapsed,
                products=products,
                handoffs=[h["reason"] for h in handoffs],
                handoff_products=[[p["offerId"] for p in h["products"]] for h in handoffs],
                text=text,
                tools=[s["tool"] for s in steps],
                tokens_in=sum(s["tokens_in"] for s in steps),
                tokens_out=sum(s["tokens_out"] for s in steps),
            )
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--published", action="store_true", help="Publish a synthetic FAQ with local OPERATOR_PASSWORD")
    args = parser.parse_args()
    run(args.port, args.data_dir, args.published)
