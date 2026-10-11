from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import posixpath
import re
import socket
import tempfile
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import cast
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("href"):
            self.links.append(values["href"] or "")
        for key in ("src", "poster"):
            if values.get(key):
                self.resources.append((values[key] or "", tag))


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
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


async def crawl(url: str, output: Path, *, max_pages: int = 20, max_depth: int = 1, timeout: float = 10.0, resource_types: frozenset[str] = frozenset({"html", "css", "js", "images", "json"}), max_resource_bytes: int = 5 * 1024 * 1024, max_total_bytes: int = 50 * 1024 * 1024, resume: bool = False) -> dict[str, object]:
    start = validate_url(url)
    origin = urlsplit(start).netloc.lower()
    _safe_output(output)
    resources = output / "resources"
    resources.mkdir()
    checkpoint = output / "checkpoint.json"
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    seen: set[str] = set()
    if resume and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        queue = deque((str(item[0]), int(item[1])) for item in saved.get("queue", []))
        seen = {str(item) for item in saved.get("seen", [])}
    pages: list[dict[str, object]] = []
    total_bytes = 0
    skipped = 0
    failed = 0
    api_observations: list[dict[str, object]] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        robots = urllib.robotparser.RobotFileParser()
        try:
            robots_response = await client.get(urljoin(start, "/robots.txt"))
            robots.parse(robots_response.text.splitlines() if robots_response.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])
        try:
            sitemap = await client.get(urljoin(start, "/sitemap.xml"))
            if sitemap.status_code == 200 and "xml" in sitemap.headers.get("content-type", ""):
                root = ET.fromstring(sitemap.text)
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
                        candidate = _normalize(urljoin(start, node.text.strip()))
                        if urlsplit(candidate).netloc.lower() == origin:
                            queue.append((candidate, 0))
        except (httpx.HTTPError, ET.ParseError, ValueError):
            pass
        while queue and len(seen) < max_pages:
            current, depth = queue.popleft()
            current = _normalize(current)
            if current in seen or urlsplit(current).netloc.lower() != origin:
                skipped += 1
                continue
            if not robots.can_fetch("VOAI-GhostAPI", current):
                skipped += 1
                continue
            seen.add(current)
            try:
                response = None
                for attempt in range(3):
                    try:
                        candidate_response = await client.get(current)
                        response = candidate_response
                        if candidate_response.status_code not in {429, 500, 502, 503, 504}:
                            break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                    await asyncio.sleep(0.2 * (2**attempt))
                if response is None:
                    raise httpx.HTTPError("no response")
                validate_url(str(response.url))
                content = response.content
                if len(content) > max_resource_bytes or total_bytes + len(content) > max_total_bytes:
                    pages.append({"url": current, "status": response.status_code, "skipped": "resource size limit"})
                    skipped += 1
                    continue
                total_bytes += len(content)
                digest = hashlib.sha256(content).hexdigest()
                suffix = Path(urlsplit(current).path).suffix or ".html"
                target = resources / f"{digest}{suffix}"
                content_type = response.headers.get("content-type", "")
                kind = "html" if "text/html" in content_type else "css" if "text/css" in content_type else "js" if "javascript" in content_type else "json" if "json" in content_type else "images" if content_type.startswith("image/") else "other"
                if kind in resource_types and not target.exists():
                    target.write_bytes(content)
                record: dict[str, object] = {"url": current, "source_page": current, "status": response.status_code, "path": str(target.relative_to(output)), "sha256": digest, "size": len(content), "content_type": response.headers.get("content-type", "")}
                record["resource_type"] = kind
                if kind == "json":
                    counter = [0]
                    try:
                        body: object = sanitize(response.json(), counter)
                    except (ValueError, TypeError):
                        body = None
                    api_observations.append({"method": "GET", "url": current, "path": urlsplit(current).path or "/", "status": response.status_code, "response_body": body, "source": "website", "source_page": current, "sanitized_values": counter[0]})
                pages.append(record)
                if "text/html" in response.headers.get("content-type", "") and depth < max_depth:
                    parser = _Links()
                    parser.feed(response.text)
                    for href in parser.links:
                        candidate = _normalize(urljoin(current, href))
                        if urlsplit(candidate).netloc.lower() == origin:
                            queue.append((candidate, depth + 1))
                    for href, _tag in parser.resources:
                        candidate = _normalize(urljoin(current, href))
                        if urlsplit(candidate).netloc.lower() == origin:
                            queue.append((candidate, depth))
            except asyncio.CancelledError:
                checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
                raise
            except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                pages.append({"url": current, "error": str(exc)})
                failed += 1
            checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
    report = {"start_url": start, "pages": pages, "api_observations": api_observations, "discovered": len(seen), "processed": len(pages), "skipped": skipped, "failed": failed, "pending": len(queue), "downloaded_bytes": total_bytes, "max_pages": max_pages, "max_depth": max_depth}
    (output / "extraction.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    checkpoint.write_text(json.dumps({"queue": list(queue), "seen": sorted(seen)}, indent=2), encoding="utf-8")
    return report


async def extract(url: str, output: Path, har_files: tuple[Path, ...] = (), *, yes: bool = False, max_pages: int = 20, max_depth: int = 1, resource_types: frozenset[str] = frozenset({"html", "css", "js", "images", "json"}), snapshot: bool = False, max_resource_bytes: int = 5 * 1024 * 1024, max_total_bytes: int = 50 * 1024 * 1024, resume: bool = False) -> Workspace | None:
    if not yes:
        raise ValueError("interactive confirmation is required; pass --yes for automation")
    workspace: Workspace | None = None
    if har_files:
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
        report = await crawl(url, output / "website", max_pages=max_pages, max_depth=max_depth, resource_types=resource_types, max_resource_bytes=max_resource_bytes, max_total_bytes=max_total_bytes, resume=resume)
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
            unique: dict[tuple[str, str, int, str], Observation] = {}
            for observation in workspace.observations:
                key = (observation.method, observation.path, observation.status, json.dumps(observation.response_body, sort_keys=True, default=str))
                unique.setdefault(key, observation)
            workspace.observations = sorted(unique.values(), key=lambda item: (item.method, item.path, item.status, item.source))
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
