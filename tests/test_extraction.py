import asyncio
import json

import pytest

import voai_ghostapi.extract as extract_module
from voai_ghostapi.core import Observation, Workspace, validate_workspace
from voai_ghostapi.extract import (
    _Links,
    _normalize,
    build_snapshot,
    extract,
    parse_resource_selection,
    validate_har_inputs,
    validate_url,
)
from voai_ghostapi.interactive import discover_api_requests


def test_validate_url_rejects_local_destinations():
    with pytest.raises(ValueError):
        validate_url("http://127.0.0.1:8000/")
    with pytest.raises(ValueError):
        validate_url("file:///etc/passwd")


def test_interactive_discovery_reports_optional_dependency(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "playwright", None)
    with pytest.raises(RuntimeError, match="optional 'browser'"):
        asyncio.run(discover_api_requests("https://example.test"))


def test_build_snapshot_copies_resources_and_writes_manifest(tmp_path):
    website = tmp_path / "website"
    website.mkdir()
    (website / "resources").mkdir()
    (website / "resources" / "page.html").write_text('<html><a href="https://example.test/api.json">API</a><img srcset="https://example.test/hero.png 1x, https://example.test/hero.png 2x"></html>')
    (website / "resources" / "style.css").write_text("body { background: url('https://example.test/hero.png'); }")
    (website / "extraction.json").write_text(json.dumps({"pages": [{"url": "https://example.test/", "path": "resources/page.html"}, {"url": "https://example.test/api.json", "path": "resources/api.json"}]}))
    (website / "resources" / "api.json").write_text('{"ok": true}')
    (website / "resources" / "hero.png").write_bytes(b"PNG")
    (website / "resources" / "app.js").write_text("fetch('https://example.test/api.json')")
    report_data = json.loads((website / "extraction.json").read_text())
    report_data["pages"].append({"url": "https://example.test/hero.png", "path": "resources/hero.png"})
    report_data["pages"].append({"url": "https://example.test/style.css", "path": "resources/style.css"})
    report_data["pages"].append({"url": "https://example.test/app.js", "path": "resources/app.js"})
    (website / "extraction.json").write_text(json.dumps(report_data))
    snapshot = tmp_path / "snapshot"
    report = build_snapshot(website, snapshot)
    assert report["files"] == 5
    assert (snapshot / "resources" / "page.html").exists()
    assert 'href="api.json"' in (snapshot / "resources" / "page.html").read_text()
    assert 'srcset="hero.png 1x, hero.png 2x"' in (snapshot / "resources" / "page.html").read_text()
    assert "url('hero.png')" in (snapshot / "resources" / "style.css").read_text()
    assert "fetch('api.json')" in (snapshot / "resources" / "app.js").read_text()
    assert json.loads((snapshot / "snapshot.json").read_text())["files"] == 5


def test_snapshot_requires_website_directory(tmp_path):
    with pytest.raises(ValueError):
        build_snapshot(tmp_path / "missing", tmp_path / "snapshot")


def test_snapshot_report_exposes_limitations(tmp_path):
    website = tmp_path / "website"
    website.mkdir()
    (website / "page.html").write_text("<p>offline</p>")
    report = build_snapshot(website, tmp_path / "snapshot")
    assert report["limitations"]


def test_workspace_rejects_unknown_observation_source():
    observation = Observation(method="GET", url="/", path="/", status=200, source="unknown")
    assert "invalid observation source" in validate_workspace(Workspace(observations=[observation]))


def test_link_parser_collects_navigation_and_resource_attributes():
    parser = _Links()
    parser.feed(
        '<a href="/items">items</a><link href="/site.css">'
        '<script src="/app.js"></script><img src="/hero.png">'
        '<video poster="/poster.jpg"></video>'
    )
    assert parser.links == ["/items"]
    assert parser.resources == [("/site.css", "link"), ("/app.js", "script"), ("/hero.png", "img"), ("/poster.jpg", "video")]


def test_resource_kind_and_suffix_cover_non_api_assets():
    assert extract_module._resource_kind("font/woff2", "https://example.test/font") == "fonts"
    assert extract_module._resource_kind("video/mp4", "https://example.test/movie") == "video"
    assert extract_module._resource_kind("audio/ogg", "https://example.test/sound") == "audio"
    assert extract_module._resource_kind("application/pdf", "https://example.test/file") == "documents"
    assert extract_module._resource_kind("image/svg+xml", "https://example.test/icon") == "svg"
    assert extract_module._resource_suffix("https://example.test/file", "application/pdf") == ".pdf"


def test_normalize_removes_fragments_and_preserves_query():
    assert _normalize("https://example.test/a#section") == "https://example.test/a"
    assert _normalize("https://example.test/a?x=1#section") == "https://example.test/a?x=1"
    assert _normalize("https://example.test/a?utm_source=mail&b=2&a=1") == "https://example.test/a?a=1&b=2"


def test_crawl_reads_nested_sitemap_indexes(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, body: bytes, content_type: str):
            self.url = url
            self.content = body
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self.text = body.decode("utf-8")

        def json(self):
            return json.loads(self.text)

    responses = {
        "https://example.test/robots.txt": Response("https://example.test/robots.txt", b"", "text/plain"),
        "https://example.test/sitemap.xml": Response("https://example.test/sitemap.xml", b"<sitemapindex><sitemap><loc>/nested.xml</loc></sitemap></sitemapindex>", "application/xml"),
        "https://example.test/nested.xml": Response("https://example.test/nested.xml", b"<urlset><url><loc>/from-sitemap</loc></url></urlset>", "application/xml"),
        "https://example.test/": Response("https://example.test/", b"<p>root</p>", "text/html"),
        "https://example.test/from-sitemap": Response("https://example.test/from-sitemap", b"<p>page</p>", "text/html"),
    }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            return responses[url]

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website", max_pages=2))
    assert {item["url"] for item in report["pages"]} == {
        "https://example.test/",
        "https://example.test/from-sitemap",
    }


def test_crawl_batches_requests_with_bounded_concurrency(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, body: bytes, content_type: str = "text/html"):
            self.url = url
            self.content = body
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self.text = body.decode("utf-8")

        def json(self):
            return json.loads(self.text)

    responses = {
        "https://example.test/robots.txt": Response("https://example.test/robots.txt", b"", "text/plain"),
        "https://example.test/sitemap.xml": Response("https://example.test/sitemap.xml", b"<urlset />", "application/xml"),
        "https://example.test/": Response("https://example.test/", b"<a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a><a href='/d'>d</a>"),
    }
    for name in "abcd":
        responses[f"https://example.test/{name}"] = Response(f"https://example.test/{name}", b"<p>page</p>")
    active = 0
    maximum = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return responses[url]

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website", max_pages=5, concurrency=2))
    assert report["processed"] == 5
    assert 1 < maximum <= 2


def test_crawl_caps_query_variants_to_prevent_loops(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, body: bytes):
            self.url = url
            self.content = body
            self.status_code = 200
            self.headers = {"content-type": "text/html"}
            self.text = body.decode("utf-8")

    responses = {
        "https://example.test/robots.txt": Response("https://example.test/robots.txt", b""),
        "https://example.test/sitemap.xml": Response("https://example.test/sitemap.xml", b"<urlset />"),
        "https://example.test/": Response("https://example.test/", b"<a href='/?utm_source=a'>1</a><a href='/?x=1'>2</a><a href='/?x=2'>3</a><a href='/?x=3'>4</a><a href='/?x=4'>5</a>"),
    }
    for index in range(1, 5):
        responses[f"https://example.test/?x={index}"] = Response(f"https://example.test/?x={index}", b"<p>variant</p>")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            return responses[url]

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website", max_pages=20))
    assert report["processed"] == 4
    assert report["pending"] == 0


def test_crawl_persists_pages_resources_and_json_observations(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, content: bytes, content_type: str, status: int = 200):
            self.url = url
            self.content = content
            self.status_code = status
            self.headers = {"content-type": content_type}
            self.text = content.decode("utf-8", "replace")

        def json(self):
            return json.loads(self.text)

    responses = {
        "https://example.test/robots.txt": Response("https://example.test/robots.txt", b"", "text/plain"),
        "https://example.test/sitemap.xml": Response("https://example.test/sitemap.xml", b"<urlset />", "application/xml"),
        "https://example.test/": Response("https://example.test/", b'<a href="/api.json"><img src="/hero.png"></a>', "text/html"),
        "https://example.test/api.json": Response("https://example.test/api.json", b'{"id": 1}', "application/json"),
        "https://example.test/hero.png": Response("https://example.test/hero.png", b"PNG", "image/png"),
    }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            return responses[url]

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = __import__("asyncio").run(extract_module.crawl("https://example.test/", tmp_path / "website", max_pages=5))
    assert report["processed"] == 3
    assert report["failed"] == 0
    assert report["api_observations"][0]["response_body"] == {"id": 1}
    assert (tmp_path / "website" / "extraction.json").exists()


def test_crawl_selected_pages_does_not_follow_other_links(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, content: bytes, content_type: str):
            self.url = url
            self.content = content
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self.text = content.decode("utf-8", "replace")

        def json(self):
            return json.loads(self.text)

    responses = {
        "https://example.test/robots.txt": Response("https://example.test/robots.txt", b"", "text/plain"),
        "https://example.test/sitemap.xml": Response("https://example.test/sitemap.xml", b"<urlset />", "application/xml"),
        "https://example.test/one": Response("https://example.test/one", b'<a href="/two">two</a>', "text/html"),
        "https://example.test/two": Response("https://example.test/two", b"two", "text/plain"),
    }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            return responses[url]

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = asyncio.run(
        extract_module.crawl(
            "https://example.test/one",
            tmp_path / "website",
            selected_pages=("/one",),
        )
    )
    assert report["processed"] == 1
    assert all(item["url"] != "https://example.test/two" for item in report["pages"])


def test_extract_har_writes_unified_workspace(tmp_path):
    capture = tmp_path / "capture.har"
    capture.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {"method": "GET", "url": "https://example.test/items/1"},
                            "response": {
                                "status": 200,
                                "headers": [{"name": "Content-Type", "value": "application/json"}],
                                "content": {"text": '{"id":1}'},
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "result"
    workspace = asyncio.run(extract("", output, (capture,), yes=True))
    assert workspace is not None
    assert workspace.observations[0].source == "har"
    assert (output / "workspace" / "workspace.json").exists()


def test_extract_combines_har_and_website_observations(tmp_path, monkeypatch):
    capture = tmp_path / "capture.har"
    capture.write_text(
        json.dumps({"log": {"entries": [{"request": {"method": "GET", "url": "https://example.test/items/1"}, "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "application/json"}], "content": {"text": '{"source":"har"}'}}}]}}),
        encoding="utf-8",
    )

    async def fake_crawl(*_args, **_kwargs):
        return {"api_observations": [{"method": "GET", "url": "https://example.test/items/2", "path": "/items/2", "status": 200, "response_body": {"source": "website"}, "source_page": "https://example.test/"}]}

    monkeypatch.setattr(extract_module, "crawl", fake_crawl)
    workspace = asyncio.run(extract_module.extract("https://example.test/", tmp_path / "combined", (capture,), yes=True))
    assert workspace is not None
    assert {item.source for item in workspace.observations} == {"har", "website"}
    assert len(workspace.observations) == 2
    assert (tmp_path / "combined" / "workspace" / "workspace.json").exists()


def test_combined_duplicate_observation_records_both_sources(tmp_path, monkeypatch):
    capture = tmp_path / "capture.har"
    capture.write_text(
        json.dumps({"log": {"entries": [{"request": {"method": "GET", "url": "https://example.test/items/1"}, "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "application/json"}], "content": {"text": '{"id":1}'}}}]}}),
        encoding="utf-8",
    )

    async def fake_crawl(*_args, **_kwargs):
        return {"api_observations": [{"method": "GET", "url": "https://example.test/items/1", "path": "/items/1", "status": 200, "response_body": {"id": 1}, "source_page": "https://example.test/"}]}

    monkeypatch.setattr(extract_module, "crawl", fake_crawl)
    workspace = asyncio.run(extract_module.extract("https://example.test/", tmp_path / "combined", (capture,), yes=True))
    assert workspace is not None
    assert len(workspace.observations) == 1
    assert workspace.observations[0].source == "both"


def test_resource_selection_supports_all_clear_aliases_and_rejects_unknown():
    assert parse_resource_selection("all") == frozenset({"all"})
    assert parse_resource_selection("clear") == frozenset()
    assert "json" in parse_resource_selection("api")
    with pytest.raises(ValueError, match="unsupported resource"):
        parse_resource_selection("does-not-exist")


def test_har_inputs_are_prevalidated_without_writing_workspace(tmp_path):
    capture = tmp_path / "capture.har"
    capture.write_text('{"log":{"entries":[]}}', encoding="utf-8")
    result = validate_har_inputs([capture])
    assert result == [{"path": str(capture), "entries": 0, "valid": True}]
    assert not (tmp_path / "workspace").exists()
    bad = tmp_path / "bad.har"
    bad.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid HAR"):
        validate_har_inputs([bad])


def test_crawl_honors_robots_and_retries_transient_failures(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, body: bytes, content_type: str, status: int = 200):
            self.url = url
            self.content = body
            self.status_code = status
            self.headers = {"content-type": content_type}
            self.text = body.decode("utf-8", "replace")

        def json(self):
            return json.loads(self.text)

    attempts = {"api": 0}
    page = Response("https://example.test/", b'<a href="/api.json"><a href="/blocked">', "text/html")
    robots = Response("https://example.test/robots.txt", b"User-agent: *\nDisallow: /blocked", "text/plain")
    sitemap = Response("https://example.test/sitemap.xml", b"<urlset />", "application/xml")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            if url.endswith("/robots.txt"):
                return robots
            if url.endswith("/sitemap.xml"):
                return sitemap
            if url.endswith("/"):
                return page
            attempts["api"] += 1
            if attempts["api"] == 1:
                return Response(url, b"busy", "text/plain", 503)
            return Response(url, b'{"ok":true}', "application/json")

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    original_sleep = asyncio.sleep
    monkeypatch.setattr(extract_module.asyncio, "sleep", lambda _delay: original_sleep(0))
    report = asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website", max_pages=5))
    assert attempts["api"] == 2
    assert report["skipped"] >= 1
    assert all("blocked" not in str(item) for item in report["pages"])


def test_crawl_rejects_redirects_to_another_origin(tmp_path, monkeypatch):
    class Response:
        def __init__(self, url: str, status: int, headers: dict[str, str] | None = None):
            self.url = url
            self.status_code = status
            self.headers = {"content-type": "text/plain", **(headers or {})}
            self.content = b""
            self.text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            if url.endswith(("robots.txt", "sitemap.xml")):
                return Response(url, 404)
            return Response(url, 302, {"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    report = asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website"))
    assert report["failed"] == 1
    assert "redirect leaves" in str(report["pages"][0]["error"])


def test_crawl_persists_checkpoint_when_cancelled(tmp_path, monkeypatch):
    class Response:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "text/plain"}
            self.content = b""
            self.text = ""
            self.url = "https://example.test/robots.txt"

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            if url.endswith(("robots.txt", "sitemap.xml")):
                return Response()
            raise asyncio.CancelledError

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(extract_module.crawl("https://example.test/", tmp_path / "website"))
    checkpoint = json.loads((tmp_path / "website" / "checkpoint.json").read_text())
    assert checkpoint["seen"] == []
    assert checkpoint["queue"] == [["https://example.test/", 0]]


def test_crawl_honors_cancel_event_and_reports_progress(tmp_path, monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            raise AssertionError(f"network should not run after cancellation: {url}")

    monkeypatch.setattr(extract_module, "validate_url", lambda value: value)
    monkeypatch.setattr(extract_module.httpx, "AsyncClient", Client)
    events: list[dict[str, object]] = []
    cancel = asyncio.Event()
    cancel.set()
    report = asyncio.run(
        extract_module.crawl(
            "https://example.test/",
            tmp_path / "website",
            cancel_event=cancel,
            progress=events.append,
        )
    )
    assert report["processed"] == 0
    assert events[-1]["stage"] == "complete"
    checkpoint = json.loads((tmp_path / "website" / "checkpoint.json").read_text())
    assert checkpoint["queue"] == [["https://example.test/", 0]]
