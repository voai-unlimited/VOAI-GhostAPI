import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from voai_ghostapi.interactive import discover_api_requests

pytest.importorskip("playwright")


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b'''<button id="tab" onclick="fetch('/api-tab')">Tab</button>
            <input id="filter"><button id="page" onclick="fetch('/api-page')">Next</button>
            <script>fetch('/api.json').then(r => r.json())</script>'''
            status = 200
            content_type = "text/html"
        elif self.path.startswith("/api"):
            status = 200 if self.headers.get("Cookie") == "session=authorized" else 401
            body = b'{"items":[1,2]}' if status == 200 else b'{"error":"unauthorized"}'
            content_type = "application/json"
        else:
            status, body, content_type = 404, b"missing", "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def test_playwright_discovers_dynamic_json_and_authorized_session(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"cookies": [{"name": "session", "value": "authorized", "domain": "127.0.0.1", "path": "/"}]}), encoding="utf-8")
        observations = asyncio.run(discover_api_requests(f"http://127.0.0.1:{server.server_port}/", storage_state=state))
        api = next(item for item in observations if item["url"].endswith("/api.json"))
        assert api["status"] == 200
        assert api["path"] == "/api.json"
        assert api["response_sample"] == {"items": [1, 2]}
        assert api["observed"] is True
        assert api["discovery_status"] == "observed"
        assert api["interaction_type"] == "navigation"
        interacted = asyncio.run(
            discover_api_requests(
                f"http://127.0.0.1:{server.server_port}/",
                storage_state=state,
                interactions=[
                    {"action": "click", "kind": "tab", "selector": "#tab"},
                    {"action": "fill", "kind": "filter", "selector": "#filter", "value": "active"},
                    {"action": "click", "kind": "pagination", "selector": "#page"},
                    {"action": "scroll", "kind": "infinite-scroll", "distance": 400},
                ],
            )
        )
        assert any(item["url"].endswith("/api-tab") for item in interacted)
        assert any(item["url"].endswith("/api-page") for item in interacted)
        assert any(item.get("triggering_interaction") == "click-1" for item in interacted if item["url"].endswith("/api-tab"))
        assert any(item.get("triggering_interaction") == "click-3" for item in interacted if item["url"].endswith("/api-page"))
        page_item = next(item for item in interacted if item["url"].endswith("/api-page"))
        assert page_item["interaction_type"] == "pagination"
        assert page_item["source_page"].startswith("http://127.0.0.1:")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_interactive_recipes_reject_unsafe_actions():
    with pytest.raises(ValueError, match="only click"):
        asyncio.run(discover_api_requests("https://example.test", interactions=[{"action": "delete"}]))


def test_interactive_recipes_are_bounded():
    recipes = [{"action": "scroll"}] * 51
    with pytest.raises(ValueError, match="at most"):
        asyncio.run(discover_api_requests("https://example.test", interactions=recipes))
