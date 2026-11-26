# ИИ-агент поддержки Softline

Проект для хакатона «ИИкузница». Агент помогает покупателю подобрать ПО по названию или потребности и отвечает на вопросы о товарах по данным каталога.

## Запуск

```sh
cp .env.example .env        # заполнить OPERATOR_PASSWORD; для реального LLM: MOCK_LLM=false и LLM_API_KEY
docker compose up -d --build
./scripts/smoke_test.sh     # BASE_URL=https://176-53-253-178.sslip.io для стенда
```

Swagger открывается по `/api/docs`. Первичная настройка сервера (Ubuntu 26.04): `sudo ./deploy/setup_server.sh`.

Локально без Docker (Python 3.12+):

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv -r backend/requirements-dev.txt
.venv/bin/pytest
cd backend && ../.venv/bin/uvicorn app.main:app --reload   # .env читается из текущей папки
```

С `MOCK_LLM=true` стенд работает без ключа и без трат: ответ модели заменяет заглушка, токены оцениваются по длине текста, стоимость считается по прайсу из `.env`.

## Архитектура

```
браузер ── Caddy (HTTPS, статика frontend/, /api/* и /health → backend, SSE без буфера)
              └── FastAPI backend (один воркер)
                    ├── api/          операции T04 (/api/v1) и расширения менеджера
                    ├── core/         llm_call() + FinOps, cookie-сессии, rate-limit, ошибки, SSE-шина
                    ├── services/     команды T04, журнал, ход агента; каталог и БЗ (T06)
                    ├── agent/        цикл агента (контур Валеры)
                    └── db/           SQLite WAL + FTS5 в ./data/app.db
```

API следует контракту T04 ([contracts/t04](contracts/t04/README.md)): шесть операций под `/api/v1`, журнал событий с `seq` и опросом `events?after=`, `Idempotency-Key` на каждом POST, cookie `support_session` с ролью `customer` или `manager`, цены в копейках, ошибки `{code, message}`.

Ход диалога: команда `message` сохраняет реплику в журнал и запускает `agent/runner.py` в фоне. Пока ход идёт, следующая реплика получает 409 `agent_busy`. Ответ агента становится событиями журнала:

- `answer` — карточки товаров и ответ покупателю;
- `draft` или режим pilot — черновик в `/api/v1/manager/drafts`, покупатель видит уведомление;
- `escalate`, исчерпанный бюджет или ошибка агента — обращение `handoff` в очередь `/api/v1/manager/handoffs`.

Оператор входит через `POST /api/v1/auth/manager/login`, отвечает в обращение (`reply`) или закрывает его (`close`). Покупатель отвечает в открытое обращение сообщением с `handoffId`.

Каждый вызов LLM проходит через `llm_call()`. Обёртка проверяет дневной лимит `DAILY_BUDGET_RUB` и пишет токены и стоимость в рублях в `llm_calls`. Сводка доступна в `GET /api/v1/manager/telemetry`, шаги агента — в `GET /api/v1/manager/conversations/{id}/trace`.

Контрактный тест `tests/test_t04_examples.py` прогоняет примеры T04 (`c-tender`, `c-help`, `c-price`, `c-need`) через настоящий API и проверяет ответы схемой `contracts/t04/openapi.yaml`. Блок КП и документов (`c-docs`) ещё не реализован: `confirm_quote`, `request_document` и `downloadDocument` отвечают 404.

Контуры, инварианты и стыки описаны в [AGENTS.md](AGENTS.md).

## Работа в команде

- Для каждой задачи создаём отдельную ветку от актуальной `main`, например `feat/t07-catalog`.
- В заголовке pull request указываем номер задачи. В описании пишем, что изменилось и как это проверить.
- Готовые PR вливаем в `main` с сохранением истории коммитов.
- Секреты храним в локальном `.env`. Рабочие копии материалов заказчика держим в `data/raw/` или `data/private/`. Для открытых примеров используем синтетические данные.

## Сдача

В `main` должна быть версия, соответствующая стенду и демонстрации. Репозиторий остаётся публичным до объявления результатов. [Положение конкурса](https://иикузница.рф/regulations/v1.html)
