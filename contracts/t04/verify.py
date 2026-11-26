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


def check_scenario(contract, scenario):
    receipts = {}
    accepted = set()
    latest_quotes = {}
    documents = {}
    conversation = scenario["id"]
    cursor = 0
    count = 0
    for step in scenario["steps"]:
        try:
            contract.check_exchange(step)
            op = step["operationId"]
            result = step.get("response", {})
            if step["status"] == 202:
                identity = (step["role"], op, tuple(sorted(step["params"].items())), step["key"])
                payload = (step["body"], result)
                require(receipts.setdefault(identity, payload) == payload, "Retry changed command or receipt")
                require(result["conversationId"] == conversation, "Wrong receipt conversation")
                accepted.add(result["actionId"])
            if op != "readEvents" or step["status"] != 200:
                continue
            require(step["params"]["after"] == cursor, "Example skipped part of the history")
            for event in result["events"]:
                require(event["seq"] == cursor + 1, "Sequence is not contiguous")
                require(event["actionId"] in accepted, "Event has no accepted action")
                cursor = event["seq"]
                count += 1
                data = event["data"]
                if data["type"] == "handoff":
                    require(data["handoff"]["conversationId"] == conversation, "Wrong handoff conversation")
                if data["type"] == "quote":
                    quote = data["quote"]
                    total = sum(line["packages"] * (line["unitPriceKopecks"] or 0) for line in quote["lines"])
                    require(total == quote["knownSubtotalKopecks"], "Incorrect subtotal")
                    if quote["state"] != "needs_review":
                        require(total == quote["totalKopecks"], "Incorrect total")
                    previous = latest_quotes.get(quote["quoteId"])
                    if previous and quote["revision"] == previous["revision"]:
                        require(previous["state"] == "ready" and quote == {**previous, "state": "confirmed"},
                                "Terms changed without a new revision")
                    else:
                        require(quote["revision"] == (previous["revision"] if previous else 0) + 1,
                                "Missing or repeated quote revision")
                    latest_quotes[quote["quoteId"]] = quote
                if data["type"] == "document":
                    doc = data["document"]
                    quote = latest_quotes[doc["quoteId"]]
                    require(doc["revision"] == quote["revision"], "Wrong document revision")
                    states = {"confirmed"} if doc["kind"] == "demo_invoice" else {"ready", "confirmed"}
                    require(quote["state"] in states, "Document issued too early")
                    key = (doc["quoteId"], doc["revision"], doc["kind"])
                    require(key not in documents, "Duplicate document event")
                    require(doc["url"] == "/api/v1/documents/" + doc["documentId"], "Wrong document URL")
                    documents[key] = doc["documentId"]
            require(result["nextAfter"] == cursor, "Wrong pagination cursor")
        except Exception as exc:
            raise ValueError(f"{conversation} / {step['label']}: {exc}") from exc
    return count


def main():
    contract = Contract(yaml.safe_load((HERE / "openapi.yaml").read_text(encoding="utf-8")))
    scenarios = json.loads((HERE / "examples.json").read_text(encoding="utf-8"))
    counts = Counter(step["operationId"] for case in scenarios for step in case["steps"])
    require(set(counts) == set(contract.operations), "An API operation has no example")
    events = sum(check_scenario(contract, case) for case in scenarios)
    print(f"{len(scenarios)} scenarios, {counts.total()} exchanges, {events} events, {len(counts)} operations: OK")


if __name__ == "__main__":
    main()
