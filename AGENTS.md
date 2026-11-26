# Правила для кодинг-агентов и участников команды

## Контуры

| Контур | Владелец | Папки |
|---|---|---|
| Данные, коммерция и стенд | Шеф | `backend/app/{contracts,core,db,services,api}`, `scripts/`, `deploy/`, Docker/Caddy, `tests/` |
| Агент | Валера | `backend/app/agent/` |
| Продукт и UX | Михаил | `frontend/` |

В чужую зону не коммитим без согласования с владельцем.

## Железные инварианты

1. **Контракты заморожены.** `backend/app/contracts/schemas.py` меняем только по согласованию команды и с зелёным `pytest tests/test_contracts.py`.
2. **Цифры только из кода и инструментов.** Цены, суммы, НДС, количества, сроки и артикулы считает детерминированный Python-код (`decimal.Decimal`, ROUND_HALF_UP). LLM арифметику не делает.
3. **Факт с источником или эскалация.** Каждый инструмент поиска возвращает `ToolResult` с `source_ref` (URL товара или статьи, ID записи). Факта без источника агент клиенту не сообщает и передаёт вопрос менеджеру. Приветствиям и уточняющим вопросам источник не нужен.
4. **LLM только через `app/core/llm.py:llm_call()`.** Прямой вызов SDK провайдера запрещён. Обёртка проверяет дневной бюджет и пишет в `llm_calls` модель, шаг, tokens_in, tokens_out, latency_ms и cost_rub.
5. **Открытый репозиторий.** В git не попадают секреты, ключи, `.env`, дампы БД, XML-фиды, архивы и сгенерированные PDF. Новые переменные окружения добавляем в `.env.example`.
6. **Стенд всегда под защитой.** Rate-limit по IP и сессии, лимит ходов на диалог, дневной потолок расходов в рублях, Basic Auth на `/api/operator/*`, `/api/kb/*`, `/api/admin/*`, `/api/telemetry/*`.
7. **Без расползания скоупа.** Идеи вне текущего этапа пишем в `IDEAS.md` и не реализуем без подтверждения.

## Стыки

- **Агент.** Каркас вызывает `app.agent.runner.run_turn(session, history, user_text, emit_step) -> AgentReply`. Каждый шаг публикуется через `await emit_step(StepTrace)` и возвращается в `reply.turn.steps`. Режим pilot и `BudgetExceededError` обрабатывает каркас (`services/turn_service.py`). Сообщения, ходы и тикеты сохраняет тоже каркас.
- **LLM.** `await llm_call(messages, step="agent_loop", profile="smart" | "fast", session_id=..., tools=...)` возвращает `LLMResponse`: `content`, `tool_calls`, `message`, `tokens_*`, `cost_rub`, `latency_ms`. `resp.to_step_trace()` даёт готовый шаг для трейса.
- **Фронт.** OpenAPI: `/api/openapi.json`, Swagger: `/api/docs`. SSE `/api/sessions/{id}/events` присылает события `step_trace`, `message_done`, `ticket_created`, `operator_reply` (см. `SseEvent`). Деньги в JSON приходят строками (`"77556.00"`).

## Процесс

- Backend запускается одним воркером uvicorn: rate-limit, SSE-шина и флаг занятости сессии хранятся в памяти процесса.
- Перед PR: `ruff check . && ruff format --check . && pytest`.
- Ветка на задачу от актуальной `main`, номер задачи в заголовке PR.
