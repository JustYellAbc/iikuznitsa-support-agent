# ИИ-агент поддержки Softline

Проект для хакатона «ИИкузница». Агент помогает покупателю подобрать ПО по названию или потребности и отвечает на вопросы о товарах по данным каталога.

## Запуск

```sh
cp .env.example .env        # заполнить OPERATOR_PASSWORD; для реального LLM: MOCK_LLM=false и LLM_API_KEY
docker compose up -d --build
./scripts/smoke_test.sh     # BASE_URL=https://176-53-253-178.sslip.io для стенда
```

Стенд: https://176-53-253-178.sslip.io. Swagger открывается по `/api/docs`. Первичная настройка сервера (Ubuntu 26.04): `sudo ./deploy/setup_server.sh`.

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
                    ├── api/          публичные роуты и роуты менеджера (Basic Auth)
                    ├── core/         llm_call() + FinOps, rate-limit, SSE-шина
                    ├── services/     оркестрация хода, каталог, БЗ, КП
                    ├── agent/        цикл агента (контур Валеры)
                    └── db/           SQLite WAL + FTS5 в ./data/app.db
```

Ход диалога: `POST /api/sessions/{id}/messages` сохраняет реплику и запускает `agent/runner.py` в фоне. Шаги агента сразу уходят в SSE `/events` как `step_trace`. Затем каркас выбирает исход:

- `answer` — сообщение клиенту (`message_done`);
- `draft` или режим pilot — черновик в очередь менеджера (`ticket_created`);
- `escalate` или исчерпанный бюджет — обращение менеджеру.

Менеджер отвечает через `POST /api/operator/tickets/{id}/reply` (`send_draft=true` отправляет черновик в один клик). Ответ приходит клиенту как `operator_reply`.

Каждый вызов LLM проходит через `llm_call()`. Обёртка проверяет дневной лимит `DAILY_BUDGET_RUB` и пишет токены и стоимость в рублях в `llm_calls`. Сводка доступна в `GET /api/telemetry/summary`.

Контуры, инварианты и стыки описаны в [AGENTS.md](AGENTS.md).

## Работа в команде

- Для каждой задачи создаём отдельную ветку от актуальной `main`, например `feat/t07-catalog`.
- В заголовке pull request указываем номер задачи. В описании пишем, что изменилось и как это проверить.
- Готовые PR вливаем в `main` с сохранением истории коммитов.
- Секреты храним в локальном `.env`. Рабочие копии материалов заказчика держим в `data/raw/` или `data/private/`. Для открытых примеров используем синтетические данные.

## Сдача

В `main` должна быть версия, соответствующая стенду и демонстрации. Репозиторий остаётся публичным до объявления результатов. [Положение конкурса](https://иикузница.рф/regulations/v1.html)
