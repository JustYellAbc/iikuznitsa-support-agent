"""Public FAQ curated by the manager from resolved support cases (T15)."""

import re
from time import perf_counter

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.agent.handoff import OFFER_HELP
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, StepTrace
from app.contracts.t04 import Id, Source
from app.core.errors import not_found
from app.db.database import get_db
from app.db.repository import live_article, new_id

router = APIRouter()
ARTICLE_PATH = "/api/v1/knowledge/"
INTRO = re.compile(
    r"^(?:(?:здравствуйте|добрый (?:день|вечер)|доброе утро|привет(?:ствую)?|хеллоу|hello|"
    r"подскажите(?: мне)?|скажите(?: мне)?|пожалуйста|хочу (?:уточнить|узнать))[\s,!:—.-]+)+"
)


def asks_to_recheck(text: str) -> bool:
    """A reference to a previous question, with no new subject of its own."""
    text = re.sub(r"[,!]\s*(?:бот|ассистент|друг|деб(?:ил)?)[.!?\s]*$", "", text.casefold())
    words = set(re.findall(r"[\w-]+", text))
    vocabulary = {
        "проверь",
        "проверьте",
        "посмотри",
        "посмотрите",
        "поищи",
        "поищите",
        "перепроверь",
        "перепроверьте",
        "базу",
        "базе",
        "знаний",
        "справке",
        "справку",
        "еще",
        "ещё",
        "раз",
        "в",
        "по",
        "пожалуйста",
        "там",
        "это",
        "есть",
        "должно",
        "быть",
        "у",
        "тебя",
        "вас",
        "точно",
        "же",
        "ответ",
        "должен",
    }
    return bool(words & {"базу", "базе", "справку", "справке"}) and words <= vocabulary


def keeps_question_qualifiers(original: str, rewritten: str) -> bool:
    """Search suggestions must retain named platforms, versions and negations."""
    protected = r"\b(?:[a-z0-9][\w.-]*|не|без|нельзя|кроме)\b"
    return set(re.findall(protected, original.casefold())) <= set(re.findall(protected, rewritten.casefold()))


@router.get(ARTICLE_PATH + "{article_id}", response_class=PlainTextResponse, tags=["База знаний"])
async def public_article(article_id: Id) -> PlainTextResponse:
    async with get_db().committed_read() as conn:
        article = await live_article(conn, article_id)
    if article is None:
        raise not_found("Статья не найдена.")
    return PlainTextResponse(
        f"{article['title']}\n\n{article['body']}",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


def question_key(text: str) -> str:
    text = " ".join(text.casefold().replace("ё", "е").split())
    text = INTRO.sub("", text)
    text = text.strip(" ?!.")
    text = re.sub(r"^(как|где) (?:мне|можно) ", r"\1 ", text)
    text = re.sub(r"^(как|где) (?:поменять|сменить) ", r"\1 изменить ", text)
    # Only normalise a how/where configuration question. Keep every qualifier,
    # including the store, negations and numbers. Other paraphrases are curated.
    if text.startswith(("как ", "где ")):
        body = text[4:]
        platform = re.search(r"\b(?:в|на) [a-z][a-z0-9.-]*(?: [a-z][a-z0-9.-]*)*\b", body)
        if platform:
            body = " ".join((body[: platform.start()] + " " + body[platform.end() :]).split())
        body = re.sub(r"^изменить настройки\b", "настроить", body)
        if body.startswith("настроить "):
            body = re.sub(r"\b([а-я]+(?:ени|ани))(?:й|я)\b", r"\1я", body)
            return body + (" " + platform[0] if platform else "")
    return text


async def answer_published(session_id: str, question: str, emit_step) -> AgentReply | None:
    started = perf_counter()
    key = question_key(question)
    if len(key.split()) < 2:
        return None
    async with get_db().committed_read() as conn:
        rows = await conn.execute_fetchall("""SELECT p.*, v.question AS phrasing FROM kb_live_publications p
            LEFT JOIN kb_phrasings v ON v.patch_id=p.id ORDER BY p.id""")
    matches_by_id = {
        row["id"]: row
        for row in rows
        if key == question_key(row["title"]) or (row["phrasing"] and key == question_key(row["phrasing"]))
    }
    matches = list(matches_by_id.values())
    if not matches:
        return None
    article = matches[0]
    conflict = any(row["body"] != article["body"] for row in matches[1:])
    ref = f"kb-patch:{article['id']}"
    sources = (
        []
        if conflict
        else [
            Source(
                url=f"{str(get_settings().public_base_url).rstrip('/')}{ARTICLE_PATH}{article['id']}",
                label="База знаний поддержки Softline",
            )
        ]
    )
    elapsed = int((perf_counter() - started) * 1000)
    step = StepTrace(
        tool="published_knowledge",
        args={"article_ids": [row["id"] for row in matches]},
        result_summary="Конфликт опубликованных ответов" if conflict else article["title"],
        source_ref=None if conflict else ref,
        latency_ms=elapsed,
    )
    await emit_step(step)
    return AgentReply(
        text=OFFER_HELP if conflict else article["body"],
        sources=sources,
        purpose="clarification" if conflict else "answer",
        turn=AgentTurn(
            turn_id=new_id("turn"),
            session_id=session_id,
            decision="answer",
            steps=[step],
            total_cost_rub=0,
            total_latency_ms=elapsed,
        ),
    )
