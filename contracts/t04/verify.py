"""Check T04 mock exchanges against OpenAPI and the cross-event invariants."""
import json
from collections import Counter
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


class Contract:
    def __init__(self, spec):
        self.components = spec["components"]
        self.operations = {}
        for route, path in spec["paths"].items():
            for method in ("get", "post"):
                if method in path:
                    operation = path[method]
                    params = path.get("parameters", []) + operation.get("parameters", [])
                    self.operations[operation["operationId"]] = (route, params, operation)

    def resolve(self, value):
        if "$ref" in value:
            prefix, section, name = value["$ref"].rsplit("/", 2)
            require(prefix == "#/components", "Expected an internal OpenAPI reference")
            return self.components[section][name]
        return value

    def check_schema(self, schema, data):
        root = {"components": self.components, **schema}
        Draft202012Validator(root, format_checker=FormatChecker()).validate(data)

    def check_exchange(self, step):
        route, declarations, op = self.operations[step["operationId"]]
        given = dict(step.get("params", {}))
        if "key" in step:
            given["Idempotency-Key"] = step["key"]
        for declaration in declarations:
            param = self.resolve(declaration)
            name = param["name"]
            if param["in"] == "path":
                require("{" + name + "}" in route, "Undeclared route parameter")
            if name in given:
                self.check_schema(param["schema"], given.pop(name))
            else:
                require(not param.get("required"), f"Missing {name}")
        require(not given, f"Unexpected parameters {given}")
        require(("body" in step) == ("requestBody" in op), "Unexpected or missing JSON body")
        if "body" in step:
            self.check_schema(op["requestBody"]["content"]["application/json"]["schema"], step["body"])
        status = str(step["status"])
        response = op["responses"].get(status)
        if response is None:
            require(status in {"401", "403", "404", "409", "422"}, f"Unexpected HTTP {status}")
            response = op["responses"]["default"]
        content = self.resolve(response)["content"]
        media = step.get("mediaType", "application/json")
        require(media in content, f"Unexpected response media type {media}")
        if media == "application/json":
            self.check_schema(content[media]["schema"], step["response"])
        else:
            require("response" not in step, "Binary download uses only headers in examples")


class Scenario:
    """Replay a single mock conversation, keeping one canonical, append-only journal."""

    def __init__(self, conversation):
        self.conversation = conversation
        self.history = []
        self.commands = {}
        self.replays = {}
        self.quotes = {}
        self.documents = {}
        self.pending_documents = {}
        self.handoffs = {}
        self.observed_actions = set()

    def check_retry(self, step):
        identity = (step["role"], step["operationId"], tuple(sorted(step.get("params", {}).items())), step["key"])
        record = (step.get("body"), step["status"], step["response"])
        first = self.replays.get(identity)
        if first is None:
            if step["status"] < 300:
                self.replays[identity] = record
            return False
        if record[0] != first[0]:
            require(step["status"] == 409 and step["response"]["code"] == "key_conflict",
                    "Reused key requires key_conflict for a different body")
        else:
            require(first == record, "Retry changed the original status or response")
        return True

    def check_handoff_event(self, handoff, command):
        require(handoff["conversationId"] == self.conversation, "Wrong handoff conversation")
        handoff_id = handoff["handoffId"]
        previous = self.handoffs.get(handoff_id)
        require(not previous or previous["status"] == "open", "Resolved handoff changed")
        if handoff["status"] == "resolved":
            require(previous is not None, "Unknown handoff was closed")
            require(command["operationId"] == "resolveHandoff" and command["params"]["handoffId"] == handoff_id
                    and command["body"]["type"] in ("close", "approve_line_price"),
                    "Closing a handoff requires an explicit decision on the same handoff")
        self.handoffs[handoff_id] = handoff

    def check_message(self, data, command):
        body = command["body"]
        if data["author"] == "customer":
            require(command["operationId"] == "submitAction" and body["type"] == "message"
                    and data["purpose"] == "input" and data["text"] == body["text"],
                    "Buyer message does not match the submitted text")
            require(data.get("handoffId") == body.get("handoffId"), "Buyer message changed its handoff")
        if data["author"] == "manager" and body["type"] in ("reply", "close"):
            require(command["operationId"] == "resolveHandoff" and data["text"] == body["reply"],
                    "Operator message does not match the reply")
        if "handoffId" in data:
            require(data["handoffId"] in self.handoffs, "Message refers to an unknown handoff")
            if command["operationId"] == "resolveHandoff":
                require(data["handoffId"] == command["params"]["handoffId"], "Wrong operator message handoff")

    def check_quote(self, quote, command):
        total = sum(line["packages"] * (line["unitPriceKopecks"] or 0) for line in quote["lines"])
        require(total == quote["knownSubtotalKopecks"], "Incorrect subtotal")
        if quote["state"] != "needs_review":
            require(total == quote["totalKopecks"], "Incorrect total")
        qid = quote["quoteId"]
        previous = self.quotes.get(qid)
        body = command["body"]
        if quote["state"] == "confirmed":
            require(command["role"] == "customer" and command["operationId"] == "submitAction"
                    and body == {"type": "confirm_quote", "quoteId": qid, "revision": quote["revision"]},
                    "Confirmation requires buyer consent to this quote and revision")
            require(previous and previous["state"] == "ready" and quote == {**previous, "state": "confirmed"},
                    "Confirmation requires unchanged ready terms")
        else:
            require(quote["revision"] == (previous["revision"] if previous else 0) + 1,
                    "Quote terms require a new revision")
            if body["type"] == "approve_line_price":
                require(previous and previous["state"] == "needs_review" and body["quoteId"] == qid
                        and body["revision"] == previous["revision"], "Price approval targets the wrong revision")
                matches = [line for line in previous["lines"] if line["offerId"] == body["offerId"]]
                require(len(matches) == 1 and matches[0]["unitPriceKopecks"] is None,
                        "Price approval requires one pending line")
                approved = [{**line, "unitPriceKopecks": body["unitPriceKopecks"]}
                            if line["offerId"] == body["offerId"] else line for line in previous["lines"]]
                require(quote["lines"] == approved, "Price approval must only set the requested line price")
        self.quotes[qid] = quote

    @staticmethod
    def document_key(record):
        return (record["quoteId"], record["revision"], record["kind"])

    def check_document(self, doc, command):
        body = command["body"]
        key = self.document_key(doc)
        require(command["role"] == "customer" and command["operationId"] == "submitAction"
                and body["type"] == "request_document" and self.document_key(body) == key,
                "Document does not match its issuance command")
        quote = self.quotes.get(doc["quoteId"])
        require(quote and doc["revision"] == quote["revision"], "Wrong document revision")
        allowed = {"confirmed"} if doc["kind"] == "demo_invoice" else {"ready", "confirmed"}
        require(quote["state"] in allowed, "Document issued too early")
        require(key not in self.documents, "Duplicate document event")
        require(not any(d["documentId"] == doc["documentId"] for d in self.documents.values()),
                "Different documents share an ID")
        require(doc["url"] == "/api/v1/documents/" + doc["documentId"], "Wrong document URL")
        require(self.pending_documents.pop(key, None) == command["response"]["actionId"],
                "Document does not complete its pending issuance")
        self.documents[key] = doc

    def read_page(self, after, page):
        require(after <= len(self.history), "Examples must establish the history before skipping part of it")
        for index, event in enumerate(page["events"], start=after):
            require(event["seq"] == index + 1, "Sequence is not contiguous")
            if index < len(self.history):
                require(event == self.history[index], "Existing journal event changed on replay")
                continue
            command = self.commands.get(event["actionId"])
            require(command is not None, "Event has no accepted action")
            self.observed_actions.add(event["actionId"])
            data = event["data"]
            if data["type"] == "handoff":
                self.check_handoff_event(data["handoff"], command)
            elif data["type"] == "message":
                self.check_message(data, command)
            elif data["type"] == "quote":
                self.check_quote(data["quote"], command)
            elif data["type"] == "document":
                self.check_document(data["document"], command)
            elif data["type"] == "action_failed" and command["body"]["type"] == "request_document":
                key = self.document_key(command["body"])
                require(self.pending_documents.pop(key, None) == event["actionId"],
                        "Failed action must complete its own pending issuance")
            self.history.append(event)
        end = after + len(page["events"])
        require(page["nextAfter"] == end, "Wrong pagination cursor")
        require(not page["hasMore"] or end > after, "Pagination must advance while more events are available")
        require(page["hasMore"] or end == len(self.history), "Page omitted known later events")

    def accept(self, step):
        body = step["body"]
        params = step["params"]
        if step["operationId"] == "resolveHandoff":
            require(step["role"] == "manager", "Only the operator may decide a handoff")
            current = self.handoffs.get(params["handoffId"])
            require(current and current["status"] == "open", "Decision requires an open handoff")
            if body["type"] == "approve_line_price":
                target = {key: body[key] for key in ("quoteId", "revision", "offerId")}
                require(current.get("quoteLine") == target, "Price approval targets a different handoff line")
        else:
            require(step["role"] == "customer" and params["conversationId"] == self.conversation,
                    "Customer action belongs to a different role or conversation")
            quote = self.quotes.get(body.get("quoteId"))
            if quote and body["revision"] < quote["revision"]:
                raise ValueError("Cannot accept an already superseded quote revision")
            if "handoffId" in body:
                current = self.handoffs.get(body["handoffId"])
                require(current and current["status"] == "open", "Buyer reply requires an open handoff")
        result = step["response"]
        require(result["conversationId"] == self.conversation, "Wrong receipt conversation")
        previous = self.commands.setdefault(result["actionId"], step)
        require(all(previous[key] == step[key] for key in ("body", "role", "operationId", "params")),
                "An action ID refers to different commands")
        if body["type"] == "request_document":
            key = self.document_key(body)
            require(key not in self.documents, "New key for a published document must return it directly")
            require(self.pending_documents.setdefault(key, result["actionId"]) == result["actionId"],
                    "Pending issuance must reuse its original receipt")

    def check_step(self, step):
        if "key" in step and self.check_retry(step):
            return
        if step["status"] >= 400:
            return
        op = step["operationId"]
        res = step.get("response", {})
        params = step.get("params", {})
        require(step["role"] in ("customer", "manager"), "Foreign session received a successful response")
        if op == "createConversation":
            require(step["role"] == "customer" and res["conversationId"] == self.conversation,
                    "Wrong conversation creation")
        elif step["status"] == 202:
            self.accept(step)
        elif op == "readEvents":
            require(params["conversationId"] == self.conversation, "Wrong history conversation")
            self.read_page(params.get("after", 0), res)
        elif op == "listHandoffs":
            require(step["role"] == "manager", "Only the operator may read the queue")
            expected = {i: h for i, h in self.handoffs.items() if h["status"] == "open"}
            actual = {h["handoffId"]: h for h in res["items"]}
            require(len(actual) == len(res["items"]) and actual == expected,
                    "Queue does not match the known open handoffs")
        elif op == "submitAction":
            body = step["body"]
            require(step["role"] == "customer" and params["conversationId"] == self.conversation
                    and body["type"] == "request_document"
                    and self.documents.get(self.document_key(body)) == res,
                    "Repeated issuance must return the original document")
            require(self.quotes[body["quoteId"]]["revision"] == body["revision"],
                    "A new issuance key requires the current quote revision")
        elif op == "downloadDocument":
            require(any(doc["documentId"] == params["documentId"] for doc in self.documents.values()),
                    "Successful download requires a published document")


def check_scenario(contract, scenario):
    state = Scenario(scenario["id"])
    for step in scenario["steps"]:
        try:
            contract.check_exchange(step)
            state.check_step(step)
        except Exception as exc:
            raise ValueError(f"{state.conversation} / {step['label']}: {exc}") from exc
    # These fixtures run to completion; live polling can of course finish an iteration earlier.
    require(state.commands.keys() == state.observed_actions, f"{state.conversation}: accepted action has no event")
    require(not state.pending_documents, f"{state.conversation}: a document request has no result")
    return len(state.history)


def main():
    contract = Contract(yaml.safe_load((HERE / "openapi.yaml").read_text(encoding="utf-8")))
    scenarios = json.loads((HERE / "examples.json").read_text(encoding="utf-8"))
    counts = Counter(step["operationId"] for case in scenarios for step in case["steps"])
    require(set(counts) == set(contract.operations), "An API operation has no example")
    events = sum(check_scenario(contract, case) for case in scenarios)
    print(f"{len(scenarios)} scenarios, {counts.total()} exchanges, {events} events, {len(counts)} operations: OK")


if __name__ == "__main__":
    main()
