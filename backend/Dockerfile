# Сборка из корня репозитория: docker compose build (context: ., dockerfile: backend/Dockerfile)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DATA_DIR=/app/data

# Кириллические шрифты и Pango для WeasyPrint (PDF КП, Фаза 3)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core fonts-liberation libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY backend/app /app/app
COPY scripts /app/scripts

RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app

EXPOSE 8000
# Один воркер: rate-limit, SSE-шина и флаг занятости сессии живут в памяти процесса
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
