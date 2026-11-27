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
from app.db.repository import new_id

router = APIRouter()
ARTICLE_PATH = "/api/v1/knowledge/"

# Existing patches default to "general". Publication for other buyers must be explicit.
# Read the actual publication snapshot, and never select text from the source conversation.
PUBLISHED = """
    SELECT p.id, a.title, a.body
    FROM kb_articles AS a
    JOIN kb_patches AS p ON a.url = 'kb-patch:' || p.id
    JOIN handoffs AS h ON h.id = p.handoff_id
    WHERE a.origin = 'patch' AND a.section = 'public_faq'
      AND p.scope = 'public_faq' AND p.status = 'approved' AND p.approved_by IS NOT NULL
      AND h.status = 'resolved' AND a.created_at >= h.updated_at
      AND length(trim(a.title)) > 0 AND length(trim(a.body)) > 0
"""


@router.get(ARTICLE_PATH + "{article_id}", response_class=PlainTextResponse, tags=["База знаний"])
async def public_article(article_id: Id) -> PlainTextResponse:
    article = await get_db().fetchone(PUBLISHED + " AND p.id = ?", (article_id,))
    if article is None:
        raise not_found("Статья не найдена.")
    return PlainTextResponse(f"{article['title']}\n\n{article['body']}", headers={"X-Content-Type-Options": "nosniff"})


def question_key(text: str) -> str:
    text = " ".join(text.casefold().replace("ё", "е").split())
    text = re.sub(r"^(?:(?:здравствуйте|добрый день|подскажите|пожалуйста)[\s,!:.-]+)+", "", text)
    return text.strip(" ?!.")


async def answer_published(session_id: str, question: str, emit_step) -> AgentReply | None:
    started = perf_counter()
    key = question_key(question)
    # Match the whole question, retaining negation, numbers and qualifiers.
    words = re.findall(r"[^\W_]+", key)
    if len(words) < 2:
        return None
    # FTS is only a candidate filter. unicode61 treats ё and е differently in Cyrillic.
    tokens = [word for word in words if "е" not in word]
    sql = PUBLISHED
    params = ()
    if tokens:
        sql += " AND a.id IN (SELECT rowid FROM kb_fts WHERE kb_fts MATCH ?)"
        params = ("title : (" + " AND ".join(f'"{word}"' for word in tokens[:6]) + ")",)
    rows = await get_db().fetchall(sql + " ORDER BY a.id DESC", params)
    matches = [row for row in rows if question_key(row["title"]) == key]
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
