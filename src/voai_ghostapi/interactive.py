"""Optional, read-only browser discovery for dynamically loaded resources.

The browser observer is deliberately conservative. It executes only bounded
recipes made up of read-only actions and records what the browser actually
observed. It never claims that an API surface is complete.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .core import infer_schema, sanitize, sanitize_path

_SAFE_ACTIONS = frozenset({"click", "fill", "select", "scroll"})
_INTERACTION_KINDS = frozenset(
    {"navigation", "tab", "filter", "search", "pagination", "infinite-scroll", "expand", "read-only"}
)
_DESTRUCTIVE_WORDS = frozenset(
    {"admin", "checkout", "delete", "logout", "pay", "payment", "purchase", "remove", "save", "submit", "update"}
)
_MAX_INTERACTIONS = 50
_MAX_BODY_SAMPLE = 4 * 1024
_SENSITIVE_QUERY_KEYS = ("authorization", "cookie", "password", "passwd", "token", "secret", "api_key", "apikey", "session")


def classify_interaction(recipe: dict[str, Any]) -> str:
    """Validate a recipe and return its observable interaction classification."""
    action = recipe.get("action")
    if action not in _SAFE_ACTIONS:
        raise ValueError(
            "only click, fill, select, and scroll interactions are permitted; approval is required for unknown actions"
        )
    kind = recipe.get("kind")
    if kind is not None and kind not in _INTERACTION_KINDS:
        raise ValueError("unknown interaction kind requires approval")
    text = " ".join(str(recipe.get(key, "")).lower() for key in ("name", "selector", "value", "kind"))
    words = set(re.findall(r"[a-z]+", text))
    if _DESTRUCTIVE_WORDS & words or any(word in text for word in _DESTRUCTIVE_WORDS):
        raise ValueError("state-changing or destructive interactions are never run automatically")
    if isinstance(kind, str):
        return kind
    if "next" in text and "page" in text:
        return "pagination"
    for candidate in ("infinite-scroll", "pagination", "filter", "search", "expand", "tab", "navigation"):
        if candidate in text:
            return candidate
    return "read-only"


def _sanitize_url(value: str, counter: list[int]) -> str:
    parts = urlsplit(value)
    path = sanitize_path(parts.path or "/", counter)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = "&".join(
        f"{sanitize(str(key), counter)}={_safe_query_value(str(key), str(item), counter)}"
        for key, item in query_pairs
    )
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _safe_query_value(key: str, value: str, counter: list[int]) -> str:
    if any(name in key.lower() for name in _SENSITIVE_QUERY_KEYS):
        counter[0] += 1
        return "<REDACTED>"
    return str(sanitize(value, counter))


def _request_parameters(value: str, counter: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in parse_qsl(urlsplit(value).query, keep_blank_values=True):
        safe_key = str(sanitize(key, counter))
        safe_item = _safe_query_value(safe_key, item, counter)
        if safe_key in result:
            previous = result[safe_key]
            result[safe_key] = [*previous, safe_item] if isinstance(previous, list) else [previous, safe_item]
        else:
            result[safe_key] = safe_item
    return result


def _request_body_shape(post_data: Any, counter: list[int]) -> Any:
    if not post_data:
        return None
    if isinstance(post_data, bytes):
        post_data = post_data.decode("utf-8", "replace")
    if not isinstance(post_data, str):
        return {"type": "unknown"}
    clean = sanitize(post_data, counter)
    try:
        return infer_schema(json.loads(clean))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"type": "string", "length": len(clean)}


async def discover_api_requests(
    url: str,
    *,
    storage_state: Path | None = None,
    timeout_ms: int = 30_000,
    interactions: list[dict[str, Any]] | None = None,
    max_body_bytes: int = 5 * 1024 * 1024,
    max_interactions: int = _MAX_INTERACTIONS,
    max_seconds: float = 120.0,
    max_depth: int = 1,
    max_memory_bytes: int = 64 * 1024 * 1024,
    max_observations: int = 1_000,
    manual_login: bool = False,
    login_wait_ms: int = 60_000,
) -> list[dict[str, Any]]:
    """Observe network responses during a bounded, read-only browser session."""
    if timeout_ms < 1 or max_seconds <= 0 or max_depth < 0:
        raise ValueError("interactive timeouts, time limit, and depth must be positive")
    if max_body_bytes < 1 or max_memory_bytes < 1 or max_observations < 1:
        raise ValueError("interactive memory and observation limits must be positive")
    if login_wait_ms < 0:
        raise ValueError("login_wait_ms must not be negative")
    if max_interactions < 1:
        raise ValueError("max_interactions must be positive")
    interaction_limit = min(max_interactions, _MAX_INTERACTIONS)
    if interactions is not None and len(interactions) > interaction_limit:
        raise ValueError(f"at most {interaction_limit} interactive actions are permitted")
    prepared: list[tuple[str, str | None, str, str, int, float, str, int]] = []
    for index, recipe in enumerate(interactions or [], start=1):
        if not isinstance(recipe, dict):
            raise ValueError("interaction recipes must be objects")  # noqa: TRY004
        action = recipe.get("action")
        classification = classify_interaction(recipe)
        selector = recipe.get("selector")
        if action in {"click", "fill", "select"} and not isinstance(selector, str):
            raise ValueError("interactive selectors are required")
        repeat = recipe.get("repeat", 1)
        if not isinstance(repeat, int) or not 1 <= repeat <= 10:
            raise ValueError("interaction repeat must be an integer from 1 to 10")
        depth = recipe.get("depth", 0)
        if not isinstance(depth, int) or not 0 <= depth <= max_depth:
            raise ValueError("interaction depth is out of bounds")
        distance = float(recipe.get("distance", 800))
        if action == "scroll" and not -100_000 <= distance <= 100_000:
            raise ValueError("scroll distance is out of bounds")
        prepared.append(
            (
                str(action),
                selector if isinstance(selector, str) else None,
                str(recipe.get("name") or f"{action}-{index}"),
                str(recipe.get("value", "")),
                repeat,
                distance,
                classification,
                depth,
            )
        )

    try:
        from playwright.async_api import Error as PlaywrightError  # type: ignore[import-not-found]
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("interactive discovery requires the optional 'browser' dependency") from exc

    if storage_state is not None and (not storage_state.is_file() or storage_state.is_symlink()):
        raise ValueError("storage state must be a regular local file")

    observations: list[dict[str, Any]] = []
    active_interaction = "navigation"
    active_kind = "navigation"
    started = time.monotonic()
    captured_bytes = 0

    def check_budget() -> None:
        if time.monotonic() - started > max_seconds:
            raise TimeoutError("interactive discovery time limit exceeded")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not manual_login)
        context = None
        try:
            context = await browser.new_context(storage_state=str(storage_state) if storage_state else None)
            page = await context.new_page()

            async def record(response: Any) -> None:
                nonlocal captured_bytes
                try:
                    check_budget()
                except TimeoutError:
                    return
                if len(observations) >= max_observations:
                    return
                content_type = response.headers.get("content-type", "")
                lowered = content_type.lower()
                supported = (
                    "json" in lowered
                    or lowered.startswith(("text/css", "text/javascript", "application/javascript", "image/", "font/", "video/", "audio/"))
                    or lowered in {"application/pdf", "application/manifest+json"}
                )
                if not supported:
                    return
                sanitized_count = [0]
                request = response.request
                item: dict[str, Any] = {
                    "method": str(request.method).upper(),
                    "url": _sanitize_url(response.url, sanitized_count),
                    "path": sanitize_path(urlsplit(response.url).path or "/", sanitized_count),
                    "request_parameters": _request_parameters(response.url, sanitized_count),
                    "request_body_shape": _request_body_shape(request.post_data, sanitized_count),
                    "status": response.status,
                    "content_type": content_type,
                    "response_content_type": content_type,
                    "source_page": _sanitize_url(page.url, sanitized_count) if page.url else None,
                    "triggering_interaction": active_interaction,
                    "interaction_type": active_kind,
                    "observed": True,
                    "discovery_status": "observed",
                }
                try:
                    body = await response.body()
                except (OSError, PlaywrightError, RuntimeError, ValueError):
                    body = b""
                item["size"] = len(body)
                item["sha256"] = hashlib.sha256(body).hexdigest()
                if len(body) > max_body_bytes:
                    item["extraction_status"] = "skipped by resource size limit"
                elif captured_bytes + len(body) > max_memory_bytes:
                    item["extraction_status"] = "skipped by interactive memory limit"
                else:
                    captured_bytes += len(body)
                    if "text/" in lowered or "json" in lowered or "javascript" in lowered or "css" in lowered:
                        text = sanitize(body.decode("utf-8", "replace"), sanitized_count)
                        item["body"] = text
                        try:
                            parsed = json.loads(text) if "json" in lowered else None
                        except (TypeError, ValueError, json.JSONDecodeError):
                            parsed = None
                        item["response_sample"] = parsed if parsed is not None else text[:_MAX_BODY_SAMPLE]
                    else:
                        encoded = base64.b64encode(body).decode("ascii")
                        item["body_base64"] = encoded
                        item["response_sample"] = encoded[:_MAX_BODY_SAMPLE]
                if sanitized_count[0]:
                    item["sanitized_values"] = sanitized_count[0]
                observations.append(item)

            page.on("response", record)
            check_budget()
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if manual_login and login_wait_ms:
                await page.wait_for_timeout(login_wait_ms)
                check_budget()
            for action, selector, name, value, repeat, distance, classification, _depth in prepared:
                active_interaction = name
                active_kind = classification
                for _ in range(repeat):
                    check_budget()
                    if action == "click":
                        assert selector is not None
                        await page.locator(selector).first.click(timeout=timeout_ms)
                    elif action == "fill":
                        assert selector is not None
                        await page.locator(selector).first.fill(value)
                    elif action == "select":
                        assert selector is not None
                        await page.locator(selector).first.select_option(value)
                    else:
                        await page.mouse.wheel(0, distance)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except PlaywrightError:
                        pass
                    await page.wait_for_timeout(100)
        finally:
            if context is not None:
                await context.close()
            await browser.close()
    return observations
