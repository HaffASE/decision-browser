"""Owned, deterministic pages and an independent server-side benchmark oracle."""

from __future__ import annotations

import html
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CASES = {
    "click": {
        "goal": "Click Activate and verify that the status says Activated.",
        "values": {},
        "expected": "Activated",
        "body": '<h1>Activation</h1><button id="activate" type="button" '
                'onclick="eventPost(\'activate\', {})">Activate</button>'
                '<p id="status">Not activated</p>',
    },
    "search": {
        "goal": "Search for orbital mechanics and verify that the results say Results for orbital mechanics.",
        "values": {"Search query": "orbital mechanics"},
        "expected": "Results for orbital mechanics",
        "body": '<h1>Article search</h1><form onsubmit="search(event)">'
                '<label for="query">Search query</label> '
                '<input id="query" name="query" type="text"> '
                '<button type="submit">Search</button></form>'
                '<p id="status">No search performed</p>',
    },
    "select": {
        "goal": "Select Newest first, apply the order, and verify that the status says Sorted newest first.",
        "values": {},
        "expected": "Sorted newest first",
        "body": '<h1>Results ordering</h1><label for="sort">Sort order</label> '
                '<select id="sort"><option value="oldest">Oldest first</option>'
                '<option value="newest">Newest first</option></select> '
                '<button type="button" onclick="sortResults()">Apply order</button>'
                '<p id="status">Not sorted</p>',
    },
    "navigate": {
        "goal": "Open the Observatory details page and verify that its status says Observatory details opened.",
        "values": {},
        "expected": "Observatory details opened",
        "body": '<h1>Observatory</h1><a href="details">Observatory details</a>'
                '<p id="status">Details not open</p>',
    },
    "counter": {
        "goal": "Click Increase exactly three times and verify that the counter reads Count: 3.",
        "values": {},
        "expected": "Count: 3",
        "body": '<h1>Counter</h1><button id="increment" type="button" onclick="increment()">'
                'Increase</button><p id="status">Count: 0</p>',
    },
}

SCRIPT = """
<script>
let pending = false;
async function eventPost(name, payload) {
  if (pending) return;
  pending = true;
  try {
    const response = await fetch('event', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name, ...payload})});
    const data = await response.json();
    if (data.status) document.getElementById('status').textContent = data.status;
  } finally { pending = false; }
}
function search(event) { event.preventDefault(); eventPost('search', {query:document.getElementById('query').value}); }
function sortResults() { eventPost('sort', {order:document.getElementById('sort').value}); }
function increment() { eventPost('increment', {}); }
</script>
"""


class FixtureSite:
    def __init__(self):
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _reply(self, status, body, content_type):
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _route(self):
                match = re.fullmatch(r"/r/([a-f0-9]{32})/([a-z]+)/(?:(details|event))?", urlsplit(self.path).path)
                if not match:
                    return None
                key, case, action = match.groups()
                with owner.lock:
                    if key not in owner.state or owner.state[key]["case"] != case:
                        return None
                return key, case, action

            def do_GET(self):
                route = self._route()
                if not route:
                    return self._reply(404, "not found", "text/plain")
                key, case, action = route
                if action == "event" or (action == "details" and case != "navigate"):
                    return self._reply(404, "not found", "text/plain")
                if action == "details":
                    with owner.lock:
                        owner.state[key]["details_visits"] += 1
                    body = '<h1>Observatory details</h1><p id="status">Observatory details opened</p>'
                else:
                    body = CASES[case]["body"]
                page = '<!doctype html><html lang="en"><meta charset="utf-8">' \
                       '<meta name="viewport" content="width=device-width, initial-scale=1">' \
                       f'<title>Benchmark {html.escape(case)}</title><body>{body}{SCRIPT}</body></html>'
                return self._reply(200, page, "text/html; charset=utf-8")

            def do_POST(self):
                route = self._route()
                if not route or route[2] != "event":
                    return self._reply(404, "not found", "text/plain")
                key, case, _ = route
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= n <= 4096:
                        raise ValueError("body size")
                    payload = json.loads(self.rfile.read(n))
                    if not isinstance(payload, dict):
                        raise TypeError("body type")
                except (TypeError, ValueError, json.JSONDecodeError):
                    return self._reply(400, "{}", "application/json")
                name = payload.get("name")
                response = {}
                with owner.lock:
                    state = owner.state[key]
                    state["events"].append({"name": name, "query": payload.get("query"), "order": payload.get("order")})
                    if case == "click" and name == "activate":
                        state["activated"] += 1
                        response["status"] = "Activated"
                    elif case == "search" and name == "search":
                        state["query"] = payload.get("query")
                        response["status"] = "Results for " + str(state["query"])
                    elif case == "select" and name == "sort":
                        state["order"] = payload.get("order")
                        response["status"] = "Sorted newest first" if state["order"] == "newest" else "Sorted oldest first"
                    elif case == "counter" and name == "increment":
                        state["count"] += 1
                        response["status"] = "Count: " + str(state["count"])
                return self._reply(200, json.dumps(response), "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def register(self, case):
        if case not in CASES:
            raise ValueError("Unknown fixture case")
        key = uuid.uuid4().hex
        with self.lock:
            self.state[key] = {"case": case, "events": [], "activated": 0, "query": None,
                               "order": None, "count": 0, "details_visits": 0}
        url = f"http://127.0.0.1:{self.server.server_port}/r/{key}/{case}/"
        return key, url

    def verify(self, key):
        with self.lock:
            state = dict(self.state[key])
        case = state["case"]
        success = {
            "click": state["activated"] >= 1,
            "search": state["query"] == "orbital mechanics",
            "select": state["order"] == "newest",
            "navigate": state["details_visits"] >= 1,
            "counter": state["count"] == 3,
        }[case]
        return {"success": success, "events": state["events"], "details_visits": state["details_visits"],
                "counter": state["count"]}
