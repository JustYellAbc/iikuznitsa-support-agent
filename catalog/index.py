"""Build and query the local Softline search index (Python 3.11+, SQLite FTS5)."""
import argparse
from collections import defaultdict
from contextlib import closing
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET

KNOWLEDGE = Path(__file__).with_name("knowledge.json")
SCHEMA = """
CREATE TABLE metadata(snapshot TEXT NOT NULL);
CREATE TABLE categories(id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL);
CREATE TABLE offers(
    id TEXT PRIMARY KEY, vendor_code TEXT, group_id TEXT, name TEXT NOT NULL,
    vendor TEXT, edition TEXT, category_id TEXT REFERENCES categories(id),
    available INTEGER, price TEXT, currency TEXT, price_state TEXT NOT NULL,
    source TEXT NOT NULL, details TEXT NOT NULL, params TEXT NOT NULL
);
CREATE INDEX offers_vendor_code ON offers(vendor_code);
CREATE VIRTUAL TABLE products USING fts5(name, vendor_code, category, features);
CREATE VIRTUAL TABLE knowledge USING fts5(title, body, source UNINDEXED);
"""


def insert_offer(db, node, categories):
    def field(tag):
        return (node.findtext(tag) or "").strip()

    params = defaultdict(list)
    for item in node.findall("param"):
        value = (item.text or "").strip()
        if item.get("name") == "Срок действия" and value.casefold() in {"не определено", "мес."}:
            continue
        if value and value.casefold() not in {"другое или отсутствует", "не указано", "нет данных"}:
            params[item.attrib["name"]].append(value)
    details = {child.tag: child.text.strip() for child in node
               if child.tag != "param" and child.text and child.text.strip()}
    available = {"true": 1, "false": 0}.get(node.get("available"))
    price = field("price")
    try:
        amount = Decimal(price)
        priced = amount.is_finite() and amount > 0
    except InvalidOperation:
        priced = False
    if not priced:
        state = "request_price"
    elif available == 1:
        state = "priced_available"
    else:
        state = "verify_availability"
    edition = " / ".join(params.get("Версия", [])) or None
    row = db.execute("INSERT INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        node.attrib["id"], field("vendorCode") or None, node.get("group_id"), field("name"),
        field("vendor"), edition, field("categoryId") or None, available, price or None,
        field("currencyId"), state, field("url"), json.dumps(details, ensure_ascii=False),
        json.dumps(params, ensure_ascii=False)))
    features = " ".join([field("vendor"), field("description"), field("sales_notes"),
                         *(v for values in params.values() for v in values)])
    db.execute("INSERT INTO products(rowid,name,vendor_code,category,features) VALUES(?,?,?,?,?)",
               (row.lastrowid, field("name"), field("vendorCode"),
                categories.get(field("categoryId"), ""), features))


def build(feed, destination, knowledge):
    if destination.resolve() in {feed.resolve(), knowledge.resolve()}:
        raise ValueError("Index destination must differ from input files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=destination.parent, suffix=".sqlite3")
    os.close(fd)
    temporary = Path(name)
    try:
        with feed.open("rb") as stream, closing(sqlite3.connect(temporary)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            stack = []
            categories = {}
            for event, node in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    if not stack:
                        if node.tag != "yml_catalog":
                            raise ValueError("Expected yml_catalog")
                        db.execute("INSERT INTO metadata VALUES(?)", (node.attrib["date"],))
                    stack.append(node)
                    continue
                stack.pop()
                if node.tag == "category" and stack[-1].tag == "categories":
                    key = node.attrib["id"]
                    categories[key] = (node.text or "").strip()
                    db.execute("INSERT INTO categories VALUES(?,?,?)",
                               (key, node.get("parentId"), categories[key]))
                    stack[-1].remove(node)
                elif node.tag == "offer" and stack[-1].tag == "offers":
                    insert_offer(db, node, categories)
                    stack[-1].remove(node)
            if not db.execute("SELECT 1 FROM offers LIMIT 1").fetchone():
                raise ValueError("Empty catalog")
            articles = json.loads(knowledge.read_text(encoding="utf-8"))
            if not articles:
                raise ValueError("Empty knowledge collection")
            db.executemany("INSERT INTO knowledge(title,body,source) VALUES(:title,:body,:source)", articles)
            counts = db.execute("SELECT price_state,count(*) FROM offers GROUP BY price_state").fetchall()
        os.replace(temporary, destination)
        return dict(counts)
    finally:
        temporary.unlink(missing_ok=True)


def search(index, query, scope="catalog", limit=5):
    # Quote FTS tokens and bind MATCH to keep raw user input out of query syntax.
    words = re.findall(r"[^\W_]+", query, re.UNICODE)
    if not words:
        return []
    expression = " OR ".join(f'"{word}"*' for word in dict.fromkeys(words))
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if scope == "knowledge":
            rows = db.execute("SELECT title,body,source FROM knowledge WHERE knowledge MATCH ? "
                              "ORDER BY bm25(knowledge,4,1,0) LIMIT ?", (expression, limit))
        else:
            rows = db.execute("""
                SELECT o.id,o.vendor_code,o.name,o.edition,o.price,o.currency,o.available,
                       o.price_state,o.source,o.params,o.details FROM products
                JOIN offers o ON o.rowid=products.rowid WHERE products MATCH ?
                ORDER BY bm25(products,8,10,3,1), o.id LIMIT ?
            """, (expression, limit))
        result = [dict(r) for r in rows]
        for item in result:
            if "params" in item:
                item["params"] = json.loads(item["params"])
                item["details"] = json.loads(item["details"])
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    import_cmd = commands.add_parser("build")
    import_cmd.add_argument("feed", type=Path)
    import_cmd.add_argument("index", type=Path)
    import_cmd.add_argument("--knowledge", type=Path, default=KNOWLEDGE)
    query_cmd = commands.add_parser("search")
    query_cmd.add_argument("index", type=Path)
    query_cmd.add_argument("query")
    query_cmd.add_argument("--scope", choices=["catalog", "knowledge"], default="catalog")
    args = parser.parse_args()
    result = (build(args.feed, args.index, args.knowledge) if args.command == "build"
              else search(args.index, args.query, args.scope))
    print(json.dumps(result, ensure_ascii=False, indent=2))
