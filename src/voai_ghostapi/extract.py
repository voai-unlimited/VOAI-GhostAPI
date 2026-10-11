from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import posixpath
import re
import socket
import tempfile
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .core import (
    Observation,
    Workspace,
    _safe_output,
    import_har,
    infer_schema,
    merge_schemas,
    route_path,
    sanitize,
)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.resources: list[tuple[str, str]] = []
        self.inline: list[tuple[str, str]] = []
        self._inline_tag: str | None = None
        self._inline_kind: str | None = None
        self._inline_data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        elif values.get("href"):
            self.resources.append((values["href"] or "", tag))
        for key in ("src", "poster"):
            if values.get(key):
                self.resources.append((values[key] or "", tag))
        if values.get("srcset"):
            for candidate in (values["srcset"] or "").split(","):
                reference = candidate.strip().split(None, 1)[0]
                if reference:
                    self.resources.append((reference, tag))
        if values.get("style"):
            self.inline.append(("css", values["style"] or ""))
        if tag in {"style", "script"} and not values.get("src"):
            self._inline_tag = tag
            self._inline_data = []
            script_type = (values.get("type") or "").lower()
            self._inline_kind = "json" if "json" in script_type else "css" if tag == "style" else "js"

    def handle_data(self, data: str) -> None:
        if self._inline_tag is not None:
            self._inline_data.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._inline_tag == tag and self._inline_kind is not None:
            content = "".join(self._inline_data)
            if content.strip():
                self.inline.append((self._inline_kind, content))
            self._inline_tag = None
            self._inline_kind = None
            self._inline_data = []


_RESOURCE_KINDS = frozenset(
    {"html", "css", "js", "images", "svg", "fonts", "json", "manifests", "icons", "video", "audio", "documents", "other"}
)

_RESOURCE_ALIASES = {
    "api": "json",
    "apis": "json",
    "javascript": "js",
    "image": "images",
    "images-and-svg": "images",
    "font": "fonts",
    "structured-data": "json",
    "video-and-audio": "video",
    "documents-and-downloads": "documents",
    "manifest": "manifests",
    "manifests": "manifests",
    "icon": "icons",
    "icons": "icons",
}


def parse_resource_selection(value: str | frozenset[str]) -> frozenset[str]:
    """Validate and normalize a resource selection from the CLI or wizard."""
    if isinstance(value, frozenset):
        raw = set(value)
    else:
        raw = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not raw or raw <= {"clear", "none"}:
        return frozenset()
    if "all" in raw or "select-all" in raw:
        if len(raw - {"all", "select-all"}) > 0:
            raise ValueError("all cannot be combined with individual resources")
        return frozenset({"all"})
    normalized = {_RESOURCE_ALIASES.get(item, item) for item in raw}
    unknown = normalized - _RESOURCE_KINDS
    if unknown:
        raise ValueError(f"unsupported resource type(s): {', '.join(sorted(unknown))}")
    return frozenset(normalized)


def validate_har_inputs(har_files: tuple[Path, ...] | list[Path]) -> list[dict[str, object]]:
    """Validate HAR containers before any persistent workspace is created."""
    results: list[dict[str, object]] = []
    for source in har_files:
        try:
            if source.is_symlink() or not source.is_file():
                raise ValueError("HAR path must be a regular file")
            if source.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("HAR exceeds 25 MiB limit")
            payload = json.loads(source.read_text(encoding="utf-8"))
            log = payload.get("log")
            entries = log.get("entries") if isinstance(log, dict) else None
            if not isinstance(entries, list):
                raise TypeError("HAR log.entries must be a list")
            results.append({"path": str(source), "entries": len(entries), "valid": True})
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid HAR {source}: {exc}") from exc
    return results


def _observation_key(observation: Observation) -> tuple[str, str, int, str]:
    return (
        observation.method,
        observation.path,
        observation.status,
        json.dumps(observation.response_body, sort_keys=True, default=str),
    )


def _merge_observations(observations: list[Observation]) -> list[Observation]:
    """Deduplicate exact observations and retain both-source provenance."""
    merged: dict[tuple[str, str, int, str], Observation] = {}
    for observation in observations:
        key = _observation_key(observation)
        previous = merged.get(key)
        if previous is None:
            merged[key] = observation
            continue
        if previous.source != observation.source:
            previous.source = "both"
        if previous.source_page is None:
            previous.source_page = observation.source_page
    return sorted(
        merged.values(),
        key=lambda item: (item.method, item.path, item.status, item.source),
    )


def _resource_kind(content_type: str, url: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(urlsplit(url).path).suffix.lower()
    if mime == "text/html" or suffix in {".html", ".htm"}:
        return "html"
    if mime == "text/css" or suffix == ".css":
        return "css"
    if "javascript" in mime or mime in {"application/ecmascript", "text/ecmascript"} or suffix in {".js", ".mjs", ".cjs"}:
        return "js"
    if mime in {"application/manifest+json", "application/x-web-app-manifest+json"} or suffix in {".webmanifest", ".manifest"}:
        return "manifests"
    if mime == "application/json" or mime.endswith("+json") or suffix in {".json", ".map"}:
        return "json"
    if mime == "image/svg+xml" or suffix == ".svg":
        return "svg"
    if mime in {"image/x-icon", "image/vnd.microsoft.icon"} or suffix == ".ico":
        return "icons"
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("font/") or suffix in {".woff", ".woff2", ".ttf", ".otf", ".eot"}:
        return "fonts"
    if mime.startswith("video/") or suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video"
    if mime.startswith("audio/") or suffix in {".mp3", ".wav", ".ogg", ".m4a"}:
        return "audio"
    if mime in {"application/pdf", "application/msword", "application/rtf"} or suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip"}:
        return "documents"
    return "other"


def _resource_suffix(url: str, content_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix and len(suffix) <= 10 and suffix[1:].replace("_", "").isalnum():
        return suffix
    mime = content_type.split(";", 1)[0].strip().lower()
    return {
        "text/html": ".html",
        "text/css": ".css",
        "application/javascript": ".js",
        "text/javascript": ".js",
        "application/json": ".json",
        "application/manifest+json": ".webmanifest",
        "image/svg+xml": ".svg",
        "image/x-icon": ".ico",
        "application/pdf": ".pdf",
    }.get(mime, ".bin")


def build_snapshot(website_dir: Path, snapshot_dir: Path) -> dict[str, object]:
    """Create a conservative local HTML snapshot from downloaded pages."""
    if not website_dir.is_dir():
        raise ValueError("website extraction directory does not exist")
    _safe_output(snapshot_dir)
    mapping: dict[str, str] = {}
    url_map: dict[str, str] = {}
    report_path = website_dir / "extraction.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for page in report.get("pages", []):
                if isinstance(page, dict) and page.get("url") and page.get("path"):
                    url_map[str(page["url"])] = str(page["path"]).replace("\\", "/")
        except (OSError, TypeError, ValueError):
            url_map = {}
    copied = 0
    for source in website_dir.rglob("*"):
        if not source.is_file() or source.name == "extraction.json":
            continue
        destination = snapshot_dir / source.relative_to(website_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        if source.suffix.lower() in {".html", ".htm"}:
            text = content.decode("utf-8", "replace")
            source_rel = str(source.relative_to(website_dir)).replace("\\", "/")
            source_url = next((url for url, path in url_map.items() if path == source_rel), "")
            for match in re.finditer(r"(?P<prefix>(?:href|src|poster)=[\"'])(?P<ref>[^\"']+)", text, re.IGNORECASE):
                reference = match.group("ref")
                target = url_map.get(_normalize(urljoin(source_url, reference))) if source_url else None
                if target:
                    local = Path(target).as_posix()
                    current = destination.relative_to(snapshot_dir).parent.as_posix()
                    replacement = posixpath.relpath(local, current or ".")
                    text = text.replace(match.group("prefix") + reference, match.group("prefix") + replacement, 1)
            def rewrite_srcset(
                match: re.Match[str],
                *,
                source_url: str = source_url,
                destination: Path = destination,
            ) -> str:
                prefix, references = match.group("prefix"), match.group("refs")
                rewritten: list[str] = []
                for candidate in references.split(","):
                    pieces = candidate.strip().split(None, 1)
                    if not pieces:
                        continue
                    target = url_map.get(_normalize(urljoin(source_url, pieces[0]))) if source_url else None
                    if target:
                        current = destination.relative_to(snapshot_dir).parent.as_posix()
                        pieces[0] = posixpath.relpath(Path(target).as_posix(), current or ".")
                    rewritten.append(" ".join(pieces))
                return prefix + ", ".join(rewritten) + match.group("quote")

            text = re.sub(r"(?P<prefix>srcset=[\"'])(?P<refs>[^\"']+)(?P<quote>[\"'])", rewrite_srcset, text, flags=re.IGNORECASE)
            destination.write_text(text, encoding="utf-8")
        elif source.suffix.lower() == ".css":
            text = content.decode("utf-8", "replace")
            source_rel = str(source.relative_to(website_dir)).replace("\\", "/")
            source_url = next((url for url, path in url_map.items() if path == source_rel), "")
            for match in re.finditer(r"url\((['\"]?)([^)'\"]+)\1\)", text, re.IGNORECASE):
                reference = match.group(2)
                target = url_map.get(_normalize(urljoin(source_url, reference))) if source_url else None
                if target:
                    current = destination.relative_to(snapshot_dir).parent.as_posix()
                    replacement = posixpath.relpath(Path(target).as_posix(), current or ".")
                    text = text.replace(match.group(0), f"url('{replacement}')", 1)
            destination.write_text(text, encoding="utf-8")
        elif source.suffix.lower() in {".js", ".mjs"}:
            text = content.decode("utf-8", "replace")
            source_rel = str(source.relative_to(website_dir)).replace("\\", "/")
            source_url = next((url for url, path in url_map.items() if path == source_rel), "")
            for match in re.finditer(r"(?P<prefix>(?:fetch|import)\(\s*['\"])(?P<ref>[^'\"]+)", text):
                reference = match.group("ref")
                target = url_map.get(_normalize(urljoin(source_url, reference))) if source_url else None
                if target:
                    current = destination.relative_to(snapshot_dir).parent.as_posix()
                    replacement = posixpath.relpath(Path(target).as_posix(), current or ".")
                    text = text.replace(match.group("prefix") + reference, match.group("prefix") + replacement, 1)
            destination.write_text(text, encoding="utf-8")
        else:
            destination.write_bytes(content)
        mapping[str(source.relative_to(website_dir))] = str(destination.relative_to(snapshot_dir))
        copied += 1
    report = {"files": copied, "mapping": mapping, "limitations": ["dynamic behavior and backend APIs remain online-dependent"]}
    (snapshot_dir / "snapshot.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def persist_interactive_resources(
    website_dir: Path,
    observations: list[dict[str, Any]],
    *,
    max_resource_bytes: int = 5 * 1024 * 1024,
    max_resources: int = 1000,
) -> list[dict[str, object]]:
    """Persist bodies captured by the optional browser network observer."""
    report_path = website_dir / "extraction.json"
    if not report_path.is_file():
        return []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    resource_records = report.setdefault("resources", [])
    resources = website_dir / "resources"
    resources.mkdir(exist_ok=True)
    known = {
        str(item.get("sha256")): item
        for item in resource_records
        if isinstance(item, dict) and item.get("path")
    }
    added: list[dict[str, object]] = []
    for observation in observations:
        try:
            if "body" in observation:
                body = str(observation["body"]).encode("utf-8")
            elif observation.get("body_base64"):
                body = base64.b64decode(str(observation["body_base64"]), validate=True)
            else:
                continue
            url = str(observation.get("url", ""))
            content_type = str(observation.get("content_type", ""))
            digest = hashlib.sha256(body).hexdigest()
            kind = _resource_kind(content_type, url)
            record: dict[str, object] = {
                "url": url,
                "source_page": str(observation.get("source_page") or url),
                "status": int(observation.get("status", 200)),
                "path": None,
                "sha256": digest,
                "size": len(body),
                "content_type": content_type,
                "resource_type": kind,
                "extraction_status": "downloaded",
            }
            if len(body) > max_resource_bytes:
                record["extraction_status"] = "skipped by resource size limit"
            elif digest in known:
                record["path"] = known[digest].get("path")
            elif len(known) >= max_resources:
                record["extraction_status"] = "skipped by resource limit"
            else:
                target = resources / f"{digest}{_resource_suffix(url, content_type)}"
                if not target.exists():
                    target.write_bytes(body)
                record["path"] = str(target.relative_to(website_dir))
                added.append(record)
                known[digest] = record
            resource_records.append(record)
        except (ValueError, TypeError, OSError):
            continue
    report["resources"] = resource_records
    report["downloaded_resources"] = len(known)
    (website_dir / "extraction.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return added


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must use http or https")
    host = parts.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        raise ValueError("local and metadata hosts are blocked")
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise ValueError("could not resolve URL host") from exc
    if any(address.is_private or address.is_loopback or address.is_link_local or address.is_reserved for address in addresses):
        raise ValueError("private or local destinations are blocked")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _normalize(url: str) -> str:
    clean, _ = urldefrag(url)
    parts = urlsplit(clean)
    tracking = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid"}
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking
    ]
    pairs.sort()
    query = urlencode(pairs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))


class _RateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.delay <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.delay
        if wait_for:
            await asyncio.sleep(wait_for)


async def _get_with_safe_redirects(
    client: httpx.AsyncClient,
    url: str,
    origin: str,
    *,
    rate_limiter: _RateLimiter | None = None,
) -> httpx.Response:
    request_url = url
    for _redirect in range(5):
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                if rate_limiter is not None:
                    await rate_limiter.wait()
                candidate = await client.get(request_url)
                response = candidate
                if candidate.status_code not in {429, 500, 502, 503, 504}:
                    break
            except httpx.HTTPError:
                if attempt == 2:
                    raise
            await asyncio.sleep(0.2 * (2**attempt))
        if response is None:
            raise httpx.HTTPError("no response")
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            if not location:
                return response
            redirected = _normalize(urljoin(request_url, location))
            validate_url(redirected)
            if urlsplit(redirected).netloc.lower() != origin:
                raise ValueError("redirect leaves the requested origin")
            request_url = redirected
            continue
        return response
    raise httpx.HTTPError("too many redirects")


async def _read_sitemap_urls(
    client: httpx.AsyncClient,
    start: str,
    origin: str,
    *,
    rate_limiter: _RateLimiter | None = None,
    max_sitemaps: int = 16,
) -> list[str]:
    """Read urlsets and nested sitemap indexes without leaving the origin."""
    pending = deque([_normalize(urljoin(start, "/sitemap.xml"))])
    visited: set[str] = set()
    pages: list[str] = []
    while pending and len(visited) < max_sitemaps:
        sitemap_url = pending.popleft()
        if sitemap_url in visited or urlsplit(sitemap_url).netloc.lower() != origin:
            continue
        visited.add(sitemap_url)
        try:
            response = await _get_with_safe_redirects(
                client, sitemap_url, origin, rate_limiter=rate_limiter
            )
            if response.status_code != 200:
                continue
            root = ET.fromstring(response.text)
        except (httpx.HTTPError, ET.ParseError, ValueError):
            continue
        root_kind = root.tag.rsplit("}", 1)[-1].lower()
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() != "loc" or not node.text:
                continue
            candidate = _normalize(urljoin(sitemap_url, node.text.strip()))
            if urlsplit(candidate).netloc.lower() != origin:
                continue
            if root_kind == "sitemapindex":
                if candidate not in visited:
                    pending.append(candidate)
            else:
                try:
                    validate_url(candidate)
                except ValueError:
                    continue
                pages.append(candidate)
    return pages


async def crawl(url: str, output: Path, *, max_pages: int = 20, max_depth: int = 1, timeout: float = 10.0, concurrency: int = 4, crawl_delay: float | None = None, resource_types: frozenset[str] = frozenset({"html", "css", "js", "images", "json"}), max_resource_bytes: int = 5 * 1024 * 1024, max_total_bytes: int = 50 * 1024 * 1024, max_resources: int = 1000, max_seconds: float | None = None, resume: bool = False, cancel_event: asyncio.Event | None = None, progress: Callable[[dict[str, Any]], None] | None = None, selected_pages: tuple[str, ...] = ()) -> dict[str, object]:
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    if max_resources < 1:
        raise ValueError("max_resources must be positive")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    resource_types = parse_resource_selection(resource_types)
    start = validate_url(url)
    origin = urlsplit(start).netloc.lower()
    if resume and output.exists() and not (output / "checkpoint.json").is_file():
        raise ValueError("resume requires an existing extraction checkpoint")
    _safe_output(output, allow_nonempty=resume)
    resources = output / "resources"
    resources.mkdir(exist_ok=True)
    checkpoint = output / "checkpoint.json"
    allowed_pages = {_normalize(urljoin(start, item)) for item in selected_pages}
    if any(urlsplit(item).netloc.lower() != origin for item in allowed_pages):
        raise ValueError("selected pages must stay on the requested origin")
    queue: deque[tuple[str, int]] = deque((item, 0) for item in sorted(allowed_pages)) if allowed_pages else deque([(start, 0)])
    seen: set[str] = set()
    if resume and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        queue = deque((str(item[0]), int(item[1])) for item in saved.get("queue", []))
        seen = {str(item) for item in saved.get("seen", [])}
    scheduled: set[str] = set(seen) | {item[0] for item in queue}
    query_variants: dict[str, set[str]] = {}

    def enqueue(candidate: str, depth: int) -> None:
        candidate = _normalize(candidate)
        parts = urlsplit(candidate)
        if parts.netloc.lower() != origin or (allowed_pages and candidate not in allowed_pages):
            return
        base = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
        variants = query_variants.setdefault(base, set())
        if candidate not in variants and len(variants) >= 4:
            return
        variants.add(candidate)
        if candidate in scheduled:
            return
        scheduled.add(candidate)
        queue.append((candidate, depth))

    for initial_url, _initial_depth in list(queue):
        parts = urlsplit(initial_url)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
        query_variants.setdefault(base, set()).add(initial_url)
    pages: list[dict[str, object]] = []
    started_at = time.monotonic()
    total_bytes = 0
    skipped = 0
    failed = 0
    api_observations: list[dict[str, object]] = []
    resource_records: list[dict[str, object]] = []
    content_targets: dict[str, Path] = {}
    downloaded_resource_count = 0
    timed_out = False
    selected_types = set(resource_types) & set(_RESOURCE_KINDS)
    if "images" in selected_types:
        selected_types.add("svg")
    if cancel_event is not None and cancel_event.is_set():
        report = {"start_url": start, "pages": pages, "resources": resource_records, "api_observations": api_observations, "discovered": 0, "processed": 0, "skipped": 0, "failed": 0, "pending": len(queue), "downloaded_bytes": 0, "downloaded_resources": 0, "max_resources": max_resources, "max_seconds": max_seconds, "timed_out": False, "max_pages": max_pages, "max_depth": max_depth}
        (output / "extraction.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
        if progress:
            progress({"stage": "complete", "processed": 0, "pending": len(queue), "failed": 0})
        return report
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        rate_limiter = _RateLimiter(crawl_delay or 0.0)
        if progress:
            progress({"stage": "robots", "current": start})
        robots = urllib.robotparser.RobotFileParser()
        try:
            robots_response = await _get_with_safe_redirects(
                client, _normalize(urljoin(start, "/robots.txt")), origin, rate_limiter=rate_limiter
            )
            robots.parse(robots_response.text.splitlines() if robots_response.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])
        if crawl_delay is None:
            try:
                robots_delay = robots.crawl_delay("VOAI-GhostAPI")
                if robots_delay is not None:
                    rate_limiter.delay = min(60.0, max(0.0, float(robots_delay)))
            except (TypeError, ValueError):
                pass
        try:
            if progress:
                progress({"stage": "sitemap", "current": start})
            for sitemap_page in await _read_sitemap_urls(
                client, start, origin, rate_limiter=rate_limiter
            ):
                enqueue(sitemap_page, 0)
        except (httpx.HTTPError, ET.ParseError, ValueError):
            pass
        semaphore = asyncio.Semaphore(concurrency)
        while queue and len(seen) < max_pages:
            if max_seconds is not None and time.monotonic() - started_at >= max_seconds:
                timed_out = True
                break
            if cancel_event is not None and cancel_event.is_set():
                checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
                break
            batch: list[tuple[str, int]] = []
            while queue and len(batch) < concurrency and len(seen) + len(batch) < max_pages:
                current, depth = queue.popleft()
                current = _normalize(current)
                if current in seen or urlsplit(current).netloc.lower() != origin or (allowed_pages and current not in allowed_pages):
                    skipped += 1
                    continue
                if not robots.can_fetch("VOAI-GhostAPI", current):
                    skipped += 1
                    continue
                seen.add(current)
                batch.append((current, depth))
            if not batch:
                continue
            for current, _depth in batch:
                if progress:
                    progress({"stage": "page", "current": current, "discovered": len(seen), "pending": len(queue)})

            async def fetch_page(item: tuple[str, int]) -> httpx.Response:
                async with semaphore:
                    return await _get_with_safe_redirects(
                        client, item[0], origin, rate_limiter=rate_limiter
                    )

            fetched = await asyncio.gather(
                *(fetch_page(item) for item in batch), return_exceptions=True
            )
            for (current, depth), fetched_response in zip(batch, fetched, strict=True):
                if isinstance(fetched_response, asyncio.CancelledError):
                    for pending_url, pending_depth in reversed(batch):
                        if pending_url not in queue:
                            queue.appendleft((pending_url, pending_depth))
                        seen.discard(pending_url)
                    checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
                    raise fetched_response
                if isinstance(fetched_response, BaseException):
                    pages.append({"url": current, "error": str(fetched_response)})
                    failed += 1
                    continue
                response = fetched_response
                try:
                    validate_url(str(response.url))
                    content = response.content
                    if len(content) > max_resource_bytes or total_bytes + len(content) > max_total_bytes:
                        pages.append({"url": current, "status": response.status_code, "skipped": "resource size limit"})
                        skipped += 1
                        continue
                    total_bytes += len(content)
                    content_type = response.headers.get("content-type", "")
                    digest = hashlib.sha256(content).hexdigest()
                    kind = _resource_kind(content_type, current)
                    selected = "all" in resource_types or kind in selected_types
                    target = content_targets.get(digest)
                    resource_limited = False
                    if selected and target is None:
                        if downloaded_resource_count >= max_resources:
                            resource_limited = True
                        else:
                            target = resources / f"{digest}{_resource_suffix(current, content_type)}"
                            if not target.exists():
                                target.write_bytes(content)
                            content_targets[digest] = target
                            downloaded_resource_count += 1
                    record: dict[str, object] = {
                        "url": current,
                        "source_page": current,
                        "status": response.status_code,
                        "path": str(target.relative_to(output)) if target is not None else None,
                        "sha256": digest,
                        "size": len(content),
                        "content_type": content_type,
                        "extraction_status": "skipped by resource limit" if resource_limited else "downloaded" if selected else "skipped by resource selection",
                    }
                    record["resource_type"] = kind
                    resource_records.append(record)
                    if progress:
                        progress({"stage": "resource", "current": current, "resource_type": kind, "downloaded_bytes": total_bytes})
                    if kind == "json":
                        counter = [0]
                        try:
                            body: object = sanitize(response.json(), counter)
                        except (ValueError, TypeError):
                            body = None
                        api_observations.append({"method": "GET", "url": current, "path": urlsplit(current).path or "/", "status": response.status_code, "response_body": body, "source": "website", "source_page": current, "sanitized_values": counter[0]})
                    pages.append(record)
                    if "text/html" in response.headers.get("content-type", ""):
                        parser = _Links()
                        parser.feed(response.text)
                        for inline_index, (inline_kind, inline_text) in enumerate(parser.inline):
                            inline_bytes = inline_text.encode("utf-8")
                            inline_url = f"{current}#inline-{inline_index}"
                            inline_digest = hashlib.sha256(inline_bytes).hexdigest()
                            inline_selected = "all" in resource_types or inline_kind in selected_types
                            inline_target = content_targets.get(inline_digest)
                            inline_limited = False
                            if inline_selected and inline_target is None:
                                if len(inline_bytes) > max_resource_bytes or downloaded_resource_count >= max_resources:
                                    inline_limited = True
                                else:
                                    inline_target = resources / f"{inline_digest}{_resource_suffix(f'inline.{inline_kind}', f'text/{inline_kind}')}"
                                    if not inline_target.exists():
                                        inline_target.write_bytes(inline_bytes)
                                    content_targets[inline_digest] = inline_target
                                    downloaded_resource_count += 1
                            resource_records.append({
                                "url": inline_url,
                                "source_page": current,
                                "status": response.status_code,
                                "path": str(inline_target.relative_to(output)) if inline_target is not None else None,
                                "sha256": inline_digest,
                                "size": len(inline_bytes),
                                "content_type": f"text/{inline_kind}",
                                "extraction_status": "skipped by resource limit" if inline_limited else "downloaded" if inline_selected else "skipped by resource selection",
                                "resource_type": inline_kind,
                            })
                        if depth < max_depth:
                            for href in parser.links:
                                enqueue(_normalize(urljoin(current, href)), depth + 1)
                            for href, _tag in parser.resources:
                                enqueue(_normalize(urljoin(current, href)), depth)
                except asyncio.CancelledError:
                    queue.appendleft((current, depth))
                    seen.discard(current)
                    checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
                    raise
                except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                    pages.append({"url": current, "error": str(exc)})
                    failed += 1
                checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
    report = {"start_url": start, "pages": pages, "resources": resource_records, "api_observations": api_observations, "discovered": len(seen), "processed": len(pages), "skipped": skipped, "failed": failed, "pending": len(queue), "downloaded_bytes": total_bytes, "downloaded_resources": len(content_targets), "max_resources": max_resources, "max_seconds": max_seconds, "timed_out": timed_out, "max_pages": max_pages, "max_depth": max_depth}
    (output / "extraction.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
    if progress:
        progress({"stage": "complete", "processed": len(pages), "pending": len(queue), "failed": failed})
    return report


async def extract(url: str, output: Path, har_files: tuple[Path, ...] = (), *, yes: bool = False, max_pages: int = 20, max_depth: int = 1, concurrency: int = 4, crawl_delay: float | None = None, resource_types: frozenset[str] = frozenset({"html", "css", "js", "images", "json"}), snapshot: bool = False, max_resource_bytes: int = 5 * 1024 * 1024, max_total_bytes: int = 50 * 1024 * 1024, max_resources: int = 1000, max_seconds: float | None = None, resume: bool = False, cancel_event: asyncio.Event | None = None, progress: Callable[[dict[str, Any]], None] | None = None, selected_pages: tuple[str, ...] = ()) -> Workspace | None:
    if not yes:
        raise ValueError("interactive confirmation is required; pass --yes for automation")
    resource_types = parse_resource_selection(resource_types)
    workspace: Workspace | None = None
    if har_files:
        validate_har_inputs(har_files)
        _safe_output(output)
        with tempfile.TemporaryDirectory(prefix="voai-har-") as temp:
            imported = [import_har(har, Path(temp) / str(index)) for index, har in enumerate(har_files)]
        observations = []
        seen: set[tuple[str, str, int, str]] = set()
        for item in imported:
            for observation in item.observations:
                key = (observation.method, observation.path, observation.status, json.dumps(observation.response_body, sort_keys=True, default=str))
                if key not in seen:
                    seen.add(key)
                    observations.append(observation.model_copy(update={"source": "har"}))
        observations.sort(key=lambda item: (item.method, item.path, item.status, json.dumps(item.response_body, sort_keys=True, default=str)))
        grouped: dict[str, list[object]] = {}
        for observation in observations:
            if observation.response_body is not None:
                grouped.setdefault(f"{observation.method} {route_path(observation.path)}", []).append(infer_schema(observation.response_body))
        workspace = Workspace(observations=observations, warnings=[warning for item in imported for warning in item.warnings], sanitized_values=sum(item.sanitized_values for item in imported), schemas={key: merge_schemas(value) for key, value in sorted(grouped.items())})
        (output / "workspace").mkdir()
        (output / "workspace" / "workspace.json").write_text(workspace.model_dump_json(indent=2), encoding="utf-8")
    if url:
        report = await crawl(url, output / "website", max_pages=max_pages, max_depth=max_depth, concurrency=concurrency, crawl_delay=crawl_delay, resource_types=resource_types, max_resource_bytes=max_resource_bytes, max_total_bytes=max_total_bytes, max_resources=max_resources, max_seconds=max_seconds, resume=resume, cancel_event=cancel_event, progress=progress, selected_pages=selected_pages)
        raw_api_observations = cast(list[object], report.get("api_observations", []))
        website_observations = [
            Observation(
                method=str(item["method"]),
                url=str(item["url"]),
                path=str(item["path"]),
                status=int(item["status"]),
                response_body=item.get("response_body"),
                source="website",
                source_page=str(item.get("source_page")) if item.get("source_page") else None,
            )
            for item in raw_api_observations if isinstance(raw_api_observations, list)
            if isinstance(item, dict)
        ]
        if website_observations:
            if workspace is None:
                workspace = Workspace()
            workspace.observations.extend(website_observations)
            workspace.observations = _merge_observations(workspace.observations)
            grouped_website: dict[str, list[object]] = {}
            for observation in workspace.observations:
                if observation.response_body is not None:
                    grouped_website.setdefault(f"{observation.method} {route_path(observation.path)}", []).append(infer_schema(observation.response_body))
            workspace.schemas = {key: merge_schemas(value) for key, value in sorted(grouped_website.items())}
            (output / "workspace").mkdir(exist_ok=True)
            (output / "workspace" / "workspace.json").write_text(workspace.model_dump_json(indent=2), encoding="utf-8")
        if snapshot:
            build_snapshot(output / "website", output / "snapshot")
    return workspace
