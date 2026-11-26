"""Loopback-only T09 preview. Replay customer-side examples from the accepted T04 contract."""
import argparse
import json
import re
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

FRONTEND = Path(__file__).resolve().parent
EXAMPLES = json.loads((FRONTEND.parent / "contracts/t04/examples.json").read_text(encoding="utf-8"))


def load_script(case):
    actions = []
    events = {}
    for step in case["steps"]:
        if step["role"] != "customer":
            continue
        response = step["response"]
        if step["operationId"] == "submitAction" and step["status"] == 202:
            action_id = response["actionId"]
            if all(item[1] != action_id for item in actions):
                actions.append((step["body"], action_id))
        if step["operationId"] == "readEvents" and step["status"] == 200:
            for event in response["events"]:
                events[event["seq"]] = event
    return actions, [events[seq] for seq in sorted(events)]


SCRIPTS = [load_script(case) for case in EXAMPLES if case["id"] in {"c-help", "c-tender", "c-need", "c-price"}]
API = "/api/v1/conversations"
FILES = {"index.html": "text/html", "chat.css": "text/css", "chat.js": "text/javascript", "render.js": "text/javascript"}


class Preview(BaseHTTPRequestHandler):
    conversations = {}
    requests_by_key = {}

    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlsplit(self.path)
        match = re.fullmatch(API + r"/([a-f0-9]+)/events", url.path)
        if match and match[1] in self.conversations:
            after = int(parse_qs(url.query).get("after", [0])[0])
            events = self.conversations[match[1]]["events"]
            page = [e for e in events if e["seq"] > after][:100]
            end = page[-1]["seq"] if page else after
            return self.respond(200, {"events": page, "nextAfter": end, "hasMore": end < len(events)})
        name = url.path.removeprefix("/chat/") or "index.html"
        if url.path.startswith("/chat/") and name in FILES:
            raw = (FRONTEND / "chat" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", FILES[name] + "; charset=utf-8")
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        key = self.headers.get("Idempotency-Key")
        if not key:
            return self.respond(400, {"code": "invalid_input", "message": "Требуется ключ повтора."})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
        cache_key = (self.path, key)
        if cache_key in self.requests_by_key:
            original, status, result = self.requests_by_key[cache_key]
            if original != body:
                return self.respond(409, {"code": "key_conflict", "message": "Команда с таким ключом уже принята."})
            return self.respond(status, result)
        if self.path == API:
            cid = uuid4().hex
            self.conversations[cid] = {"script": None, "position": 0, "events": []}
            status, result = 201, {"conversationId": cid}
        else:
            match = re.fullmatch(API + r"/([a-f0-9]+)/actions", self.path)
            if not match or match[1] not in self.conversations:
                return self.send_error(404)
            cid = match[1]
            chat = self.conversations[cid]
            script = chat["script"] or next((s for s in SCRIPTS if s[0][0][0] == body), None)
            pos = chat["position"]
            if not script or pos >= len(script[0]) or script[0][pos][0] != body:
                return self.respond(422, {"code": "invalid_input", "message": "Локальный пример ожидает реплику из сценария Т04."})
            actions, events = script
            action_id = actions[pos][1]
            next_id = actions[pos + 1][1] if pos + 1 < len(actions) else None
            cutoff = next((e["seq"] for e in events if e["actionId"] == next_id), float("inf"))
            visible = deepcopy([e for e in events if e["seq"] < cutoff])
            for event in visible:
                if event["data"]["type"] == "handoff":
                    event["data"]["handoff"]["conversationId"] = cid
            chat.update(script=script, position=pos + 1, events=visible)
            status, result = 202, {"conversationId": cid, "actionId": action_id}
        self.requests_by_key[cache_key] = body, status, result
        self.respond(status, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    port = parser.parse_args().port
    print(f"http://127.0.0.1:{port}/chat/", flush=True)
    HTTPServer(("127.0.0.1", port), Preview).serve_forever()
