"""#90: стенд закрыт от индексации, а витрина помечена как демо-прототип конкурса."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PAGES = ["index.html", "chat/index.html", "manager/index.html"]
NOTICE = "Демо-прототип конкурса „ИИкузница“"


def test_backend_marks_every_response_noindex(client):
    from app.config import get_settings

    for path in ["/health", "/api/openapi.json", "/api/docs", "/api/v1/missing"]:
        assert client.get(path).headers["x-robots-tag"] == "noindex, nofollow", path
    get_settings().rate_limit_ip_per_min = 1
    for key in ["a", "b"]:
        r = client.post("/api/v1/conversations", headers={"Idempotency-Key": key})
    assert r.status_code == 429 and r.headers["x-robots-tag"] == "noindex, nofollow"


def test_robots_txt_disallows_everything():
    lines = (FRONTEND / "robots.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["User-agent: *", "Disallow: /"]
    # Caddy отдаёт существующий файл раньше SPA-фолбэка на /index.html, а заголовок ставит на весь стенд
    caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "try_files {path} " in caddyfile and 'X-Robots-Tag "noindex, nofollow"' in caddyfile


@pytest.mark.parametrize("page", PAGES)
def test_pages_are_noindex_and_carry_demo_notice(page):
    html = (FRONTEND / page).read_text(encoding="utf-8")
    assert re.search(r'<meta name="robots" content="noindex, nofollow[^"]*">', html)
    # Маркировка в самом HTML, а не в скрипте: её видят и без JS, и в ответе страницы
    body = html.split("<body>", 1)[1].lstrip()
    assert body.startswith('<div class="demo-banner" role="note">')
    text = re.sub(r"<[^>]+>", "", body.split("</div>", 1)[0])
    assert text.startswith(NOTICE) and text.endswith("не является официальным магазином store.softline.ru")
    assert "href" not in body.split("</div>", 1)[0]
