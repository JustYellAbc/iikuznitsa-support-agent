"""Download customer-facing Softline help into a local JSON snapshot (requires bs4)."""
import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from bs4 import BeautifulSoup

from index import KNOWLEDGE


ARTICLES = {
    "id": ["login", "password", "authorization-data", "registration"],
    "lk": ["create-order", "invoices", "unpaid-invoices", "fulfillment",
           "view-statuses", "closing-documents", "renewal", "order-contact"],
}


def sections(url):
    with urlopen(url, timeout=25) as response:
        page = BeautifulSoup(response.read(), "html.parser")
    article = page.select_one("article")
    if not article or not article.find("h1"):
        raise ValueError(f"Cannot find article: {url}")
    title = article.h1.get_text(" ", strip=True)
    heading = title
    lines = []
    for element in article.select("h2,h3,p,li"):
        if element.name in {"h2", "h3"}:
            if lines:
                yield {"title": heading, "body": "\n".join(lines), "source": url}
                lines = []
            heading = title + " · " + element.get_text(" ", strip=True)
        elif not element.find_parent(["li", "p"]):
            text = element.get_text(" ", strip=True)
            if text:
                lines.append(text)
    if lines:
        yield {"title": heading, "body": "\n".join(lines), "source": url}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve() == KNOWLEDGE.resolve():
        parser.error("Choose a separate output for the private snapshot")
    snapshot = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
    for group, pages in ARTICLES.items():
        for slug in pages:
            url = f"https://docs.softline.com/ru-ru/{group}/{slug}"
            chunks = list(sections(url))
            if not chunks:
                raise ValueError(f"Empty article: {url}")
            snapshot.extend(chunks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(snapshot)} sections written to {args.output}")
