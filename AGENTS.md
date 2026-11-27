# Правила изоляции данных и закрытой выборки (Eval)

- Запрещено читать, индексировать, копировать или использовать при разработке промптов и кода содержимое внешнего каталога закрытой контрольной выборки (`EVAL_HOLDOUT_DIR`, по умолчанию `~/.local/share/iikuznitsa-eval-private`, права 700 на каталог и 600 на файлы). Там лежат `holdout_eval.json`, `dev_ids.json` (дополнение holdout, так же чувствителен) и секретный seed.
- `data/private/` под этот запрет не попадает: там лежит индекс каталога для T07/T14 (`catalog.sqlite3`). Это не eval-данные, но и в git он не попадает.
- Seed разбивки берётся только из `EVAL_SPLIT_SEED` (не меньше 128 бит, например `secrets.token_hex(16)`), значения по умолчанию нет. Seed, его хэши и списки ID не выводим, не коммитим и не публикуем.
- Holdout нельзя хранить в репозитории, в `data/private` или во временном каталоге (`/tmp`): `eval/split_chats.py` откажется туда писать.
- Сырые переписки из `data/raw/чаты поддержки.zip` содержат не полностью обезличенные ПДн. Их запрещено коммитить в публичный репозиторий и отправлять внешним LLM-провайдерам без маскирования. `eval/run_eval.py` применяет `scrub_pii` перед отправкой holdout-сообщений, но маскирование эвристическое (см. «Известные ограничения» в `eval/README.md`). В тестах и при отладке выводим только счётчики и ID, а не тексты реплик.
- Для воспроизводимой генерации закрытой выборки проверяющий задаёт собственный `EVAL_SPLIT_SEED` и внешний `EVAL_HOLDOUT_DIR` через переменные окружения.
# Правила для кодинг-агентов и участников команды

## Контуры

| Контур | Владелец | Папки |
|---|---|---|
| Данные, коммерция и стенд | Шеф | `backend/app/{contracts,core,db,services,api}`, `scripts/`, `deploy/`, Docker/Caddy, `tests/` |
| Агент | Валера | `backend/app/agent/` |
| Продукт и UX | Михаил | `frontend/` |

В чужую зону не коммитим без согласования с владельцем.

## Железные инварианты

1. **Контракты заморожены.** Публичный API — контракт T04 (`contracts/t04/openapi.yaml`, модели в `backend/app/contracts/t04.py`), внутренние контракты — `backend/app/contracts/schemas.py`. Меняем только по согласованию команды и с зелёными `pytest tests/test_contracts.py tests/test_t04_examples.py`.
2. **Цифры только из кода и инструментов.** Цены, суммы, НДС, количества, сроки и артикулы считает детерминированный Python-код (`decimal.Decimal`, ROUND_HALF_UP). LLM арифметику не делает.
3. **Факт с источником или эскалация.** Каждый инструмент поиска возвращает `ToolResult` с `source_ref` (URL товара или статьи, ID записи). Факта без источника агент клиенту не сообщает и передаёт вопрос менеджеру. Приветствиям и уточняющим вопросам источник не нужен.
4. **LLM только через `app/core/llm.py:llm_call()`.** Прямой вызов SDK провайдера запрещён. Обёртка проверяет дневной бюджет и пишет в `llm_calls` модель, шаг, tokens_in, tokens_out, latency_ms и cost_rub.
5. **Открытый репозиторий.** В git не попадают секреты, ключи, `.env`, дампы БД, XML-фиды, архивы и сгенерированные PDF. Новые переменные окружения добавляем в `.env.example`.
6. **Стенд всегда под защитой.** Rate-limit по IP и диалогу, лимит ходов на диалог, дневной потолок расходов в рублях, роль `manager` (cookie `support_session` после `POST /api/v1/auth/manager/login`) на `/api/v1/manager/*`.
7. **Без расползания скоупа.** Идеи вне текущего этапа пишем в `IDEAS.md` и не реализуем без подтверждения.

## Стыки

- **Агент.** Каркас вызывает `app.agent.runner.run_turn(session, history, user_text, emit_step) -> AgentReply`. Шаги трейса — `await emit_step(StepTrace)` и `reply.turn.steps`. Ответ каркас пишет в журнал T04 по порядку: `products` → `message` → `handoff`. Товары строим через `Product.from_catalog(...)`: цены в копейках, `priceState` по правилам T04. Источники — `Source(url="https://...", label=...)`. Обращение — `HandoffRequest(reason=<одна из 8 причин T04>, question, products)` при `decision="escalate"`. Режим pilot, `BudgetExceededError` и ошибки агента обрабатывает каркас (`services/turn_service.py`).
- **LLM.** `await llm_call(messages, step="agent_loop", profile="smart" | "fast", session_id=..., tools=...)` возвращает `LLMResponse`: `content`, `tool_calls`, `message`, `tokens_*`, `cost_rub`, `latency_ms`. `resp.to_step_trace()` даёт готовый шаг для трейса.
- **Фронт.** Шесть операций T04 под `/api/v1` (см. `contracts/t04/README.md`), у каждого POST — `Idempotency-Key`. Ошибки — `{code, message}`, включая `rate_limited` (429, `Retry-After`) и `agent_busy` (409). Расширения T05: `/api/v1/auth/manager/login`, `/api/v1/manager/{telemetry,mode,drafts,kb/patches}`, `/api/v1/manager/conversations/{id}/trace`, SSE `/api/v1/conversations/{id}/events/stream`, `/health`. OpenAPI: `/api/openapi.json`, Swagger: `/api/docs`.

## Процесс

- Backend запускается одним воркером uvicorn: rate-limit, SSE-шина и флаг занятости сессии хранятся в памяти процесса.
- Перед PR: `ruff check . && ruff format --check . && pytest`.
- Ветка на задачу от актуальной `main`, номер задачи в заголовке PR.
